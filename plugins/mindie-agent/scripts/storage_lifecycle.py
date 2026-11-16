"""Shared-storage lifecycle helpers for the WorkBuddy adapter.

[Role]
Serializes component updates, unbinds pinned cache interpreters, retires
superseded plugin caches, and holds a resident MCP runtime lease.

[Invariants]
- `component_update_lock` is the only cross-process updater mutex; a contended
  acquisition returns `False` and never invents a failed update.
- Cache refreshes rewrite only Python launcher tokens in existing
  `plugins/cache/*/mindie-agent/<version>` records; account and consent state
  are never touched, and a manifest reached through a symlink/junction, a Git
  checkout, or a configured source root is reported and left untouched.
- Cache rollback ranking reads formal versions only: `.trash-*` remnants and
  non-semver names never consume the single rollback slot.
- A cache generation leaves only through `retire_directory` (two-phase rename);
  Windows never holds a descriptor across the NTFS rename, and incomplete
  process inspection or a live lease keeps the generation.
- A borrowed generation survives a narrowed `--config`: every default sibling
  config home is inspected and its pinned generations hold a lease across both
  prune phases.
- An ordinary post-update occupancy becomes `cleanup_warning`, never
  `receipt_unconfirmed`; `hold_runtime_lease` protects the `sys.executable`
  generation and serves unleased when it is absent or has moved.

[Timeouts & Locks]
- Updater mutex: `1.0s` non-blocking poll budget. Reclamation uses one absolute
  deadline (`8.0s` default), never waits on a lock, and leases never block.

[Side-Effect Policy]
- Writes `<data_home>/wb-component-update.lock`, atomically rewrites pinned
  launchers in existing cache records, retires obsolete generations, and
  creates/releases the generation's own `.inuse.lock`. No network call.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from paths import config_path, data_home, read_json, state_dir, workbuddy_config_dir

UPDATE_LOCK_NAME = "wb-component-update.lock"
UPDATE_LOCK_TIMEOUT = 1.0
DEFAULT_MAINTENANCE_BUDGET = 8.0
CACHE_PLUGIN_NAME = "mindie-agent"
_TRASH_PREFIX = ".trash-"
# A rollback candidate is a formal release directory, never a `.trash-*`
# remnant or an installer scratch name such as `latest` / `tmp-abc`.
_CACHE_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+")
_CACHE_JSON_NAMES = ("hooks.json", "hooks/hooks.json", "mcp.json", ".mcp.json")
_BORROWED_CONFIG_FILES = (
    "codex.json", "cc.json", "kimi.json", "agy.json", "wb.json", "oc.json",
    "dsh/config.json", "dsh/engine.json", "diagnostics.json",
)
_PYTHON_LAUNCHER = re.compile(r"python(?:w)?(?:\d+(?:\.\d+)*)?(?:\.exe)?")
_MAX_STRING_DEPTH = 4


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


def _is_junction(value) -> bool:
    """Report an NTFS junction without following it; unknown inspection refuses."""
    try:
        from mindie_knowledge.filesystem import is_junction
    except ImportError:
        is_junction = None
    if is_junction is not None:
        try:
            return bool(is_junction(value))
        except TimeoutError:
            raise
        except (OSError, ValueError):
            return True
    try:
        return bool(getattr(os.lstat(os.fspath(value)), "st_reparse_tag", 0))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False


def _linked_segment(anchor: Path, target: Path) -> Path | None:
    """First symlink/junction between `anchor` and `target` (inclusive), if any."""
    anchor, target = Path(anchor), Path(target)
    try:
        parts = target.relative_to(anchor).parts
    except ValueError:
        parts = ()
    probe = anchor
    for part in parts:
        probe = probe / part
        if probe.is_symlink() or _is_junction(probe):
            return probe
    return target if (not parts and (target.is_symlink() or _is_junction(target))) else None


def _iter_strings(value, depth: int = 0):
    """Bounded walk over a parsed profile's string leaves."""
    if isinstance(value, str):
        yield value
    elif depth < _MAX_STRING_DEPTH and isinstance(value, (dict, list)):
        items = value.values() if isinstance(value, dict) else value
        for item in items:
            yield from _iter_strings(item, depth + 1)


def _default_config_homes() -> list[Path]:
    """Default sibling config homes (`MINDIE_CONFIG_HOME`, XDG, Windows roaming)."""
    try:
        from mindie_knowledge.storage.legacy_roots import _config_homes
        return list(_config_homes(None))
    except (ImportError, AttributeError, TypeError):
        pass
    homes = [Path.home() / ".config" / "mindie-agent"]
    override = os.environ.get("MINDIE_CONFIG_HOME")
    if override and os.path.isabs(override):
        homes.insert(0, Path(override))
    if os.name == "nt":
        homes.extend(Path(os.environ[key]) / "mindie-agent"
                     for key in ("APPDATA", "LOCALAPPDATA") if os.environ.get(key))
    return homes


def _config_homes_for_sweep(config: str | Path | None) -> list[Path]:
    """Every config home whose borrowed runtimes must survive a legacy sweep.

    Passing one narrowed directory would hide a sibling profile such as
    `codex.json` that still borrows a generation inside this data home.
    """
    homes: list[Path] = []
    seen: set[str] = set()
    for home in (config_path(config).parent, *_default_config_homes()):
        key = os.path.normcase(os.path.abspath(os.fspath(home)))
        if key not in seen:
            seen.add(key)
            homes.append(Path(home))
    return homes


def _strip_win_prefix(text: str) -> str:
    if text[:8].upper() == "\\\\?\\UNC\\":
        return "\\\\" + text[8:]
    return text[4:] if text.startswith(("\\\\?\\", "//?/")) else text


def _borrowed_runtime_owners(config_homes) -> tuple[list[Path], bool]:
    """Runtime generations pinned by sibling profiles; bool flags scan completeness."""
    try:
        from mindie_knowledge.storage import owning_dir
    except ImportError:
        return [], True
    owners, seen, complete = [], set(), True
    for home in config_homes:
        for name in _BORROWED_CONFIG_FILES:
            document = Path(home) / name
            try:
                if not (document.exists() or document.is_symlink()):
                    continue
                payload = read_json(document)
            except TimeoutError:
                raise
            except (OSError, ValueError):
                complete, payload = False, None
            for text in _iter_strings(payload) if payload else ():
                token = _strip_win_prefix(text.strip().strip('"').strip("'"))
                launcher = re.split(r"[\\/]", token)[-1].casefold() if os.path.isabs(token) else ""
                owner = owning_dir(token) if _PYTHON_LAUNCHER.fullmatch(launcher) else None
                clean = Path(_strip_win_prefix(os.fspath(owner))) if owner is not None else None
                key = os.path.normcase(os.path.abspath(os.fspath(clean))) if clean is not None else ""
                if key and key not in seen:
                    seen.add(key)
                    owners.append(clean)
    return owners, complete


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
        return (text, "") if close < 0 else (text[1:close], text[close + 1:])
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
            if not version_dir.name.startswith(_TRASH_PREFIX):
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


def _cache_manifest_blocker(cache_root: Path, target: Path) -> str | None:
    """Why `target` must not be read or rewritten, or `None` when it is safe.

    Every segment below the managed cache root must be a real entry: a `hooks/`
    symlink or junction can point at an external Git checkout.
    """
    root, path = Path(cache_root), Path(target)
    try:
        path.relative_to(root)
    except ValueError:
        return "outside cache root"
    if _linked_segment(root, path):
        return "linked path"
    try:
        resolved_root, resolved = root.resolve(), path.resolve()
        resolved.relative_to(resolved_root)
    except TimeoutError:
        raise
    except ValueError:
        return "outside cache root"
    except (OSError, RuntimeError):
        return "unresolvable"
    if resolved == resolved_root:
        return "outside cache root"
    try:
        from mindie_knowledge.storage.legacy_roots import protected_source_roots
        from mindie_knowledge.storage.lifecycle import has_git_ancestor, is_within_roots
        if has_git_ancestor(resolved):
            return "Git checkout"
        if is_within_roots(resolved, protected_source_roots()):
            return "protected source root"
    except TimeoutError:
        raise
    except ImportError:
        return None
    except (AttributeError, OSError):
        return "unverifiable"
    return None


def _write_json_no_follow(target: Path, payload: dict) -> None:
    """Atomically replace `target` in its own directory without resolving links."""
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(serialized)
            stream.flush()
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except TimeoutError:
            raise
        except OSError:
            pass
        raise


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
            if not target.is_file():
                continue
            blocker = _cache_manifest_blocker(root, target)
            if blocker is not None:
                report["warnings"].append(f"{target.name}: skipped ({blocker})")
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
            # Re-verify immediately before the rename: a link swapped in after
            # the read must not redirect the atomic replace.
            recheck = _cache_manifest_blocker(root, target)
            if recheck is not None:
                report["warnings"].append(f"{target.name}: skipped ({recheck})")
                continue
            try:
                _write_json_no_follow(target, payload)
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
    report: dict = {"schema": "mindie-wb-cache-prune/1", "status": "completed",
                    "reclaimed": [], "kept": [], "warnings": [], "partial": False,
                    "reclaimed_trash": 0}
    root = Path(wb_dir).expanduser().resolve() if wb_dir is not None else workbuddy_config_dir(config)
    try:
        from mindie_knowledge.storage import (
            is_lease_held, is_path_used_by_live_process, live_process_prefixes,
            retire_directory, sweep_trash)
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
        # Leftover `.trash-*` remnants are half-retired generations, not
        # releases: finish deleting them first so they can never be ranked.
        try:
            trash = sweep_trash(plugin_dir, deadline=deadline)
        except TimeoutError:
            raise
        except OSError as exc:
            report["warnings"].append(f"trash {plugin_dir.name}: {type(exc).__name__}")
            trash = {}
        report["reclaimed_trash"] += int(trash.get("removed") or 0)
        report["warnings"].extend(f"trash: {w}" for w in list(trash.get("warnings") or ())[:2])
        if trash.get("partial"):
            report["status"] = "partial"
            report["partial"] = True
        try:
            versions = sorted(
                [entry for entry in plugin_dir.iterdir()
                 if entry.is_dir() and not entry.is_symlink()
                 and _CACHE_VERSION.match(entry.name)],
                # Rank by formal version, not directory mtime: refreshing a
                # pinned launcher must not promote an older generation.
                key=lambda entry: (_version_key(entry.name), entry.stat().st_mtime),
                reverse=True)
        except TimeoutError:
            raise
        except OSError as exc:
            report["warnings"].append(f"scan {plugin_dir.name}: {type(exc).__name__}")
            continue
        pinned = _pinned_cache_versions(root, plugin_dir)
        retained = set(pinned)
        active_key = (_version_key(active_version)
                      if isinstance(active_version, str)
                      and _CACHE_VERSION.match(active_version) else None)
        active = plugin_dir / active_version if active_key is not None else None
        if active is not None and active.is_dir() and not active.is_symlink():
            retained.add(active_version)
        if active_key is None:
            if not retained and versions:
                retained.add(versions[0].name)
            older = [entry for entry in versions if entry.name not in retained]
        else:
            # Keep the active release, everything newer (a newer installed
            # release is not this sweep's rollback candidate), and the single
            # immediately previous formal release as the rollback slot.
            retained.update(entry.name for entry in versions
                            if _version_key(entry.name) > active_key)
            older = [entry for entry in versions
                     if _version_key(entry.name) < active_key]
        for entry in older[:max(0, int(keep_previous))]:
            retained.add(entry.name)
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


def _caller_config_pins(config: str | Path | None) -> list[str]:
    """Absolute source/state pins of the caller's own profile document."""
    document = config_path(config)
    try:
        payload = read_json(document) if document.is_file() else {}
    except TimeoutError:
        raise
    except (OSError, ValueError):
        payload = {}
    return [value for key in ("state_dir", "root", "venv_dir", "package_dir", "source_root")
            if isinstance(value := payload.get(key), str) and os.path.isabs(value)]


def _runtime_root_blocker(runtimes: Path, config: str | Path | None) -> str | None:
    """Why the component-runtimes root must not be pruned, or `None` when safe."""
    linked = _linked_segment(data_home(), runtimes)
    if linked is not None:
        return f"linked path: {linked.name[:32]}"
    try:
        from mindie_knowledge.storage.legacy_roots import protected_source_roots
        from mindie_knowledge.storage.lifecycle import has_git_ancestor, is_within_roots
        if has_git_ancestor(runtimes):
            return "Git checkout ancestor"
        roots = protected_source_roots(config_home=_config_homes_for_sweep(config))
        if is_within_roots(runtimes, roots):
            return "protected source root"
    except TimeoutError:
        raise
    except ImportError:
        return None
    except (AttributeError, OSError):
        return "unverifiable"
    return None


def post_update_maintenance(
    config: str | Path | None = None,
    new_python: str | Path | None = None,
    *,
    previous_python: str | Path | None = None,
    active_version: str | None = None,
    budget_seconds: float = DEFAULT_MAINTENANCE_BUDGET,
) -> dict:
    """Refresh external cache pins, retire old caches, and reclaim storage in one budget."""
    report: dict = {"schema": "mindie-wb-storage-maintenance/1", "status": "completed",
                    "warnings": [], "cache_refresh": None, "cache_cleanup": None,
                    "component_prune": None, "legacy_sweep": None}
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
    blocker = _runtime_root_blocker(runtimes, config) if runtimes.is_dir() else None
    if blocker is not None:
        # A runtimes root reached through a link, a Git checkout, or a user
        # source pin is foreign storage: refuse the whole first phase.
        report["warnings"].append(f"component prune: skipped ({blocker})")
        report["component_prune"] = {"status": "protected_root", "warnings": [blocker]}
    if runtimes.is_dir() and blocker is None:
        keep_paths: list[str] = _caller_config_pins(config)
        try:
            from mindie_knowledge.storage import owning_dir
        except ImportError:
            owning_dir = None
        for interpreter in (new_python, previous_python):
            if not interpreter:
                continue
            path = str(Path(interpreter).expanduser())
            keep_paths.append(path)
            owner = owning_dir(path) if owning_dir is not None else None
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
        from mindie_knowledge.storage import acquire_runtime_lease, owning_dir, sweep_legacy_installations

        config_homes = _config_homes_for_sweep(config)
        borrowed_owners, complete = _borrowed_runtime_owners(config_homes)
        for interpreter in (new_python, previous_python):
            if interpreter:
                owner = owning_dir(_strip_win_prefix(os.fspath(Path(interpreter).expanduser())))
                if owner is not None:
                    borrowed_owners.append(Path(_strip_win_prefix(os.fspath(owner))))
        with contextlib.ExitStack() as leases:
            leased = complete
            if not complete:
                report["warnings"].append("legacy sweep: skipped (incomplete config scan)")
            seen_owners: set[str] = set()
            for owner in borrowed_owners:
                key = os.path.normcase(os.path.abspath(os.fspath(owner)))
                if key in seen_owners or not owner.is_dir():
                    continue
                seen_owners.add(key)
                try:
                    leases.enter_context(acquire_runtime_lease(owner))
                except TimeoutError:
                    raise
                except (OSError, RuntimeError) as exc:
                    leased = False
                    report["warnings"].append(f"legacy sweep: lease {type(exc).__name__}")
            if leased:
                report["legacy_sweep"] = sweep_legacy_installations(
                    config_home=config_homes, data_home=data_home(),
                    budget_seconds=remaining(5.0), deadline=deadline)
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
def _hold_owner_lease(owner: Path | None):
    """Hold one generation's shared lease; an absent/moved owner serves unleased."""
    lease = None
    if owner is not None:
        try:
            from mindie_knowledge.storage import acquire_runtime_lease
            lease = acquire_runtime_lease(owner)
        except TimeoutError: raise
        except (ImportError, OSError, RuntimeError): lease = None
    try: yield lease
    finally:
        if lease is not None: lease.release()


@contextlib.contextmanager
def hold_runtime_lease(executable: str | Path | None = None):
    """Hold a shared lease for the generation owning `executable` while serving."""
    try: from mindie_knowledge.storage import owning_dir
    except ImportError:
        yield None
        return
    raw = _strip_win_prefix(str(executable or sys.executable))
    owner = owning_dir(raw)
    with _hold_owner_lease(Path(_strip_win_prefix(os.fspath(owner))) if owner is not None else None) as lease:
        yield lease


def configure_uv_cache(target_dir: str | Path, env: dict[str, str]) -> dict[str, str]:
    """Pin uv's cache to the target volume unless the caller set `UV_CACHE_DIR`."""
    if not isinstance(env, dict): return env
    try: from mindie_knowledge.storage import configure_same_volume_uv_cache
    except ImportError: return env
    try: return configure_same_volume_uv_cache(Path(target_dir), env)
    except TimeoutError: raise
    except OSError: return env


__all__ = ["DEFAULT_MAINTENANCE_BUDGET", "UPDATE_LOCK_NAME", "component_update_lock", "configure_uv_cache",
           "hold_runtime_lease", "post_update_maintenance", "prune_workbuddy_plugin_caches", "refresh_workbuddy_cache_python"]
