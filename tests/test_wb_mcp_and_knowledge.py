"""Tier 0 & Tier 1 tests for full 40-tool MCP exposure (`remote`, `tools`, `knowledge`), `knowledge_cli`, and Receipt Honesty."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

import bridge
import knowledge_cli
import mcp_server


SESSION_A = "454c6ee9-afea-47be-8188-abb9c7fb1f26"


def test_complete_mcp_tool_surfaces_exposed():
    remote_resp = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "remote")
    tools_resp = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "tools")
    know_resp = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "knowledge")

    remote_names = {t["name"] for t in remote_resp["result"]["tools"]}
    tool_names = {t["name"] for t in tools_resp["result"]["tools"]}
    know_names = {t["name"] for t in know_resp["result"]["tools"]}

    # 1. All 20 canonical + 13 native WorkBuddy remote-dev MCP tools (33 total)
    expected_remote = {
        "remote_auth",
        "remote_read",
        "remote_write",
        "remote_edit",
        "remote_multi_edit",
        "remote_bash",
        "remote_glob",
        "remote_grep",
        "remote_ls",
        "remote_apply_patch",
        "remote_job_status",
        "remote_job_tail",
        "remote_job_output",
        "remote_job_stop",
        "remote_job_stdin",
        "remote_artifact_manifest",
        "remote_artifact_pull",
        "remote_artifact_push",
        "remote_context_snapshot",
        "remote_probe",
        "remote_Read",
        "remote_Write",
        "remote_Edit",
        "remote_MultiEdit",
        "remote_Bash",
        "remote_Glob",
        "remote_Grep",
        "remote_TaskOutput",
        "remote_BashOutput",
        "remote_NotebookEdit",
        "remote_TaskStop",
        "remote_KillShell",
        "remote_ViewImage",
    }
    assert remote_names == expected_remote

    # 2. All 14 MindIE component + NPU observation + diagnostics tools
    expected_tools = {
        "mindie_status",
        "profiler",
        "sim",
        "coordinator",
        "debugger",
        "npu_top",
        "npu_status",
        "server_status",
        "list_mounts",
        "find_npu_capacity",
        "npu_telemetry_window",
        "list_npu_servers",
        "diagnostics_status",
        "diagnostics_bundle",
    }
    assert tool_names == expected_tools

    # 3. All 6 MindIE knowledge & entry tools + accurate JSON Schema types
    expected_knowledge = {
        "mindie_entry",
        "knowledge_query",
        "knowledge_explain",
        "knowledge_feedback",
        "knowledge_distill",
        "knowledge_sync",
    }
    assert know_names == expected_knowledge
    assert len(remote_names) + len(tool_names) + len(know_names) == 53

    know_by_name = {t["name"]: t["inputSchema"] for t in know_resp["result"]["tools"]}
    assert know_by_name["knowledge_query"]["properties"]["limit"]["type"] == "integer"
    assert know_by_name["knowledge_query"]["properties"]["conditions"]["type"] == "object"
    assert know_by_name["knowledge_query"]["properties"]["include_unknown"]["type"] == "boolean"
    assert know_by_name["knowledge_query"]["properties"]["mode"]["enum"] == ["reuse", "diagnose", "explore"]
    assert know_by_name["knowledge_explain"]["properties"]["refs"]["type"] == "array"
    assert know_by_name["knowledge_explain"]["properties"]["mode"]["enum"] == ["reuse", "diagnose", "explore"]
    assert know_by_name["knowledge_distill"]["properties"]["inputs"]["type"] == "array"
    assert know_by_name["knowledge_distill"]["properties"]["proposal"]["type"] == "object"


def test_mindie_entry_mcp_tool_activates_and_deactivates_session(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # Activate via `mindie_entry` MCP tool using WorkBuddy `_meta["workbuddy.ai/conversationId"]`
    act_rpc = mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "mindie_entry",
                "arguments": {"action": "activate", "project_root": str(workspace)},
                "_meta": {"workbuddy.ai/conversationId": SESSION_A},
            },
        },
        "knowledge",
    )
    act_res = act_rpc["result"]
    assert act_res["isError"] is False
    assert act_res["structuredContent"]["status"] == "active"
    assert act_res["structuredContent"]["session"] == SESSION_A

    # Status check
    stat_rpc = mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "mindie_entry",
                "arguments": {"action": "status"},
                "_meta": {"workbuddy.ai/conversationId": SESSION_A},
            },
        },
        "knowledge",
    )
    assert stat_rpc["result"]["structuredContent"]["knowledge_lease"]["enabled"] == 1

    # Deactivate
    deact_rpc = mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "mindie_entry",
                "arguments": {"action": "deactivate"},
                "_meta": {"workbuddy.ai/conversationId": SESSION_A},
            },
        },
        "knowledge",
    )
    assert deact_rpc["result"]["structuredContent"]["status"] in {"inactive", "deactivated"}


def test_debugger_and_diagnostics_mcp_tools_execute_end_to_end():
    # `debugger` decode-code action on Ascend RTS error code 507011
    dbg_rpc = mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "debugger",
                "arguments": {
                    "action": "decode-code",
                    "code": "507011",
                },
                "_meta": {"workbuddy.ai/conversationId": SESSION_A},
            },
        },
        "tools",
    )
    dbg_res = dbg_rpc["result"]
    assert dbg_res["isError"] is False
    assert dbg_res["structuredContent"]["component"] == "debugger"

    # `mindie_status` and `diagnostics_status`
    status_rpc = mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "mindie_status",
                "arguments": {},
                "_meta": {"workbuddy.ai/conversationId": SESSION_A},
            },
        },
        "tools",
    )
    assert status_rpc["result"]["isError"] is False
    assert status_rpc["result"]["structuredContent"]["harness"] == "workbuddy"

    diag_rpc = mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {
                "name": "diagnostics_status",
                "arguments": {},
                "_meta": {"workbuddy.ai/conversationId": SESSION_A},
            },
        },
        "tools",
    )
    assert diag_rpc["result"]["isError"] is False


def test_knowledge_cli_and_knowledge_mcp_auto_bind_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("WB_CONVERSATION_ID", SESSION_A)

    with pytest.raises(knowledge_cli.KnowledgeInactive):
        knowledge_cli.invoke("describe")

    bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_A,
            "cwd": str(workspace),
            "prompt": "/mindie-agent enable knowledge",
        }
    )

    desc = knowledge_cli.invoke("describe")
    assert desc["isError"] is False
    names = {t["name"] for t in desc["tools"]}
    assert {"knowledge_query", "knowledge_explain", "knowledge_feedback", "knowledge_distill", "knowledge_sync"} <= names


def test_receipt_honesty_preserves_succeeded_remote_result_on_local_sqlite_error(monkeypatch: pytest.MonkeyPatch):
    import remote_dev.mcp.tools as rd_tools

    monkeypatch.setattr(
        rd_tools,
        "call_tool",
        lambda name, args, profile="canonical": {"text": "fixture", "result": {"outcome": "success", "status": "ok"}},
    )

    class BrokenFinishReceipts(mcp_server.RemoteReceipts):
        def finish(self, call_id: str, status: str) -> None:
            raise sqlite3.OperationalError("database disk image is malformed on finish")

    res = mcp_server.call_remote_surface(
        "remote_probe",
        {},
        meta={"workbuddy.ai/conversationId": SESSION_A},
        receipts=BrokenFinishReceipts(),
    )
    assert res["isError"] is False
    assert res["structuredContent"]["result"]["outcome"] == "success"
    assert res["structuredContent"]["receipt_unconfirmed"] is True
    assert "receipt_warning" in res["structuredContent"]


def test_remote_surface_claims_original_arguments_before_clamping_yield_time_ms(monkeypatch: pytest.MonkeyPatch):
    import identity
    import remote_dev.mcp.tools as rd_tools

    seen_args: list[dict] = []

    def _fake_call_tool(name: str, args: dict, profile: str = "canonical") -> dict:
        seen_args.append(dict(args))
        return {"text": "fixture", "result": {"outcome": "success", "yield_time_ms": args.get("yield_time_ms")}}

    monkeypatch.setattr(rd_tools, "call_tool", _fake_call_tool)

    # Publish PreToolUse bind with yield_time_ms=60000 (above 30000 clamp ceiling)
    raw_input = {"command": "npu-smi info", "yield_time_ms": 60000}
    identity.publish_bind(SESSION_A, "remote", "remote_bash", raw_input)

    # Claim via PreToolUse without _meta; must match raw_input (60000) before clamping to 30000
    res = mcp_server.call_remote_surface("remote_bash", raw_input)
    assert res["isError"] is False
    assert seen_args == [{"command": "npu-smi info", "yield_time_ms": 30000}]


def test_remote_timeout_arithmetic_enforces_outer_greater_than_inner_plus_grace():
    # remote_bash has a 600,000 ms (600s) native wait; with 60s grace, outer budget must be 660s
    assert mcp_server.remote_timeout("remote_bash", {}) == 660.0
    assert mcp_server.remote_timeout("remote_run_command", {"WaitMsBeforeAsync": 10000}) == 660.0
    assert mcp_server.remote_timeout("remote_read", {"timeout_ms": 200000}) == 260.0
