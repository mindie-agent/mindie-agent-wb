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
    ("tools", "mindie_status", {}),
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
    completed = subprocess.run([sys.executable, str(script), surface],
                               input="".join(json.dumps(item) + "\n" for item in requests),
                               text=True, capture_output=True, timeout=15, env=dict(os.environ))
    assert completed.returncode == 0, completed.stderr
    replies = {item["id"]: item for item in map(json.loads, completed.stdout.splitlines())}
    assert set(replies) == {1, 2, 3, 4}
    assert replies[2]["result"]["isError"] is True
    assert replies[2]["result"]["structuredContent"]["execution"] == "not_started"
    assert replies[3]["result"] == {}
    assert replies[4]["result"]["tools"]

