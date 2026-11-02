"""Pytest isolation fixtures for `mindie-agent-wb` (Tier 0, Tier 1, Tier 2 & Tier 3)."""

from __future__ import annotations

from pathlib import Path
import sys
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

CODE_ROOT = Path(__file__).resolve().parents[2]
for sibling in ("remote-dev", "diagnostics", "coordinator", "profiler", "sim", "debugger", "npu-top"):
    cand = CODE_ROOT / sibling
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.append(str(cand))
py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
for sibling_venv in (
    CODE_ROOT / "knowledge" / ".venv" / "lib",
    CODE_ROOT / "remote-dev" / ".venv" / "lib",
):
    if sibling_venv.is_dir():
        for sp in sorted(sibling_venv.glob(f"{py_ver}/site-packages")):
            if str(sp) not in sys.path:
                sys.path.append(str(sp))


@pytest.fixture(autouse=True)
def isolated_mindie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Strictly isolate all MindIE and WorkBuddy/CodeBuddy paths per test."""
    cfg_home = tmp_path / "config"
    data_home = tmp_path / "data"
    wb_home = tmp_path / "workbuddy"
    cb_home = tmp_path / "codebuddy"
    cfg_home.mkdir(parents=True, exist_ok=True)
    data_home.mkdir(parents=True, exist_ok=True)
    wb_home.mkdir(parents=True, exist_ok=True)
    cb_home.mkdir(parents=True, exist_ok=True)

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
    return dict(
        config_home=cfg_home,
        data_home=data_home,
        wb_home=wb_home,
        cb_home=cb_home,
        config_path=cfg_home / "wb.json",
    )
