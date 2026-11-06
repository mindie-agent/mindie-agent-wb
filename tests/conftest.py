"""Pytest isolation fixtures for `mindie-agent-wb` (Tier 0, Tier 1, Tier 2 & Tier 3)."""

from __future__ import annotations

from pathlib import Path
import sys
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

CODE_ROOT = Path(__file__).resolve().parents[2]
for sibling in ("knowledge", "remote-dev", "diagnostics", "coordinator", "profiler", "sim", "debugger", "npu-top"):
    cand = CODE_ROOT / sibling
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

from wb_native_compat import ensure_wb_native_support

ensure_wb_native_support()
py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
for sibling_venv in (
    CODE_ROOT / "knowledge" / ".venv" / "lib",
    CODE_ROOT / "remote-dev" / ".venv" / "lib",
):
    if sibling_venv.is_dir():
        for sp in sorted(sibling_venv.glob(f"{py_ver}/site-packages")):
            if str(sp) not in sys.path:
                sys.path.append(str(sp))


def pytest_addoption(parser):
    parser.addoption("--live-host", action="store_true", default=False,
                     help="Run explicit Tier 3 host and network acceptance in isolated profiles")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live-host"):
        return
    skip = pytest.mark.skip(reason="Tier 3 acceptance requires --live-host")
    for item in items:
        if item.get_closest_marker("live_host"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_mindie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Strictly isolate HOME and all MindIE and WorkBuddy/CodeBuddy paths per test."""
    import os
    import shutil
    import tempfile

    base_dir = Path(tempfile.mkdtemp(prefix="mwb-")) if os.name == "nt" else tmp_path
    home_dir = base_dir / "home"
    cfg_home = base_dir / "config"
    data_home = base_dir / "data"
    wb_home = base_dir / "workbuddy"
    cb_home = base_dir / "codebuddy"
    home_dir.mkdir(parents=True, exist_ok=True)
    cfg_home.mkdir(parents=True, exist_ok=True)
    data_home.mkdir(parents=True, exist_ok=True)
    wb_home.mkdir(parents=True, exist_ok=True)
    cb_home.mkdir(parents=True, exist_ok=True)
    gitleaks_dir = home_dir / ".cache" / "mindie-agent" / "gitleaks"
    gitleaks_dir.mkdir(parents=True, exist_ok=True)
    gitleaks_stub = gitleaks_dir / ("gitleaks.exe" if sys.platform == "win32" else "gitleaks")
    gitleaks_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    try:
        gitleaks_stub.chmod(0o755)
    except OSError:
        pass

    monkeypatch.setenv("MINDIE_WB_PYTHON", os.environ.get("MINDIE_WB_TEST_PYTHON", sys.executable))
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(data_home))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(cfg_home / "wb.json"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(cfg_home / "diagnostics.json"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(data_home / "diagnostics"))
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(wb_home))
    monkeypatch.setenv("CODEBUDDY_CONFIG_DIR", str(cb_home))
    monkeypatch.setenv("_MINDIE_WB_REEXEC", "1")
    monkeypatch.delenv("WB_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("CODEBUDDY_SESSION_ID", raising=False)
    try:
        yield dict(
            home=home_dir,
            config_home=cfg_home,
            data_home=data_home,
            wb_home=wb_home,
            cb_home=cb_home,
            config_path=cfg_home / "wb.json",
        )
    finally:
        if os.name == "nt" and base_dir != tmp_path:
            shutil.rmtree(base_dir, ignore_errors=True)
