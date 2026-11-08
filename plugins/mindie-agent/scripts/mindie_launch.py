"""Stable entrypoint invoked by WorkBuddy `hooks.json`, `.mcp.json`, and `mcp.json`.

[Role]
Dispatches Tencent WorkBuddy lifecycle hooks (`hook session-start`,
`hook user-prompt-expansion`, `hook user-prompt-submit`, `hook pre-tool-use`,
`hook stop`), stdio MCP servers (`mcp remote`, `mcp tools`, `mcp knowledge`),
setup commands (`setup ...`), and component updates (`update ...`) under explicit
timeout budgets.

[Invariants]
- Enforces `HOOK_TOTAL = 10.0s >= HOOK_EXEC_BUDGET (8.0s) + CLEANUP_GRACE (2.0s)`.
- Hook failures emit a host-visible error payload and exit 0. WorkBuddy fail-opens
  on a bare non-zero exit, so PreToolUse uses `permissionDecision=deny` rather than
  `return 1`. They never print `{"suppressOutput": true}` to hide the fault.
- If the configured profile interpreter differs from `sys.executable`, re-executes
  into the configured interpreter so `mindie_knowledge`, `remote_dev`,
  `mindie_diagnostics`, `mindie_profiler`, `mindie_sim`, `mindie_coordinator`,
  `mindie_debugger`, and `npu_top` imports resolve deterministically.

[Timeouts & Locks]
- `HOOK_TOTAL = 10.0s`, `HOOK_LOCK_BUDGET = 2.0s`, `BOOTSTRAP_TOTAL = 540.0s`,
  `SETUP_REEXEC_GRACE = 15.0s`. Hook re-exec uses `timeout=HOOK_TOTAL`; setup
  re-exec uses `timeout=BOOTSTRAP_TOTAL + SETUP_REEXEC_GRACE`; MCP stdio stays unbounded.

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

HOOK_EXEC_BUDGET = 8.0
HOOK_CLEANUP_GRACE = 2.0
HOOK_TOTAL = HOOK_EXEC_BUDGET + HOOK_CLEANUP_GRACE
HOOK_LOCK_BUDGET = 2.0
BOOTSTRAP_TOTAL = 540.0
SETUP_REEXEC_GRACE = 15.0
_HOOK_EVENT_NAMES = {
    "session-start": "SessionStart",
    "user-prompt-expansion": "UserPromptExpansion",
    "user-prompt-submit": "UserPromptSubmit",
    "pre-invocation": "UserPromptSubmit",
    "pre-tool-use": "PreToolUse",
    "stop": "Stop",
}


def _norm_python_path(py_exe: str | Path) -> Path:
    p = Path(py_exe).expanduser()
    return p.parent.resolve() / p.name


def _has_full_wb_components(py_exe: str | Path) -> bool:
    p = Path(py_exe)
    if not p.is_file():
        return False
    dbg_bin = p.parent / ("mindie-debugger.exe" if os.name == "nt" else "mindie-debugger")
    if dbg_bin.is_file():
        return True
    venv_root = p.parent.parent
    for sp in (venv_root / "Lib" / "site-packages", *venv_root.glob("lib/python*/site-packages")):
        if (sp / "mindie_debugger").is_dir():
            return True
    return False


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
            if _has_full_wb_components(candidate):
                return str(_norm_python_path(candidate))
    search_dirs = [cfg.parent]
    host_cfg_dir = Path.home() / ".config" / "mindie-agent"
    if host_cfg_dir not in search_dirs:
        search_dirs.append(host_cfg_dir)
    for cdir in search_dirs:
        for sibling_cfg in ("wb.json", "agy.json", "codex.json", "cc.json", "kimi.json"):
            scfg = cdir / sibling_cfg
            if scfg.is_file():
                try:
                    py = read_json(scfg).get("python")
                    if isinstance(py, str) and _has_full_wb_components(py):
                        return str(_norm_python_path(py))
                except ValueError:
                    pass
    return None


def _emit_hook_failure(action: str, exc: BaseException) -> int:
    import json

    event_name = _HOOK_EVENT_NAMES.get(action, action)
    detail = f"{type(exc).__name__}: {exc}"
    print(f"MindIE WorkBuddy hook error: {detail}", file=sys.stderr)
    hook_out: dict = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "systemMessage": f"MindIE WorkBuddy hook failed: {detail}",
        }
    }
    if action == "pre-tool-use":
        hook_out["hookSpecificOutput"]["permissionDecision"] = "deny"
        hook_out["hookSpecificOutput"]["permissionDecisionReason"] = detail
    sys.stdout.buffer.write((json.dumps(hook_out, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _reexec_stdio(stream):
    import subprocess

    try:
        if os.name == "nt":
            import msvcrt

            if msvcrt.get_osfhandle(stream.fileno()) in (-1, -2, 0):
                return subprocess.DEVNULL
        else:
            stream.fileno()
        return stream
    except Exception:
        return subprocess.DEVNULL


def main(argv: list[str] | None = None) -> int:
    apply_enterprise_transport_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("mindie_launch requires a subcommand: hook | mcp | setup | update")

    target_py = _preferred_python()
    target_script = Path(__file__).resolve()
    if argv[0] in {"hook", "mcp"} and config_path().is_file():
        # The profile activates interpreter and payload together. A newly
        # downloaded native entry must keep serving the previous generation
        # until installation has prepared and activated its replacement.
        active = read_json(config_path())
        active_python = active.get("python")
        active_scripts = active.get("runtime_scripts")
        validation = active.get("product_validation") or {}
        expected_version = validation.get("native_version")
        retained = active.get("source_root")
        directories = ([Path(retained) / "plugins/mindie-agent/scripts"]
                       if isinstance(retained, str) else [])
        if isinstance(active_scripts, str):
            directories.append(Path(active_scripts))
        if isinstance(active_python, str) and Path(active_python).is_file():
            for directory in directories:
                manifest = directory.parent / "plugin.json"
                paired_script = directory / "mindie_launch.py"
                if (manifest.is_file() and paired_script.is_file()
                        and read_json(manifest).get("version") == expected_version):
                    target_py = str(_norm_python_path(active_python))
                    target_script = paired_script.resolve()
                    break
            else:
                raise RuntimeError("active plugin payload does not match its recorded version")
    if (
        target_py is not None
        and (_norm_python_path(sys.executable) != _norm_python_path(target_py)
             or target_script != Path(__file__).resolve())
    ):
        env = dict(os.environ)
        env["_MINDIE_WB_REEXEC"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if argv[0] in {"hook", "mcp"}:
            env["WB_PLUGIN_ROOT"] = str(target_script.parent.parent)
        import subprocess

        run_kwargs: dict = {"env": env}
        if os.name == "nt":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            run_kwargs["stdin"] = _reexec_stdio(sys.stdin)
            run_kwargs["stdout"] = _reexec_stdio(sys.stdout)
            run_kwargs["stderr"] = _reexec_stdio(sys.stderr)
        if argv[0] == "hook":
            run_kwargs["timeout"] = HOOK_TOTAL
        elif argv[0] == "setup":
            run_kwargs["timeout"] = BOOTSTRAP_TOTAL + SETUP_REEXEC_GRACE
        try:
            proc = subprocess.run([target_py, "-I", str(target_script), *argv], **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            if argv[0] == "hook":
                return _emit_hook_failure(argv[1] if len(argv) > 1 else "session-start", exc)
            raise
        return int(proc.returncode)

    role = argv[0]
    rest = argv[1:]
    if role == "hook":
        import bridge

        try:
            return bridge.main(rest)
        except Exception as exc:
            return _emit_hook_failure(rest[0] if rest else "session-start", exc)
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
