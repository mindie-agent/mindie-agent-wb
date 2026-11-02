"""Stable entrypoint invoked by WorkBuddy `hooks.json`, `.mcp.json`, and `mcp.json`.

[Role]
Dispatches Tencent WorkBuddy lifecycle hooks (`hook session-start`,
`hook user-prompt-expansion`, `hook user-prompt-submit`, `hook pre-tool-use`,
`hook stop`), stdio MCP servers (`mcp remote`, `mcp tools`, `mcp knowledge`),
setup commands (`setup ...`), and component updates (`update ...`) under explicit
timeout budgets.

[Invariants]
- Enforces `HOOK_TOTAL = 10.0s >= HOOK_EXEC_BUDGET (8.0s) + CLEANUP_GRACE (2.0s)`.
- If the configured profile interpreter differs from `sys.executable`, re-executes
  into the configured interpreter so `mindie_knowledge`, `remote_dev`,
  `mindie_diagnostics`, `mindie_profiler`, `mindie_sim`, `mindie_coordinator`,
  `mindie_debugger`, and `npu_top` imports resolve deterministically.

[Timeouts & Locks]
- `HOOK_TOTAL = 10.0s`, `HOOK_LOCK_BUDGET = 2.0s`, `BOOTSTRAP_TOTAL = 540.0s`.

[Side-Effect Policy]
- Delegates hook, MCP, setup, and update execution to `bridge.py`,
  `mcp_server.py`, `setup.py`, and `component_update.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paths import apply_enterprise_transport_env, config_path, data_home, read_json

HOOK_TOTAL = 10.0
HOOK_LOCK_BUDGET = 2.0
BOOTSTRAP_TOTAL = 540.0


def _norm_python_path(py_exe: str | Path) -> Path:
    p = Path(py_exe).expanduser()
    return p.parent.resolve() / p.name


def _preferred_python() -> str | None:
    cfg = config_path()
    if cfg.is_file():
        try:
            py = read_json(cfg).get("python")
            if isinstance(py, str) and Path(py).is_file():
                return str(_norm_python_path(py))
        except ValueError:
            pass
    env_py = (os.environ.get("MINDIE_WB_PYTHON") or "").strip()
    if env_py and Path(env_py).is_file():
        return str(_norm_python_path(env_py))
    for venv_name in ("wb-venv", "agy-venv"):
        vdir = data_home() / venv_name
        for candidate in (vdir / "Scripts" / "python.exe", vdir / "bin" / "python"):
            if candidate.is_file():
                return str(_norm_python_path(candidate))
    search_dirs = [cfg.parent]
    host_cfg_dir = Path.home() / ".config" / "mindie-agent"
    if host_cfg_dir not in search_dirs:
        search_dirs.append(host_cfg_dir)
    for cdir in search_dirs:
        for sibling_cfg in ("wb.json", "codex.json", "agy.json", "cc.json", "kimi.json"):
            scfg = cdir / sibling_cfg
            if scfg.is_file():
                try:
                    py = read_json(scfg).get("python")
                    if isinstance(py, str) and ".venv" in py and Path(py).is_file():
                        return str(_norm_python_path(py))
                except ValueError:
                    pass
    return None


def main(argv: list[str] | None = None) -> int:
    apply_enterprise_transport_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("mindie_launch requires a subcommand: hook | mcp | setup | update")

    target_py = _preferred_python()
    if (
        target_py is not None
        and _norm_python_path(sys.executable) != _norm_python_path(target_py)
        and os.environ.get("_MINDIE_WB_REEXEC") != "1"
    ):
        env = dict(os.environ)
        env["_MINDIE_WB_REEXEC"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        import subprocess

        proc = subprocess.run([target_py, "-I", str(Path(__file__).resolve()), *argv], env=env)
        return int(proc.returncode)

    role = argv[0]
    rest = argv[1:]
    if role == "hook":
        import bridge

        try:
            return bridge.main(rest)
        except Exception as exc:
            print(f"MindIE WorkBuddy hook warning: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.stdout.buffer.write(b'{"suppressOutput":true}\n')
            sys.stdout.buffer.flush()
            return 0
    if role == "mcp":
        import mcp_server

        return mcp_server.main(rest)
    if role == "setup":
        import setup

        return setup.main(rest)
    if role == "update":
        import component_update

        return component_update.main(rest)
    raise SystemExit(f"unknown mindie_launch role: {role}")


if __name__ == "__main__":
    raise SystemExit(main())
