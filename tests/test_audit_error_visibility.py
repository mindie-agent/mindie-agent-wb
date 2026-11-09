"""Tier 0 behavior tests for #332 WorkBuddy error-visibility fixes."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import types

import pytest

import component_update
import mcp_server
import mindie_launch
import setup
import wb_native_compat


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"


class _CaptureStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def _patch_npu_top(monkeypatch: pytest.MonkeyPatch, handle_request, *, client_error: Exception | None = None):
    client_exc = type("ClientError", (Exception,), {})

    class FakeClient:
        def __init__(self) -> None:
            if client_error is not None:
                raise client_error

    pkg = types.ModuleType("npu_top")
    client_mod = types.ModuleType("npu_top.client")
    client_mod.ClientError = client_exc
    client_mod.VawsTopClient = FakeClient
    mcp_mod = types.ModuleType("npu_top.mcp")
    mcp_mod.handle_request = handle_request
    pkg.client = client_mod
    pkg.mcp = mcp_mod
    monkeypatch.setitem(sys.modules, "npu_top", pkg)
    monkeypatch.setitem(sys.modules, "npu_top.client", client_mod)
    monkeypatch.setitem(sys.modules, "npu_top.mcp", mcp_mod)
    return client_exc


def test_npu_top_none_or_missing_result_is_error_not_empty_success(monkeypatch: pytest.MonkeyPatch):
    _patch_npu_top(monkeypatch, lambda req, client: None)
    none_res = mcp_server._execute_npu_top_observation("npu_status", {})
    assert none_res["isError"] is True
    assert none_res["structuredContent"]["error"]
    assert none_res["content"][0]["text"] != "{}"

    _patch_npu_top(monkeypatch, lambda req, client: {"jsonrpc": "2.0", "id": 1})
    missing = mcp_server._execute_npu_top_observation("npu_status", {})
    assert missing["isError"] is True
    assert "missing" in missing["structuredContent"]["error"]

    _patch_npu_top(monkeypatch, lambda req, client: {"jsonrpc": "2.0", "id": 1, "result": "not-an-object"})
    bad = mcp_server._execute_npu_top_observation("npu_status", {})
    assert bad["isError"] is True

    _patch_npu_top(monkeypatch, lambda req, client: {"jsonrpc": "2.0", "id": 1, "result": {}})
    empty = mcp_server._execute_npu_top_observation("npu_status", {})
    assert empty["isError"] is True
    assert "empty object" in empty["structuredContent"]["error"]

    _patch_npu_top(
        monkeypatch,
        lambda req, client: {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "ok"}]}},
    )
    ok = mcp_server._execute_npu_top_observation("npu_status", {})
    assert ok.get("isError") is not True
    assert ok["content"][0]["text"] == "ok"


def test_mcp_parse_error_writes_jsonrpc_32700(monkeypatch: pytest.MonkeyPatch):
    stdout = _CaptureStdout()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"{not-json\n")))
    monkeypatch.setattr(sys, "stdout", stdout)
    assert mcp_server.serve("tools") == 0
    payload = json.loads(stdout.buffer.getvalue())
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] is None
    assert payload["error"]["code"] == -32700
    assert "Parse error" in payload["error"]["message"]


def test_mcp_non_object_request_is_invalid_request_not_crash(monkeypatch: pytest.MonkeyPatch):
    stdout = _CaptureStdout()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"[]\n123\n\"tool\"\n")))
    monkeypatch.setattr(sys, "stdout", stdout)
    assert mcp_server.serve("tools") == 0
    frames = [json.loads(line) for line in stdout.buffer.getvalue().decode("utf-8").splitlines() if line]
    assert len(frames) == 3
    for payload in frames:
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] is None
        assert payload["error"]["code"] == -32600
        assert "Invalid Request" in payload["error"]["message"]

    params = mcp_server.handle_rpc(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": ["npu_status"]},
        "tools",
    )
    assert params["error"]["code"] == -32602


def test_hook_exception_is_visible_nonzero_not_suppress_output(monkeypatch: pytest.MonkeyPatch):
    import bridge

    def _boom(_argv):
        raise RuntimeError("publish_bind failed")

    monkeypatch.setattr(bridge, "main", _boom)
    stdout = _CaptureStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    rc = mindie_launch.main(["hook", "pre-tool-use"])
    assert rc == 0
    payload = json.loads(stdout.buffer.getvalue())
    assert payload.get("suppressOutput") is not True
    hook_out = payload["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    assert hook_out["permissionDecision"] == "deny"
    assert "RuntimeError" in hook_out["permissionDecisionReason"]
    assert "publish_bind failed" in hook_out["systemMessage"]


def test_hook_timeout_is_json_deny_not_bare_exit(monkeypatch: pytest.MonkeyPatch):
    import bridge

    def _boom(_argv):
        raise TimeoutError("hook budget exhausted")

    monkeypatch.setattr(bridge, "main", _boom)
    stdout = _CaptureStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    rc = mindie_launch.main(["hook", "pre-tool-use"])
    assert rc == 0
    payload = json.loads(stdout.buffer.getvalue())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "TimeoutError" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_and_setup_reexec_pass_timeout_mcp_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import subprocess

    other = tmp_path / "other-python"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(mindie_launch, "_preferred_python", lambda: str(other))
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        seen.append({"argv": list(argv), "kwargs": dict(kwargs)})
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mindie_launch.main(["hook", "pre-tool-use"]) == 0
    assert seen[-1]["kwargs"]["timeout"] == mindie_launch.HOOK_TOTAL
    assert mindie_launch.main(["mcp", "tools"]) == 0
    assert "timeout" not in seen[-1]["kwargs"]
    assert mindie_launch.main(["setup", "init"]) == 0
    assert seen[-1]["kwargs"]["timeout"] == mindie_launch.BOOTSTRAP_TOTAL + mindie_launch.SETUP_REEXEC_GRACE


def test_hook_reexec_nonzero_is_json_deny_not_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import subprocess

    other = tmp_path / "other-python"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(mindie_launch, "_preferred_python", lambda: str(other))
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: types.SimpleNamespace(returncode=1))
    stdout = _CaptureStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    rc = mindie_launch.main(["hook", "pre-tool-use"])
    assert rc == 0
    payload = json.loads(stdout.buffer.getvalue())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "exited 1" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_startup_error_is_json_deny_not_bare_exit(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict
):
    cfg = isolated_mindie_env["config_path"]
    cfg.write_text(
        json.dumps(
            {
                "python": sys.executable,
                "product_validation": {"native_version": "9.9.9"},
                "source_root": str(isolated_mindie_env["home"] / "missing-src"),
            }
        ),
        encoding="utf-8",
    )
    stdout = _CaptureStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    rc = mindie_launch.main(["hook", "pre-tool-use"])
    assert rc == 0
    payload = json.loads(stdout.buffer.getvalue())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "RuntimeError" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def test_pretooluse_outer_timeout_exceeds_hook_total():
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    pretool = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
    assert pretool > mindie_launch.HOOK_TOTAL
    assert pretool >= mindie_launch.HOOK_TOTAL + mindie_launch.HOOK_CLEANUP_GRACE


def test_read_stream_delta_does_not_mask_oserror_or_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import remote_dev.mcp.native_handlers_claude as claude_handlers

    log_path = tmp_path / "stdout.log"
    log_path.write_bytes(b"hello-log")
    record_path = tmp_path / "job.json"
    record_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(claude_handlers, "_job_file", lambda record, stream: log_path)

    def _tail_must_not_run(_path):
        raise AssertionError("must not fall back to tail after log OSError")

    monkeypatch.setattr(claude_handlers, "_tail_text", _tail_must_not_run)
    original_stat = Path.stat
    log_key = os.path.abspath(os.fspath(log_path))

    def timeout_stat(self, *args, **kwargs):
        if os.path.abspath(os.fspath(self)) == log_key:
            raise TimeoutError("npu log stat timed out")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", timeout_stat)
    with pytest.raises(TimeoutError, match="npu log stat timed out"):
        wb_native_compat._read_stream_delta(record_path, "stdout", 0, "", full_snapshot=False, full_preview_text="")

    def denied_stat(self, *args, **kwargs):
        if os.path.abspath(os.fspath(self)) == log_key:
            raise PermissionError(13, "Permission denied")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    with pytest.raises(OSError, match="job log unreadable") as excinfo:
        wb_native_compat._read_stream_delta(record_path, "stdout", 0, "", full_snapshot=False, full_preview_text="")
    assert str(log_path) in str(excinfo.value)
    assert "13" in str(excinfo.value)


def test_read_stream_delta_missing_file_still_uses_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import remote_dev.mcp.native_handlers_claude as claude_handlers

    log_path = tmp_path / "missing.log"
    record_path = tmp_path / "job.json"
    record_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(claude_handlers, "_job_file", lambda record, stream: log_path)
    monkeypatch.setattr(claude_handlers, "_tail_text", lambda path: ("tailed-output", 0))
    text, size, _head = wb_native_compat._read_stream_delta(
        record_path, "stdout", 0, "", full_snapshot=False, full_preview_text=""
    )
    assert text == "tailed-output"
    assert size == len(b"tailed-output")


def test_saved_offsets_unreadable_record_is_not_empty_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    record_path = tmp_path / "job.json"
    record_path.write_text("{}", encoding="utf-8")

    def timeout_read(_path):
        raise TimeoutError("record read timed out")

    monkeypatch.setattr("remote_dev.core.state_store.read_json", timeout_read)
    with pytest.raises(TimeoutError, match="record read timed out"):
        wb_native_compat._load_saved_wb_offsets(record_path)

    def denied_read(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("remote_dev.core.state_store.read_json", denied_read)
    with pytest.raises(ValueError, match="wb job record unreadable"):
        wb_native_compat._load_saved_wb_offsets(record_path)


def test_cursor_commit_does_not_invent_success_outcome():
    payload = {"result": {"task": {}, "preview": {}}, "text": "observed"}
    result = payload["result"]
    with pytest.raises(OSError, match="disk full"):
        wb_native_compat._cursor_commit_failure(payload, result, OSError("disk full"))
    assert "outcome" not in result
    assert "status" not in result

    payload = {
        "result": {"outcome": "success", "status": "ok", "summary": "Task output ready.", "task": {}, "preview": {}},
        "text": "observed",
    }
    result = payload["result"]
    out = wb_native_compat._cursor_commit_failure(payload, result, OSError("disk full"))
    assert out["result"]["execution_outcome"] == "success"
    assert out["result"]["status"] == "local_recording_failed"
    assert out["result"]["operation_completed"] is True
    assert "success" not in {out["result"].get("outcome")}


def test_background_update_spawn_failure_writes_failed_status(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict
):
    import subprocess

    cfg = isolated_mindie_env["config_path"]
    cfg.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    monkeypatch.delenv("MINDIE_CONFIG_HOME", raising=False)
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(isolated_mindie_env["data_home"]))

    def boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert component_update.trigger_background_update_if_due(config=cfg) is None
    status = json.loads((isolated_mindie_env["data_home"] / "wb-component-update-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "PermissionError" in status["error"]
    assert "13" in status["error"]


def test_session_start_spawn_failure_is_visible_not_suppress_output(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict
):
    import subprocess

    import bridge

    cfg = isolated_mindie_env["config_path"]
    cfg.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    monkeypatch.delenv("MINDIE_CONFIG_HOME", raising=False)
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(isolated_mindie_env["data_home"]))

    def boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "Popen", boom)
    out = bridge.handle_session_start({"session_id": "454c6ee9-afea-47be-8188-abb9c7fb1f26"})
    assert out.get("suppressOutput") is not True
    message = out["hookSpecificOutput"]["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "failed to start" in message
    assert "PermissionError" in message or "13" in message


def test_background_update_timeout_does_not_leave_in_progress(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict
):
    import subprocess

    cfg = isolated_mindie_env["config_path"]
    cfg.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    monkeypatch.delenv("MINDIE_CONFIG_HOME", raising=False)
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(cfg))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(isolated_mindie_env["data_home"]))

    def boom(*_args, **_kwargs):
        raise TimeoutError("spawn timed out")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(TimeoutError, match="spawn timed out"):
        component_update.trigger_background_update_if_due(config=cfg)
    status = json.loads((isolated_mindie_env["data_home"] / "wb-component-update-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "TimeoutError" in status["error"]
    assert status["status"] != "in_progress"


def test_mcp_initialize_survives_background_update_timeout(monkeypatch: pytest.MonkeyPatch):
    def boom(*_a, **_k):
        raise TimeoutError("spawn timed out")

    monkeypatch.setattr(mcp_server, "trigger_background_update_if_due", boom)
    resp = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, "tools")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "protocolVersion" in resp["result"]
    assert "error" not in resp


def test_inventory_read_failure_is_failed_not_up_to_date(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict
):
    cfg = isolated_mindie_env["config_path"]
    setup.ensure_default_profile(config=cfg, workspace_root=isolated_mindie_env["home"])
    monkeypatch.setattr(component_update, "prepare", lambda *_a, **_k: None)

    def boom(*_a, **_k):
        raise OSError("inventory unreadable")

    monkeypatch.setattr("mindie_knowledge.component_runtime.read_inventory", boom)
    res = component_update.check(config=cfg, source=REPO_ROOT)
    assert res["status"] == "failed"
    assert "inventory_error" in res
    assert "OSError" in res["inventory_error"]
    assert "installed" not in res
    assert res.get("installed") != {}


def test_inventory_timeout_clears_in_progress(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict
):
    import time

    from paths import atomic_write_json, data_home

    cfg = isolated_mindie_env["config_path"]
    setup.ensure_default_profile(config=cfg, workspace_root=isolated_mindie_env["home"])
    monkeypatch.setattr(component_update, "prepare", lambda *_a, **_k: None)
    atomic_write_json(
        data_home() / "wb-component-update-status.json",
        {"checked_at": time.time(), "status": "in_progress"},
    )

    def boom(*_a, **_k):
        raise TimeoutError("inventory timed out")

    monkeypatch.setattr("mindie_knowledge.component_runtime.read_inventory", boom)
    res = component_update.check(config=cfg, source=REPO_ROOT)
    assert res["status"] == "failed"
    assert "TimeoutError" in res["inventory_error"]
    status = json.loads((data_home() / "wb-component-update-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "TimeoutError" in status["error"]


def test_sharing_status_unreadable_configs_require_repair_without_fake_defaults(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict
):
    cfg = isolated_mindie_env["config_path"]
    comm = isolated_mindie_env["config_home"] / "mindie-community.json"
    diag = isolated_mindie_env["config_home"] / "diagnostics.json"
    comm.write_text("{not-json", encoding="utf-8")
    diag.write_text("{also-bad", encoding="utf-8")
    status = setup.sharing_status(cfg)
    assert status["community_state"] == "corrupt"
    assert status["diagnostics_state"] == "corrupt"
    assert status["repair_required"] is True
    assert status["sharing_enabled"] is False
    assert status["reporting_enabled"] is False
    assert status["repository"] is None
    assert status["needs_onboarding_prompt"] is False

    def timeout_read(_path):
        raise TimeoutError("config read timed out")

    monkeypatch.setattr(setup, "read_json", timeout_read)
    missing = isolated_mindie_env["config_home"] / "mindie-consent.json"
    missing.write_text("{}", encoding="utf-8")
    with pytest.raises(TimeoutError, match="config read timed out"):
        setup._read_optional_json(missing)


def test_reporting_activation_failure_is_visible(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict, capsys: pytest.CaptureFixture[str]
):
    from mindie_knowledge import consent_store

    cfg = isolated_mindie_env["config_path"]
    setup.ensure_default_profile(config=cfg, workspace_root=isolated_mindie_env["home"])

    def boom(*_a, **_k):
        raise OSError("reporting config unreadable")

    monkeypatch.setattr(consent_store, "activate_reporting", boom, raising=False)
    payload = setup.configure_community(config=cfg, enabled=True, fork="user/knowledge-vllm-ascend")
    assert payload["enabled"] is True
    assert payload["status"] == "partial"
    assert "reporting_error" in payload
    assert "OSError" in payload["reporting_error"]
    err = capsys.readouterr().err
    assert "reporting activation failed" in err

    monkeypatch.setattr(
        mcp_server,
        "_resolve_claim",
        lambda *_a, **_k: {"session": "s", "bind_id": None, "workspace_root": str(isolated_mindie_env["home"])},
    )
    sharing = mcp_server.call_knowledge_surface(
        "mindie_entry",
        {"op": "sharing-enable"},
        config=cfg,
    )
    assert sharing["isError"] is True
    assert sharing["structuredContent"]["status"] == "partial"
    assert "reporting_error" in sharing["structuredContent"]

    monkeypatch.setattr(
        setup,
        "configure_community",
        lambda **_k: {
            "enabled": True,
            "status": "partial",
            "reporting_error": "OSError: reporting config unreadable",
        },
    )
    rc = setup.main(["sharing", "--enable", "--config", str(cfg)])
    assert rc == 1


def test_reporting_return_failure_is_partial_not_success(
    monkeypatch: pytest.MonkeyPatch, isolated_mindie_env: dict, capsys: pytest.CaptureFixture[str]
):
    from mindie_knowledge import consent_store

    cfg = isolated_mindie_env["config_path"]
    setup.ensure_default_profile(config=cfg, workspace_root=isolated_mindie_env["home"])
    monkeypatch.setattr(
        consent_store,
        "activate_reporting",
        lambda *_a, **_k: {"status": "configuration_unavailable", "enabled": False},
        raising=False,
    )
    payload = setup.configure_community(config=cfg, enabled=True, fork="user/knowledge-vllm-ascend")
    assert payload["enabled"] is True
    assert payload["status"] == "partial"
    assert "configuration_unavailable" in payload["reporting_error"]
    assert "reporting activation failed" in capsys.readouterr().err
    monkeypatch.setattr(
        mcp_server,
        "_resolve_claim",
        lambda *_a, **_k: {"session": "s", "bind_id": None, "workspace_root": str(isolated_mindie_env["home"])},
    )
    sharing = mcp_server.call_knowledge_surface("mindie_entry", {"op": "sharing-enable"}, config=cfg)
    assert sharing["isError"] is True
    assert sharing["structuredContent"]["status"] == "partial"
    rc = setup.main(["sharing", "--enable", "--config", str(cfg)])
    assert rc == 1


def test_setup_reexec_passes_bootstrap_timeout(monkeypatch: pytest.MonkeyPatch):
    seen: dict = {}
    monkeypatch.delenv("_MINDIE_WB_SETUP_BOOTSTRAPPED", raising=False)
    monkeypatch.setattr(setup.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(setup, "apply_enterprise_transport_env", lambda target=None: {} if target is None else target)
    monkeypatch.setattr(setup, "stage_source_baseline", lambda *_a, **_k: Path("."))
    monkeypatch.setattr(setup, "ensure_runtime_python", lambda *_a, **_k: sys.executable)

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["timeout"] = kwargs.get("timeout")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    assert setup.main(["init"]) == 0
    assert seen["timeout"] == setup.BOOTSTRAP_TIMEOUT + 15.0
