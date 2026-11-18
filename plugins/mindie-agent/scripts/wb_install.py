"""Self-contained WorkBuddy plugin and Connector installer (`wb_install.py`).
[Role]
Copies the MindIE plugin tree into the WorkBuddy cache, pins the verified
runtime interpreter, and registers the native plugin (and official Connector
marketplace entry when present).
[Invariants]
- Zero imports from deleted legacy `paths.py` or `storage_lifecycle.py`.
- `install_workbuddy_plugin` is the only writer of WorkBuddy plugin registries.
- Installed `hooks.json`, `mcp.json`, and `.mcp.json` pin verified `python`.
- Only formal `X.Y.Z` cache directories other than the active version are pruned.
[Timeouts & Locks]
- Local file copies and small atomic JSON writes only; no network access.
[Side-Effect Policy]
- Writes the plugin cache, marketplace/installed/settings records, connector
  cache, and per-account state under `<wb_root>`.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".py", ".json", ".md", ".toml", ".svg", ".txt", ".yml", ".yaml", ".cfg", ".ini"}
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".git", ".venv"}
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SAFE_SEG_RE = re.compile(r"^[A-Za-z0-9._+-]{1,128}\Z")
def _home(kind: str) -> Path:
    explicit, home = os.environ.get(f"MINDIE_{kind.upper()}_HOME"), Path(os.environ.get("USERPROFILE") or Path.home())
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA" if kind == "config" else "LOCALAPPDATA") or home / "AppData" / ("Roaming" if kind == "config" else "Local")) / "mindie-agent"
    return Path(os.environ.get("XDG_CONFIG_HOME" if kind == "config" else "XDG_DATA_HOME") or home / (".config" if kind == "config" else ".local/share")) / "mindie-agent"
def _config_path(config=None) -> Path:
    return Path(config).expanduser() if config else Path(os.environ.get("MINDIE_AGENT_CONFIG") or _home("config") / "wb.json")
def _wb_root(wb_dir=None) -> Path:
    explicit = wb_dir or os.environ.get("WORKBUDDY_CONFIG_DIR") or os.environ.get("WORKBUDDY_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    if os.name == "nt":
        return (Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "workbuddy").resolve()
    return (home / ".workbuddy").resolve()
def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes().decode("utf-8-sig")) if path.is_file() else {}
        return value if isinstance(value, dict) else {}
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return {}
def _atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=str(path.parent))
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
def _read_registry(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_bytes().decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"WorkBuddy registry {path} must be a JSON object")
    return value
def _copy_tree(source: Path, destination: Path) -> None:
    src_res, dst_res = source.resolve(), destination.parent.resolve() / destination.name
    if src_res == dst_res or dst_res.is_relative_to(src_res):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage.", dir=str(destination.parent)))
    backup = destination.parent / f".{destination.name}.old.{os.getpid()}"
    try:
        for path in source.rglob("*"):
            if not path.is_file() or path.suffix == ".pyc" or any(part in SKIP_PARTS for part in path.relative_to(source).parts):
                continue
            raw = path.read_bytes()
            if path.suffix in TEXT_SUFFIXES and b"\r\n" in raw:
                raw = raw.replace(b"\r\n", b"\n")
            target = stage / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        if destination.exists():
            os.replace(destination, backup)
        os.replace(stage, destination)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
def _verified_python(config=None) -> str | None:
    candidates: list[str] = []
    recorded = _read_json(_config_path(config)).get("python")
    candidates.append(recorded) if isinstance(recorded, str) else None
    env_python = os.environ.get("MINDIE_WB_PYTHON")
    candidates.append(env_python) if isinstance(env_python, str) and env_python.strip() else None
    runtimes = _home("data") / "runtimes" / "wb"
    for receipt in sorted(runtimes.rglob("runtime.json")) if runtimes.is_dir() else []:
        data = _read_json(receipt)
        candidates.append(data["python"]) if (data.get("validation") or {}).get("status") == "validated" and isinstance(data.get("python"), str) else None
    venv = _home("data") / "wb-venv"
    candidates.extend(str(venv / name) for name in ("Scripts/python.exe", "bin/python"))
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path.parent.resolve() / path.name)
    return None
def _pin_python(plugin_dir: Path, python: str | None) -> None:
    if not python:
        return
    hooks_file = plugin_dir / "hooks" / "hooks.json"
    hooks = _read_json(hooks_file)
    for entries in (hooks.get("hooks") or {}).values() if isinstance(hooks.get("hooks"), dict) else []:
        for group in entries if isinstance(entries, list) else []:
            for item in (group.get("hooks") or []) if isinstance(group, dict) else []:
                command = item.get("command") if isinstance(item, dict) else None
                if isinstance(command, str) and command.startswith(("python ", "python3 ")):
                    item["command"] = f'"{python}" {command.split(" ", 1)[1]}'
    _atomic_json(hooks_file, hooks) if hooks else None
    for name in ("mcp.json", ".mcp.json"):
        document = _read_json(plugin_dir / name)
        for server in (document.get("mcpServers") or {}).values() if isinstance(document.get("mcpServers"), dict) else []:
            if isinstance(server, dict) and server.get("command") in {"python", "python3"}:
                server["command"] = python
        _atomic_json(plugin_dir / name, document) if document else None
def _discover_accounts(wb_root: Path) -> list[tuple[str, str]]:
    accounts: dict[str, str] = {}
    for state_file in (wb_root / "plugin-account-state").glob("*.json") if (wb_root / "plugin-account-state").is_dir() else []:
        identity = str(_read_json(state_file).get("identityKey") or "")
        if state_file.stem.strip():
            accounts[state_file.stem.strip()] = identity.split("|", 1)[1] if "|" in identity else ""
    snapshot = _read_json(wb_root / "storage" / "skeleton" / "account-snapshot.json").get("primary")
    if isinstance(snapshot, dict) and str(snapshot.get("uid") or "").strip():
        accounts[str(snapshot["uid"]).strip()] = str(snapshot.get("enterpriseId") or "").strip()
    for state_file in (wb_root / "connectors").glob("*/connector-states.json") if (wb_root / "connectors").is_dir() else []:
        parts = str(_read_json(state_file).get("accountIdentityKey") or "").split("|")
        uid = (parts[0] if parts and parts[0] else state_file.parent.name).strip()
        accounts[uid] = (parts[1].strip() if len(parts) > 1 else accounts.get(uid, "")) if uid else ""
    claw = _read_json(wb_root / "settings.json").get("claw")
    accounts.setdefault(str(claw["legacyOwnerUid"]).strip(), "") if isinstance(claw, dict) and str(claw.get("legacyOwnerUid") or "").strip() else None
    return list(accounts.items())
def _record(path: Path, version: str, now: str) -> list[dict]:
    return [{"scope": "user", "installPath": str(path.resolve()), "version": version, "installedAt": now, "lastUpdated": now}]
def _prune(wb_root: Path, version: str) -> list[str]:
    cache, removed = wb_root / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent", []
    for target in cache.iterdir() if cache.is_dir() else []:
        if target.is_dir() and not target.is_symlink() and VERSION_RE.fullmatch(target.name) and target.name != version:
            shutil.rmtree(target, ignore_errors=True)
            if not target.exists():
                removed.append(str(target))
    return removed
def install_workbuddy_plugin(*, wb_dir=None, config=None, source_root=None) -> dict:
    """Install, register, and enable `mindie-agent` under the WorkBuddy config root."""
    wb_root, plugins = _wb_root(wb_dir), _wb_root(wb_dir) / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    source = Path(source_root).expanduser().resolve() if source_root is not None else PLUGIN_ROOT
    source = source / "plugins" / "mindie-agent" if not (source / "plugin.json").is_file() and (source / "plugins" / "mindie-agent" / "plugin.json").is_file() else source
    manifest, version = _read_json(source / "plugin.json"), str(_read_json(source / "plugin.json").get("version") or "0.1.0")
    if version in {".", ".."} or not SAFE_SEG_RE.fullmatch(version):
        raise ValueError(f"invalid plugin version: {version!r}")
    now, python = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", _verified_python(config)
    marketplace, plugin_id = "mindie-agent-marketplace", "mindie-agent@mindie-agent-marketplace"
    cache_dest = plugins / "cache" / marketplace / "mindie-agent" / version
    _copy_tree(source, cache_dest)
    _pin_python(cache_dest, python)
    repo = source.parents[1] if (source.parents[1] / "pyproject.toml").is_file() else source
    known = _read_registry(plugins / "known_marketplaces.json")
    known[marketplace] = {"manifestName": marketplace, "type": "directory", "source": {"source": "directory", "path": str(repo)},
                          "installLocation": str(repo), "description": "MindIE Agent official plugin and connector marketplace for Tencent WorkBuddy",
                          "lastUpdated": now, "autoUpdate": False}
    _atomic_json(plugins / "known_marketplaces.json", known)
    installed = _read_registry(plugins / "installed_plugins.json") or {"version": 2, "plugins": {}}
    records = installed.setdefault("plugins", {})
    records[plugin_id] = _record(cache_dest, version, now)
    connector_root = plugins / "marketplaces" / "workbuddy-connector-plugins-official"
    settings = _read_registry(wb_root / "settings.json")
    enabled = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    enabled[plugin_id] = True
    connector_dest = connector_cache = None
    if connector_root.is_dir():
        connector_id = "mindie-agent@workbuddy-connector-plugins-official"
        connector_dest, connector_cache = connector_root / "connectors" / "mindie-agent", plugins / "cache" / "workbuddy-connector-plugins-official" / "mindie-agent" / version
        _copy_tree(source, connector_dest)
        _pin_python(connector_dest, python)
        _copy_tree(source, connector_cache)
        _pin_python(connector_cache, python)
        records[connector_id] = _record(connector_cache, version, now)
        enabled[connector_id] = False
        catalog_file = connector_root / ".codebuddy-plugin" / "marketplace.json"
        catalog = _read_registry(catalog_file)
        catalog["plugins"] = [{"name": "mindie-agent", "description": manifest.get("description", ""), "source": "./connectors/mindie-agent", "version": version},
                              *[item for item in (catalog.get("plugins") or []) if isinstance(item, dict) and item.get("name") != "mindie-agent"]]
        _atomic_json(catalog_file, catalog)
        for uid, enterprise in _discover_accounts(wb_root):
            if uid in {".", ".."} or not SAFE_SEG_RE.fullmatch(uid):
                continue
            account_file = wb_root / "plugin-account-state" / f"{uid}.json"
            account, state = _read_json(account_file), _read_json(account_file).get("plugins")
            state = state if isinstance(state, dict) else {}
            previous = state.get(connector_id)
            activated = previous.get("activatedAt") if isinstance(previous, dict) and isinstance(previous.get("activatedAt"), str) else now
            state[connector_id] = {"enabled": True, "activatedAt": activated, "updatedAt": now}
            _atomic_json(account_file, {"version": 1, "identityKey": f"{uid}|{enterprise}", "plugins": state})
    settings["enabledPlugins"] = enabled
    _atomic_json(plugins / "installed_plugins.json", installed)
    _atomic_json(wb_root / "settings.json", settings)
    return {"status": "installed", "plugin_id": plugin_id, "enabled": True, "version": version, "cache_install_path": str(cache_dest.resolve()),
            "connector_path": str(connector_dest.resolve()) if connector_dest else None,
            "connector_cache_path": str(connector_cache.resolve()) if connector_cache else None,
            "known_marketplaces": str(plugins / "known_marketplaces.json"), "installed_plugins": str(plugins / "installed_plugins.json"),
            "settings_file": str(wb_root / "settings.json"), "pruned_versions": _prune(wb_root, version)}
