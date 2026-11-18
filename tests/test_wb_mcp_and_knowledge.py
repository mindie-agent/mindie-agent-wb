"""Tier 0 & Tier 1 tests for full 52-tool MCP exposure (`remote`, `tools`, `knowledge`), `knowledge_cli`, and Receipt Honesty."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

import admission
import knowledge_cli
import mcp_server
import paths


SESSION_A = "454c6ee9-afea-47be-8188-abb9c7fb1f26"


def test_complete_mcp_tool_surfaces_exposed():
    adm_db = paths.admission_path()
    assert not adm_db.exists()

    remote_resp = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "remote")
    tools_resp = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "tools")
    know_resp = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "knowledge")
    assert not adm_db.exists()

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

    # 3. All 5 MindIE knowledge tools exposed directly on startup (no mindie_entry)
    expected_knowledge = {
        "knowledge_query",
        "knowledge_explain",
        "knowledge_feedback",
        "knowledge_distill",
        "knowledge_sync",
    }
    assert know_names == expected_knowledge
    assert "mindie_entry" not in know_names
    assert len(remote_names) + len(tool_names) + len(know_names) == 52

    know_by_name = {t["name"]: t["inputSchema"] for t in know_resp["result"]["tools"]}
    know_ann = {t["name"]: t["annotations"] for t in know_resp["result"]["tools"]}
    assert know_ann["knowledge_query"]["readOnlyHint"] is True
    assert know_ann["knowledge_explain"]["readOnlyHint"] is True
    assert know_ann["knowledge_sync"]["openWorldHint"] is True
    assert know_by_name["knowledge_query"]["properties"]["limit"]["type"] == "integer"
    assert know_by_name["knowledge_query"]["properties"]["conditions"]["type"] == "object"
    assert know_by_name["knowledge_query"]["properties"]["include_unknown"]["type"] == "boolean"
    assert know_by_name["knowledge_query"]["properties"]["mode"]["enum"] == ["reuse", "diagnose", "explore"]
    assert know_by_name["knowledge_explain"]["properties"]["refs"]["type"] == "array"
    assert know_by_name["knowledge_explain"]["properties"]["mode"]["enum"] == ["reuse", "diagnose", "explore"]
    assert know_by_name["knowledge_distill"]["properties"]["inputs"]["type"] == "array"
    assert know_by_name["knowledge_distill"]["properties"]["proposal"]["type"] == "object"


def test_knowledge_mcp_zero_session_binding_gate_and_auto_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WB_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("WORKBUDDY_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEBUDDY_SESSION_ID", raising=False)
    monkeypatch.delenv("AGY_CONVERSATION_ID", raising=False)

    monkeypatch.setattr(knowledge_cli, "_ensure_loop_connection", lambda _eng: {"socket": "mock"})
    monkeypatch.setattr("mindie_knowledge.loop.transport.rpc", lambda conn, method, payload, timeout=30.0: {"cards": [], "session": payload["_session_id"]})

    # Zero _meta and zero PreToolUse bind falls back cleanly to _CONNECTION_SESSION_ID and provisions default lease (not_before=0.0)
    rpc_res = mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "knowledge_query",
                "arguments": {"query": "A3 dual-Die HCCL"},
            },
        },
        "knowledge",
    )
    assert rpc_res["result"]["isError"] is False
    recorded_session = rpc_res["result"]["structuredContent"]["session"]
    assert recorded_session == mcp_server._CONNECTION_SESSION_ID
    lease = admission.knowledge_lease(recorded_session)
    assert lease is not None
    assert lease["enabled"] == 1
    assert lease["entry_enabled"] == 1
    assert lease["activated_at"] == 0.0


def test_debugger_and_diagnostics_mcp_tools_execute_end_to_end():
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


def test_knowledge_cli_default_on_without_session_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WB_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("WORKBUDDY_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEBUDDY_SESSION_ID", raising=False)
    monkeypatch.delenv("AGY_CONVERSATION_ID", raising=False)

    desc = knowledge_cli.invoke("describe")
    assert desc["isError"] is False
    assert desc["session_id"] == "wb-cli-default"
    assert not paths.admission_path().exists()
    names = {t["name"] for t in desc["tools"]}
    assert names == {"knowledge_query", "knowledge_explain", "knowledge_feedback", "knowledge_distill", "knowledge_sync"}

    monkeypatch.setattr(knowledge_cli, "_ensure_loop_connection", lambda _eng: {"socket": "mock"})
    monkeypatch.setattr("mindie_knowledge.loop.transport.rpc", lambda conn, method, payload, timeout=30.0: {"cards": [], "session": payload["_session_id"]})
    q_res = knowledge_cli.invoke("knowledge_query", {"query": "ACLGraph sync"})
    assert q_res["isError"] is False
    assert q_res["result"]["session"] == "wb-cli-default"
    assert admission.knowledge_lease("wb-cli-default")["activated_at"] == 0.0


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

    raw_input = {"command": "npu-smi info", "yield_time_ms": 60000}
    identity.publish_bind(SESSION_A, "remote", "remote_bash", raw_input)

    res = mcp_server.call_remote_surface("remote_bash", raw_input, meta={"workbuddy.ai/conversationId": SESSION_A})
    assert res["isError"] is False
    assert seen_args == [{"command": "npu-smi info", "yield_time_ms": 30000}]


def test_knowledge_timeout_records_unknown_receipt_and_zero_meta_skips_bind_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import sqlite3
    import identity
    import mindie_knowledge.loop.cli as loop_cli
    import mindie_knowledge.loop.transport as loop_transport

    # 1. Zero-meta `_resolve_claim` never opens `mcp-binds.sqlite3` even when corrupt!
    identity.bind_db_path().parent.mkdir(parents=True, exist_ok=True)
    identity.bind_db_path().write_bytes(b"corrupt-bind-db-bytes")
    claim = mcp_server._resolve_claim("knowledge", "knowledge_query", {"query": "ok"}, meta=None)
    assert claim["session"] == mcp_server._CONNECTION_SESSION_ID
    identity.bind_db_path().unlink()

    # 2. `knowledge_sync` / `knowledge_distill` TimeoutError records `"unknown"` receipt!
    monkeypatch.setattr(loop_cli, "ensure_service", lambda _cfg: "fake-conn")
    monkeypatch.setattr(
        loop_transport,
        "rpc",
        lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("sync rpc timed out")),
    )
    receipts = mcp_server.RemoteReceipts(tmp_path / "know-timeout-receipts.sqlite3")
    sync_err = mcp_server.call_knowledge_surface("knowledge_sync", {}, rpc_id=77, receipts=receipts)
    assert sync_err["isError"] is True
    with sqlite3.connect(receipts.path) as db:
        statuses = [r[0] for r in db.execute("SELECT status FROM receipts")]
    assert statuses == ["unknown"]


def test_remote_timeout_arithmetic_enforces_outer_greater_than_inner_plus_grace():
    assert mcp_server.remote_timeout("remote_bash", {}) == 660.0
    assert mcp_server.remote_timeout("remote_run_command", {"WaitMsBeforeAsync": 10000}) == 660.0
    assert mcp_server.remote_timeout("remote_read", {"timeout_ms": 200000}) == 260.0
