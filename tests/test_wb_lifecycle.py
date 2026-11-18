"""Tier 0 & Tier 1 tests for WorkBuddy hook pass-throughs, consent-first `Stop` capture, and MCP bind security."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

import admission
import bridge
import identity
import paths
import setup


SESSION_A = "454c6ee9-afea-47be-8188-abb9c7fb1f26"
SESSION_B = "ec33ebf9-0cba-4100-8142-c61503f6c587"


def test_passive_session_start_and_ordinary_prompt_stay_quiet(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    start_out = bridge.handle_session_start(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
        }
    )
    assert start_out == {"suppressOutput": True}
    assert not paths.admission_path().exists()
    assert not paths.bind_db_path().exists()

    prompt_out = bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "prompt": "Fix the failing unit test in model_runner.py",
        }
    )
    assert prompt_out == {"suppressOutput": True}
    assert not paths.admission_path().exists()
    assert not paths.bind_db_path().exists()


def test_user_prompt_submit_passthrough_creates_zero_sqlite_state(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    out = bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "prompt": "排查 A3 双 Die HCCL 通信超时",
        }
    )
    assert out == {"suppressOutput": True}
    assert not paths.admission_path().exists()
    assert not paths.bind_db_path().exists()

    # Default-enabled knowledge provisions a lease on demand with not_before=0.0
    lease = admission.ensure_default_lease(SESSION_A, workspace, not_before=0.0)
    assert lease["enabled"] == 1
    assert lease["entry_enabled"] == 1
    assert lease["activated_at"] == 0.0
    assert Path(lease["project_root"]) == workspace.resolve()
    assert admission.knowledge_lease(SESSION_B) is None

    # Programmatic deactivation mutes the session cleanly
    deact = admission.deactivate(SESSION_A)
    assert deact["status"] == "inactive"
    assert admission.is_deactivated(SESSION_A) is True
    assert admission.knowledge_lease(SESSION_A) is None


def test_pre_tool_use_passthrough_and_identity_bind_security(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    args = {"file_path": "/workspace/vllm_ascend/worker.py"}

    hook_res = bridge.handle_pre_tool_use(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "tool_use_id": "call-001",
            "tool_name": "mcp__plugin_mindie-agent_mindie-remote-dev__remote_read",
            "tool_input": args,
        }
    )
    assert hook_res == {"suppressOutput": True}
    assert "hookSpecificOutput" not in hook_res
    assert not paths.bind_db_path().exists()

    # Forged session identity in tool arguments is rejected in PreToolUse too
    with pytest.raises(ValueError, match="must not declare session identity"):
        bridge.handle_pre_tool_use(
            {
                "session_id": SESSION_A,
                "cwd": str(workspace),
                "tool_name": "mcp__plugin_mindie-agent_mindie-tools__mindie_status",
                "tool_input": {"session_id": SESSION_A},
            }
        )

    # Explicit one-shot bind consumption and replay rejection remain intact
    identity.publish_bind(SESSION_A, "remote", "remote_read", args, tool_use_id="call-001")
    claimed = identity.claim_bind("remote", "remote_read", args)
    assert claimed["session"] == SESSION_A
    assert claimed["tool_use_id"] == "call-001"
    identity.finish_bind(claimed["bind_id"], True)

    with pytest.raises(ValueError, match="no active PreToolUse bind"):
        identity.claim_bind("remote", "remote_read", args)

    # WorkBuddy `_meta["workbuddy.ai/conversationId"]` transport resolution
    meta_claim = identity.claim_bind(
        "tools",
        "mindie_status",
        {},
        meta={"workbuddy.ai/conversationId": SESSION_B},
    )
    assert meta_claim["session"] == SESSION_B

    # Reject forged identity fields inside tool arguments
    with pytest.raises(ValueError, match="must not declare session identity"):
        identity.claim_bind("tools", "mindie_status", {"session_id": SESSION_A})

    # Cross-conversation bind isolation
    shared_args = {"argv": ["leases"]}
    identity.publish_bind(SESSION_A, "tools", "coordinator", shared_args)
    identity.publish_bind(SESSION_B, "tools", "coordinator", shared_args)

    with pytest.raises(ValueError, match="ambiguous PreToolUse binds"):
        identity.claim_bind("tools", "coordinator", shared_args)

    claim_a = identity.claim_bind(
        "tools",
        "coordinator",
        shared_args,
        meta={"workbuddy.ai/conversationId": SESSION_A},
    )
    assert claim_a["session"] == SESSION_A

    claim_b = identity.claim_bind(
        "tools",
        "coordinator",
        shared_args,
        meta={"workbuddy.ai/conversationId": SESSION_B},
    )
    assert claim_b["session"] == SESSION_B


def test_stop_checks_consent_and_scope_before_auto_provisioning_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import os

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    gl_sh = bin_dir / "gitleaks"
    gl_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gl_sh.chmod(0o755)
    (bin_dir / "gitleaks.cmd").write_bytes(b"@exit /b 0\r\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    if ".CMD" not in pathext.upper():
        pathext = f"{pathext};.CMD"
    monkeypatch.setenv("PATHEXT", pathext)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    projects_dir = tmp_path / "workbuddy" / "projects" / "ws"
    projects_dir.mkdir(parents=True)
    transcript = projects_dir / f"{SESSION_A}.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "id": "m1",
                "type": "message",
                "timestamp": 1761899000000,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Investigate HCCL timeout"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # 1. Unconfigured Stop returns suppressOutput and never creates admission.sqlite3
    passive_stop = bridge.handle_stop(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "transcript_path": str(transcript),
        }
    )
    assert passive_stop == {"suppressOutput": True}
    assert passive_stop.get("decision") != "block"
    assert not paths.admission_path().exists()

    # 2. Enable community sharing ONLY for `workspace`; out-of-scope `outside` still creates zero SQLite
    setup.configure_community(enabled=True, fork="user/knowledge-vllm-ascend", project_root=workspace)
    out_of_scope_stop = bridge.handle_stop(
        {
            "session_id": SESSION_A,
            "cwd": str(outside),
            "transcript_path": str(transcript),
        }
    )
    assert out_of_scope_stop == {"suppressOutput": True}
    assert not paths.admission_path().exists()

    # 3. Simulate an earlier zero-cwd MCP call creating a lease at `outside`; Stop with no cwd never guesses from stale lease,
    #    and Stop with verified `workspace` re-activates lease to `workspace` and prepares `state-v4.sqlite3`!
    admission.ensure_default_lease(SESSION_A, outside, not_before=0.0)
    assert Path(admission.knowledge_lease(SESSION_A)["project_root"]).resolve() == outside.resolve()
    no_cwd_stop = bridge.handle_stop({"session_id": SESSION_A, "transcript_path": str(transcript)})
    assert no_cwd_stop == {"suppressOutput": True}
    assert Path(admission.knowledge_lease(SESSION_A)["project_root"]).resolve() == outside.resolve()

    import mindie_knowledge.loop.handoff as handoff_mod
    import shutil as _shutil

    eng_doc = json.loads(paths.engine_config_path().read_text(encoding="utf-8"))
    store_db = handoff_mod._store_path(eng_doc)
    _shutil.rmtree(store_db.parent.parent, ignore_errors=True)
    assert not store_db.exists()

    captured_calls = []

    def fake_capture_hook(engine_cfg, payload):
        captured_calls.append((engine_cfg, payload))
        return {"stage": "accepted-local"}

    import mindie_knowledge.loop.cli as cli_mod

    monkeypatch.setattr(cli_mod, "capture_hook", fake_capture_hook)

    active_stop = bridge.handle_stop(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "transcript_path": str(transcript),
        }
    )
    assert active_stop == {"suppressOutput": True}
    assert active_stop.get("decision") != "block"
    assert paths.admission_path().is_file()
    assert store_db.is_file()
    lease = admission.knowledge_lease(SESSION_A)
    assert lease is not None and lease["activated_at"] == 0.0
    assert Path(lease["project_root"]).resolve() == workspace.resolve()
    assert len(captured_calls) == 1
    _, forwarded = captured_calls[0]
    assert forwarded["hook_event_name"] == "Stop"
    assert forwarded["identity_kind"] == "turn"
    assert isinstance(forwarded["turn_id"], str) and forwarded["turn_id"]
    assert forwarded["harness"] == "wb"
    assert forwarded["session_id"] == SESSION_A
    assert forwarded["transcript_path"] == str(transcript.resolve())


def test_real_stop_inserts_capture_row_in_store_v4_and_mcp_preserves_lease_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import os
    import sqlite3
    import mcp_server
    import mindie_knowledge.loop.cli as loop_cli
    import mindie_knowledge.loop.handoff as handoff_mod
    import mindie_knowledge.loop.transport as loop_transport

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    gl_sh = bin_dir / "gitleaks"
    gl_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gl_sh.chmod(0o755)
    gl_cmd = bin_dir / "gitleaks.cmd"
    gl_cmd.write_bytes(b"@exit /b 0\r\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    if ".CMD" not in pathext.upper():
        pathext = f"{pathext};.CMD"
    monkeypatch.setenv("PATHEXT", pathext)

    workspace = tmp_path / "verified-ws"
    workspace.mkdir()
    plugin_cwd = tmp_path / "plugin-cwd"
    plugin_cwd.mkdir()

    cfg = paths.config_path()
    setup.ensure_default_profile(config=cfg, workspace_root=None)
    setup.configure_community(config=cfg, enabled=True, fork="user/knowledge-vllm-ascend", project_root=workspace)

    transcript = tmp_path / f"{SESSION_B}.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "id": "m_real_1",
                "type": "message",
                "timestamp": 1999999999000,
                "message": {"role": "user", "content": [{"type": "text", "text": "Fix Ascend 507015"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        handoff_mod,
        "request_wake",
        lambda *_a, **_kw: {"wake": "requested", "runtime": "starting", "reason": "waking"},
    )

    stop_out = bridge.handle_stop(
        {
            "session_id": SESSION_B,
            "workspace_root": str(workspace),
            "transcript_path": str(transcript),
        },
        config=cfg,
    )
    assert stop_out == {"suppressOutput": True}

    eng_doc = json.loads(paths.engine_config_path(cfg).read_text(encoding="utf-8"))
    store_db = handoff_mod._store_path(eng_doc)
    assert store_db.is_file()
    with sqlite3.connect(store_db) as db:
        count = db.execute("SELECT COUNT(*) FROM captures WHERE session=?", (SESSION_B,)).fetchone()[0]
    assert count == 1

    # Verify a subsequent zero-workspace MCP knowledge call running from `plugin_cwd` preserves `workspace`!
    monkeypatch.chdir(plugin_cwd)
    monkeypatch.setattr(loop_cli, "ensure_service", lambda _cfg: "fake-conn")
    monkeypatch.setattr(loop_transport, "rpc", lambda *_a, **_kw: {"cards": [], "foundation_cards": []})
    q_res = mcp_server.call_knowledge_surface(
        "knowledge_query",
        {"query": "ACLGraph"},
        meta={"workbuddy.ai/conversationId": SESSION_B},
        config=cfg,
    )
    assert q_res["isError"] is False
    assert Path(admission.knowledge_lease(SESSION_B, config=cfg)["project_root"]).resolve() == workspace.resolve()
