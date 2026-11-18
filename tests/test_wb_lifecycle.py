"""Fast isolated tests for WorkBuddy hook pass-throughs, fast paths, and consent-first capture."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import time

import pytest

import bridge
import mindie_launch

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
    for action, event in (
        ("session-start", {"session_id": SESSION_A, "cwd": str(workspace)}),
        ("user-prompt-submit", {"session_id": SESSION_A, "cwd": str(workspace), "prompt": "fix tests"}),
        ("pre-invocation", {"session_id": SESSION_A, "cwd": str(workspace), "prompt": "fix tests"}),
        ("pre-tool-use", {"session_id": SESSION_A, "cwd": str(workspace), "tool_name": "mcp__mindie-tools__mindie_status", "tool_input": {}}),
    ):
        code, payload, _ = _run_launcher(monkeypatch, action, event)
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
    workspace, outside = tmp_path / "ws", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    event = {"session_id": SESSION_A, "cwd": str(workspace), "transcript_path": str(workspace / f"{SESSION_A}.jsonl")}
    cases = [("unconfigured", None, None)]
    consent_file, community_file = isolated_mindie_env["config_path"].with_name("mindie-consent.json"), isolated_mindie_env["config_path"].with_name("mindie-community.json")
    consent_file.write_text(json.dumps({"choice": "contribute"}), encoding="utf-8")
    community_file.write_text(json.dumps({"enabled": False, "project_roots": [str(workspace)], "consent_config": str(consent_file)}), encoding="utf-8")
    cases.append(("disabled", None, None))
    community_file.write_text(json.dumps({"enabled": True, "project_roots": [str(outside)], "consent_config": str(consent_file)}), encoding="utf-8")
    cases.append(("out-of-scope", None, None))
    for label, _consent, _community in cases:
        code, payload, elapsed = _run_launcher(monkeypatch, "stop", event)
        assert code == 0 and payload == {"suppressOutput": True}, label
        assert elapsed < 0.1, (label, elapsed)
    assert _sqlite_files(isolated_mindie_env["data_home"]) == []


def _write_engine(tmp_path: Path, cfg_file: Path) -> Path:
    redactor = tmp_path / "gitleaks"
    redactor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    redactor.chmod(0o755)
    engine_file = cfg_file.with_name("wb.engine.json")
    data = cfg_file.parent.parent / "data"
    cfg_file.write_text(json.dumps({"engine_config": str(engine_file), "admission_path": str(data / "adm.sqlite3")}), encoding="utf-8")
    engine_file.write_text(json.dumps({
        "root": str(data / "domains"), "domain": "test", "capture_mode": "public-transcript",
        "transcript_adapter": str(PLUGIN_ROOT / "scripts" / "wb_transcript.py"), "redactor_executable": str(redactor),
    }), encoding="utf-8")
    return engine_file


def _write_transcript(root: Path, workspace: Path, session: str, prompt: str) -> Path:
    path = root / "projects" / "ws" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "message", "sessionId": session, "timestamp": 1761899000000, "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}) + "\n", encoding="utf-8")
    return path


def test_stop_capture_contribute_and_local_only_preserve_revocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_mindie_env):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg_file = isolated_mindie_env["config_path"]
    _write_engine(tmp_path, cfg_file)
    transcript_a = _write_transcript(isolated_mindie_env["wb_home"], workspace, SESSION_A, "A3 HCCL timeout")
    transcript_b = _write_transcript(isolated_mindie_env["wb_home"], workspace, SESSION_B, "ACLGraph capture")
    calls: list[dict] = []

    def fake_capture(engine_path, forwarded, timeout=None):
        calls.append(dict(forwarded))
        return {"stage": "accepted-local"}

    monkeypatch.setattr("mindie_knowledge.loop.cli.capture_hook", fake_capture)
    monkeypatch.setattr(bridge, "sharing_status", lambda profile, cwd=None: {"tier": "contribute", "cwd_in_scope": True})
    consent_file = cfg_file.with_name("mindie-consent.json")
    consent_file.write_text(json.dumps({"choice": "contribute"}), encoding="utf-8")
    cfg_file.with_name("mindie-community.json").write_text(json.dumps({"enabled": True, "project_roots": [str(workspace)], "consent_config": str(consent_file)}), encoding="utf-8")
    event = {"session_id": SESSION_A, "cwd": str(workspace), "transcript_path": str(transcript_a)}
    code, payload, _ = _run_launcher(monkeypatch, "stop", event)
    assert code == 0 and payload == {"suppressOutput": True}
    assert len(calls) == 1 and calls[0]["session_id"] == SESSION_A and calls[0]["cwd"] == str(workspace.resolve())
    assert calls[0]["mindie_activation"] and calls[0]["transcript_path"].endswith(transcript_a.name)

    from mindie_harness.api import ProfileRef
    from mindie_knowledge.loop.activation import Admission

    admission = Admission(ProfileRef.resolve("wb", plugin_root=PLUGIN_ROOT, config_path=cfg_file).admission_db)
    assert admission.inspect(SESSION_A)["status"] == "active"
    admission.deactivate(SESSION_A)
    assert bridge.handle_stop(event, config=cfg_file) == {"suppressOutput": True}
    assert len(calls) == 1
    assert admission.inspect(SESSION_A)["status"] == "inactive"

    monkeypatch.setattr(bridge, "sharing_status", lambda profile, cwd=None: {"tier": "local-only", "cwd_in_scope": True})
    event_b = {"session_id": SESSION_B, "cwd": str(workspace), "transcript_path": str(transcript_b)}
    assert bridge.handle_stop(event_b, config=cfg_file) == {"suppressOutput": True}
    assert len(calls) == 2 and calls[1]["session_id"] == SESSION_B
