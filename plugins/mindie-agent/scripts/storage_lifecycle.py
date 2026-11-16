"""Shared-storage lifecycle helpers for the WorkBuddy adapter.

[Role]
Serializes component updates, unbinds pinned WorkBuddy cache interpreters,
retires superseded plugin caches, and holds a resident MCP runtime lease.

[Invariants]
- `component_update_lock` is the only cross-process updater mutex; a contended
  acquisition returns `False` and never invents a failed update.
- Cache refreshes touch only existing `plugins/cache/*/mindie-agent/<version>`
  records and rewrite only Python launcher tokens. Account and consent state
  are never read or written.
- A cache generation leaves only through `retire_directory` (two-phase rename);
  Windows never holds an entry descriptor across the NTFS rename.
- Incomplete process inspection or a live lease keeps the generation.
- A post-update local occupancy becomes `receipt_warning` on a successful
  switch, never a one-way fault.
- `hold_runtime_lease` protects the generation owning `sys.executable`; an
  absent or moved generation simply serves without a lease.

[Timeouts & Locks]
- Updater mutex: `1.0s` non-blocking poll budget. Reclamation uses one absolute
  deadline (`8.0s` default), checks every entry, and never waits on a lock.
  `hold_runtime_lease` uses the shared non-blocking OS lease.

[Side-Effect Policy]
- Writes `<data_home>/wb-component-update.lock`, atomically rewrites pinned
  launchers in existing cache records, retires obsolete generations, and
  creates/releases the generation's own `.inuse.lock`. No network call.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import sys
import time

from paths import atomic_write_json, config_path, data_home, read_json, state_dir, workbuddy_config_dir

UPDATE_LOCK_NAME = "wb-component-update.lock"
UPDATE_LOCK_TIMEOUT = 1.0
DEFAULT_MAINTENANCE_BUDGET = 8.0
CACHE_PLUGIN_NAME = "mindie-agent"
_CACHE_JSON_NAMES = ("hooks.json", "hooks/hooks.json", "mcp.json", ".mcp.json")
_PYTHON_LAUNCHER = re.compile(r"python(?:w)?(?:\d+(?:\.\d+)*)?(?:\.exe)?")


def _version_key(name: str):
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"[.+-]", name))


def _path_key(value) -> str:
    """Shared cross-platform identity for cache path comparisons."""
    try:
        from mindie_knowledge.filesystem import path_identity

        return path_identity(value)
    except TimeoutError:
        raise
    except (ImportError, OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(os.fspath(value)))


@contextlib.contextmanager
def component_update_lock(root: str | Path | None = None, *, timeout: float = UPDATE_LOCK_TIMEOUT):
    """Hold the cross-process updater mutex under `<data_home>`; yields `acquired`."""
    directory = Path(root) if root is not None else data_home()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = directory / UPDATE_LOCK_NAME
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            while True:
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
        yield locked
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _split_launcher(command: str) -> tuple[str, str]:
    text = command.lstrip()
    if not text:
        return "", ""
    if text[0] in "\"'":
        close = text.find(text[0], 1)
        if close < 0:
            return text, ""
        return text[1:close], text[close + 1:]
    head, _, rest = text.partition(" ")
    return head, ((" " + rest) if rest else "")


def _is_python_launcher(token: str) -> bool:
    if not token or not token.strip():
        return False
    name = re.split(r"[\\/]", token.strip())[-1].casefold()
    return _PYTHON_LAUNCHER.fullmatch(name) is not None


def _rebind_launcher(command: str, new_python: str) -> tuple[str, bool]:
    token, rest = _split_launcher(command)
    if not _is_python_launcher(token):
        return command, False
    return f'"{new_python}"{rest}', True


def _cache_plugin_dirs(wb_root: Path):
    cache_root = Path(wb_root) / "plugins" / "cache"
    if cache_root.is_symlink() or not cache_root.is_dir():
        return
    try:
        with os.scandir(cache_root) as iterator:
            markets = [Path(item.path) for item in iterator
                       if item.is_dir(follow_symlinks=False)]
    except TimeoutError:
        raise
    except OSError:
        return
    for market in markets:
        plugin_dir = market / CACHE_PLUGIN_NAME
        if plugin_dir.is_symlink() or not plugin_dir.is_dir():
            continue
        try:
            with os.scandir(plugin_dir) as iterator:
                versions = [Path(item.path) for item in iterator
                            if item.is_dir(follow_symlinks=False)]
        except TimeoutError:
            raise
        except OSError:
            continue
        for version_dir in versions:
            if not version_dir.name.startswith(".trash-"):
                yield version_dir


def _refresh_hooks(payload: dict, new_python: str) -> bool:
    changed = False
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for group in entries:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for item in group["hooks"]:
                command = item.get("command") if isinstance(item, dict) else None
                if isinstance(command, str):
                    rebound, did = _rebind_launcher(command, new_python)
                    if did and rebound != command:
                        item["command"] = rebound
                        changed = True
    return changed


def _refresh_mcp(payload: dict, new_python: str) -> bool:
    changed = False
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    for server in servers.values():
        command = server.get("command") if isinstance(server, dict) else None
        if isinstance(command, str) and _is_python_launcher(command.strip().strip('"').strip("'")):
            if command != new_python:
                server["command"] = new_python
                changed = True
    return changed


def refresh_workbuddy_cache_python(
    new_python: str | Path | None,
    *,
    wb_dir: str | Path | None = None,
    config: str | Path | None = None,
) -> dict:
    """Atomically rebind pinned Python launchers in existing MindIE cache records."""
    report: dict = {"schema": "mindie-wb-cache-refresh/1", "updated": [], "warnings": []}
    if not isinstance(new_python, (str, Path)) or not str(new_python).strip():
        return report
    python_text = str(Path(new_python).expanduser())
    root = Path(wb_dir).expanduser().resolve() if wb_dir is not None else workbuddy_config_dir(config)
    for version_dir in _cache_plugin_dirs(root):
        for relative in _CACHE_JSON_NAMES:
            target = version_dir / relative
            if target.is_symlink() or not target.is_file():
                continue
            try:
                payload = read_json(target)
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                report["warnings"].append(f"{target.name}: {type(exc).__name__}")
                continue
            changed = _refresh_hooks(payload, python_text) if relative.endswith("hooks.json") else False
            if relative.endswith(("mcp.json", ".mcp.json")):
                changed = _refresh_mcp(payload, python_text) or changed
            if not changed:
                continue
            try:
                atomic_write_json(target, payload)
            except TimeoutError:
                raise
            except OSError as exc:
                report["warnings"].append(f"{target.name}: {type(exc).__name__}")
                continue
            report["updated"].append(str(target))
    return report


def _pinned_cache_versions(wb_root: Path, plugin_dir: Path) -> set[str]:
    installed_file = Path(wb_root) / "plugins" / "installed_plugins.json"
    if not installed_file.is_file():
        return set()
    try:
        data = read_json(installed_file)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return set()
    pinned: set[str] = set()
    for records in (data.get("plugins") or {}).values():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            install_path = record.get("installPath")
            if not isinstance(install_path, str):
                continue
            try:
                if _path_key(Path(install_path).parent) == _path_key(plugin_dir):
                    pinned.add(Path(install_path).name)
            except TimeoutError:
                raise
            except (OSError, RuntimeError):
                continue
    return pinned


def prune_workbuddy_plugin_caches(
    *,
    wb_dir: str | Path | None = None,
    config: str | Path | None = None,
    active_version: str | None = None,
    keep_previous: int = 1,
    budget_seconds: float = 5.0,
) -> dict:
    """Retire superseded MindIE plugin caches, keeping active plus one rollback."""
    report: dict = {
        "schema": "mindie-wb-cache-prune/1",
        "status": "completed",
        "reclaimed": [],
        "kept": [],
        "warnings": [],
        "partial": False,
    }
    root = Path(wb_dir).expanduser().resolve() if wb_dir is not None else workbuddy_config_dir(config)
    try:
        from mindie_knowledge.storage import (
            is_lease_held,
            is_path_used_by_live_process,
            live_process_prefixes,
            retire_directory,
        )
    except ImportError:
        report["status"] = "unavailable"
        return report
    deadline = time.monotonic() + max(0.1, float(budget_seconds))
    live_paths, inspection_complete = live_process_prefixes(
        timeout=max(0.1, min(2.0, deadline - time.monotonic())))
    if not inspection_complete:
        report["status"] = "inspection_incomplete"
        report["partial"] = True
        report["warnings"].append("process inspection incomplete")
        return report
    plugin_dirs = sorted({version_dir.parent for version_dir in _cache_plugin_dirs(root)})
    for plugin_dir in plugin_dirs:
        try:
            versions = sorted(
                [entry for entry in plugin_dir.iterdir()
                 if entry.is_dir() and not entry.is_symlink()],
                # Rank by formal version, not directory mtime: refreshing a
                # pinned launcher rewrites files and must not promote an older
                # generation into the rollback slot.
                key=lambda entry: (_version_key(entry.name), entry.stat().st_mtime),
                reverse=True,
            )
        except TimeoutError:
            raise
        except OSError as exc:
            report["warnings"].append(f"scan {plugin_dir.name}: {type(exc).__name__}")
            continue
        pinned = _pinned_cache_versions(root, plugin_dir)
        retained = set(pinned)
        if active_version and (plugin_dir / active_version).is_dir():
            retained.add(active_version)
        if not retained and versions:
            retained.add(versions[0].name)
        rollback_slots = max(0, int(keep_previous))
        for entry in versions:
            if rollback_slots <= 0:
                break
            if entry.name in retained:
                continue
            retained.add(entry.name)
            rollback_slots -= 1
        for entry in versions:
            if entry.name in retained:
                report["kept"].append(str(entry))
                continue
            if time.monotonic() >= deadline:
                report["status"] = "partial"
                report["partial"] = True
                report["warnings"].append("budget exhausted")
                return report
            if is_lease_held(entry) or is_path_used_by_live_process(entry, live_paths):
                report["kept"].append(str(entry))
                continue
            try:
                outcome = retire_directory(entry, deadline=deadline)
            except TimeoutError:
                raise
            except OSError as exc:
                report["warnings"].append(f"retire {entry.name}: {type(exc).__name__}")
                continue
            if outcome.get("status") in {"retired", "trashed_partial"}:
                report["reclaimed"].append(str(entry))
            else:
                report["kept"].append(str(entry))
                if outcome.get("status") == "deferred_in_use":
                    report["status"] = "partial"
                    report["partial"] = True
                    report["warnings"].append(f"retire {entry.name}: deferred in use")
                elif outcome.get("error"):
                    report["warnings"].append(f"retire {entry.name}: {outcome['error']}")
            if outcome.get("partial"):
                report["status"] = "partial"
                report["partial"] = True
    return report


def _warnings_for(report: dict) -> list[str]:
    status = report.get("status")
    if status in {"partial", "inspection_incomplete", "unreadable", "refused_link", "protected_root"}:
        return [f"{status}: {warning}" for warning in report.get("warnings", [])[:4]]
    return []


def post_update_maintenance(
    config: str | Path | None = None,
    new_python: str | Path | None = None,
    *,
    previous_python: str | Path | None = None,
    active_version: str | None = None,
    budget_seconds: float = DEFAULT_MAINTENANCE_BUDGET,
) -> dict:
    """Refresh external cache pins, retire old caches, and reclaim storage in one budget."""
    report: dict = {
        "schema": "mindie-wb-storage-maintenance/1",
        "status": "completed",
        "warnings": [],
        "cache_refresh": None,
        "cache_cleanup": None,
        "component_prune": None,
        "legacy_sweep": None,
    }
    deadline = time.monotonic() + max(0.1, float(budget_seconds))

    def remaining(limit: float) -> float:
        return max(0.1, min(float(limit), deadline - time.monotonic()))

    if new_python:
        try:
            report["cache_refresh"] = refresh_workbuddy_cache_python(new_python, config=config)
            report["warnings"].extend(report["cache_refresh"].get("warnings", []))
        except TimeoutError as exc:
            report["warnings"].append(f"cache refresh: {type(exc).__name__}: {exc}")
        except Exception as exc:
            report["warnings"].append(f"cache refresh: {type(exc).__name__}: {exc}")
        try:
            report["cache_cleanup"] = prune_workbuddy_plugin_caches(
                config=config,
                active_version=active_version,
                budget_seconds=remaining(3.0),
            )
            report["warnings"].extend(_warnings_for(report["cache_cleanup"]))
        except TimeoutError as exc:
            report["warnings"].append(f"cache cleanup: {type(exc).__name__}: {exc}")
        except Exception as exc:
            report["warnings"].append(f"cache cleanup: {type(exc).__name__}: {exc}")

    runtimes = state_dir(config) / "component-runtimes"
    if runtimes.is_dir():
        keep_paths: list[str] = []
        try:
            from mindie_knowledge.storage import owning_dir
        except ImportError:
            owning_dir = None
        for interpreter in (new_python, previous_python):
            if not interpreter:
                continue
            path = str(Path(interpreter).expanduser())
            keep_paths.append(path)
            if owning_dir is not None:
                owner = owning_dir(path)
                if owner is not None:
                    keep_paths.append(str(owner))
        try:
            from mindie_knowledge.storage import prune_component_runtimes

            report["component_prune"] = prune_component_runtimes(
                runtimes,
                keep_paths=keep_paths,
                keep_recent_verified=2,
                budget_seconds=remaining(3.0),
                deadline=deadline,
            )
            report["warnings"].extend(_warnings_for(report["component_prune"]))
            if report["component_prune"].get("deferred_dirs"):
                report["warnings"].append("component runtimes deferred in use")
        except TimeoutError as exc:
            report["warnings"].append(f"component prune: {type(exc).__name__}: {exc}")
        except Exception as exc:
            report["warnings"].append(f"component prune: {type(exc).__name__}: {exc}")

    try:
        from mindie_knowledge.storage import sweep_legacy_installations

        # The active generation and its rollback must survive this second
        # sweep even when the profile's source_root/package_dir pins have not
        # been re-read yet; temporary shared leases are the liveness authority.
        with contextlib.ExitStack() as leases:
            leases.enter_context(hold_runtime_lease(new_python))
            if previous_python:
                leases.enter_context(hold_runtime_lease(previous_python))
            report["legacy_sweep"] = sweep_legacy_installations(
                config_home=config_path(config).parent,
                data_home=data_home(),
                budget_seconds=remaining(5.0),
                deadline=deadline,
            )
        report["warnings"].extend(_warnings_for(report["legacy_sweep"]))
        if report["legacy_sweep"].get("deferred_dirs"):
            report["warnings"].append("legacy storage deferred in use")
    except TimeoutError as exc:
        report["warnings"].append(f"legacy sweep: {type(exc).__name__}: {exc}")
    except Exception as exc:
        report["warnings"].append(f"legacy sweep: {type(exc).__name__}: {exc}")

    if report["warnings"]:
        report["status"] = "partial"
    return report


@contextlib.contextmanager
def hold_runtime_lease(executable: str | Path | None = None):
    """Hold a shared lease for the generation owning `executable` while serving."""
    try:
        from mindie_knowledge.storage import acquire_runtime_lease, owning_dir
    except ImportError:
        yield None
        return
    owner = owning_dir(str(executable or sys.executable))
    if owner is None:
        yield None
        return
    try:
        lease = acquire_runtime_lease(owner)
    except TimeoutError:
        raise
    except (OSError, RuntimeError):
        yield None
        return
    try:
        yield lease
    finally:
        lease.release()


def configure_uv_cache(target_dir: str | Path, env: dict[str, str]) -> dict[str, str]:
    """Pin uv's cache to the target volume unless the caller set `UV_CACHE_DIR`."""
    if not isinstance(env, dict):
        return env
    try:
        from mindie_knowledge.storage import configure_same_volume_uv_cache
    except ImportError:
        return env
    try:
        return configure_same_volume_uv_cache(Path(target_dir), env)
    except TimeoutError:
        raise
    except OSError:
        return env


__all__ = [
    "DEFAULT_MAINTENANCE_BUDGET",
    "UPDATE_LOCK_NAME",
    "component_update_lock",
    "configure_uv_cache",
    "hold_runtime_lease",
    "post_update_maintenance",
    "prune_workbuddy_plugin_caches",
    "refresh_workbuddy_cache_python",
]
