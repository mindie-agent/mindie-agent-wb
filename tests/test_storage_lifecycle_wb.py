"""Tier 0/1 regression coverage for WorkBuddy storage lifecycle governance."""

from __future__ import annotations

import contextlib
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import component_update
import paths
import setup
import storage_lifecycle


REPO_ROOT = Path(__file__).resolve().parents[1]


def _touch_old(path: Path, seconds: float = 7200.0) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _make_generation(root: Path, name: str, *, components: bool = True, age: float = 7200.0) -> Path:
    generation = root / name
    (generation / ".venv" / "bin").mkdir(parents=True)
    (generation / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    if components:
        manifest = generation / "components.json"
        manifest.write_text('{"schema":"test"}\n', encoding="utf-8")
        _touch_old(manifest, age)
    _touch_old(generation, age)
    return generation


def _make_cache_version(wb_root: Path, version: str, python_exe: Path, *, age: float = 7200.0) -> Path:
    directory = wb_root / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent" / version
    (directory / "hooks").mkdir(parents=True)
    (directory / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'"{python_exe}" -I -X utf8 "launch.py" hook session-start',
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (directory / "mcp.json").write_text(
        json.dumps({"mcpServers": {"mindie-tools": {"command": str(python_exe), "args": ["-I"]}}}),
        encoding="utf-8",
    )
    (directory / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"mindie-tools": {"command": str(python_exe), "args": ["-I", "-X", "utf8"]}}}),
        encoding="utf-8",
    )
    _touch_old(directory, age)
    return directory


def test_silent_reclamation_of_failed_runtimes_excess_sources_and_old_caches():
    data = paths.data_home()
    state = data / "wb-state"
    runtimes = state / "component-runtimes"
    active = _make_generation(runtimes, "active", age=9000.0)
    for index in range(1, 5):
        _make_generation(runtimes, f"verified-{index}", age=7000.0 + index)
    broken = _make_generation(runtimes, "failed-build", components=False, age=10800.0)

    sources = data / "wb-source"
    for index, name in enumerate(("0.1.0-old", "0.1.1-middle", "0.1.2-new")):
        source = sources / name
        source.mkdir(parents=True)
        (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        _touch_old(source, 9000.0 + (2 - index) * 60.0)

    wb_root = paths.workbuddy_config_dir()
    old_python = Path("/opt/mindie/old/bin/python")
    new_python = active / ".venv" / "bin" / "python"
    for version in ("0.1.1", "0.1.2", "0.1.3", "0.1.4"):
        _make_cache_version(wb_root, version, old_python, age=8000.0 - float(version.rsplit(".", 1)[1]))

    report = storage_lifecycle.post_update_maintenance(
        paths.config_path(), new_python, active_version="0.1.4", budget_seconds=15.0,
    )

    assert report["status"] == "completed", report
    assert not broken.exists()
    remaining = [entry for entry in runtimes.iterdir() if entry.is_dir() and not entry.name.startswith(".trash-")]
    assert len(remaining) == 3, [entry.name for entry in remaining]
    assert active in remaining

    source_names = {entry.name for entry in sources.iterdir() if entry.is_dir()}
    assert "0.1.0-old" not in source_names
    assert {"0.1.1-middle", "0.1.2-new"} <= source_names

    cache_versions = {
        entry.name
        for entry in (wb_root / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent").iterdir()
        if entry.is_dir()
    }
    assert cache_versions == {"0.1.3", "0.1.4"}, cache_versions
    rollback_mcp = json.loads(
        (wb_root / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent" / "0.1.3" / "mcp.json").read_text(
            encoding="utf-8"
        )
    )
    assert rollback_mcp["mcpServers"]["mindie-tools"]["command"] == str(new_python)

    server_source = (REPO_ROOT / "plugins" / "mindie-agent" / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    assert len(server_source.splitlines()) < 800


def test_installed_cache_interpreters_refresh_and_unregistered_state_stays_untouched():
    wb_root = paths.workbuddy_config_dir()
    old_python = Path("/opt/mindie/old/bin/python")
    new_python = Path("/opt/mindie/new/bin/python")
    _make_cache_version(wb_root, "0.1.2", old_python)
    old_version = _make_cache_version(wb_root, "0.1.3", old_python)
    current_version = _make_cache_version(wb_root, "0.1.4", old_python)

    other = wb_root / "plugins" / "cache" / "another-marketplace" / "another-plugin" / "1.0.0"
    other.mkdir(parents=True)
    other_mcp = other / "mcp.json"
    other_mcp.write_text(
        json.dumps({"mcpServers": {"other": {"command": str(old_python)}}}), encoding="utf-8"
    )
    account = wb_root / "plugin-account-state" / "user.json"
    account.parent.mkdir(parents=True)
    account.write_text(
        json.dumps({"plugins": {"mindie-agent@workbuddy-connector-plugins-official": {"enabled": True}}}),
        encoding="utf-8",
    )
    account_before = account.read_bytes()

    refresh = storage_lifecycle.refresh_workbuddy_cache_python(new_python, wb_dir=wb_root)
    assert refresh["warnings"] == []
    assert len(refresh["updated"]) == 9
    for directory in (old_version, current_version):
        hooks = json.loads((directory / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert command.startswith(f'"{new_python}" -I -X utf8')
        assert json.loads((directory / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["mindie-tools"][
            "command"
        ] == str(new_python)
        assert json.loads((directory / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["mindie-tools"][
            "command"
        ] == str(new_python)

    assert json.loads(other_mcp.read_text(encoding="utf-8"))["mcpServers"]["other"]["command"] == str(old_python)
    assert account.read_bytes() == account_before

    pruned = storage_lifecycle.prune_workbuddy_plugin_caches(
        wb_dir=wb_root, active_version="0.1.4", budget_seconds=10.0,
    )
    assert pruned["status"] == "completed", pruned
    plugin_cache = wb_root / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent"
    assert {entry.name for entry in plugin_cache.iterdir() if entry.is_dir()} == {"0.1.3", "0.1.4"}
    assert json.loads((plugin_cache / "0.1.3" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "mindie-tools"
    ]["command"] == str(new_python)


def _spawn_lease_child(generation: Path, marker: Path) -> subprocess.Popen:
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from mindie_knowledge.storage import acquire_runtime_lease\n"
        "lease = acquire_runtime_lease(Path(sys.argv[1]))\n"
        "Path(sys.argv[2]).write_text('held', encoding='utf-8')\n"
        "time.sleep(120)\n"
        "lease.release()\n"
    )
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(generation), str(marker)],
        **kwargs,
    )


def test_borrowed_interpreter_source_pins_git_checkout_and_live_lease_survive():
    from mindie_knowledge.storage import prune_component_runtimes, sweep_legacy_installations

    data = paths.data_home()
    config = paths.config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    runtimes = data / "wb-state" / "component-runtimes"

    active = _make_generation(runtimes, "active", age=9000.0)
    borrowed = _make_generation(runtimes, "borrowed-codex", age=9100.0)
    pinned = _make_generation(runtimes, "package-source", age=9200.0)
    live = _make_generation(runtimes, "live-lease", age=9300.0)
    disposable = _make_generation(runtimes, "disposable", age=10800.0)

    external = config.parent.parent / "external-git-checkout"
    (external / ".git").mkdir(parents=True)
    (external / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (external / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    config.write_text(
        json.dumps({"source_root": str(external), "package_dir": str(pinned)}), encoding="utf-8"
    )
    (config.parent / "codex.json").write_text(
        json.dumps({"python": str(borrowed / ".venv" / "bin" / "python")}), encoding="utf-8"
    )

    marker = config.parent / "lease-ready"
    child = _spawn_lease_child(live, marker)
    try:
        deadline = time.monotonic() + 10.0
        while not marker.is_file() and time.monotonic() < deadline:
            if child.poll() is not None:
                pytest.fail(f"lease child exited early: {child.stderr.read().decode('utf-8', 'replace')}")
            time.sleep(0.05)
        assert marker.is_file(), "lease child did not acquire its runtime lease"

        result = prune_component_runtimes(
            runtimes,
            keep_paths=[str(active / ".venv" / "bin" / "python")],
            keep_recent_verified=0,
            budget_seconds=15.0,
            live_paths=set(),
            inspection_complete=True,
        )
        assert result["status"] == "completed", result
        assert active.is_dir()
        assert borrowed.is_dir()
        assert pinned.is_dir()
        assert live.is_dir()
        assert not disposable.exists()

        sources = data / "wb-source"
        for index, name in enumerate(("0.1.0-git", "0.1.1-mid", "0.1.2-mid", "0.1.3-new")):
            source = sources / name
            source.mkdir(parents=True)
            (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            _touch_old(source, 9000.0 + (3 - index) * 120.0)
        (sources / "0.1.0-git" / ".git").mkdir()
        (sources / "0.1.0-git" / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        sweep = sweep_legacy_installations(
            config_home=config.parent,
            data_home=data,
            budget_seconds=15.0,
            force=True,
        )
        assert sweep["status"] in {"completed", "partial"}, sweep
        assert external.is_dir()
        assert pinned.is_dir()
        assert borrowed.is_dir()
        assert live.is_dir()
        assert (sources / "0.1.0-git").is_dir()
        assert not (sources / "0.1.1-mid").exists()
    finally:
        child.terminate()
        try:
            child.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10.0)


def _fake_artifact(root: Path, fake_python: Path) -> dict:
    from mindie_knowledge.component_runtime import source_baseline

    baseline = source_baseline(REPO_ROOT)
    return {
        "generation": str(root / "gen-1"),
        "source": str(REPO_ROOT),
        "python": str(fake_python),
        "components": {
            "schema": "mindie-component-runtime/1",
            "adapter_source_sha256": baseline["source_sha256"],
            "runtime": {
                "mindie-knowledge": {
                    "repository": "mindie-agent/knowledge",
                    "version": "0.13.99",
                    "revision": "a" * 40,
                }
            },
            "lock_sha256": "b" * 64,
        },
        "installed": {
            "mindie-knowledge": {
                "repository": "mindie-agent/knowledge",
                "version": "0.13.99",
                "revision": "a" * 40,
            }
        },
    }


def test_post_switch_gc_deferral_is_cleanup_warning_not_an_unconfirmed_receipt(tmp_path, monkeypatch):
    config = tmp_path / "config" / "wb.json"
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(tmp_path / "data"))
    setup.ensure_default_profile(config=config, workspace_root=tmp_path)
    fake_python = tmp_path / "new-venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(component_update, "prepare", lambda *args, **kwargs: _fake_artifact(tmp_path, fake_python))
    monkeypatch.setattr(
        component_update,
        "post_update_maintenance",
        lambda *args, **kwargs: {
            "schema": "mindie-wb-storage-maintenance/1",
            "status": "partial",
            "warnings": ["cache cleanup: deferred in use"],
        },
    )
    result = component_update.check(config=config, source=REPO_ROOT)

    # Principle 14: the primary profile/state receipts were written, so an
    # ordinary post-switch GC deferral must never claim a lost receipt.
    assert result["status"] == "updated"
    assert "receipt_unconfirmed" not in result
    assert "receipt_warning" not in result
    assert "deferred in use" in result["cleanup_warning"]
    status = json.loads((tmp_path / "data" / "wb-component-update-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "updated"
    assert "receipt_unconfirmed" not in status
    assert "deferred in use" in status["cleanup_warning"]


def test_custom_config_sweep_preserves_runtime_borrowed_by_default_codex_profile():
    """Astra R1 P1 #1: a narrowed `--config` must not hide sibling borrow pins."""
    data = paths.data_home()
    default_home = paths.config_home()
    custom_dir = default_home / "profiles" / "team"
    custom_dir.mkdir(parents=True, exist_ok=True)
    custom_config = custom_dir / "wb.json"

    runtimes = data / "wb-state" / "component-runtimes"
    borrowed = _make_generation(runtimes, "borrowed-codex", age=40000.0)
    pinned = _make_generation(runtimes, "custom-pinned", age=42000.0)
    disposable = _make_generation(runtimes, "disposable", age=50000.0)
    _make_generation(runtimes, "verified-1", age=9000.0)
    _make_generation(runtimes, "verified-2", age=8500.0)

    custom_config.write_text(json.dumps({"package_dir": str(pinned)}), encoding="utf-8")
    (default_home / "codex.json").write_text(
        json.dumps({"python": str(borrowed / ".venv" / "bin" / "python")}), encoding="utf-8"
    )
    installed = data / "installed-venv" / "bin"
    installed.mkdir(parents=True)
    new_python = installed / "python"
    new_python.write_text("", encoding="utf-8")

    report = storage_lifecycle.post_update_maintenance(
        custom_config, new_python, active_version="0.1.14", budget_seconds=20.0,
    )

    assert report["status"] == "completed", report
    # The borrowed generation survives both phases, including the legacy sweep
    # that used to read only `custom_dir`.
    assert borrowed.is_dir(), report["legacy_sweep"]
    assert pinned.is_dir(), report["legacy_sweep"]
    assert not disposable.exists()
    # Both the sibling `codex.json` borrow pin and the caller's own profile pin
    # were visible to the sweep.
    assert report["legacy_sweep"]["protected_roots"] >= 4


def test_cache_prune_cleans_trash_and_keeps_formal_rollback_version():
    """Astra R1 P1 #2: `.trash-*` and non-semver names never consume the slot."""
    wb_root = paths.workbuddy_config_dir()
    old_python = Path("/opt/mindie/old/bin/python")
    _make_cache_version(wb_root, "0.1.14", old_python)
    _make_cache_version(wb_root, "0.1.13", old_python)
    _make_cache_version(wb_root, ".trash-0.1.12-deadbeef", old_python)
    _make_cache_version(wb_root, "scratch-unranked", old_python)

    report = storage_lifecycle.prune_workbuddy_plugin_caches(
        wb_dir=wb_root, active_version="0.1.14", budget_seconds=10.0,
    )

    plugin_cache = wb_root / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent"
    remaining = {entry.name for entry in plugin_cache.iterdir()}
    assert ".trash-0.1.12-deadbeef" not in remaining, remaining
    assert remaining == {"0.1.13", "0.1.14", "scratch-unranked"}, remaining
    assert report["reclaimed_trash"] >= 1
    assert str(plugin_cache / "0.1.13") in report["kept"], report


def test_cache_refresh_never_writes_through_a_linked_hooks_directory(tmp_path):
    """Astra R1 P1 #3: intermediate links must never reach an external checkout."""
    wb_root = paths.workbuddy_config_dir()
    old_python = Path("/opt/mindie/old/bin/python")
    new_python = Path("/opt/mindie/new/bin/python")
    external = tmp_path / "external-hooks-checkout"
    (external / ".git").mkdir(parents=True)
    (external / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    external_hooks = external / "hooks.json"
    external_hooks.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f'"{old_python}" -I hook'}]}]}},
            indent=2,
        ),
        encoding="utf-8",
    )
    before = external_hooks.read_bytes()

    cache = wb_root / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent" / "0.1.14"
    cache.mkdir(parents=True)
    (cache / "mcp.json").write_text(
        json.dumps({"mcpServers": {"mindie-tools": {"command": str(old_python)}}}), encoding="utf-8"
    )
    try:
        (cache / "hooks").symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create directory symlinks")

    refresh = storage_lifecycle.refresh_workbuddy_cache_python(new_python, wb_dir=wb_root)

    assert external_hooks.read_bytes() == before
    assert any("skipped" in warning for warning in refresh["warnings"]), refresh
    assert str(cache / "mcp.json") in refresh["updated"], refresh
    assert json.loads((cache / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "mindie-tools"
    ]["command"] == str(new_python)


def test_component_prune_refuses_a_linked_runtimes_root():
    data = paths.data_home()
    state = data / "wb-state"
    real = state / "real-runtimes"
    _make_generation(real, "obsolete", age=50000.0)
    runtimes = state / "component-runtimes"
    state.mkdir(parents=True, exist_ok=True)
    try:
        runtimes.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create directory symlinks")

    report = storage_lifecycle.post_update_maintenance(active_version="0.1.14", budget_seconds=15.0)

    assert report["component_prune"]["status"] == "protected_root", report
    assert (real / "obsolete").is_dir()


def test_update_failures_never_classify_dns_or_local_prerequisites_as_internal():
    import diagnostic_support

    class LockTimeout(Exception):
        pass

    def categorised(exc, category):
        exc.category = category
        return exc

    transport = [
        categorised(RuntimeError("dns lookup failed"), "resolver"),
        TimeoutError("dns lookup failed"),
    ]
    prerequisites = [
        categorised(RuntimeError("another updater holds the lock"), "busy"),
        categorised(RuntimeError("retry after the builder exits"), "deferred"),
        categorised(RuntimeError("missing toolchain"), "prerequisite"),
        LockTimeout("update lock held"),
        PermissionError("read-only install root"),
        OSError(errno.ENOSPC, "no space left on device"),
    ]
    for exc in transport:
        category, reportable, code = diagnostic_support.classify_update_failure(exc)
        assert (category, reportable) == ("connectivity", False), (exc, category, code)
        assert diagnostic_support.is_internal_mcp_exception(exc) is False
    for exc in prerequisites:
        category, reportable, code = diagnostic_support.classify_update_failure(exc)
        assert (category, reportable) == ("prerequisite", False), (exc, category, code)
        assert diagnostic_support.is_internal_mcp_exception(exc) is False
    # A genuine product fault still classifies as a reportable interior bug.
    assert diagnostic_support.classify_update_failure(RuntimeError("broken helper")) == (
        "internal", True, "update_internal_failure",
    )

    recorded = {}
    import mindie_diagnostics.integration as integration

    def spy(component, operation, **kwargs):
        recorded.update(kwargs)
        return {"recorded": True}

    original = integration.record_failure
    integration.record_failure = spy
    try:
        diagnostic_support.failure(
            "component.update", "prepare", "update", exception=categorised(RuntimeError("dns"), "resolver")
        )
    finally:
        integration.record_failure = original
    assert recorded["category"] == "connectivity"
    assert recorded["reportable"] is False


def test_component_update_lock_contention_is_busy_not_a_fault(monkeypatch):
    @contextlib.contextmanager
    def occupied(*args, **kwargs):
        yield False

    called = []
    monkeypatch.setattr(component_update, "component_update_lock", occupied)
    monkeypatch.setattr(component_update, "prepare", lambda *args, **kwargs: called.append(True))
    result = component_update.check()
    assert result["status"] == "busy"
    assert result["busy"] is True
    assert called == []


def test_component_check_refreshes_pinned_external_cache_after_switch(tmp_path, monkeypatch):
    config = tmp_path / "config" / "wb.json"
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(tmp_path / "workbuddy"))
    setup.ensure_default_profile(config=config, workspace_root=tmp_path)
    old_python = tmp_path / "old-venv" / "bin" / "python"
    new_python = tmp_path / "new-venv" / "bin" / "python"
    for executable in (old_python, new_python):
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
    cache = (
        tmp_path / "workbuddy" / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent" / "0.1.13"
    )
    (cache / "hooks").mkdir(parents=True)
    (cache / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": f'"{old_python}" -I "launch.py" hook session-start'}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (cache / "mcp.json").write_text(
        json.dumps({"mcpServers": {"mindie-tools": {"command": str(old_python), "args": ["-I"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(component_update, "prepare", lambda *args, **kwargs: _fake_artifact(tmp_path, new_python))

    result = component_update.check(config=config, source=REPO_ROOT)

    assert result["status"] == "updated"
    assert result["python"] == str(new_python)
    assert result["storage_maintenance"]["status"] == "completed", result["storage_maintenance"]
    assert json.loads((cache / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["mindie-tools"]["command"] == str(
        new_python
    )
    assert json.loads((cache / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0][
        "hooks"
    ][0]["command"].startswith(f'"{new_python}" -I')
