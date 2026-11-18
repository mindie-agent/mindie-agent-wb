"""Thin WorkBuddy profile, sharing, doctor, update, and installer CLI (`setup.py`).
[Role]
Initializes `wb.json` / `wb.engine.json`, invokes `install_workbuddy_plugin`,
and delegates sharing, doctor, update, and release-metadata validation.
[Invariants]
- `--help`, `-h`, and `--dry-run` (including unique prefixes such as `--dry`)
  are strictly read-only: zero config writes, zero installer calls.
- `init` alone never mounts the WorkBuddy plugin; `install-wb` and
  `init --install-wb` are the only mount paths.
- `release-metadata` validates the compatibility envelope plus AST symbols in
  every declared `code_paths` / `test_ids` entry.
[Timeouts & Locks]
- Local JSON writes are immediate; doctor/update/sharing inherit each delegated
  harness budget and SQLite busy timeout.
[Side-Effect Policy]
- Writes `wb.json` and `wb.engine.json` on `init`; delegates all other mutations
  to the owning harness or installer module.
"""
from __future__ import annotations
import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from wb_install import install_workbuddy_plugin
PLUGIN_ROOT = _HERE.parent
REPO_ROOT = PLUGIN_ROOT.parents[1] if (PLUGIN_ROOT.parents[1] / "pyproject.toml").is_file() else PLUGIN_ROOT
def _home(kind: str) -> Path:
    explicit, home = os.environ.get(f"MINDIE_{kind.upper()}_HOME"), Path(os.environ.get("USERPROFILE") or Path.home())
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA" if kind == "config" else "LOCALAPPDATA") or home / "AppData" / ("Roaming" if kind == "config" else "Local")) / "mindie-agent"
    return Path(os.environ.get("XDG_CONFIG_HOME" if kind == "config" else "XDG_DATA_HOME") or home / (".config" if kind == "config" else ".local/share")) / "mindie-agent"
def _config(config=None) -> Path:
    return Path(config).expanduser() if config else Path(os.environ.get("MINDIE_AGENT_CONFIG") or _home("config") / "wb.json")
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
def _gitleaks() -> str | None:
    found = shutil.which("gitleaks")
    if found:
        return found
    caches = [Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"), Path.home() / ".cache"]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        caches.append(Path(os.environ["LOCALAPPDATA"]))
    for cache in caches:
        directory = cache / "mindie-agent" / "gitleaks"
        if directory.is_dir():
            candidate = next((path for path in sorted(directory.rglob("gitleaks*")) if path.is_file() and path.name.lower() in {"gitleaks", "gitleaks.exe"}), None)
            if candidate is not None:
                return str(candidate.resolve())
    return None
def _contracts() -> dict:
    native = _read_json(REPO_ROOT / "product-contract.json").get("native_version") or _read_json(PLUGIN_ROOT / "plugin.json").get("version")
    return {"schema": "mindie-candidate-check/1", "status": "validated" if native else "unverified", "harness": "workbuddy", "native_version": native}
def ensure_profile(*, config=None, workspace_root=None, domain: str | None = "vllm-ascend", python_exe=None) -> dict:
    """Write the WorkBuddy adapter and engine profile documents."""
    cfg_file, engine_file, data = _config(config), _config(config).with_name("wb.engine.json"), _home("data")
    state, previous, prev_engine = data / "state" / "wb", _read_json(_config(config)), _read_json(engine_file)
    chosen_domain = domain or previous.get("domain") or prev_engine.get("domain") or "vllm-ascend"
    recorded = previous.get("python")
    interpreter = python_exe or (recorded if isinstance(recorded, str) and Path(recorded).is_file() else sys.executable)
    validation = _contracts()
    feeds = prev_engine.get("feeds") if domain is None and isinstance(prev_engine.get("feeds"), list) else [{"repository": "mindie-agent/knowledge-vllm-ascend", "ref": "main", "domain": chosen_domain, "interval_seconds": 300}]
    engine = dict(prev_engine, schema="mindie-engine-config/1", harness="wb", domain=chosen_domain, root=str(data / "domains"),
                  state_dir=str(state / "engine"), admission_path=str(state / "admission.sqlite3"),
                  community_config=str(cfg_file.with_name("mindie-community.json")), settings_path=str(cfg_file.with_name("mindie-community.json")),
                  capture_mode="public-transcript", transcript_adapter=str(PLUGIN_ROOT / "scripts" / "wb_transcript.py"), redactor_executable=_gitleaks(),
                  feeds=feeds, product_validation=validation)
    adapter = dict(previous, schema="mindie-adapter-config/1", harness="workbuddy", python=str(interpreter), runtime_scripts=str(PLUGIN_ROOT / "scripts"),
                   source_root=str(REPO_ROOT), engine_config=str(engine_file), state_dir=str(state), admission_path=str(state / "admission.sqlite3"),
                   domain=chosen_domain, product_validation=validation)
    _atomic_json(engine_file, engine)
    _atomic_json(cfg_file, adapter)
    return adapter
def _profile(config=None):
    from mindie_harness.paths import ProfileRef
    return ProfileRef.resolve("wb", plugin_root=PLUGIN_ROOT, config_path=_config(config))
def _emit(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0
def _dry(cmd: str | None, install: bool) -> dict:
    return {"schema": "mindie-wb-setup/1", "status": "dry-run", "bootstrap": False, "cmd": cmd, "uv": shutil.which("uv"), "gh": shutil.which("gh"),
            "install_workbuddy_plugin": install, "registers_native_plugin": install,
            "registers_connector_if_marketplace_present": install, "profile_only": cmd in (None, "init") and not install}
def _symbol_exists(path: Path, symbol: str | None) -> bool:
    if not path.is_file():
        return False
    if not symbol:
        return True
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except TimeoutError:
        raise
    except (OSError, SyntaxError):
        return False
    return symbol in {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
def release_metadata(path: str | Path | None = None) -> dict:
    compatibility = _read_json(Path(path) if path else REPO_ROOT / "harness-compatibility.json")
    from mindie_knowledge.releases import validate_compatibility
    validate_compatibility(compatibility, harness_name="workbuddy")
    errors = []
    for interface, entry in (compatibility.get("interface_registry") or {}).items():
        for reference in entry.get("code_paths") or []:
            target, _, symbol = reference.partition(":")
            errors.append(f"{interface}: missing code path {reference}") if not _symbol_exists(REPO_ROOT / target, symbol or None) else None
        for reference in entry.get("test_ids") or []:
            target, _, symbol = reference.partition("::")
            errors.append(f"{interface}: missing test {reference}") if not _symbol_exists(REPO_ROOT / target, symbol or None) else None
    if errors:
        raise ValueError("; ".join(errors))
    return {"schema": "mindie-harness-compatibility/1", "status": "validated", "harness": "workbuddy", "interfaces": sorted(compatibility.get("interface_registry") or {})}
def _add(sub, name: str):
    parser = sub.add_parser(name)
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="Print the plan only; never write.")
    return parser
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure MindIE Agent for Tencent WorkBuddy")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan only; never write.")
    sub = parser.add_subparsers(dest="cmd")
    init = _add(sub, "init"); init.add_argument("--config"); init.add_argument("--workspace-root"); init.add_argument("--domain", default="vllm-ascend"); init.add_argument("--install-wb", action="store_true"); init.add_argument("--wb-dir")
    install = _add(sub, "install-wb"); install.add_argument("--config"); install.add_argument("--wb-dir")
    sharing = _add(sub, "sharing"); sharing.add_argument("--config")
    group = sharing.add_mutually_exclusive_group(required=False); group.add_argument("--status", action="store_true"); group.add_argument("--enable", action="store_true"); group.add_argument("--local-only", action="store_true"); group.add_argument("--disable", action="store_true")
    sharing.add_argument("--project-root", action="append", default=[]); sharing.add_argument("--repository"); sharing.add_argument("--fork"); sharing.add_argument("--account")
    doctor = _add(sub, "doctor"); doctor.add_argument("--config"); doctor.add_argument("--probe-remote", action="store_true"); doctor.add_argument("--cwd"); doctor.add_argument("--timeout", type=float)
    update = _add(sub, "update"); update.add_argument("--config"); update.add_argument("--force", action="store_true"); update.add_argument("--offline", action="store_true"); update.add_argument("--channel-base-url"); update.add_argument("--background", action="store_true")
    release = _add(sub, "release-metadata"); release.add_argument("--path")
    return parser
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    cmd = args.cmd or "init"
    if cmd == "sharing" and not (args.status or args.enable or args.local_only or args.disable):
        parser.error("sharing requires one of --status, --enable, --local-only, or --disable")
    if getattr(args, "dry_run", False):
        return _emit(_dry(None if args.cmd is None else cmd, cmd == "install-wb" or (cmd == "init" and bool(getattr(args, "install_wb", False)))))
    if cmd == "init":
        profile = ensure_profile(config=getattr(args, "config", None), workspace_root=getattr(args, "workspace_root", None), domain=getattr(args, "domain", "vllm-ascend"))
        if getattr(args, "install_wb", False):
            profile["workbuddy_install"] = install_workbuddy_plugin(wb_dir=getattr(args, "wb_dir", None), config=getattr(args, "config", None), source_root=profile.get("source_root"))
        return _emit(profile)
    if cmd == "install-wb":
        profile = ensure_profile(config=args.config, domain=None)
        return _emit(install_workbuddy_plugin(wb_dir=args.wb_dir, config=args.config, source_root=profile.get("source_root")))
    if cmd == "sharing":
        from mindie_harness.sharing import configure_sharing, sharing_status
        profile, choice = _profile(args.config), "contribute" if args.enable else ("local-only" if args.local_only else "disabled")
        return _emit(sharing_status(profile) if args.status else configure_sharing(profile, choice=choice, project_roots=args.project_root, repository=args.repository, fork=args.fork, account=args.account))
    if cmd == "doctor":
        from mindie_harness.api import run_host
        extra = ["--probe-remote"] if args.probe_remote else []
        extra += ["--cwd", args.cwd] if args.cwd else []
        extra += ["--timeout", str(args.timeout)] if args.timeout is not None else []
        return int(run_host(["doctor", "--host", "wb", "--plugin-root", str(PLUGIN_ROOT), *(["--config", args.config] if args.config else []), *extra]))
    if cmd == "update":
        from mindie_harness.runtime.wheelhouse import update_runtime
        return _emit(update_runtime(_profile(args.config), force=args.force, offline=args.offline, channel_base_url=args.channel_base_url, background=args.background))
    if cmd == "release-metadata":
        return _emit(release_metadata(args.path))
    parser.error(f"unknown command: {cmd}")
    return 2
if __name__ == "__main__":
    raise SystemExit(main())
