"""Tier 1 crash-gateway, provenance, and post-success Receipt Honesty (F4) tests for WorkBuddy (`mindie-agent-wb`)."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mindie_diagnostics.integration import configure_reporting, record_failure  # noqa: E402
import diagnostic_support  # noqa: E402
import mcp_server  # noqa: E402


def test_wb_diagnostics_v2_provenance_and_post_success_timeout_honesty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path.resolve()
    cfg_dir = base / "config" / "mindie-agent"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    wb_cfg = cfg_dir / "wb.json"
    wb_cfg.write_text(json.dumps({"host": "workbuddy"}), encoding="utf-8")
    diag_cfg = cfg_dir / "diagnostics.json"
    monkeypatch.setenv("HOME", str(base / "home"))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(base / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(base / "diag"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(diag_cfg))
    monkeypatch.setenv("WORKBUDDY_SESSION_ID", "wb-test-session-01")
    (base / "home").mkdir(parents=True, exist_ok=True)
    (base / "diag").mkdir(parents=True, exist_ok=True)
    configure_reporting(True, roots=[str(base / "diag")], config=diag_cfg)

    def _wrapped_perm(*_a, **_k):
        try:
            raise PermissionError("operational_denied")
        except PermissionError as err:
            raise RuntimeError("wrapper") from err

    monkeypatch.setattr(mcp_server, "_resolve_claim", lambda *_a, **_k: {"session": "wb-test-session-01", "bind_id": None})
    monkeypatch.setattr(mcp_server, "status_facts", _wrapped_perm)
    perm_res = mcp_server.call_tools_surface("mindie_status", {}, config=wb_cfg)
    assert perm_res["isError"] is True
    assert "diagnostic" not in perm_res["structuredContent"]

    tok = "5" * 32
    ev_dir = base / "diag" / "events" / "mindie-coordinator"
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "events.jsonl").write_text(
        json.dumps({"component": "mindie-coordinator", "event": "operation.end", "status": "success", "reportable": False, "operation_id": "1" * 32, "invocation_token": tok}) + "\n",
        encoding="utf-8",
    )
    assert diagnostic_support.record_component_crash(
        "coordinator", ["hosts"], 1, f"mindie-diagnostic-incident: {'1' * 32}\n", config=wb_cfg, invocation_token=tok
    ) is None
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_INVOCATION_TOKEN", tok)
    rec = record_failure("mindie-coordinator", "mindie.hosts", stage="cli_dispatch", category="internal_exception", exception=KeyError("k"))
    assert diagnostic_support.record_component_crash(
        "coordinator", ["hosts"], 1, f"mindie-diagnostic-incident: {rec['incident_id']}\n", config=wb_cfg, invocation_token=tok
    ) == {"incident_id": rec["incident_id"], "logging_failed": False}

    # F4: Post-success TimeoutError in receipt_store.finish preserves completed result!
    import remote_dev.mcp.outcome as rd_outcome
    import remote_dev.mcp.tools as rd_tools

    class _TimeoutFinishReceipts:
        def begin(self, *_a, **_k):
            return None

        def finish(self, *_a, **_k):
            raise TimeoutError("wb_finish_timeout")

    monkeypatch.setattr(rd_tools, "call_tool", lambda *_a, **_k: {"status": "succeeded", "stdout": "ok"})
    monkeypatch.setattr(rd_outcome, "operation_outcome", lambda *_a, **_k: "succeeded")
    rem_ok = mcp_server.call_remote_surface("remote_bash", {"command": "true"}, config=wb_cfg, receipts=_TimeoutFinishReceipts())
    assert rem_ok["isError"] is False
    assert rem_ok["structuredContent"]["receipt_unconfirmed"] is True
    assert "TimeoutError" in rem_ok["structuredContent"]["receipt_warning"]

    # R6-1: Post-promotion Path.stat() TimeoutError / PermissionError on eng_file or community_config_path
    import component_update
    from paths import community_config_path, engine_config_path

    comm_p = community_config_path(wb_cfg)
    eng_p = engine_config_path(wb_cfg)
    comm_p.write_text("{}", encoding="utf-8")
    eng_p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        component_update,
        "prepare",
        lambda *_a, **_k: {
            "python": "/tmp/fake-wb-py",
            "components": {"runtime": "/tmp/fake-wb-rt"},
            "installed": {"mindie-diagnostics": {"version": "0.5.11"}},
        },
    )
    orig_stat = Path.stat
    orig_atomic = component_update.atomic_write_json
    for target_p in (eng_p, comm_p):
        for exc_cls in (TimeoutError, PermissionError):
            with monkeypatch.context() as mp:
                promoted_flag = {"done": False}

                def _tracking_write(path, payload, *, _orig=orig_atomic, _flag=promoted_flag):
                    _orig(path, payload)
                    if Path(path) == wb_cfg:
                        _flag["done"] = True

                def _faulty_stat(self, *args, _t=target_p, _e=exc_cls, _flag=promoted_flag, **kwargs):
                    if _flag["done"] and self == _t:
                        raise _e("wb_post_promote_stat_fault")
                    return orig_stat(self, *args, **kwargs)

                mp.setattr(component_update, "atomic_write_json", _tracking_write)
                mp.setattr(Path, "stat", _faulty_stat)
                res_r6 = component_update.check(config=wb_cfg, sync_feeds=True)
                assert res_r6["status"] == "updated"
                assert res_r6["receipt_unconfirmed"] is True
                assert exc_cls.__name__ in res_r6["receipt_warning"]
                saved_status = json.loads((base / "data" / "wb-component-update-status.json").read_text(encoding="utf-8"))
                assert saved_status["status"] == "updated"
                assert saved_status["receipt_unconfirmed"] is True

    # R7-2: Non-promoted read_inventory() TimeoutError must record status="failed" in status_file AND re-raise TimeoutError.
    with monkeypatch.context() as mp:
        mp.setattr(component_update, "prepare", lambda *_a, **_k: None)
        mp.setattr(
            "mindie_knowledge.component_runtime.read_inventory",
            lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("wb_non_promoted_inv_timeout")),
        )
        with pytest.raises(TimeoutError, match="wb_non_promoted_inv_timeout"):
            component_update.check(config=wb_cfg)
        failed_st = json.loads((base / "data" / "wb-component-update-status.json").read_text(encoding="utf-8"))
        assert failed_st["status"] == "failed"
        assert "TimeoutError" in failed_st["error"]
