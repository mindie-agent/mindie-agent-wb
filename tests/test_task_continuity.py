"""Failed MindIE admission preserves the MCP connection and truthful results."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import mcp_server


def invoke(name, arguments, **kwargs):
    return mcp_server.call_remote_surface(name, arguments, meta={"workbuddy.ai/conversationId": "continuity-session"}, **kwargs)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_CONVERSATION_ID", "454c6ee9-afea-47be-8188-abb9c7fb1f26")
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("REMOTE_DEV_CREDENTIAL_FILE", raising=False)


def test_real_path_rejection_is_failed_in_mcp_and_receipt(session, tmp_path):
    receipts = mcp_server.RemoteReceipts(tmp_path / "receipts.sqlite3")
    response = invoke("remote_write", {
        "host": "auth.example", "port": 22, "root": "/workspace", "cwd": "/workspace",
        "file_path": "/outside-root.txt", "content": "fixture",
    }, receipts=receipts)
    assert response["structuredContent"]["result"]["outcome"] == "blocked"
    assert response["isError"] is True
    with sqlite3.connect(receipts.path) as db:
        assert db.execute("SELECT status FROM receipts").fetchone()[0] == "failed"


def test_uncertain_result_stays_unknown_in_receipt(session, tmp_path, monkeypatch):
    import remote_dev.mcp.tools as tools

    monkeypatch.setattr(tools, "call_tool", lambda *args, **kwargs: {
        "text": "fixture transport capture", "result": {
            "outcome": "failed", "status": "submission_uncertain",
            "error_details": {"submission_state": "uncertain"},
        },
    })
    receipts = mcp_server.RemoteReceipts(tmp_path / "receipts.sqlite3")
    response = invoke("remote_bash", {"host": "auth.example", "port": 22, "command": "fixture"},
                      receipts=receipts)
    assert response["isError"] is True
    with sqlite3.connect(receipts.path) as db:
        assert db.execute("SELECT status FROM receipts").fetchone()[0] == "unknown"


@pytest.mark.parametrize("surface,name,arguments", [
    ("remote", "remote_auth", {"host": "auth.example", "port": 22, "action": "status"}),
    ("knowledge", "mindie_entry", {"op": "status"}),
])
def test_admission_failure_does_not_terminate_stdio_server(tmp_path, monkeypatch, surface, name, arguments):
    import os
    from paths import remote_receipts_path

    monkeypatch.delenv("AGY_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("REMOTE_DEV_SESSION_ID", raising=False)
    receipt = remote_receipts_path()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_bytes(b"fixture invalid sqlite database")
    script = Path(mcp_server.__file__)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": name, "arguments": arguments}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
    ]
    # Missing native admission must fail only this request.
    completed = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(script), surface],
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=15,
        env=dict(os.environ),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    assert completed.returncode == 0, completed.stderr
    replies = {item["id"]: item for item in map(json.loads, completed.stdout.splitlines())}
    assert set(replies) == {1, 2, 3, 4}
    assert replies[2]["result"]["isError"] is True
    assert replies[2]["result"]["structuredContent"]["execution"] == "not_started"
    assert replies[3]["result"] == {}
    assert replies[4]["result"]["tools"]


def test_deterministic_tools_execute_without_meta_or_pretool_bind_while_knowledge_stays_gated(tmp_path, monkeypatch):
    monkeypatch.delenv("AGY_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("REMOTE_DEV_SESSION_ID", raising=False)
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    receipts = mcp_server.RemoteReceipts(tmp_path / "receipts.sqlite3")
    remote_res = mcp_server.call_remote_surface(
        "remote_auth", {"host": "auth.example", "port": 22, "action": "status"},
        meta=None, receipts=receipts,
    )
    assert remote_res["isError"] is False
    tools_res = mcp_server.call_tools_surface("diagnostics_status", {}, meta=None)
    assert tools_res["isError"] is False
    knowledge_res = mcp_server.call_knowledge_surface("mindie_entry", {"op": "status"}, meta=None)
    assert knowledge_res["isError"] is True
    req = {
        "jsonrpc": "2.0", "id": 88, "method": "tools/call",
        "params": {"name": "diagnostics_status", "arguments": {}},
    }
    first_rpc = mcp_server.handle_rpc(req, "tools")
    assert first_rpc["result"]["isError"] is False
    replay_rpc = mcp_server.handle_rpc(req, "tools")
    assert replay_rpc["result"]["isError"] is True
    seen_env = {}
    orig_run = mcp_server.bounded_run

    def spy_run(cmd, stdin_data="", **kwargs):
        seen_env.update(kwargs.get("env") or {})
        return orig_run(cmd, stdin_data, **kwargs)

    monkeypatch.setattr(mcp_server, "bounded_run", spy_run)
    coord_res = mcp_server.call_tools_surface(
        "coordinator", {"argv": ["hosts", "--action", "list"], "cwd": str(tmp_path)},
        meta={"workbuddy.ai/conversationId": "wb-coord-session"},
    )
    assert coord_res["isError"] is False, coord_res
    assert "AGY_CONVERSATION_ID" not in seen_env
    assert json.loads(seen_env["MINDIE_COORDINATOR_NATIVE_CALL"]) == {
        "workbuddy.ai/conversationId": "wb-coord-session",
    }
    import os
    from identity import publish_bind

    rem_args = {"host": "auth.example", "port": 22, "action": "status"}
    wb_sess = "wb-session-replay-guard"
    replay_receipts = mcp_server.RemoteReceipts(tmp_path / "replay-receipts.sqlite3")
    publish_bind(wb_sess, "remote", "remote_auth", rem_args)
    rem_req = {"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "remote_auth", "arguments": rem_args}}
    assert mcp_server.handle_rpc(rem_req, "remote", receipts=replay_receipts)["result"]["isError"] is False
    publish_bind(wb_sess, "remote", "remote_auth", rem_args)
    assert mcp_server.handle_rpc(rem_req, "remote", receipts=replay_receipts)["result"]["isError"] is True
    assert mcp_server.handle_rpc({**rem_req, "id": 100}, "remote", receipts=replay_receipts)["result"]["isError"] is False
    assert "REMOTE_DEV_SESSION_ID" not in os.environ
    with sqlite3.connect(replay_receipts.path) as db:
        assert [row[0] for row in db.execute("SELECT session FROM receipts ORDER BY started")] == [wb_sess, wb_sess]

