"""Tier 0 & Tier 1 tests for WorkBuddy hook lifecycle, `/mindie-agent` session gate, and MCP bind security."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

import admission
import bridge
import identity
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
    assert admission.knowledge_lease(SESSION_A) is None

    prompt_out = bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "prompt": "Fix the failing unit test in model_runner.py",
        }
    )
    assert prompt_out == {"suppressOutput": True}
    assert admission.knowledge_lease(SESSION_A) is None


def test_explicit_mindie_agent_gesture_activates_and_deactivates_lease(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    out = bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "prompt": "/mindie-agent 排查 A3 双 Die HCCL 通信超时",
        }
    )
    assert "hookSpecificOutput" in out
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert SESSION_A in out["hookSpecificOutput"]["additionalContext"]

    lease = admission.knowledge_lease(SESSION_A)
    assert lease is not None
    assert lease["enabled"] == 1
    assert lease["entry_enabled"] == 1
    assert Path(lease["project_root"]) == workspace.resolve()

    # Another session never inherits SESSION_A's lease
    assert admission.knowledge_lease(SESSION_B) is None

    # Subsequent ordinary prompt in SESSION_A keeps the lease active
    follow_up = bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "prompt": "Show the HCCL ranktable",
        }
    )
    assert follow_up == {"suppressOutput": True}
    assert admission.knowledge_lease(SESSION_A) is not None

    # Explicit deactivation revokes the lease
    deact = bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "prompt": "/mindie-agent deactivate",
        }
    )
    assert "deactivated" in deact["hookSpecificOutput"]["additionalContext"]
    assert admission.knowledge_lease(SESSION_A) is None


def test_pre_tool_use_binds_mcp_call_and_rejects_replay(tmp_path: Path):
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
    assert hook_res["hookSpecificOutput"]["permissionDecision"] == "allow"

    claimed = identity.claim_bind("remote", "remote_read", args)
    assert claimed["session"] == SESSION_A
    assert claimed["tool_use_id"] == "call-001"
    identity.finish_bind(claimed["bind_id"], True)

    with pytest.raises(ValueError, match="no active PreToolUse bind"):
        identity.claim_bind("remote", "remote_read", args)

    # Also verify WorkBuddy `_meta["workbuddy.ai/conversationId"]` transport fallback
    meta_claim = identity.claim_bind(
        "tools",
        "mindie_status",
        {},
        meta={"workbuddy.ai/conversationId": SESSION_B},
    )
    assert meta_claim["session"] == SESSION_B

    # And reject forged identity fields inside tool arguments
    with pytest.raises(ValueError, match="must not declare session identity"):
        identity.claim_bind("tools", "mindie_status", {"session_id": SESSION_A})

    # Cross-conversation bind isolation: SESSION_A must never claim SESSION_B's identical bind
    shared_args = {"argv": ["leases"]}
    identity.publish_bind(SESSION_A, "tools", "coordinator", shared_args)
    identity.publish_bind(SESSION_B, "tools", "coordinator", shared_args)

    # Without _meta, two concurrent identical binds across different conversations are rejected as ambiguous
    with pytest.raises(ValueError, match="ambiguous PreToolUse binds"):
        identity.claim_bind("tools", "coordinator", shared_args)

    # With _meta specifying SESSION_A, only SESSION_A's bind is claimed
    claim_a = identity.claim_bind(
        "tools",
        "coordinator",
        shared_args,
        meta={"workbuddy.ai/conversationId": SESSION_A},
    )
    assert claim_a["session"] == SESSION_A

    # And SESSION_B's bind remains available for SESSION_B
    claim_b = identity.claim_bind(
        "tools",
        "coordinator",
        shared_args,
        meta={"workbuddy.ai/conversationId": SESSION_B},
    )
    assert claim_b["session"] == SESSION_B


def test_stop_never_forces_continuation_and_requires_manual_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
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
                    "content": [{"type": "text", "text": "/mindie-agent test"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Unactivated Stop returns quiet suppressOutput and never forces continuation
    passive_stop = bridge.handle_stop(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "transcript_path": str(transcript),
        }
    )
    assert passive_stop == {"suppressOutput": True}
    assert passive_stop.get("decision") != "block"

    # Activate session and enable community sharing for workspace
    bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "prompt": "/mindie-agent test",
        }
    )
    setup.configure_community(enabled=True, fork="user/knowledge-vllm-ascend", project_root=workspace)

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
    assert len(captured_calls) == 1
    _, forwarded = captured_calls[0]
    assert forwarded["harness"] == "wb"
    assert forwarded["session_id"] == SESSION_A
    assert forwarded["transcript_path"] == str(transcript.resolve())
