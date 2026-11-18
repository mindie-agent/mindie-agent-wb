"""Stable entrypoint invoked by WorkBuddy `hooks.json`, `.mcp.json`, and `mcp.json`.

[Role]
Dispatches Tencent WorkBuddy lifecycle hooks (`hook session-start`,
`hook user-prompt-submit`, `hook pre-tool-use`, `hook stop`), stdio MCP servers
(`mcp remote`, `mcp tools`, `mcp knowledge`), setup commands (`setup ...`), and
component updates (`update ...`) under explicit timeout budgets.

[Invariants]
- Enforces `HOOK_TOTAL = 10.0s >= HOOK_EXEC_BUDGET (8.0s) + CLEANUP_GRACE (2.0s)`.
- Passive `Stop` and `UserPromptSubmit` fast-paths execute in pure stdlib before
  `_preferred_python()`, re-exec, or `bridge` import (`< 100ms`, zero SQLite created).
- Hook failures emit a host-visible error payload and exit 0 (`permissionDecision=deny`
  on `PreToolUse`), never printing `{"suppressOutput": true}` to mask faults.

[Timeouts & Locks]
- `HOOK_EXEC_BUDGET = 8.0s`, `HOOK_CLEANUP_GRACE = 2.0s`, `HOOK_TOTAL = 10.0s`,
  `HOOK_LOCK_BUDGET = 2.0s`, `BOOTSTRAP_TOTAL = 540.0s`, `SETUP_REEXEC_GRACE = 15.0s`.

[Side-Effect Policy]
- Delegates hook, MCP, setup, and update execution to `bridge.py`,
  `mcp_server.py`, `setup.py`, and `component_update.py`.
"""

from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import types

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paths import apply_enterprise_transport_env, community_config_path, config_home, config_path, data_home, read_json

HOOK_EXEC_BUDGET = 8.0
HOOK_CLEANUP_GRACE = 2.0
HOOK_TOTAL = HOOK_EXEC_BUDGET + HOOK_CLEANUP_GRACE
HOOK_LOCK_BUDGET = 2.0
BOOTSTRAP_TOTAL = 540.0
SETUP_REEXEC_GRACE = 15.0
MAX_HOOK_STDIN_BYTES = 32 * 1024 * 1024
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z")
_HOOK_EVENT_NAMES = {
    "session-start": "SessionStart",
    "user-prompt-expansion": "UserPromptExpansion",
    "user-prompt-submit": "UserPromptSubmit",
    "pre-invocation": "UserPromptSubmit",
    "pre-tool-use": "PreToolUse",
    "stop": "Stop",
}


def _parse_hook_stdin(raw: bytes) -> dict:
    if len(raw) > MAX_HOOK_STDIN_BYTES:
        raise ValueError("native hook payload exceeds 32 MiB byte budget")
    if not raw.strip():
        return {}
    event = json.loads(raw.decode("utf-8"))
    if not isinstance(event, dict):
        raise ValueError("native hook payload must be a JSON object")
    return event


def _path_within_stdlib(candidate: str, root: str) -> bool:
    try:
        cand_res = str(Path(candidate).expanduser().resolve())
        root_res = str(Path(root).expanduser().resolve())
    except TimeoutError:
        raise
    except OSError:
        return False
    if os.name == "nt":
        cand_res = os.path.normcase(cand_res)
        root_res = os.path.normcase(root_res)
    if cand_res == root_res:
        return True
    try:
        rel = os.path.relpath(cand_res, root_res)
    except ValueError:
        return False
    return rel != ".." and not rel.startswith(".." + os.sep) and not os.path.isabs(rel)


def _may_capture_stop_stdlib(event: dict) -> bool:
    """Pure-stdlib fast-path check without importing `bridge`, `identity`, or `remote_dev`."""
    if event.get("stop_hook_active") is True:
        return False
    cfg_file = config_path()
    if not cfg_file.is_file():
        return False
    raw_sid = event.get("session_id") or event.get("conversationId")
    if not isinstance(raw_sid, str) or not _SESSION_RE.fullmatch(raw_sid.strip()):
        return False
    workspace = None
    for key in ("cwd", "workspace_root", "workspaceRoot", "directory", "project_root"):
        val = event.get(key)
        if isinstance(val, str) and os.path.isabs(val):
            workspace = val
            break
    if workspace is None:
        wpaths = event.get("workspacePaths")
        if isinstance(wpaths, list) and wpaths and isinstance(wpaths[0], str) and os.path.isabs(wpaths[0]):
            workspace = wpaths[0]
    if workspace is None:
        return False

    comm_file = community_config_path(cfg_file)
    if not comm_file.is_file():
        return False
    try:
        comm = read_json(comm_file)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False
    if (
        comm.get("schema") != "mindie-community-config/1"
        or comm.get("enabled") is not True
        or not isinstance(comm.get("generation"), str)
        or not comm["generation"]
        or type(comm.get("enabled_at")) not in (int, float)
        or not math.isfinite(comm["enabled_at"])
        or comm["enabled_at"] <= 0
    ):
        return False
    consent_ref = comm.get("consent_config")
    if not isinstance(consent_ref, str) or not os.path.isabs(consent_ref):
        return False
    cfile = Path(consent_ref)
    if not cfile.is_file():
        return False
    try:
        consent = read_json(cfile)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False
    if consent.get("choice") != "contribute":
        return False
    roots = comm.get("project_roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and os.path.isabs(r) for r in roots):
        return False
    return any(_path_within_stdlib(workspace, root) for root in roots)


def _norm_python_path(py_exe: str | Path) -> Path:
    p = Path(py_exe).expanduser()
    return p.parent.resolve() / p.name


def _has_full_wb_components(py_exe: str | Path) -> bool:
    p = Path(py_exe)
    norm = str(p).replace("\\", "/")
    if "/uv/python/" in norm or not p.is_file():
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
    for extra in (config_home(), Path.home() / ".config" / "mindie-agent"):
        if extra not in search_dirs:
            search_dirs.append(extra)
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


def _parse_cli_timeout(argv: list[str]) -> float | None:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=float, default=None)
    try:
        known, _ = parser.parse_known_args(argv[1:] if argv else [])
    except (SystemExit, ValueError):
        return None
    return float(known.timeout) if known.timeout is not None and known.timeout > 0 else None


def _timeout_role_budget(role: str, argv: list[str] | None = None) -> float | None:
    if role == "hook":
        return HOOK_EXEC_BUDGET
    if role in {"setup", "update"}:
        requested = _parse_cli_timeout(argv or [])
        inner = BOOTSTRAP_TOTAL if requested is None else requested
        return inner + SETUP_REEXEC_GRACE
    return None


def _run_hook_with_exec_budget(action: str, rest: list[str]) -> int:
    """Bound in-process hook dispatch to HOOK_EXEC_BUDGET (POSIX SIGALRM)."""
    import signal
    import bridge

    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return bridge.main(rest)

    def _on_alarm(_signum, _frame):
        raise TimeoutError(f"hook exec exceeded HOOK_EXEC_BUDGET={HOOK_EXEC_BUDGET}s")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _on_alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, float(HOOK_EXEC_BUDGET))
        try:
            return int(bridge.main(rest))
        except Exception as exc:
            return _emit_hook_failure(action, exc)
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
        finally:
            signal.signal(signal.SIGALRM, previous)


def _reexec(
    target_py: str,
    target_script: Path,
    argv: list[str],
    env: dict,
    *,
    stdin_bytes: bytes | None = None,
) -> int:
    from bounded import _attach_windows_job, _close_windows_job, _kill_process_tree

    timeout = _timeout_role_budget(argv[0], argv)
    cmd = [target_py, "-I", "-X", "utf8", str(target_script), *argv]
    popen_kwargs: dict = {"env": env}
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if timeout is not None:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen_kwargs["creationflags"] = flags
        popen_kwargs["stdin"] = subprocess.PIPE if stdin_bytes is not None else _reexec_stdio(sys.stdin)
        popen_kwargs["stdout"] = _reexec_stdio(sys.stdout)
        popen_kwargs["stderr"] = _reexec_stdio(sys.stderr)
    else:
        if stdin_bytes is not None:
            popen_kwargs["stdin"] = subprocess.PIPE
        if timeout is not None:
            popen_kwargs["start_new_session"] = True

    if timeout is None:
        return int(subprocess.run(cmd, input=stdin_bytes, **popen_kwargs).returncode)

    proc = subprocess.Popen(cmd, **popen_kwargs)
    job = _attach_windows_job(proc, kill_on_close=(argv[0] != "hook"))
    try:
        proc.communicate(input=stdin_bytes, timeout=timeout)
        rc = int(proc.returncode)
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        _kill_process_tree(proc, job, wait_timeout=min(0.5, HOOK_CLEANUP_GRACE))
        job = None
        if argv[0] == "hook":
            return _emit_hook_failure(argv[1] if len(argv) > 1 else "session-start", exc)
        raise
    except BaseException:
        _kill_process_tree(proc, job, wait_timeout=min(0.5, HOOK_CLEANUP_GRACE))
        job = None
        raise
    finally:
        if job is not None:
            _close_windows_job(job)

    if argv[0] == "hook" and rc != 0:
        return _emit_hook_failure(
            argv[1] if len(argv) > 1 else "session-start",
            RuntimeError(f"hook interpreter exited {rc}"),
        )
    return rc


def main(argv: list[str] | None = None) -> int:
    apply_enterprise_transport_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("mindie_launch requires a subcommand: hook | mcp | setup | update")

    try:
        return _dispatch(argv)
    except Exception as exc:
        if argv[0] == "hook":
            return _emit_hook_failure(argv[1] if len(argv) > 1 else "session-start", exc)
        raise


def _dispatch(argv: list[str]) -> int:
    buffered_stdin: bytes | None = None
    if argv in (["hook", "user-prompt-submit"], ["hook", "user-prompt-expansion"]):
        _parse_hook_stdin(sys.stdin.buffer.read(MAX_HOOK_STDIN_BYTES + 1))
        sys.stdout.buffer.write(b'{"suppressOutput": true}\n')
        sys.stdout.buffer.flush()
        return 0

    if argv == ["hook", "stop"] and os.environ.get("_MINDIE_WB_REEXEC") != "1":
        buffered_stdin = sys.stdin.buffer.read(MAX_HOOK_STDIN_BYTES + 1)
        event = _parse_hook_stdin(buffered_stdin)
        if not _may_capture_stop_stdlib(event):
            sys.stdout.buffer.write(b'{"suppressOutput": true}\n')
            sys.stdout.buffer.flush()
            return 0

    target_py = _preferred_python()
    target_script = Path(__file__).resolve()
    if argv[0] in {"hook", "mcp"} and config_path().is_file():
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
        env["PYTHONUTF8"] = "1"
        if argv[0] in {"hook", "mcp"}:
            env["WB_PLUGIN_ROOT"] = str(target_script.parent.parent)
        if argv[0] == "hook":
            env["_MINDIE_WB_HOOK_BUDGETED"] = "1"
        return _reexec(target_py, target_script, argv, env, stdin_bytes=buffered_stdin)

    if buffered_stdin is not None:
        sys.stdin = types.SimpleNamespace(buffer=io.BytesIO(buffered_stdin))  # type: ignore[assignment]

    role = argv[0]
    rest = argv[1:]
    if role == "hook":
        import bridge

        action = rest[0] if rest else "session-start"
        budgeted = os.environ.get("_MINDIE_WB_HOOK_BUDGETED") == "1"
        if os.name == "nt" and not budgeted and os.environ.get("_MINDIE_WB_REEXEC") != "1":
            env = dict(os.environ)
            env["_MINDIE_WB_HOOK_BUDGETED"] = "1"
            env["_MINDIE_WB_REEXEC"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            return _reexec(sys.executable, Path(__file__).resolve(), argv, env, stdin_bytes=buffered_stdin)
        if os.name != "nt" and not budgeted:
            return _run_hook_with_exec_budget(action, rest)
        try:
            return bridge.main(rest)
        except Exception as exc:
            return _emit_hook_failure(action, exc)
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
