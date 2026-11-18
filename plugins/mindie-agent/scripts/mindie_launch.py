"""Stdlib entrypoint for WorkBuddy hooks, MCP servers, setup, and updates.
[Role]
Dispatches `hook`, `mcp`, `setup`, `update`, `doctor`, and `sharing` roles and
verifies the cold seed bundle.
[Invariants]
- Pure stdlib top-level imports; passive hooks run before third-party imports.
- Passive prompt/tool/session-start/unconfigured `Stop` hooks never import
  SQLite or create `admission.sqlite3`; `PreToolUse` never auto-approves and
  denies forged `_session_id` / `mindie_activation` keys.
[Timeouts & Locks]
- Passive hooks are bounded by `<= 1 MiB` stdin reads and a `< 5ms` atomic
  pending-update marker; cold bootstrap delegates to seed zipapp budgets.
[Side-Effect Policy]
- Passive `session-start` writes `pending-update-check.json`; active roles
  delegate every other effect to their target module or the cold seed runtime.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
HERE, PLUGIN_ROOT = Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
MAX_STDIN_BYTES = 1024 * 1024
FORGED_KEYS = frozenset({"_session_id", "mindie_activation"})
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z")
def _home(kind: str) -> Path:
    explicit, home = os.environ.get(f"MINDIE_{kind.upper()}_HOME"), Path(os.environ.get("USERPROFILE") or Path.home())
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA" if kind == "config" else "LOCALAPPDATA") or home / "AppData" / ("Roaming" if kind == "config" else "Local")) / "mindie-agent"
    return Path(os.environ.get("XDG_CONFIG_HOME" if kind == "config" else "XDG_DATA_HOME") or home / (".config" if kind == "config" else ".local/share")) / "mindie-agent"
def _config_path() -> Path:
    return Path(os.environ.get("MINDIE_AGENT_CONFIG") or _home("config") / "wb.json").expanduser()
def _read_json(path: Path) -> dict:
    try:
        raw = path.read_bytes() if path.is_file() else b""
        value = json.loads(raw.decode("utf-8-sig")) if 0 < len(raw) <= MAX_STDIN_BYTES else None
        return value if isinstance(value, dict) else {}
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return {}
def _event(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8-sig")) if 0 < len(raw) <= MAX_STDIN_BYTES else None
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}
def _emit(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0
def verify_seed_bundle(plugin_dir: str | Path | None = None) -> dict:
    """Verify `assets/mindie-harness-seed.pyz` against `assets/seed-lock.json` in 64 KiB chunks."""
    root = Path(plugin_dir or PLUGIN_ROOT).expanduser().resolve()
    pyz, lock = root / "assets" / "mindie-harness-seed.pyz", _read_json(root / "assets" / "seed-lock.json")
    expected, size_expected = str(lock.get("sha256") or "").lower(), lock.get("size_bytes")
    if lock.get("schema") != "mindie-harness-seed/1" or len(expected) != 64 or type(size_expected) is not int or size_expected <= 0 or not pyz.is_file():
        raise ValueError(f"Missing or malformed seed bundle at {pyz}")
    digest, size = hashlib.sha256(), 0
    with pyz.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
            size += len(chunk)
    if size != size_expected or digest.hexdigest() != expected:
        raise ValueError(f"Seed integrity mismatch for {pyz}")
    return lock
def _forged(value) -> bool:
    stack = [value]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if FORGED_KEYS & set(cur):
                return True
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return False
def _path_within(candidate: str, root: str) -> bool:
    try:
        cand, base = os.path.normcase(os.path.realpath(candidate)), os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([cand, base]) == base
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False
def _workspace(event: dict) -> str | None:
    for key in ("cwd", "workspace", "workspace_root", "workspaceRoot", "directory", "project_root"):
        if isinstance(event.get(key), str) and os.path.isabs(event[key]):
            return event[key]
    paths = event.get("workspacePaths")
    return paths[0] if isinstance(paths, list) and paths and isinstance(paths[0], str) and os.path.isabs(paths[0]) else None
def _may_capture_stop(event: dict) -> bool:
    """Pure-stdlib consent, community, and project-root gate for `Stop`."""
    workspace, session = _workspace(event), event.get("session_id") or event.get("conversationId")
    if event.get("stop_hook_active") is True or workspace is None or not isinstance(session, str) or not SESSION_RE.fullmatch(session.strip()):
        return False
    cfg_file, cfg = _config_path(), _read_json(_config_path())
    comm_path = Path(cfg["community_config"]) if isinstance(cfg.get("community_config"), str) and os.path.isabs(cfg["community_config"]) else cfg_file.parent / "mindie-community.json"
    community = _read_json(comm_path)
    consent_ref = community.get("consent_config") or cfg.get("consent_config")
    consent = _read_json(Path(consent_ref) if isinstance(consent_ref, str) and os.path.isabs(consent_ref) else cfg_file.parent / "mindie-consent.json")
    roots = community.get("project_roots")
    return (consent.get("choice") in {"contribute", "local-only"} and community.get("enabled") is True and isinstance(roots, list)
            and bool(roots) and all(isinstance(root, str) and os.path.isabs(root) for root in roots) and any(_path_within(workspace, root) for root in roots))
def _write_pending() -> None:
    state = _home("data") / "state" / "wb"
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, raw_tmp = tempfile.mkstemp(prefix=".pending.tmp.", dir=str(state))
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps({"schema": "mindie-pending-update/1", "host": "wb", "reason": "session_start", "requested_at": time.time()}, sort_keys=True).encode() + b"\n")
            os.replace(tmp, state / "pending-update-check.json")
        finally:
            tmp.unlink(missing_ok=True)
    except TimeoutError:
        raise
    except OSError:
        pass
def _norm(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.parent.resolve() / path.name
def _active_python() -> str | None:
    import importlib.util
    python = _read_json(_config_path()).get("python")
    path = Path(python).expanduser() if isinstance(python, str) else None
    if path is not None and path.is_file():
        norm = _norm(path)
        if norm != _norm(sys.executable) or importlib.util.find_spec("mindie_harness") is not None:
            return str(norm)
    try:
        return str(_norm(sys.executable)) if importlib.util.find_spec("mindie_harness") is not None else None
    except (ImportError, ValueError):
        return None
def _stdio() -> dict:
    try: sys.stdin.fileno(); sys.stdout.fileno(); sys.stderr.fileno(); return {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
    except Exception: return {}
def _seed_handoff(argv: list[str], raw: bytes) -> int:
    verify_seed_bundle(PLUGIN_ROOT)
    repo = PLUGIN_ROOT.parents[1]
    command = [sys.executable, "-I", "-X", "utf8", str(PLUGIN_ROOT / "assets" / "mindie-harness-seed.pyz"),
               "--host", "wb", "--plugin-root", str(repo if (repo / "pyproject.toml").is_file() else PLUGIN_ROOT), "--config", str(_config_path()), *argv]
    if argv and argv[0] == "hook":
        return int(subprocess.run(command, input=raw, env=dict(os.environ, PYTHONUTF8="1"), creationflags=CREATE_NO_WINDOW).returncode)
    return int(subprocess.run(command, **_stdio(), env=dict(os.environ, PYTHONUTF8="1"), creationflags=CREATE_NO_WINDOW).returncode)
def _reexec(python: str, argv: list[str], raw: bytes) -> int:
    command = [python, "-I", "-X", "utf8", str(Path(__file__).resolve()), *argv]
    env = dict(os.environ, WB_PLUGIN_ROOT=str(PLUGIN_ROOT), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if argv and argv[0] == "hook":
        return int(subprocess.run(command, input=raw, env=env, creationflags=CREATE_NO_WINDOW).returncode)
    return int(subprocess.run(command, **_stdio(), env=env, creationflags=CREATE_NO_WINDOW).returncode)
def _fast_hook(action: str, raw: bytes) -> int | None:
    if action == "pre-tool-use" and len(raw) > MAX_STDIN_BYTES:
        return _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "hook payload exceeds 1 MiB"}})
    event = _event(raw)
    if action in {"user-prompt-submit", "user-prompt-expansion", "pre-invocation"}:
        return _emit({"suppressOutput": True})
    if action == "pre-tool-use":
        return _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "forged session identity keys are not accepted"}} if _forged(event) else {"suppressOutput": True})
    if action == "session-start":
        _write_pending()
        return _emit({"suppressOutput": True})
    return None if action != "stop" or _may_capture_stop(event) else _emit({"suppressOutput": True})
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("mindie_launch requires a role: hook | mcp | setup | update | doctor | sharing")
    role, raw = argv[0], (sys.stdin.buffer.read(MAX_STDIN_BYTES + 1) if argv[0] == "hook" else b"")
    if role == "hook" and len(argv) > 1 and (fast := _fast_hook(argv[1], raw)) is not None:
        return fast
    readonly = role in {"setup", "update", "doctor", "sharing"} and any(t in {"-h", "--help"} or (t.startswith("--dry") and "--dry-run".startswith(t)) for t in argv)
    python = _active_python()
    if python is None and not readonly:
        return _seed_handoff(argv, raw)
    if python is not None and _norm(python) != _norm(sys.executable):
        return _reexec(python, argv, raw)
    if role == "hook":
        import bridge
        return int(bridge.main(argv[1:], raw=raw))
    if role == "mcp":
        import mcp_server
        return int(mcp_server.main(argv[1:]))
    if role in {"setup", "update", "doctor", "sharing"}:
        import setup
        return int(setup.main(argv[1:] if role == "setup" else [role, *argv[1:]]))
    raise SystemExit(f"unknown mindie_launch role: {role}")
if __name__ == "__main__":
    raise SystemExit(main())
