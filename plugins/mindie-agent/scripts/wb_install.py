"""WorkBuddy plugin and Connector installer (`wb_install.py`).

[Role]
Copies the MindIE plugin tree into the WorkBuddy config directory and registers
the native plugin. When the official connector marketplace is present, also
registers and activates the Connector for discovered accounts.

[Invariants]
- `install_workbuddy_plugin` is the only writer of WorkBuddy plugin registries.
  `setup.py` calls it for `install-wb` and `init --install-wb` only.
- Account discovery reads WorkBuddy's own state files. Session identity is never
  taken from MCP tool arguments.

[Timeouts & Locks]
- Local copy and JSON writes only. No network calls and no runtime bootstrap.

[Side-Effect Policy]
- Writes the plugin cache, marketplace records, `installed_plugins.json`,
  `settings.json`, and per-account connector state under the WorkBuddy config dir.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile

from paths import atomic_write_json, data_home, plugin_root, read_json, workbuddy_config_dir

_TEXT_SUFFIXES = {".py", ".json", ".md", ".toml", ".svg", ".txt", ".yml", ".yaml"}


def _copy_lf_clean(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = src.read_bytes()
    if src.suffix in _TEXT_SUFFIXES and b"\r\n" in raw:
        raw = raw.replace(b"\r\n", b"\n")
    dst.write_bytes(raw)


def _copy_plugin_tree(src_plugin: Path, dst_plugin: Path) -> None:
    if dst_plugin.exists():
        shutil.rmtree(dst_plugin)
    dst_plugin.mkdir(parents=True, exist_ok=True)
    for sub in src_plugin.rglob("*"):
        if any(part in {"__pycache__", ".pytest_cache", ".git", ".venv"} for part in sub.parts):
            continue
        if sub.is_file() and sub.suffix != ".pyc":
            _copy_lf_clean(sub, dst_plugin / sub.relative_to(src_plugin))


def _discover_workbuddy_accounts(wb_root: Path) -> list[tuple[str, str]]:
    """Discover `(uid, enterprise_id)` pairs for signed-in or persisted WorkBuddy accounts."""
    accounts: dict[str, str] = {}
    acct_state_dir = wb_root / "plugin-account-state"
    if acct_state_dir.is_dir():
        for state_file in acct_state_dir.glob("*.json"):
            try:
                ident = str(read_json(state_file).get("identityKey") or "")
                uid = state_file.stem.strip()
                if uid:
                    accounts[uid] = ident.split("|", 1)[1] if "|" in ident else ""
            except ValueError:
                pass

    snap_file = wb_root / "storage" / "skeleton" / "account-snapshot.json"
    if snap_file.is_file():
        try:
            primary = read_json(snap_file).get("primary")
            if isinstance(primary, dict) and str(primary.get("uid") or "").strip():
                uid = str(primary["uid"]).strip()
                ent = str(primary.get("enterpriseId") or "").strip()
                if ent or uid not in accounts:
                    accounts[uid] = ent
        except ValueError:
            pass

    conn_dir = wb_root / "connectors"
    if conn_dir.is_dir():
        for cs_file in conn_dir.glob("*/connector-states.json"):
            try:
                parts = str(read_json(cs_file).get("accountIdentityKey") or "").split("|")
                uid = (parts[0] if parts and parts[0] else cs_file.parent.name).strip()
                ent = parts[1].strip() if len(parts) > 1 else ""
                if uid and (ent or uid not in accounts):
                    accounts[uid] = ent or accounts.get(uid, "")
            except ValueError:
                pass

    settings_file = wb_root / "settings.json"
    if settings_file.is_file():
        try:
            claw = read_json(settings_file).get("claw")
            if isinstance(claw, dict) and str(claw.get("legacyOwnerUid") or "").strip():
                accounts.setdefault(str(claw["legacyOwnerUid"]).strip(), "")
        except ValueError:
            pass

    return list(accounts.items())


def _installed_record(install_path: Path, version: str, now_iso: str) -> list[dict]:
    return [
        {
            "scope": "user",
            "installPath": str(install_path.resolve()),
            "version": version,
            "installedAt": now_iso,
            "lastUpdated": now_iso,
        }
    ]


def _is_temp_checkout_path(path: Path) -> bool:
    raw = str(path).replace("\\", "/")
    if "mindie-agent-wb-install" in raw or raw.startswith(("/tmp/", "/private/tmp/")):
        return True
    try:
        return Path(tempfile.gettempdir()).resolve() in path.resolve().parents
    except OSError:
        return False


def install_workbuddy_plugin(
    *,
    wb_dir: str | Path | None = None,
    config: str | Path | None = None,
    source_root: str | Path | None = None,
) -> dict:
    """Install, register, and enable `mindie-agent` in `~/.workbuddy` (Plugin, MCP servers, Skills & Connector)."""
    wb_root = Path(wb_dir).expanduser().resolve() if wb_dir is not None else workbuddy_config_dir(config)
    plugins_dir = wb_root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)

    src_plugin = plugin_root()
    if source_root is not None:
        src_repo = Path(source_root).expanduser().resolve()
    else:
        src_repo = src_plugin.parent.parent
        if _is_temp_checkout_path(src_repo):
            staged = (data_home() / "wb-source").resolve()
            if (staged / "pyproject.toml").is_file():
                src_repo = staged
    if not (src_plugin / "plugin.json").is_file() and (src_repo / "plugins" / "mindie-agent" / "plugin.json").is_file():
        src_plugin = src_repo / "plugins" / "mindie-agent"
    manifest = read_json(src_plugin / "plugin.json")
    version = str(manifest.get("version") or "0.1.0")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # 1. Stage into ~/.workbuddy/plugins/cache/mindie-agent-marketplace/mindie-agent/<version>
    marketplace_name = "mindie-agent-marketplace"
    plugin_id = f"mindie-agent@{marketplace_name}"
    cache_dest = plugins_dir / "cache" / marketplace_name / "mindie-agent" / version
    _copy_plugin_tree(src_plugin, cache_dest)

    # 2. Register marketplace in known_marketplaces.json
    known_mp_file = plugins_dir / "known_marketplaces.json"
    known_mp = read_json(known_mp_file) if known_mp_file.is_file() else {}
    known_mp[marketplace_name] = {
        "manifestName": marketplace_name,
        "type": "directory",
        "source": {"source": "directory", "path": str(src_repo.resolve())},
        "installLocation": str(src_repo.resolve()),
        "description": "MindIE Agent official plugin and connector marketplace for Tencent WorkBuddy",
        "lastUpdated": now_iso,
        "autoUpdate": False,
    }
    atomic_write_json(known_mp_file, known_mp)

    # 3. Register installed plugin in installed_plugins.json
    installed_file = plugins_dir / "installed_plugins.json"
    installed_data = read_json(installed_file) if installed_file.is_file() else {"version": 2, "plugins": {}}
    plugins_map = installed_data.setdefault("plugins", {})
    plugins_map[plugin_id] = _installed_record(cache_dest, version, now_iso)

    # 4. Also register in workbuddy-connector-plugins-official marketplace & cache if present
    connector_mp_name = "workbuddy-connector-plugins-official"
    connector_plugin_id = f"mindie-agent@{connector_mp_name}"
    connector_mp_root = plugins_dir / "marketplaces" / connector_mp_name
    connector_dest = None
    connector_cache_dest = None
    if connector_mp_root.is_dir():
        connector_dest = connector_mp_root / "connectors" / "mindie-agent"
        _copy_plugin_tree(src_plugin, connector_dest)
        connector_cache_dest = plugins_dir / "cache" / connector_mp_name / "mindie-agent" / version
        _copy_plugin_tree(src_plugin, connector_cache_dest)
        plugins_map[connector_plugin_id] = _installed_record(connector_cache_dest, version, now_iso)
        mp_json_file = connector_mp_root / ".codebuddy-plugin" / "marketplace.json"
        if mp_json_file.is_file():
            try:
                raw_mp = json.loads(mp_json_file.read_text(encoding="utf-8"))
                if isinstance(raw_mp, dict) and isinstance(raw_mp.get("plugins"), list):
                    entries = [p for p in raw_mp["plugins"] if isinstance(p, dict) and p.get("name") != "mindie-agent"]
                    entries.insert(
                        0,
                        {
                            "name": "mindie-agent",
                            "description": manifest["description"],
                            "source": "./connectors/mindie-agent",
                            "version": version,
                        },
                    )
                    raw_mp["plugins"] = entries
                    atomic_write_json(mp_json_file, raw_mp)
            except (OSError, ValueError):
                pass

    atomic_write_json(installed_file, installed_data)

    # 5. Enable plugin in ~/.workbuddy/settings.json -> enabledPlugins
    settings_file = wb_root / "settings.json"
    settings_data = read_json(settings_file) if settings_file.is_file() else {}
    enabled_plugins = settings_data.get("enabledPlugins")
    if not isinstance(enabled_plugins, dict):
        enabled_plugins = {}
        settings_data["enabledPlugins"] = enabled_plugins
    enabled_plugins[plugin_id] = True
    if connector_mp_root.is_dir():
        enabled_plugins[connector_plugin_id] = False
    atomic_write_json(settings_file, settings_data)

    # 6. Activate & enable connector in ~/.workbuddy/plugin-account-state/<uid>.json
    activated_accounts: list[str] = []
    if connector_mp_root.is_dir():
        acct_state_dir = wb_root / "plugin-account-state"
        acct_state_dir.mkdir(parents=True, exist_ok=True)
        for uid, ent_id in _discover_workbuddy_accounts(wb_root):
            acct_file = acct_state_dir / f"{uid}.json"
            acct_data = read_json(acct_file) if acct_file.is_file() else {}
            acct_plugins = acct_data.get("plugins") if isinstance(acct_data.get("plugins"), dict) else {}
            prev = acct_plugins.get(connector_plugin_id)
            act_at = prev.get("activatedAt") if isinstance(prev, dict) and isinstance(prev.get("activatedAt"), str) else now_iso
            acct_plugins[connector_plugin_id] = {"enabled": True, "activatedAt": act_at, "updatedAt": now_iso}
            atomic_write_json(acct_file, {"version": 1, "identityKey": f"{uid}|{ent_id}", "plugins": acct_plugins})
            activated_accounts.append(uid)

    return {
        "status": "installed",
        "plugin_id": plugin_id,
        "enabled": True,
        "version": version,
        "cache_install_path": str(cache_dest.resolve()),
        "connector_path": str(connector_dest.resolve()) if connector_dest else None,
        "connector_cache_path": str(connector_cache_dest.resolve()) if connector_cache_dest else None,
        "known_marketplaces": str(known_mp_file),
        "installed_plugins": str(installed_file),
        "settings_file": str(settings_file),
        "activated_accounts": activated_accounts,
    }


