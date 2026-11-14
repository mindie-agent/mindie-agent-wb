"""Pytest isolation fixtures for `mindie-agent-wb` (Tier 0, Tier 1, Tier 2 & Tier 3)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import pytest

REAL_HOME = Path.home()
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
for sibling_name in ("knowledge", "remote-dev"):
    sibling_dir = CODE_ROOT / sibling_name / ".venv"
    for sp in (
        sibling_dir / "Lib" / "site-packages",
        *(sorted((sibling_dir / "lib").glob(f"{py_ver}/site-packages")) if (sibling_dir / "lib").is_dir() else ()),
    ):
        if sp.is_dir() and str(sp) not in sys.path:
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


def _force_writable(func, target_path, _exc):
    try:
        os.chmod(target_path, stat.S_IWRITE | stat.S_IREAD)
        func(target_path)
    except TimeoutError:
        raise
    except OSError:
        pass


@pytest.fixture(autouse=True)
def isolated_mindie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Strictly isolate HOME, AppData, XDG, and all MindIE and WorkBuddy/CodeBuddy paths per test."""
    base_dir = Path(tempfile.mkdtemp(prefix="mwb-")) if os.name == "nt" else tmp_path
    home_dir = base_dir / "home"
    cfg_home = base_dir / "config"
    data_home = base_dir / "data"
    state_home = base_dir / "state"
    cache_home = base_dir / "cache"
    appdata = base_dir / "appdata" / "Roaming"
    localappdata = base_dir / "appdata" / "Local"
    wb_home = base_dir / "workbuddy"
    cb_home = base_dir / "codebuddy"
    for folder in (home_dir, cfg_home, data_home, state_home, cache_home, appdata, localappdata, wb_home, cb_home):
        folder.mkdir(parents=True, exist_ok=True)

    gitleaks_dir = home_dir / ".cache" / "mindie-agent" / "gitleaks"
    real_gitleaks = REAL_HOME / ".cache" / "mindie-agent" / "gitleaks"
    if real_gitleaks.is_dir():
        shutil.copytree(real_gitleaks, gitleaks_dir, dirs_exist_ok=True)
    else:
        gitleaks_dir.mkdir(parents=True, exist_ok=True)
        gitleaks_stub = gitleaks_dir / ("gitleaks.exe" if sys.platform == "win32" else "gitleaks")
        gitleaks_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        try:
            gitleaks_stub.chmod(0o755)
        except TimeoutError:
            raise
        except OSError:
            pass

    monkeypatch.setenv("MINDIE_WB_PYTHON", os.environ.get("MINDIE_WB_TEST_PYTHON", sys.executable))
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(data_home))
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(data_home / "remote"))
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(data_home / "remote-auth"))
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
            raw = str(base_dir.resolve())
            rm_target = raw if raw.startswith("\\\\?\\") else f"\\\\?\\{raw}"
            shutil.rmtree(rm_target, onexc=_force_writable)
