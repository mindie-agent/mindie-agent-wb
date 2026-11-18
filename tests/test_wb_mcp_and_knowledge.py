"""Fast isolated tests for disjoint MCP catalogs and zero-`_meta` knowledge dispatch."""

from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

import mcp_server
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
    assert "mindie_doctor" in tool_names and "mindie_status" in tool_names
    assert not any(name.startswith("knowledge_") for name in tool_names)
    assert knowledge_names == {"knowledge_query", "knowledge_explain", "knowledge_feedback", "knowledge_distill", "knowledge_sync"}
    for catalog in (remote.mcp_tools("remote", profile), tools.mcp_tools("tools", profile), knowledge.mcp_tools("knowledge", profile)):
        assert _no_meta(catalog)
    assert not profile.admission_db.exists()


def test_knowledge_query_zero_meta_and_receipt_honesty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config, engine = tmp_path / "wb.json", tmp_path / "wb.engine.json"
    config.write_text(json.dumps({"engine_config": str(engine), "admission_path": str(tmp_path / "adm.sqlite3")}), encoding="utf-8")
    engine.write_text(json.dumps({"root": str(tmp_path / "domains"), "domain": "test"}), encoding="utf-8")
    from mindie_knowledge.loop.store import Store

    Store(str(tmp_path / "domains"), "test").close()
    adapter, profile = _adapter("knowledge", config)
    context = RequestContext(1, "knowledge", "knowledge_query", "session-a", tmp_path, {}, Deadline.from_timeout(30.0), threading.Event())
    unavailable = {"content": [{"type": "text", "text": "unavailable"}], "isError": True, "structuredContent": {"reason": "knowledge_unavailable"}}
    payloads: list[tuple[str, dict]] = []

    def fake_rpc(connection, method, payload, *, timeout=None):
        payloads.append((method, dict(payload)))
        return {"cards": [], "session": payload["_session_id"]}

    monkeypatch.setattr("mindie_harness.mcp.gateway.invoke_knowledge_tool", lambda *args, **kwargs: unavailable)
    monkeypatch.setattr("mindie_knowledge.loop.cli.ensure_service", lambda engine_path: {"url": "http://127.0.0.1:1", "token": "test"})
    monkeypatch.setattr("mindie_knowledge.loop.transport.rpc", fake_rpc)
    monkeypatch.setattr("mindie_knowledge.loop.activation.Admission.finish", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("receipt store offline")))

    result = adapter.dispatch_tool(context, {"query": "A3 dual-Die HCCL"}, profile)
    assert result["isError"] is False
    assert result["structuredContent"]["session"] == "session-a"
    assert result["structuredContent"]["receipt_unconfirmed"] is True
    assert "receipt_warning" in result["structuredContent"]
    method, payload = payloads[0]
    assert method == "query"
    assert payload["_session_id"] == "session-a" and "_meta" not in payload
    assert (tmp_path / "adm.sqlite3").exists()
