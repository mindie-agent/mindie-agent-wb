"""MindIE Agent profile provisioner, runtime bootstrapper, and WorkBuddy installer (`setup.py`).

[Role]
Initializes and updates the MindIE-owned Tencent WorkBuddy adapter profile
(`wb.json`, `wb.engine.json`, `mindie-community.json`, `mindie-consent.json`),
discovers or bootstraps the persistent component virtualenv (`wb-venv`), and
registers the plugin & connector in `~/.workbuddy/plugins/` (`install-wb`).

[Invariants]
- Passive Default: `ensure_default_profile` and `configure_community` write
  MindIE-owned JSON configs atomically via `os.replace()` and never activate a
  session lease or start the background knowledge loop.
- `install_workbuddy_plugin` only updates WorkBuddy plugin registries when
  explicitly requested (or during `init --install-wb`).

[Timeouts & Locks]
- Local config validation/writes are immediate; first-time `uv sync` runtime
  bootstrap is bounded by `BOOTSTRAP_TIMEOUT = 540.0s`.

[Side-Effect Policy]
- Writes `wb.json`, `wb.engine.json`, `mindie-community.json`, and
  `mindie-consent.json` under `config_home()`, and stages `wb-source` /
  `wb-venv` under `data_home()` when bootstrapping.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run
from paths import (
    admission_path,
    apply_enterprise_transport_env,
    atomic_write_json,
    community_config_path,
    config_path,
    consent_path,
    data_home,
    engine_config_path,
    plugin_root,
    read_json,
    repo_root,
    state_dir,
    workbuddy_config_dir,
)

BOOTSTRAP_TIMEOUT = 540.0
_TEXT_SUFFIXES = {".py", ".json", ".md", ".toml", ".svg", ".txt", ".yml", ".yaml"}
_BASELINE_ITEMS = (
    "pyproject.toml",
    "product-contract.json",
    "harness-compatibility.json",
    ".codebuddy-plugin",
    "plugins",
)


def _discover_checkout_source() -> Path | None:
    """Locate a complete `mindie-agent-wb` checkout for source baseline retention."""
    candidates = [
        plugin_root().parent.parent,
        repo_root(),
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "mindie-agent-wb-install",
        Path("/tmp/mindie-agent-wb-install"),
        data_home() / "wb-source",
    ]
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if (resolved / "pyproject.toml").is_file() and (resolved / "product-contract.json").is_file():
            return resolved
    return None


def _copy_lf_clean(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = src.read_bytes()
    if src.suffix in _TEXT_SUFFIXES and b"\r\n" in raw:
        raw = raw.replace(b"\r\n", b"\n")
    dst.write_bytes(raw)


def stage_source_baseline(source_dir: str | Path | None = None, dest_dir: str | Path | None = None) -> Path:
    """Persist an LF-normalized adapter source baseline under `data_home()/wb-source`."""
    src = Path(source_dir).resolve() if source_dir is not None else _discover_checkout_source()
    dst = Path(dest_dir).resolve() if dest_dir is not None else (data_home() / "wb-source").resolve()
    if src is None:
        return dst if (dst / "pyproject.toml").is_file() else repo_root()
    if src == dst:
        for fpath in dst.rglob("*"):
            if fpath.is_file() and fpath.suffix in _TEXT_SUFFIXES and ".git" not in fpath.parts:
                raw = fpath.read_bytes()
                if b"\r\n" in raw:
                    fpath.write_bytes(raw.replace(b"\r\n", b"\n"))
        return dst

    dst.mkdir(mode=0o700, parents=True, exist_ok=True)
    for item_name in _BASELINE_ITEMS:
        item_src = src / item_name
        if not item_src.exists():
            continue
        item_dst = dst / item_name
        if item_src.is_file():
            _copy_lf_clean(item_src, item_dst)
        else:
            for sub in item_src.rglob("*"):
                if any(part in {"__pycache__", ".pytest_cache", ".git", ".venv"} for part in sub.parts):
                    continue
                if sub.is_file() and sub.suffix != ".pyc":
                    _copy_lf_clean(sub, item_dst / sub.relative_to(item_src))
    return dst


def _attach_local_component_paths(py_exe: Path) -> None:
    """Ensure `npu_top` and `mindie_debugger` sibling checkouts are importable in the runtime venv."""
    try:
        probe = bounded_run(
            [
                str(py_exe),
                "-I",
                "-c",
                "import site; print(site.getsitepackages()[0])",
            ],
            "",
            timeout=10.0,
        )
        if probe.returncode != 0:
            return
        site_dir = Path(probe.stdout.strip())
        if not site_dir.is_dir():
            return
        code_root = plugin_root().parent.parent.parent
        extra_paths: list[str] = []
        for sibling, pkg in (("debugger", "mindie_debugger"), ("npu-top", "npu_top")):
            cand = code_root / sibling
            if (cand / pkg).is_dir():
                extra_paths.append(str(cand.resolve()))
        if extra_paths:
            pth_file = site_dir / "mindie_wb_extra_components.pth"
            pth_file.write_text("\n".join(extra_paths) + "\n", encoding="utf-8")
    except Exception:
        pass


def _probe_runtime_python(py_exe: str | Path) -> bool:
    cand = Path(py_exe)
    if not cand.is_file():
        return False
    probe = bounded_run(
        [str(cand), "-I", "-c", "import mindie_knowledge, remote_dev, mindie_diagnostics"],
        "",
        timeout=15.0,
    )
    return probe.returncode == 0


def _norm_python_path(py_exe: str | Path) -> Path:
    """Normalize a Python executable path while preserving virtualenv `bin/python` symlinks."""
    p = Path(py_exe).expanduser()
    return p.parent.resolve() / p.name


def _discover_existing_mindie_python() -> str | None:
    """Check existing MindIE virtualenvs on the host before running a full network `uv sync`."""
    candidates: list[Path] = []
    venv_dir = data_home() / "wb-venv"
    candidates.append(venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    agy_venv = data_home() / "agy-venv"
    candidates.append(agy_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

    search_dirs = [config_path().parent]
    host_cfg_dir = Path.home() / ".config" / "mindie-agent"
    if host_cfg_dir not in search_dirs:
        search_dirs.append(host_cfg_dir)

    for cfg_dir in search_dirs:
        for cfg_name in ("wb.json", "codex.json", "agy.json", "cc.json", "kimi.json"):
            cfg_cand = cfg_dir / cfg_name
            if cfg_cand.is_file():
                try:
                    py = read_json(cfg_cand).get("python")
                    if isinstance(py, str) and ".venv" in py:
                        candidates.append(Path(py))
                except ValueError:
                    pass

    for cand in candidates:
        if _probe_runtime_python(cand):
            resolved = _norm_python_path(cand)
            _attach_local_component_paths(resolved)
            return str(resolved)
    return None


def _current_python_has_runtime() -> bool:
    return all(
        importlib.util.find_spec(mod) is not None
        for mod in ("mindie_knowledge", "remote_dev", "mindie_diagnostics")
    )


def ensure_runtime_python(
    source_root: str | Path | None = None,
    python_exe: str | None = None,
    *,
    timeout: float = BOOTSTRAP_TIMEOUT,
) -> str:
    """Return an interpreter with MindIE runtime components installed, bootstrapping `wb-venv` if needed."""
    if python_exe:
        resolved = _norm_python_path(python_exe)
        _attach_local_component_paths(resolved)
        return str(resolved)
    if sys.prefix != sys.base_prefix and _current_python_has_runtime():
        resolved = _norm_python_path(sys.executable)
        _attach_local_component_paths(resolved)
        return str(resolved)

    existing = _discover_existing_mindie_python()
    if existing is not None:
        return existing

    if _current_python_has_runtime():
        resolved = _norm_python_path(sys.executable)
        _attach_local_component_paths(resolved)
        return str(resolved)

    venv_dir = data_home() / "wb-venv"
    venv_py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    src_root = stage_source_baseline(source_root)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to bootstrap the MindIE WorkBuddy runtime")

    env = apply_enterprise_transport_env(dict(os.environ))
    env["UV_PROJECT_ENVIRONMENT"] = str(venv_dir)
    proc = bounded_run(
        [
            uv,
            "sync",
            "--project",
            str(src_root),
            "--all-extras",
            "--no-dev",
            "--python",
            "3.13",
        ],
        "",
        timeout=timeout,
        max_output=256 * 1024,
        env=env,
    )
    if proc.returncode != 0 or not venv_py.is_file():
        detail = (proc.stderr or proc.stdout or "").strip()[-1000:]
        raise RuntimeError(f"uv sync failed while bootstrapping wb-venv (exit {proc.returncode}): {detail}")
    resolved = _norm_python_path(venv_py)
    _attach_local_component_paths(resolved)
    return str(resolved)


def validate_contracts() -> dict:
    root = repo_root()
    contract_file = root / "product-contract.json"
    compat_file = root / "harness-compatibility.json"
    contract = read_json(contract_file) if contract_file.is_file() else {"native_version": "0.1.0"}
    compat = read_json(compat_file) if compat_file.is_file() else {"harness": "workbuddy"}
    return dict(
        schema="mindie-candidate-check/1",
        status="validated",
        harness=compat.get("harness", "workbuddy"),
        native_version=contract.get("native_version", "0.1.0"),
    )


def ensure_default_profile(
    *,
    config: str | Path | None = None,
    workspace_root: str | Path | None = None,
    domain: str = "vllm-ascend",
    python_exe: str | None = None,
) -> dict:
    apply_enterprise_transport_env()
    interpreter = ensure_runtime_python(python_exe=python_exe)

    from mindie_knowledge.loop import settings as settings_mod

    cfg_file = config_path(config)
    eng_file = engine_config_path(cfg_file)
    sdir = state_dir(cfg_file)
    adm_file = admission_path(cfg_file)
    comm_file = community_config_path(cfg_file)
    scripts_dir = Path(__file__).resolve().parent
    data_root = data_home() / "domains" / domain
    sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    direct_root = plugin_root().parent.parent
    if not (direct_root / "pyproject.toml").is_file() or "mindie-agent-wb-install" in str(direct_root):
        resolved_source = stage_source_baseline()
    else:
        resolved_source = repo_root(cfg_file)

    validation = validate_contracts()
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        cache_dir = Path.home() / ".cache" / "mindie-agent" / "gitleaks"
        if cache_dir.is_dir():
            for candidate in sorted(cache_dir.rglob("gitleaks*"), reverse=True):
                if candidate.is_file() and candidate.name in {"gitleaks", "gitleaks.exe"}:
                    gitleaks = str(candidate.resolve())
                    break

    cfile = consent_path(cfg_file)
    engine_payload = dict(
        schema="mindie-engine-config/1",
        harness="wb",
        domain=domain,
        root=str(data_root),
        state_dir=str(sdir / "engine"),
        admission_path=str(adm_file),
        community_config=str(comm_file),
        settings_path=str(comm_file),
        transcript_adapter=str(scripts_dir / "wb_transcript.py"),
        summary_command=[interpreter, "-I", str(scripts_dir / "organizer.py")],
        redactor_executable=gitleaks,
        feeds=[
            {
                "repository": "mindie-agent/knowledge-vllm-ascend",
                "ref": "main",
                "domain": domain,
                "interval_seconds": 300,
            }
        ],
        product_validation=validation,
    )
    atomic_write_json(eng_file, engine_payload)

    adapter_payload = dict(
        schema="mindie-adapter-config/1",
        harness="workbuddy",
        python=interpreter,
        runtime_scripts=str(scripts_dir),
        source_root=str(resolved_source),
        engine_config=str(eng_file),
        state_dir=str(sdir),
        admission_path=str(adm_file),
        domain=domain,
        product_validation=validation,
    )
    atomic_write_json(cfg_file, adapter_payload)

    if not comm_file.is_file():
        roots = [str(Path(workspace_root).resolve())] if workspace_root else []
        with settings_mod.CommunityWriteContext(comm_file) as ctx:
            ctx.configure(
                comm_file,
                dict(
                    enabled=False,
                    repository="mindie-agent/knowledge-vllm-ascend",
                    project_roots=roots,
                    branch="main",
                    idle_seconds=300,
                    consent_config=str(cfile.resolve()),
                ),
            )

    return adapter_payload


def _copy_plugin_tree(src_plugin: Path, dst_plugin: Path) -> None:
    if dst_plugin.exists():
        shutil.rmtree(dst_plugin)
    dst_plugin.mkdir(parents=True, exist_ok=True)
    for sub in src_plugin.rglob("*"):
        if any(part in {"__pycache__", ".pytest_cache", ".git", ".venv"} for part in sub.parts):
            continue
        if sub.is_file() and sub.suffix != ".pyc":
            _copy_lf_clean(sub, dst_plugin / sub.relative_to(src_plugin))


def install_workbuddy_plugin(
    *,
    wb_dir: str | Path | None = None,
    config: str | Path | None = None,
) -> dict:
    """Install and register `mindie-agent` in `~/.workbuddy/plugins` (both native plugin cache & connector marketplace)."""
    wb_root = Path(wb_dir).expanduser().resolve() if wb_dir is not None else workbuddy_config_dir(config)
    plugins_dir = wb_root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)

    src_plugin = plugin_root()
    src_repo = src_plugin.parent.parent
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
        "source": {
            "source": "directory",
            "path": str(src_repo.resolve()),
        },
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
    plugins_map[plugin_id] = [
        {
            "scope": "user",
            "installPath": str(cache_dest.resolve()),
            "version": version,
            "installedAt": now_iso,
            "lastUpdated": now_iso,
        }
    ]
    atomic_write_json(installed_file, installed_data)

    # 4. Also register in workbuddy-connector-plugins-official marketplace if present
    connector_mp_root = plugins_dir / "marketplaces" / "workbuddy-connector-plugins-official"
    connector_dest = None
    if connector_mp_root.is_dir():
        connector_dest = connector_mp_root / "connectors" / "mindie-agent"
        _copy_plugin_tree(src_plugin, connector_dest)
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
                            "version": f"{version}+wb.mindie",
                        },
                    )
                    raw_mp["plugins"] = entries
                    atomic_write_json(mp_json_file, raw_mp)
            except (OSError, ValueError):
                pass

    return {
        "status": "installed",
        "plugin_id": plugin_id,
        "version": version,
        "cache_install_path": str(cache_dest.resolve()),
        "connector_path": str(connector_dest.resolve()) if connector_dest else None,
        "known_marketplaces": str(known_mp_file),
        "installed_plugins": str(installed_file),
    }


def configure_community(
    *,
    config: str | Path | None = None,
    enabled: bool,
    fork: str | None = None,
    account: str | None = None,
    project_root: str | Path | None = None,
    remote_project_root: str | None = None,
    visibility: str = "public",
    repository: str = "mindie-agent/knowledge-vllm-ascend",
) -> dict:
    from mindie_knowledge import consent_store
    from mindie_knowledge.loop import settings as settings_mod

    cfg_file = config_path(config)
    if not cfg_file.is_file():
        ensure_default_profile(config=cfg_file, workspace_root=project_root)
    comm_file = community_config_path(cfg_file)
    cfile = consent_path(cfg_file).resolve()
    consent_store.record_choice(cfile, "contribute" if enabled else "disabled")
    current = settings_mod.load(comm_file)
    roots = [str(p) for p in current.project_roots]
    if project_root is not None:
        resolved_root = str(Path(project_root).resolve())
        if resolved_root not in roots:
            roots.insert(0, resolved_root)
    resolved_fork = fork if fork is not None else current.raw.get("fork")
    resolved_account = (
        account
        if account is not None
        else current.raw.get("account")
        or (resolved_fork.split("/", 1)[0] if isinstance(resolved_fork, str) and "/" in resolved_fork else None)
    )
    updated = settings_mod.write(
        comm_file,
        enabled=bool(enabled),
        repository=repository,
        project_roots=roots,
        consent_config=str(cfile),
        fork=resolved_fork,
        account=resolved_account,
        visibility=visibility,
        remote_project_root=(
            remote_project_root
            if remote_project_root is not None
            else current.raw.get("remote_project_root")
        ),
    )
    return updated.as_dict()


def main(argv: list[str] | None = None) -> int:
    apply_enterprise_transport_env()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        raw_argv = ["init"]
    if (
        importlib.util.find_spec("mindie_knowledge") is None
        and os.environ.get("_MINDIE_WB_SETUP_BOOTSTRAPPED") != "1"
    ):
        staged_src = stage_source_baseline()
        target_py = ensure_runtime_python(staged_src)
        env = apply_enterprise_transport_env(dict(os.environ))
        env["_MINDIE_WB_SETUP_BOOTSTRAPPED"] = "1"
        env["MINDIE_WB_SOURCE_ROOT"] = str(staged_src)
        proc = subprocess.run([target_py, "-I", str(Path(__file__).resolve()), *raw_argv], env=env)
        return int(proc.returncode)

    parser = argparse.ArgumentParser(description="Configure MindIE Agent for Tencent WorkBuddy")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init_p = sub.add_parser("init")
    init_p.add_argument("--config", default=None)
    init_p.add_argument("--workspace-root", default=None)
    init_p.add_argument("--domain", default="vllm-ascend")
    init_p.add_argument("--install-wb", action="store_true", help="Also register plugin in ~/.workbuddy/plugins")
    init_p.add_argument("--wb-dir", default=None, help="Explicit ~/.workbuddy directory")

    inst_p = sub.add_parser("install-wb")
    inst_p.add_argument("--config", default=None)
    inst_p.add_argument("--wb-dir", default=None)

    share_p = sub.add_parser("sharing")
    share_p.add_argument("--config", default=None)
    share_p.add_argument("--enable", action="store_true")
    share_p.add_argument("--disable", action="store_true")
    share_p.add_argument("--fork", default=None)
    share_p.add_argument("--project-root", default=None)
    share_p.add_argument("--remote-project-root", default=None)
    share_p.add_argument("--visibility", default="public")

    args = parser.parse_args(raw_argv)
    if args.cmd == "init":
        result = ensure_default_profile(
            config=args.config,
            workspace_root=args.workspace_root,
            domain=args.domain,
        )
        if args.install_wb:
            result["workbuddy_install"] = install_workbuddy_plugin(wb_dir=args.wb_dir, config=args.config)
    elif args.cmd == "install-wb":
        ensure_default_profile(config=args.config)
        result = install_workbuddy_plugin(wb_dir=args.wb_dir, config=args.config)
    else:
        enabled = True if args.enable else False if args.disable else True
        result = configure_community(
            config=args.config,
            enabled=enabled,
            fork=args.fork,
            project_root=args.project_root,
            remote_project_root=args.remote_project_root,
            visibility=args.visibility,
        )
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
