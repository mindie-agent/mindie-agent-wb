"""Clean-architecture contract tests for `mindie-agent-wb` (Group G7)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import threading
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
SESSION_PARENT = "454c6ee9-afea-47be-8188-abb9c7fb1f26"
SESSION_SUBAGENT = "ec33ebf9-0cba-4100-8142-c61503f6c587"


def test_ad11_deferred_knowledge_mcp_and_passive_unactivated_tools_list(tmp_path: Path):
    import admission
    import mcp_server
    import paths

    dot_mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert dot_mcp["mcpServers"]["mindie-remote-dev"]["alwaysLoad"] is True
    assert dot_mcp["mcpServers"]["mindie-tools"]["alwaysLoad"] is True
    assert dot_mcp["mcpServers"]["mindie-knowledge"]["alwaysLoad"] is False
    assert dot_mcp["mcpServers"]["mindie-knowledge"]["defer_loading"] is True

    # Before activation: tools/list on knowledge exposes ONLY mindie_entry and never creates admission.sqlite3
    adm_db = paths.admission_path()
    assert not adm_db.exists()
    unactivated = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "knowledge")
    names_before = {t["name"] for t in unactivated["result"]["tools"]}
    assert names_before == {"mindie_entry"}
    assert not adm_db.exists()

    # Activate via mindie_entry(op="activate") -> tools/list now exposes all 6 tools
    ws = tmp_path / "ws"
    ws.mkdir()
    act_resp = mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "mindie_entry",
                "arguments": {"op": "activate", "project_root": str(ws)},
                "_meta": {"workbuddy.ai/conversationId": SESSION_PARENT},
            },
        },
        "knowledge",
    )
    assert act_resp["result"]["isError"] is False
    activated = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "knowledge")
    names_after = {t["name"] for t in activated["result"]["tools"]}
    assert "knowledge_query" in names_after and len(names_after) == 6

    # Deactivate -> returns to mindie_entry only
    admission.deactivate(SESSION_PARENT)
    deactivated = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, "knowledge")
    assert {t["name"] for t in deactivated["result"]["tools"]} == {"mindie_entry"}


def test_ad02_shared_remote_dev_state_dir_across_parent_and_subagent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import hashlib
    import host_adapter
    import mcp_server
    import paths
    from remote_dev.core import state_store

    monkeypatch.delenv("REMOTE_DEV_STATE_DIR", raising=False)
    monkeypatch.delenv("REMOTE_DEV_SESSION_ID", raising=False)
    observed_dirs: list[tuple[str | None, str, str]] = []

    def fake_call_tool(name: str, args: dict, profile: str = "wb"):
        observed_dirs.append(
            (
                host_adapter.current_remote_session(),
                str(state_store.state_root()),
                state_store.resolve_ledger_scope(),
            )
        )
        return {"outcome": "success", "status": "ok", "summary": "ok", "job_id": "job-parent-01"}

    monkeypatch.setattr("remote_dev.mcp.tools.call_tool", fake_call_tool)
    mcp_server.call_remote_surface(
        "remote_job_status",
        {"job_id": "job-parent-01"},
        meta={"workbuddy.ai/conversationId": SESSION_PARENT},
        rpc_id="parent-1",
    )
    mcp_server.call_remote_surface(
        "remote_job_status",
        {"job_id": "job-parent-01"},
        meta={"workbuddy.ai/conversationId": SESSION_SUBAGENT},
        rpc_id="sub-1",
    )
    assert "REMOTE_DEV_SESSION_ID" not in os.environ
    assert "REMOTE_DEV_STATE_DIR" not in os.environ
    assert len(observed_dirs) == 2
    expected_shared = str(paths.state_dir() / "remote")
    assert observed_dirs[0] == (
        SESSION_PARENT,
        expected_shared,
        "id-" + hashlib.sha256(SESSION_PARENT.encode("utf-8")).hexdigest(),
    )
    assert observed_dirs[1] == (
        SESSION_SUBAGENT,
        expected_shared,
        "id-" + hashlib.sha256(SESSION_SUBAGENT.encode("utf-8")).hexdigest(),
    )


def test_passive_stop_fastpath_and_seed_bundle_integrity(tmp_path: Path):
    import host_adapter
    import wb_install

    lock = host_adapter.verify_seed_bundle(PLUGIN_ROOT)
    assert lock["schema"] == "mindie-harness-seed/1"
    orig_pyz = (PLUGIN_ROOT / "assets" / "mindie-harness-seed.pyz").read_bytes()

    copied = tmp_path / "copied-plugin"
    wb_install._copy_plugin_tree(PLUGIN_ROOT, copied)
    assert (copied / "assets" / "mindie-harness-seed.pyz").read_bytes() == orig_pyz
    assert host_adapter.verify_seed_bundle(copied) == lock

    launch_script = SCRIPTS_DIR / "mindie_launch.py"
    payload = json.dumps({"session_id": SESSION_PARENT})
    subprocess.run([sys.executable, "-I", str(launch_script), "hook", "stop"], input=payload, text=True, capture_output=True, check=True)
    samples_ms: list[float] = []
    res = None
    for _ in range(5):
        t0 = time.perf_counter()
        res = subprocess.run([sys.executable, "-I", str(launch_script), "hook", "stop"], input=payload, text=True, capture_output=True, check=True)
        samples_ms.append((time.perf_counter() - t0) * 1000.0)
    assert res is not None and json.loads(res.stdout) == {"suppressOutput": True}
    assert min(samples_ms) < 100.0


def test_host_adapter_concurrent_mcp_and_harness_rsi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mindie_harness.api import HostAdapter
    import experiment_ledger
    import host_adapter
    import mcp_server
    import rsi_loop

    adapter = host_adapter.WBHostAdapter()
    assert isinstance(adapter, HostAdapter)
    profile = adapter.resolve_profile()
    assert profile.host == "wb"
    assert [t["name"] for t in adapter.mcp_tools("knowledge", profile)] == ["mindie_entry"]
    assert rsi_loop.__name__.startswith("mindie_harness.rsi")
    assert experiment_ledger.__name__.startswith("mindie_harness.rsi")

    started = threading.Event()
    release = threading.Event()
    orig_tools = mcp_server.call_tools_surface

    def slow_or_fast(name: str, arguments=None, **kwargs):
        if name == "profiler":
            started.set()
            release.wait(timeout=3.0)
            return {"content": [{"type": "text", "text": "done"}], "structuredContent": {"ok": True}, "isError": False}
        return orig_tools(name, arguments, **kwargs)

    monkeypatch.setattr(mcp_server, "call_tools_surface", slow_or_fast)
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    srv = threading.Thread(
        target=lambda: mcp_server.serve("tools", stdin=os.fdopen(r_in, "rb", buffering=0), stdout=os.fdopen(w_out, "wb", buffering=0)),
        daemon=True,
    )
    srv.start()
    writer = os.fdopen(w_in, "wb", buffering=0)
    reader = os.fdopen(r_out, "rb")
    try:
        host_adapter.write_wire_frame(writer, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, "content-length")
        init_msg, framing, _ = host_adapter.read_wire_frame(reader)
        assert framing == "content-length" and init_msg["result"]["capabilities"]["tools"]["listChanged"] is True

        host_adapter.write_wire_frame(
            writer,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "profiler", "arguments": {"argv": ["--version"]}}},
            "jsonl",
        )
        assert started.wait(timeout=2.0)
        host_adapter.write_wire_frame(
            writer,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "mindie_status", "arguments": {}}},
            "jsonl",
        )
        fast_msg, _, _ = host_adapter.read_wire_frame(reader)
        assert fast_msg["id"] == 3 and fast_msg["result"]["isError"] is False
        release.set()
        done_msg, _, _ = host_adapter.read_wire_frame(reader)
        assert done_msg["id"] == 2
    finally:
        release.set()
        writer.close()
        srv.join(timeout=3.0)
        reader.close()


def test_ad07_symlinks_ad10_skill_hygiene_and_v0_1_14_upgrade(tmp_path: Path):
    import admission
    import diagnostic_support
    import paths
    import setup

    real_dir = tmp_path / "real-pkg"
    real_dir.mkdir()
    (real_dir / "METADATA").write_text("Metadata-Version: 2.1\nName: mindie-sim\nVersion: 0.4.0\n", encoding="utf-8")
    dist_info = tmp_path / "sp" / "mindie_sim-0.4.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").symlink_to(real_dir / "METADATA")
    assert diagnostic_support._inspect_dist_info(tmp_path / "sp", "mindie_sim")[0] == "0.4.0"

    skill_text = (PLUGIN_ROOT / "skills" / "mindie-agent" / "SKILL.md").read_text(encoding="utf-8")
    domain_text = (PLUGIN_ROOT / "skills" / "mindie-agent" / "references" / "domain-skills.md").read_text(encoding="utf-8")
    for forbidden in ("(out_tensor, metrics_dict)", "mindie-bench-ablation/1", "experiment_ledger.py", "PROGRESS.md"):
        assert forbidden not in skill_text
        assert forbidden not in domain_text

    old_tree = tmp_path / "wb-v0.1.14"
    old_tree.mkdir()
    archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", "--format=tar", "v0.1.14"], capture_output=True, timeout=10, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tf:
        tf.extractall(old_tree, filter="data")
    staged_old = setup.stage_source_baseline(old_tree)
    assert json.loads((staged_old / "plugins" / "mindie-agent" / "plugin.json").read_text(encoding="utf-8"))["version"] == "0.1.14"

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = paths.config_path()
    admission.activate(SESSION_PARENT, ws, config=cfg)
    setup.configure_community(enabled=False, config=cfg)
    consent_before = paths.consent_path(cfg).read_bytes()
    upgraded = setup.ensure_default_profile(config=cfg, workspace_root=ws)
    assert upgraded["product_validation"]["native_version"] == json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
    assert paths.consent_path(cfg).read_bytes() == consent_before
    assert admission.knowledge_lease(SESSION_PARENT, config=cfg) is not None


def test_idle_mcp_emits_tools_list_changed_on_external_hook_activation(tmp_path: Path):
    import admission
    import host_adapter
    import mcp_server

    ws = tmp_path / "ws-idle"
    ws.mkdir()
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    srv = threading.Thread(
        target=lambda: mcp_server.serve(
            "knowledge",
            stdin=os.fdopen(r_in, "rb", buffering=0),
            stdout=os.fdopen(w_out, "wb", buffering=0),
        ),
        daemon=True,
    )
    srv.start()
    writer = os.fdopen(w_in, "wb", buffering=0)
    reader = os.fdopen(r_out, "rb")
    try:
        host_adapter.write_wire_frame(writer, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, "jsonl")
        init_msg, _, eof = host_adapter.read_wire_frame(reader)
        assert not eof and init_msg["id"] == 1

        host_adapter.write_wire_frame(writer, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "jsonl")
        list_before, _, _ = host_adapter.read_wire_frame(reader)
        assert {t["name"] for t in list_before["result"]["tools"]} == {"mindie_entry"}

        # Simulate external UserPromptSubmit hook activating /mindie-agent while MCP is completely idle
        admission.activate(SESSION_PARENT, ws)

        received: list[dict] = []
        got_notification = threading.Event()

        def _read_one():
            msg, _, _ = host_adapter.read_wire_frame(reader)
            received.append(msg)
            got_notification.set()

        rd = threading.Thread(target=_read_one, daemon=True)
        rd.start()
        assert got_notification.wait(timeout=1.5), "Idle MCP server did not push notifications/tools/list_changed"
        assert received[0].get("method") == "notifications/tools/list_changed"

        host_adapter.write_wire_frame(writer, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, "jsonl")
        list_after, _, _ = host_adapter.read_wire_frame(reader)
        assert len(list_after["result"]["tools"]) == 6
    finally:
        writer.close()
        srv.join(timeout=3.0)
        reader.close()


def test_concurrent_remote_sessions_isolate_read_ledger_without_env_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import hashlib
    import mcp_server
    from remote_dev.core import state_store

    monkeypatch.delenv("REMOTE_DEV_SESSION_ID", raising=False)
    barrier = threading.Barrier(2, timeout=3.0)
    scopes: dict[str, tuple[str, str]] = {}

    def interleaved_tool(name: str, args: dict, profile: str = "wb"):
        before = state_store.resolve_ledger_scope()
        barrier.wait()
        after = state_store.resolve_ledger_scope()
        scopes[args["tag"]] = (before, after)
        return {"outcome": "success", "status": "ok", "summary": "ok"}

    monkeypatch.setattr("remote_dev.mcp.tools.call_tool", interleaved_tool)
    receipts = mcp_server.RemoteReceipts(tmp_path / "receipts.sqlite3")

    t1 = threading.Thread(
        target=lambda: mcp_server.call_remote_surface(
            "remote_probe", {"host": "127.0.0.1", "tag": "parent"}, meta={"workbuddy.ai/conversationId": SESSION_PARENT}, rpc_id="p1", receipts=receipts
        )
    )
    t2 = threading.Thread(
        target=lambda: mcp_server.call_remote_surface(
            "remote_probe", {"host": "127.0.0.1", "tag": "sub"}, meta={"workbuddy.ai/conversationId": SESSION_SUBAGENT}, rpc_id="s1", receipts=receipts
        )
    )
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert "REMOTE_DEV_SESSION_ID" not in os.environ
    exp_p = "id-" + hashlib.sha256(SESSION_PARENT.encode("utf-8")).hexdigest()
    exp_s = "id-" + hashlib.sha256(SESSION_SUBAGENT.encode("utf-8")).hexdigest()
    assert scopes["parent"] == (exp_p, exp_p)
    assert scopes["sub"] == (exp_s, exp_s)
