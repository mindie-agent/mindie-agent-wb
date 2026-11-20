"""Stdlib entrypoint for WorkBuddy hooks, MCP servers, setup, and updates.
[Role] Dispatches `hook`, `mcp`, `setup`, `update`, `doctor`, and `sharing` roles,
verifies the cold seed bundle, and re-enters native WorkBuddy scripts after seed provisioning.
[Invariants] Pure stdlib top-level imports; `session-start` and prompt hooks return BEFORE
reading `stdin`; passive hooks never create SQLite; `WindowsApps` 0-byte Python stubs are
rejected; cold seed requires `setup.py init` (`>= 9` domains) before entering resident MCP.
[Timeouts & Locks] Passive hooks `< 5ms`; cold seed bounded by `300s` + `60s` init check.
[Side-Effect Policy] Passive `session-start` writes `pending-update-check.json`; active roles
provision via seed when needed and re-exec into the owning WorkBuddy script.
"""
from __future__ import annotations
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
HERE, PLUGIN_ROOT = Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))
MAX_STDIN_BYTES, SEED_TIMEOUT_SEC, INIT_TIMEOUT_SEC = 1024 * 1024, 300.0, 60.0
FORGED_KEYS = frozenset({"_session_id", "mindie_activation"})
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z")
def _home(kind: str) -> Path:
    explicit, home = os.environ.get(f"MINDIE_{kind.upper()}_HOME"), Path(os.environ.get("USERPROFILE") or Path.home())
    if explicit: return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA" if kind == "config" else "LOCALAPPDATA") or home / "AppData" / ("Roaming" if kind == "config" else "Local")) / "mindie-agent"
    return Path(os.environ.get("XDG_CONFIG_HOME" if kind == "config" else "XDG_DATA_HOME") or home / (".config" if kind == "config" else ".local/share")) / "mindie-agent"
def _config_path(config: str | Path | None = None) -> Path:
    return Path(config).expanduser() if config else Path(os.environ.get("MINDIE_AGENT_CONFIG") or _home("config") / "wb.json").expanduser()
def _read_json(path: Path) -> dict:
    try:
        raw = path.read_bytes() if path.is_file() else b""
        value = json.loads(raw.decode("utf-8-sig")) if 0 < len(raw) <= MAX_STDIN_BYTES else None
        return value if isinstance(value, dict) else {}
    except TimeoutError: raise
    except (OSError, ValueError): return {}
def _event(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8-sig")) if 0 < len(raw) <= MAX_STDIN_BYTES else None
        return value if isinstance(value, dict) else {}
    except ValueError: return {}
def _emit(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0
def _usable_python(path: Path) -> bool:
    if "\\microsoft\\windowsapps\\" in str(path).replace("/", "\\").lower(): return False
    try:
        st = path.stat()
        return path.is_file() and (os.name != "nt" or st.st_size > 0)
    except TimeoutError: raise
    except OSError: return False
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
            digest.update(chunk); size += len(chunk)
    if size != size_expected or digest.hexdigest() != expected:
        raise ValueError(f"Seed integrity mismatch for {pyz}")
    return lock
def _forged(value) -> bool:
    stack = [value]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if FORGED_KEYS & set(cur): return True
            stack.extend(cur.values())
        elif isinstance(cur, list): stack.extend(cur)
    return False
def _path_within(candidate: str, root: str) -> bool:
    try:
        cand, base = os.path.normcase(os.path.realpath(candidate)), os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([cand, base]) == base
    except TimeoutError: raise
    except (OSError, ValueError): return False
def _workspace(event: dict) -> str | None:
    for key in ("cwd", "workspace", "workspace_root", "workspaceRoot", "directory", "project_root"):
        if isinstance(event.get(key), str) and os.path.isabs(event[key]): return event[key]
    paths = event.get("workspacePaths")
    return paths[0] if isinstance(paths, list) and paths and isinstance(paths[0], str) and os.path.isabs(paths[0]) else None
def _may_capture_stop(event: dict, config: str | Path | None = None) -> bool:
    """Pure-stdlib consent, community, and project-root gate for `Stop`."""
    workspace, session = _workspace(event), event.get("session_id") or event.get("conversationId")
    if event.get("stop_hook_active") is True or workspace is None or not isinstance(session, str) or not SESSION_RE.fullmatch(session.strip()):
        return False
    cfg_file = _config_path(config)
    cfg = _read_json(cfg_file)
    comm_path = Path(cfg["community_config"]) if isinstance(cfg.get("community_config"), str) and os.path.isabs(cfg["community_config"]) else cfg_file.parent / "mindie-community.json"
    community = _read_json(comm_path)
    consent_ref = community.get("consent_config") or cfg.get("consent_config")
    consent = _read_json(Path(consent_ref) if isinstance(consent_ref, str) and os.path.isabs(consent_ref) else cfg_file.parent / "mindie-consent.json")
    roots = community.get("project_roots")
    return (community.get("schema") == "mindie-community-config/1" and community.get("enabled") is True
            and consent.get("schema") == "mindie-consent/1" and consent.get("choice") in {"contribute", "local-only"}
            and isinstance(consent.get("choice_revision"), str) and bool(consent["choice_revision"].strip())
            and isinstance(roots, list) and bool(roots) and all(isinstance(root, str) and os.path.isabs(root) for root in roots)
            and any(_path_within(workspace, root) for root in roots))
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
    except TimeoutError: raise
    except OSError: pass
def _norm(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.parent.resolve() / path.name
def _has_harness() -> bool:
    import importlib.util
    try: return importlib.util.find_spec("mindie_harness") is not None
    except (ImportError, ValueError): return False
def _discovered_runtime_pythons() -> list[Path]:
    runtimes = _home("data") / "runtimes" / "wb"
    if not runtimes.is_dir(): return []
    scored: list[tuple[float, Path]] = []
    for receipt in runtimes.rglob("runtime.json"):
        if ".trash" in receipt.parts: continue
        data = _read_json(receipt)
        validation, py_raw = data.get("validation"), data.get("python")
        if isinstance(validation, dict) and validation.get("status") == "validated" and isinstance(py_raw, str):
            py_path = Path(py_raw).expanduser()
            if _usable_python(py_path):
                stamp = validation.get("validated_at")
                scored.append((float(stamp) if type(stamp) in (int, float) else 0.0, py_path))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in scored]
def _active_python() -> str | None:
    cfg, candidates = _read_json(_config_path()), []
    rel = cfg.get("component_release")
    if isinstance(rel, str) and rel:
        receipt = _read_json(Path(rel).expanduser() / "runtime.json")
        if (receipt.get("validation") or {}).get("status") == "validated" and isinstance(receipt.get("python"), str):
            candidates.append(Path(receipt["python"]).expanduser())
    if isinstance(cfg.get("python"), str): candidates.append(Path(cfg["python"]).expanduser())
    candidates.extend(_discovered_runtime_pythons())
    for path in candidates:
        if _usable_python(path):
            norm = _norm(path)
            if norm != _norm(sys.executable) or _has_harness(): return str(norm)
    return str(_norm(sys.executable)) if _usable_python(Path(sys.executable)) and _has_harness() else None
def _seed_bootstrap_python() -> str:
    if sys.executable and _usable_python(Path(sys.executable)): return sys.executable
    uv_bin = shutil.which("uv")
    if uv_bin:
        res = subprocess.run([uv_bin, "python", "find", ">=3.11"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10.0, creationflags=CREATE_NO_WINDOW)
        if res.returncode == 0 and (lines := (res.stdout or "").strip().splitlines()) and _usable_python(Path(lines[-1].strip())):
            return lines[-1].strip()
    raise RuntimeError("No usable Python >=3.11 interpreter found for cold seed bootstrap")
def _has_initialized_domains(cfg_file: Path) -> bool:
    doc = _read_json(cfg_file)
    dc, ec = doc.get("domain_configs"), doc.get("engine_config")
    return isinstance(dc, dict) and len(dc) >= 9 and isinstance(ec, str) and Path(ec).is_file()
def _stdio() -> dict:
    try: sys.stdin.fileno(); sys.stdout.fileno(); sys.stderr.fileno(); return {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
    except TimeoutError: raise
    except Exception: return {}
def _reexec(python: str, argv: list[str], raw: bytes) -> int:
    command = [python, "-I", "-X", "utf8", str(Path(__file__).resolve()), *argv]
    env = dict(os.environ, WB_PLUGIN_ROOT=str(PLUGIN_ROOT), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if argv and argv[0] == "hook":
        return int(subprocess.run(command, input=raw, env=env, creationflags=CREATE_NO_WINDOW).returncode)
    return int(subprocess.run(command, **_stdio(), env=env, creationflags=CREATE_NO_WINDOW).returncode)
def _seed_handoff(argv: list[str], raw: bytes) -> int:
    verify_seed_bundle(PLUGIN_ROOT)
    repo, cfg_file = PLUGIN_ROOT.parents[1], _config_path()
    env = dict(os.environ, WB_PLUGIN_ROOT=str(PLUGIN_ROOT), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    seed_cmd = [_seed_bootstrap_python(), "-I", "-X", "utf8", str(PLUGIN_ROOT / "assets" / "mindie-harness-seed.pyz"),
                "--host", "wb", "--plugin-root", str(repo if (repo / "pyproject.toml").is_file() else PLUGIN_ROOT), "--config", str(cfg_file)]
    proc = subprocess.run(seed_cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=SEED_TIMEOUT_SEC, env=env, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        if proc.stderr: sys.stderr.buffer.write(proc.stderr); sys.stderr.buffer.flush()
        return int(proc.returncode)
    python = _active_python()
    if python is None and isinstance(seeded := _event(proc.stdout or b"").get("python"), str) and _usable_python(Path(seeded).expanduser()):
        python = str(_norm(seeded))
    if python is None:
        sys.stderr.write("MindIE cold seed completed without a validated runtime interpreter.\n")
        return 1
    if argv and argv[0] in {"mcp", "hook"} and not _has_initialized_domains(cfg_file):
        init_cmd = [python, "-I", "-X", "utf8", str(HERE / "setup.py"), "init", "--config", str(cfg_file)]
        init_proc = subprocess.run(init_cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=INIT_TIMEOUT_SEC, env=env, creationflags=CREATE_NO_WINDOW)
        if init_proc.returncode != 0 or not _has_initialized_domains(cfg_file):
            if init_proc.stderr: sys.stderr.buffer.write(init_proc.stderr)
            else: sys.stderr.buffer.write(b"MindIE setup.py init failed to provision 9-domain knowledge bindings.\n")
            sys.stderr.buffer.flush()
            return int(init_proc.returncode or 1)
    return _reexec(python, argv, raw)
def _fast_hook(action: str, raw: bytes) -> int | None:
    if action == "pre-tool-use" and len(raw) > MAX_STDIN_BYTES:
        return _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "hook payload exceeds 1 MiB"}})
    event = _event(raw)
    if action == "pre-tool-use":
        return _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "forged session identity keys are not accepted"}} if _forged(event) else {"suppressOutput": True})
    return None if action != "stop" or _may_capture_stop(event) else _emit({"suppressOutput": True})
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv: raise SystemExit("mindie_launch requires a role: hook | mcp | setup | update | doctor | sharing")
    role = argv[0]
    action = argv[1].lower().replace("_", "-") if role == "hook" and len(argv) > 1 else ""
    if role == "hook" and action == "session-start": _write_pending(); return _emit({"suppressOutput": True})
    if role == "hook" and action in {"user-prompt-submit", "user-prompt-expansion", "pre-invocation"}: return _emit({"suppressOutput": True})
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1) if role == "hook" else b""
    if role == "hook" and action and (fast := _fast_hook(action, raw)) is not None: return fast
    readonly = role in {"setup", "update", "doctor", "sharing"} and any(t in {"-h", "--help"} or (t.startswith("--dry") and "--dry-run".startswith(t)) for t in argv)
    python = _active_python()
    if python is None and not readonly: return _seed_handoff(argv, raw)
    if python is not None and _norm(python) != _norm(sys.executable): return _reexec(python, argv, raw)
    if role == "hook":
        import bridge; return int(bridge.main(argv[1:], raw=raw))
    if role == "mcp":
        import mcp_server; return int(mcp_server.main(argv[1:]))
    if role in {"setup", "update", "doctor", "sharing"}:
        import setup; return int(setup.main(argv[1:] if role == "setup" else [role, *argv[1:]]))
    raise SystemExit(f"unknown mindie_launch role: {role}")
if __name__ == "__main__":
    raise SystemExit(main())
