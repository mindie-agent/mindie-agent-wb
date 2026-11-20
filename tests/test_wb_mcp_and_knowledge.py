"""Fast isolated tests for disjoint MCP catalogs, zero-SQLite public reads, and Receipt Honesty."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading

import pytest

import mcp_server
import setup
from mindie_harness.api import ProfileRef
from mindie_harness.mcp.router import RequestContext
from mindie_harness.process.supervisor import Deadline

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "mindie-agent"


def _no_meta(value) -> bool:
    if isinstance(value, dict):
        return "_meta" not in value and all(_no_meta(item) for item in value.values())
    return all(_no_meta(item) for item in value) if isinstance(value, list) else True


def _adapter(surface: str, config: Path):
    profile = ProfileRef.resolve("wb", plugin_root=PLUGIN_ROOT, config_path=config)
    return mcp_server.WBManagedAdapter(surface, plugin_root=PLUGIN_ROOT, config_path=config), profile


def test_disjoint_mcp_catalogs(tmp_path: Path):
    config = tmp_path / "wb.json"
    remote, profile = _adapter("remote", config)
    tools, _ = _adapter("tools", config)
    knowledge, _ = _adapter("knowledge", config)
    remote_names = {item["name"] for item in remote.mcp_tools("remote", profile)}
    tool_names = {item["name"] for item in tools.mcp_tools("tools", profile)}
    knowledge_names = {item["name"] for item in knowledge.mcp_tools("knowledge", profile)}

    assert len(remote_names) == 15
    assert {"remote_Read", "remote_Write", "remote_Bash", "remote_auth", "remote_probe", "remote_artifact_pull", "remote_artifact_push"} <= remote_names
    assert "remote_BashOutput" not in remote_names and "remote_KillShell" not in remote_names
    assert len(tool_names) == 11
    assert "mindie_doctor" in tool_names and "mindie_status" in tool_names
    assert not any(name.startswith("knowledge_") for name in tool_names)
    assert knowledge_names == {"knowledge_query", "knowledge_explain", "knowledge_feedback", "knowledge_distill", "knowledge_sync"}
    for catalog in (remote.mcp_tools("remote", profile), tools.mcp_tools("tools", profile), knowledge.mcp_tools("knowledge", profile)):
        assert _no_meta(catalog)
    assert not profile.admission_db.exists()


def test_knowledge_query_zero_meta_and_receipt_honesty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config, adm_db = tmp_path / "wb.json", tmp_path / "adm.sqlite3"
    config.write_text(json.dumps({"admission_path": str(adm_db)}), encoding="utf-8")
    setup.ensure_profile(config=config, python_exe=sys.executable)
    assert not adm_db.exists()

    adapter, profile = _adapter("knowledge", config)
    assert profile.admission_db == adm_db
    payloads: list[tuple[str, dict]] = []
    fake_conn = {"url": "http://127.0.0.1:1", "token": "test", "domain": "vllm-ascend"}

    def fake_rpc(connection, method, payload, *, timeout=None):
        payloads.append((method, dict(payload)))
        if method == "distill":
            return {"status": "noop", "decision": {"action": "noop", "reason": "routine_qa_or_unverified"}, "publication": "not_requested"}
        return {"cards": [], "session": payload["_session_id"]}

    monkeypatch.setattr("mindie_knowledge.loop.cli.ensure_service", lambda engine_path: fake_conn)
    monkeypatch.setattr("mindie_knowledge.loop.cli.connect", lambda *args, **kwargs: fake_conn)
    monkeypatch.setattr("mindie_knowledge.loop.cli.rpc", fake_rpc)
    monkeypatch.setattr("mindie_knowledge.loop.transport.rpc", fake_rpc)

    ctx_query = RequestContext(1, "knowledge", "knowledge_query", "session-a", tmp_path, {}, Deadline.from_timeout(30.0), threading.Event())
    query_res = adapter.dispatch_tool(ctx_query, {"query": "A3 dual-Die HCCL"}, profile)
    assert query_res["isError"] is False
    assert query_res["structuredContent"]["session"] == "session-a"
    method, payload = payloads[0]
    assert method == "query"
    assert payload["_session_id"] == "session-a" and "_meta" not in payload
    assert not (tmp_path / "adm.sqlite3").exists()

    ctx_explain = RequestContext(2, "knowledge", "knowledge_explain", "session-a", tmp_path, {}, Deadline.from_timeout(30.0), threading.Event())
    explain_res = adapter.dispatch_tool(ctx_explain, {"ref": "mindie://vllm-ascend/aclgraph-001@1"}, profile)
    assert explain_res["isError"] is False
    assert payloads[1][0] == "explain"
    assert not (tmp_path / "adm.sqlite3").exists()

    # Now verify Receipt Honesty on admitted `knowledge_distill` when `Admission.finish` fails after RPC success.
    from mindie_knowledge.loop.activation import Admission

    Admission(adm_db).activate("session-a", project_root=str(tmp_path), root_session="session-a", not_before=0.0)
    monkeypatch.setattr(
        "mindie_knowledge.loop.activation.Admission.finish",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("receipt store offline")),
    )
    ctx_distill = RequestContext(3, "knowledge", "knowledge_distill", "session-a", tmp_path, {}, Deadline.from_timeout(60.0), threading.Event())
    distill_res = adapter.dispatch_tool(ctx_distill, {"inputs": [], "task_state": "completed"}, profile)
    assert distill_res["isError"] is False
    assert distill_res["structuredContent"]["receipt_unconfirmed"] is True
    assert "receipt_warning" in distill_res["structuredContent"]
