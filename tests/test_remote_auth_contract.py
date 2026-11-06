"""Authentication recovery stays actionable across the native MCP adapter."""
import json

import pytest

import mcp_server
from remote_dev.auth.store import auth_error
from remote_dev.core.errors import AUTH_ERROR_HINTS


def test_missing_server_environment_points_to_supplied_password(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("REMOTE_DEV_CREDENTIAL_FILE", raising=False)
    monkeypatch.delenv("PRIVATE_MISSING_SSH_PASSWORD", raising=False)
    response = mcp_server.call_remote_surface("remote_auth", {
        "host": "auth.example", "port": 22, "user": "fixture",
        "password_env": "PRIVATE_MISSING_SSH_PASSWORD",
    }, meta={"workbuddy.ai/conversationId": "ssh-auth-contract-session"})
    assert response["isError"] is True
    details = response["structuredContent"]["error_details"]
    assert details["category"] == "credential_unavailable"
    assert details["submission_state"] == "not_sent"
    assert AUTH_ERROR_HINTS["credential_unavailable"] in response["content"][0]["text"]
    assert "PRIVATE_MISSING_SSH_PASSWORD" not in json.dumps(response)


@pytest.mark.parametrize("category", ["authentication_rejected", "host_key", "connection_unavailable"])
def test_auth_failure_preserves_safe_maintenance_and_delivery_state(monkeypatch, category):
    import remote_dev.mcp.tools as tools

    failure = auth_error("PRIVATE_SSH_STDERR_AND_PATH", category)
    failure.bootstrap = {
        "status": "unknown", "installation": "unknown", "key_verified": False,
        "error": "PRIVATE_BOOTSTRAP_STDERR", "credential_ref": "PRIVATE_CREDENTIAL_REF",
    }

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(tools, "call_tool", fail)
    response = mcp_server.call_remote_surface("remote_auth", {
        "host": "auth.example", "port": 22,
    }, meta={"workbuddy.ai/conversationId": "ssh-auth-contract-session"})
    assert response["isError"] is True
    details = response["structuredContent"]["error_details"]
    assert details["category"] == category
    assert details["submission_state"] == "not_sent"
    assert details["bootstrap"] == {
        "status": "unknown", "installation": "unknown", "key_verified": False,
    }
    assert AUTH_ERROR_HINTS[category] in response["content"][0]["text"]
    assert "PRIVATE" not in json.dumps(response)

