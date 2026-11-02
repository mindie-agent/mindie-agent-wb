"""Pytest isolation fixtures for `mindie-agent-wb` (Tier 0, Tier 1, Tier 2 & Tier 3)."""

from __future__ import annotations

from pathlib import Path
import sys
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from wb_native_compat import ensure_wb_native_support

ensure_wb_native_support()

CODE_ROOT = Path(__file__).resolve().parents[2]
for sibling in ("knowledge", "remote-dev", "diagnostics", "coordinator", "profiler", "sim", "debugger", "npu-top"):
    cand = CODE_ROOT / sibling
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))
py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
for sibling_venv in (
    CODE_ROOT / "knowledge" / ".venv" / "lib",
    CODE_ROOT / "remote-dev" / ".venv" / "lib",
):
    if sibling_venv.is_dir():
        for sp in sorted(sibling_venv.glob(f"{py_ver}/site-packages")):
            if str(sp) not in sys.path:
                sys.path.append(str(sp))


def _find_host_runtime_python() -> str | None:
    import json

    host_cfg_dir = Path.home() / ".config" / "mindie-agent"
    for name in ("wb.json", "codex.json", "agy.json", "cc.json", "kimi.json"):
        cand = host_cfg_dir / name
        if cand.is_file():
            try:
                py = json.loads(cand.read_text(encoding="utf-8")).get("python")
                if isinstance(py, str) and Path(py).is_file():
                    return py
            except Exception:
                pass
    return None


_HOST_RUNTIME_PYTHON = _find_host_runtime_python()


@pytest.fixture(autouse=True)
def isolated_mindie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Strictly isolate HOME and all MindIE and WorkBuddy/CodeBuddy paths per test."""
    home_dir = tmp_path / "home"
    cfg_home = tmp_path / "config"
    data_home = tmp_path / "data"
    wb_home = tmp_path / "workbuddy"
    cb_home = tmp_path / "codebuddy"
    home_dir.mkdir(parents=True, exist_ok=True)
    cfg_home.mkdir(parents=True, exist_ok=True)
    data_home.mkdir(parents=True, exist_ok=True)
    wb_home.mkdir(parents=True, exist_ok=True)
    cb_home.mkdir(parents=True, exist_ok=True)

    if _HOST_RUNTIME_PYTHON:
        monkeypatch.setenv("MINDIE_WB_PYTHON", _HOST_RUNTIME_PYTHON)
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
    return dict(
        home=home_dir,
        config_home=cfg_home,
        data_home=data_home,
        wb_home=wb_home,
        cb_home=cb_home,
        config_path=cfg_home / "wb.json",
    )
