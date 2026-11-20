"""Fast isolated tests for WorkBuddy hook pass-throughs, cold seed re-entry, and consent-first capture."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

import bridge
import mindie_launch
import setup

SESSION_A = "454c6ee9-afea-47be-8188-abb9c7fb1f26"
SESSION_B = "ec33ebf9-0cba-4100-8142-c61503f6c587"
PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "mindie-agent"


def _run_launcher(monkeypatch: pytest.MonkeyPatch, action: str, event: dict):
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(json.dumps(event).encode("utf-8"))})())
    sink = type("Stdout", (), {"buffer": io.BytesIO()})()
    monkeypatch.setattr(sys, "stdout", sink)
    started = time.perf_counter()
    code = mindie_launch.main(["hook", action])
    return code, json.loads(sink.buffer.getvalue()), time.perf_counter() - started


def _sqlite_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.sqlite3*")] if root.is_dir() else []


def test_passive_session_start_and_prompts_create_zero_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_mindie_env):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    class _BombBuffer:
        def read(self, *_args, **_kwargs):
            raise AssertionError("passive session-start / prompt hooks must never read sys.stdin")

    monkeypatch.setattr(sys, "stdin", type("BombStdin", (), {"buffer": _BombBuffer()})())
    for action in ("session-start", "user-prompt-submit", "user-prompt-expansion", "pre-invocation"):
        sink = type("Stdout", (), {"buffer": io.BytesIO()})()
        monkeypatch.setattr(sys, "stdout", sink)
        assert mindie_launch.main(["hook", action]) == 0
        assert json.loads(sink.buffer.getvalue()) == {"suppressOutput": True}
        sink_bridge = type("Stdout", (), {"buffer": io.BytesIO()})()
        monkeypatch.setattr(sys, "stdout", sink_bridge)
        assert bridge.main([action]) == 0
        assert json.loads(sink_bridge.buffer.getvalue()) == {"suppressOutput": True}

    code, payload, _ = _run_launcher(
        monkeypatch,
        "pre-tool-use",
        {"session_id": SESSION_A, "cwd": str(workspace), "tool_name": "mcp__mindie-tools__mindie_status", "tool_input": {}},
    )
    assert code == 0 and payload == {"suppressOutput": True}
    assert (isolated_mindie_env["data_home"] / "state" / "wb" / "pending-update-check.json").is_file()
    assert _sqlite_files(isolated_mindie_env["data_home"]) == []
    assert bridge.handle_user_prompt_submit({"session_id": SESSION_A}) == {"suppressOutput": True}
    assert bridge.handle_session_start({"session_id": SESSION_A}) == {"suppressOutput": True}


def test_pre_tool_use_denies_forged_identity_keys(monkeypatch: pytest.MonkeyPatch, isolated_mindie_env):
    forged = {"session_id": SESSION_A, "tool_name": "mcp__mindie-tools__mindie_status", "tool_input": {"_session_id": SESSION_A, "mindie_activation": "forged"}}
    code, payload, _ = _run_launcher(monkeypatch, "pre-tool-use", forged)
    assert code == 0
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert bridge.handle_pre_tool_use(forged)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _sqlite_files(isolated_mindie_env["data_home"]) == []


def test_hook_stop_fast_paths_are_under_100ms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_mindie_env):
    workspace, outside = tmp_path / "ws", tmp_path / "ws-other"
    workspace.mkdir()
    outside.mkdir()
    event = {"session_id": SESSION_A, "cwd": str(workspace), "transcript_path": str(workspace / f"{SESSION_A}.jsonl")}
    consent_file, community_file = isolated_mindie_env["config_path"].with_name("mindie-consent.json"), isolated_mindie_env["config_path"].with_name("mindie-community.json")
    for label, consent_doc, community_doc in (
        ("unconfigured", None, None),
        (
            "disabled",
            {"schema": "mindie-consent/1", "choice": "contribute", "choice_revision": "rev-01"},
            {"schema": "mindie-community-config/1", "enabled": False, "project_roots": [str(workspace)], "consent_config": str(consent_file)},
        ),
        (
            "out-of-scope",
            {"schema": "mindie-consent/1", "choice": "contribute", "choice_revision": "rev-01"},
            {"schema": "mindie-community-config/1", "enabled": True, "project_roots": [str(outside)], "consent_config": str(consent_file)},
        ),
        (
            "missing-schema",
            {"choice": "contribute"},
            {"enabled": True, "project_roots": [str(workspace)], "consent_config": str(consent_file)},
        ),
    ):
        if consent_doc is not None:
            consent_file.write_text(json.dumps(consent_doc), encoding="utf-8")
        if community_doc is not None:
            community_file.write_text(json.dumps(community_doc), encoding="utf-8")
        code, payload, elapsed = _run_launcher(monkeypatch, "stop", event)
        assert code == 0 and payload == {"suppressOutput": True}, label
        assert elapsed < 0.1, (label, elapsed)
        assert bridge.handle_stop(event, config=isolated_mindie_env["config_path"]) == {"suppressOutput": True}
    assert _sqlite_files(isolated_mindie_env["data_home"]) == []
    assert not isolated_mindie_env["config_path"].exists()


def _write_transcript(root: Path, session: str, prompt: str) -> Path:
    path = root / "projects" / "ws" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "message", "sessionId": session, "timestamp": 1761899000000, "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}) + "\n", encoding="utf-8")
    return path


def test_stop_capture_contribute_and_local_only_preserve_revocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_mindie_env):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg_file = isolated_mindie_env["config_path"]
    setup.ensure_profile(config=cfg_file, python_exe=sys.executable)
    transcript_a = _write_transcript(isolated_mindie_env["wb_home"], SESSION_A, "A3 HCCL timeout")
    transcript_b = _write_transcript(isolated_mindie_env["wb_home"], SESSION_B, "ACLGraph capture")
    calls: list[dict] = []

    def fake_capture(engine_path, forwarded, timeout=None):
        calls.append(dict(forwarded))
        return {"stage": "accepted-local"}

    monkeypatch.setattr("mindie_knowledge.loop.cli.capture_hook", fake_capture)
    monkeypatch.setattr("mindie_knowledge.loop.handoff.accept_stop", lambda engine_path, forwarded, timeout=None, deadline=None: fake_capture(engine_path, forwarded, timeout=timeout))

    consent_file = cfg_file.with_name("mindie-consent.json")
    community_file = cfg_file.with_name("mindie-community.json")
    consent_file.write_text(
        json.dumps({"schema": "mindie-consent/1", "choice": "contribute", "choice_revision": "rev-01"}),
        encoding="utf-8",
    )
    community_file.write_text(
        json.dumps({"schema": "mindie-community-config/1", "enabled": True, "project_roots": [str(workspace)], "consent_config": str(consent_file)}),
        encoding="utf-8",
    )

    # Without native `turn_id`, Stop MUST emit `identity_kind="notification"` (never synthetic `turn_id`).
    event = {"session_id": SESSION_A, "cwd": str(workspace), "transcript_path": str(transcript_a)}
    code, payload, _ = _run_launcher(monkeypatch, "stop", event)
    assert code == 0 and payload == {"suppressOutput": True}
    assert len(calls) == 1 and calls[0]["session_id"] == SESSION_A and calls[0]["cwd"] == str(workspace.resolve())
    assert calls[0]["identity_kind"] == "notification" and calls[0].get("event_id") and "turn_id" not in calls[0]
    assert calls[0]["transcript_path"].endswith(transcript_a.name)

    # Explicit user revocation (`inactive`) MUST NEVER be revived by subsequent Stop hooks.
    from mindie_harness.api import ProfileRef
    from mindie_knowledge.loop.activation import Admission

    admission = Admission(ProfileRef.resolve("wb", plugin_root=PLUGIN_ROOT, config_path=cfg_file).admission_db)
    assert admission.inspect(SESSION_A)["status"] == "active"
    admission.deactivate(SESSION_A)
    assert bridge.handle_stop(event, config=cfg_file) == {"suppressOutput": True}
    assert len(calls) == 1
    assert admission.inspect(SESSION_A)["status"] == "inactive"

    # With `local-only` tier and an explicit native `turn_id`, Stop uses `identity_kind="turn"`.
    consent_file.write_text(
        json.dumps({"schema": "mindie-consent/1", "choice": "local-only", "choice_revision": "rev-02"}),
        encoding="utf-8",
    )
    community_file.write_text(
        json.dumps({"schema": "mindie-community-config/1", "enabled": True, "local_only": True, "project_roots": [str(workspace)], "consent_config": str(consent_file)}),
        encoding="utf-8",
    )
    event_b = {"session_id": SESSION_B, "cwd": str(workspace), "transcript_path": str(transcript_b), "turn_id": "turn-native-42"}
    assert bridge.handle_stop(event_b, config=cfg_file) == {"suppressOutput": True}
    assert len(calls) == 2 and calls[1]["session_id"] == SESSION_B
    assert calls[1]["identity_kind"] == "turn" and calls[1]["turn_id"] == "turn-native-42" and "event_id" not in calls[1]


def test_cold_launcher_discovers_validated_generations_and_silently_provisions_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_mindie_env):
    monkeypatch.setattr(mindie_launch, "_has_harness", lambda: False)
    assert mindie_launch._active_python() is None

    # 1. Validated generation discovery when `wb.json["python"]` is not yet written.
    gen_dir = isolated_mindie_env["data_home"] / "runtimes" / "wb" / "generations" / "gen-new"
    fake_py = gen_dir / ".venv" / "python-fake"
    fake_py.parent.mkdir(parents=True, exist_ok=True)
    fake_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (gen_dir / "runtime.json").write_text(
        json.dumps({"python": str(fake_py), "validation": {"status": "validated", "validated_at": 100.0}}),
        encoding="utf-8",
    )
    trash_dir = isolated_mindie_env["data_home"] / "runtimes" / "wb" / ".trash" / "gen-retired"
    trash_py = trash_dir / "python-trash"
    trash_py.parent.mkdir(parents=True, exist_ok=True)
    trash_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (trash_dir / "runtime.json").write_text(
        json.dumps({"python": str(trash_py), "validation": {"status": "validated", "validated_at": 999.0}}),
        encoding="utf-8",
    )
    assert mindie_launch._active_python() == str(mindie_launch._norm(fake_py))

    # 2. Cold `mcp remote` seed provisioning captures seed stdout (byte-0 clean MCP wire) and re-execs native script.
    winapps_stub = tmp_path / "Microsoft" / "WindowsApps" / "python.exe"
    winapps_stub.parent.mkdir(parents=True, exist_ok=True)
    winapps_stub.write_bytes(b"")
    assert mindie_launch._usable_python(winapps_stub) is False

    fake_py.unlink()
    assert mindie_launch._active_python() is None
    recorded_cmds: list[tuple[list[str], dict]] = []
    provisioned_py = tmp_path / "provisioned-python"
    provisioned_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    eng_stub = tmp_path / "vllm-ascend.engine.json"
    eng_stub.write_text("{}", encoding="utf-8")
    nine_domains = {f"dom-{idx}": str(eng_stub) for idx in range(9)}

    def fake_run(cmd, **kwargs):
        recorded_cmds.append((list(cmd), dict(kwargs)))
        if any("mindie-harness-seed.pyz" in part for part in cmd):
            isolated_mindie_env["config_path"].write_text(json.dumps({"python": str(provisioned_py)}), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"status": "provisioned", "python": str(provisioned_py)}).encode("utf-8"), stderr=b"")
        if any(part.endswith("setup.py") for part in cmd):
            isolated_mindie_env["config_path"].write_text(
                json.dumps({"python": str(provisioned_py), "engine_config": str(eng_stub), "domain_configs": nine_domains}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=b'{"status":"initialized"}\n', stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    sink = type("Stdout", (), {"buffer": io.BytesIO()})()
    monkeypatch.setattr(sys, "stdout", sink)
    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = mindie_launch.main(["mcp", "remote"])
    assert rc == 0
    assert sink.buffer.getvalue() == b""
    assert len(recorded_cmds) == 3
    seed_argv, seed_kw = recorded_cmds[0]
    assert seed_argv[-2:] == ["--config", str(isolated_mindie_env["config_path"])]
    assert "mcp" not in seed_argv
    assert seed_kw.get("capture_output") is True and seed_kw.get("stdin") == subprocess.DEVNULL and seed_kw.get("timeout") == 300.0
    reexec_argv, _ = recorded_cmds[2]
    assert reexec_argv[0] == str(mindie_launch._norm(provisioned_py))
    assert reexec_argv[-2:] == ["mcp", "remote"]

    # 3. Fail-closed post-check: if `setup.py init` leaves `< 9` domains, cold seed MUST NOT enter resident MCP.
    isolated_mindie_env["config_path"].unlink(missing_ok=True)
    recorded_cmds.clear()

    def fake_run_incomplete(cmd, **kwargs):
        recorded_cmds.append((list(cmd), dict(kwargs)))
        if any("mindie-harness-seed.pyz" in part for part in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"status": "provisioned", "python": str(provisioned_py)}).encode("utf-8"), stderr=b"")
        if any(part.endswith("setup.py") for part in cmd):
            isolated_mindie_env["config_path"].write_text(
                json.dumps({"python": str(provisioned_py), "engine_config": str(eng_stub), "domain_configs": {"vllm-ascend": str(eng_stub)}}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=b"{}\n", stderr=b"")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run_incomplete)
    assert mindie_launch.main(["mcp", "remote"]) != 0
    assert len(recorded_cmds) == 2
