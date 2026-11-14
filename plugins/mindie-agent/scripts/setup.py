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
- `install_workbuddy_plugin` runs only for `install-wb` and `init --install-wb`.
- `--help`, `-h`, and parsed `--dry-run` (including a unique abbreviation such as
  `--dry`) do not bootstrap a runtime or write config.

[Timeouts & Locks]
- Local config validation/writes are immediate; first-time `uv sync` runtime
  bootstrap is bounded by `BOOTSTRAP_TIMEOUT = 540.0s`. Interpreter re-exec uses
  `timeout=BOOTSTRAP_TIMEOUT + 15.0s`.

[Side-Effect Policy]
- Writes `wb.json`, `wb.engine.json`, `mindie-community.json`, and
  `mindie-consent.json` under `config_home()`, and stages `wb-source` /
  `wb-venv` under `data_home()` when bootstrapping.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run
from wb_native_compat import ensure_wb_native_support

ensure_wb_native_support()
from paths import (
    admission_path,
    apply_enterprise_transport_env,
    atomic_write_json,
    community_config_path,
    config_home,
    config_path,
    consent_path,
    data_home,
    engine_config_path,
    plugin_root,
    read_json,
    repo_root,
    state_dir,
    which_native,
)
from setup_sharing import (
    configure_community_impl,
    read_optional_json as _read_optional_spec,
    sharing_status as _sharing_status_impl,
)
from wb_install import install_workbuddy_plugin

BOOTSTRAP_TIMEOUT = 540.0
_TEXT_SUFFIXES = {".py", ".json", ".md", ".toml", ".svg", ".txt", ".yml", ".yaml"}
_BASELINE_ITEMS = ("pyproject.toml", "product-contract.json", "harness-compatibility.json", ".codebuddy-plugin", "plugins")


def _copy_lf_clean(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = src.read_bytes()
    if src.suffix in _TEXT_SUFFIXES and b"\r\n" in raw:
        raw = raw.replace(b"\r\n", b"\n")
    dst.write_bytes(raw)


def _discover_checkout_source(workspace_root: str | Path | None = None) -> Path | None:
    """Locate a complete `mindie-agent-wb` checkout for source baseline retention."""
    candidates: list[Path] = [HERE.parent.parent.parent, plugin_root().parent.parent]
    if workspace_root is not None:
        ws = Path(workspace_root).expanduser()
        candidates.extend([ws, ws / "mindie-agent-wb"])
    cwd = Path.cwd()
    candidates.extend([
        cwd, cwd / "mindie-agent-wb", repo_root(),
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "mindie-agent-wb-install",
        Path("/tmp/mindie-agent-wb-install"), data_home() / "wb-source",
    ])
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except TimeoutError:
            raise
        except OSError:
            continue
        if (resolved / "pyproject.toml").is_file() and (resolved / "product-contract.json").is_file():
            return resolved
    return None


def _payload_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
    return digest.hexdigest()


def stage_source_baseline(
    source_dir: str | Path | None = None,
    dest_dir: str | Path | None = None,
    *,
    workspace_root: str | Path | None = None,
) -> Path:
    """Publish an immutable payload generation before its profile can activate."""
    src = (Path(source_dir).resolve() if source_dir is not None
           else _discover_checkout_source(workspace_root=workspace_root))
    base = (data_home() / "wb-source").resolve()
    if src is None:
        return base if (base / "pyproject.toml").is_file() else repo_root()
    data_home().mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".source-stage-", dir=data_home()) as folder:
        staged = Path(folder)
        for item_name in _BASELINE_ITEMS:
            item = src / item_name
            if not item.exists():
                continue
            files = [item] if item.is_file() else sorted(item.rglob("*"))
            for path in files:
                if (path.is_file() and path.suffix != ".pyc"
                        and not any(part in {"__pycache__", ".pytest_cache", ".git", ".venv"}
                                    for part in path.relative_to(src).parts)):
                    _copy_lf_clean(path, staged / path.relative_to(src))
        digest = _payload_digest(staged)
        if dest_dir is not None:
            dst = Path(dest_dir).resolve()
            dst.mkdir(parents=True, exist_ok=True)
            for path in staged.rglob("*"):
                if path.is_file():
                    _copy_lf_clean(path, dst / path.relative_to(staged))
            return dst
        else:
            version = read_json(staged / "plugins/mindie-agent/plugin.json")["version"]
            if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise ValueError("plugin payload requires a formal version")
            dst = base / f"{version}-{digest}"
        if dst.is_dir():
            if _payload_digest(dst) != digest:
                raise ValueError("retained plugin payload content changed")
            return dst
        dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.replace(staged, dst)
        except TimeoutError:
            raise
        except OSError:
            if not dst.is_dir() or _payload_digest(dst) != digest:
                raise
        return dst


def _probe_runtime_python(py_exe: str | Path) -> bool:
    cand = Path(py_exe)
    if not cand.is_file():
        return False
    probe = bounded_run(
        [
            str(cand),
            "-I",
            "-X",
            "utf8",
            "-c",
            "import mindie_knowledge, remote_dev, mindie_diagnostics, mindie_profiler, mindie_sim, mindie_coordinator, mindie_debugger, npu_top",
        ],
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
    env_py = os.environ.get("MINDIE_WB_PYTHON")
    if isinstance(env_py, str) and env_py.strip():
        candidates.append(Path(env_py.strip()))
    venv_dir = data_home() / "wb-venv"
    candidates.append(venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    agy_venv = data_home() / "agy-venv"
    candidates.append(agy_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

    search_dirs = [config_path().parent]
    for extra in (config_home(), Path.home() / ".config" / "mindie-agent"):
        if extra not in search_dirs:
            search_dirs.append(extra)

    for cfg_dir in search_dirs:
        for cfg_name in ("wb.json", "codex.json", "agy.json", "cc.json", "kimi.json"):
            cfg_cand = cfg_dir / cfg_name
            if cfg_cand.is_file():
                try:
                    py = read_json(cfg_cand).get("python")
                    if isinstance(py, str):
                        norm = py.replace("\\", "/")
                        if ("venv" in norm or ".venv" in norm) and "/uv/python/" not in norm:
                            candidates.append(Path(py))
                except ValueError:
                    pass

    for cand in candidates:
        if _probe_runtime_python(cand):
            resolved = _norm_python_path(cand)
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
    config: str | Path | None = None,
    timeout: float = BOOTSTRAP_TIMEOUT,
) -> str:
    """Return an interpreter with MindIE runtime components installed, bootstrapping `wb-venv` if needed."""
    from component_update import compatible_runtime

    def verified(interpreter):
        return compatible_runtime(str(interpreter), source=source_root or _discover_checkout_source() or repo_root(config),
                                  config=config, timeout=timeout)

    if python_exe:
        return verified(_norm_python_path(python_exe))
    if sys.prefix != sys.base_prefix and _current_python_has_runtime():
        resolved = _norm_python_path(sys.executable)
        return verified(resolved)

    existing = _discover_existing_mindie_python()
    if existing is not None:
        return verified(existing)

    if _current_python_has_runtime():
        resolved = _norm_python_path(sys.executable)
        return verified(resolved)

    venv_dir = data_home() / "wb-venv"
    venv_py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    src_root = stage_source_baseline(source_root)
    uv = which_native("uv")
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
    return verified(resolved)


def validate_contracts() -> dict:
    root = _discover_checkout_source() or repo_root()
    contract_file = root / "product-contract.json"
    compat_file = root / "harness-compatibility.json"
    contract = read_json(contract_file)
    compat = read_json(compat_file)
    return dict(
        schema="mindie-candidate-check/1",
        status="validated",
        harness=compat.get("harness", "workbuddy"),
        native_version=contract["native_version"],
    )


def _write_initial_community_config(interpreter: str, comm_file: Path, payload: dict) -> None:
    if importlib.util.find_spec("mindie_knowledge") is not None:
        from mindie_knowledge.loop import settings as settings_mod

        with settings_mod.CommunityWriteContext(comm_file) as ctx:
            ctx.configure(comm_file, payload)
        return
    proc = bounded_run(
        [
            interpreter,
            "-I",
            "-X",
            "utf8",
            "-c",
            (
                "import json, sys; from pathlib import Path; "
                "from mindie_knowledge.loop import settings; "
                "p = Path(sys.argv[1]); data = json.loads(sys.argv[2]); "
                "ctx = settings.CommunityWriteContext(p); "
                "ctx.__enter__(); "
                "ctx.configure(p, data); "
                "ctx.__exit__(None, None, None)"
            ),
            str(comm_file),
            json.dumps(payload),
        ],
        "",
        timeout=30.0,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to initialize community config via {interpreter}: {proc.stderr or proc.stdout}")


def _discover_gitleaks() -> str | None:
    found = which_native("gitleaks")
    if found:
        return found
    expected_name = "gitleaks.exe" if os.name == "nt" else "gitleaks"
    cache_dirs = [Path.home() / ".cache" / "mindie-agent" / "gitleaks"]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        cache_dirs.append(Path(os.environ["LOCALAPPDATA"]) / "mindie-agent" / "gitleaks")
    for cache_dir in cache_dirs:
        if cache_dir.is_dir():
            for candidate in sorted(cache_dir.rglob("gitleaks*"), reverse=True):
                if candidate.is_file() and candidate.name.lower() == expected_name:
                    return str(candidate.resolve())
    return None


def ensure_default_profile(
    *,
    config: str | Path | None = None,
    workspace_root: str | Path | None = None,
    domain: str = "vllm-ascend",
    python_exe: str | None = None,
) -> dict:
    apply_enterprise_transport_env()
    interpreter = ensure_runtime_python(python_exe=python_exe, config=config)

    cfg_file = config_path(config)
    eng_file = engine_config_path(cfg_file)
    sdir = state_dir(cfg_file)
    adm_file = admission_path(cfg_file)
    comm_file = community_config_path(cfg_file)
    data_root = data_home() / "domains"
    sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    resolved_source = stage_source_baseline(workspace_root=workspace_root)
    scripts_dir = (resolved_source / "plugins/mindie-agent/scripts").resolve()

    validation = validate_contracts()
    gitleaks = _discover_gitleaks()

    cfile = consent_path(cfg_file)
    engine_payload = dict(read_json(eng_file) if eng_file.is_file() else {},
        schema="mindie-engine-config/1",
        harness="wb",
        domain=domain,
        root=str(data_root),
        state_dir=str(sdir / "engine"),
        admission_path=str(adm_file),
        community_config=str(comm_file),
        settings_path=str(comm_file),
        transcript_adapter=str(scripts_dir / "wb_transcript.py"),
        summary_command=[interpreter, "-I", "-X", "utf8", str(scripts_dir / "organizer.py")],
        redactor_executable=gitleaks,
        feeds=[{"repository": "mindie-agent/knowledge-vllm-ascend", "ref": "main", "domain": domain, "interval_seconds": 300}],
        product_validation=validation,
    )
    atomic_write_json(eng_file, engine_payload)

    adapter_payload = dict(read_json(cfg_file) if cfg_file.is_file() else {},
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
        _write_initial_community_config(
            interpreter,
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


def _read_optional_json(path: Path) -> tuple[str, dict]:
    return _read_optional_spec(path, read_json_fn=read_json)


def sharing_status(config: str | Path | None = None) -> dict:
    return _sharing_status_impl(config, read_json_fn=read_json)


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
    return configure_community_impl(
        ensure_profile_fn=ensure_default_profile,
        ensure_python_fn=ensure_runtime_python,
        setup_script=Path(__file__).resolve(),
        config=config,
        enabled=enabled,
        fork=fork,
        account=account,
        project_root=project_root,
        remote_project_root=remote_project_root,
        visibility=visibility,
        repository=repository,
    )


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


def _emit_json(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _dry_plan(cmd: str | None, install: bool) -> dict:
    """Read-only description of the command. Does not stage a runtime or write config."""
    return {
        "schema": "mindie-wb-setup/1",
        "status": "dry-run",
        "bootstrap": False,
        "cmd": cmd,
        "uv": which_native("uv"),
        "gh": which_native("gh"),
        "install_workbuddy_plugin": install,
        "registers_native_plugin": install,
        "registers_connector_if_marketplace_present": install,
        "profile_only": cmd in (None, "init") and not install,
    }


def _add_dry_run(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS if suppress_default else False,
        help="Print the plan only; do not bootstrap a runtime or write config",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure MindIE Agent for Tencent WorkBuddy")
    _add_dry_run(parser)
    sub = parser.add_subparsers(dest="cmd", required=False)

    init_p = sub.add_parser("init")
    _add_dry_run(init_p, suppress_default=True)
    init_p.add_argument("--config", default=None)
    init_p.add_argument("--workspace-root", default=None)
    init_p.add_argument("--domain", default="vllm-ascend")
    init_p.add_argument(
        "--install-wb",
        action="store_true",
        help="Call install_workbuddy_plugin to mount the Connector and native plugin",
    )
    init_p.add_argument("--wb-dir", default=None, help="Explicit ~/.workbuddy directory")

    inst_p = sub.add_parser("install-wb", help="Mount the Connector and native plugin via install_workbuddy_plugin")
    _add_dry_run(inst_p, suppress_default=True)
    inst_p.add_argument("--config", default=None)
    inst_p.add_argument("--wb-dir", default=None)

    share_p = sub.add_parser("sharing")
    _add_dry_run(share_p, suppress_default=True)
    share_p.add_argument("--config", default=None)
    mode = share_p.add_mutually_exclusive_group(required=False)
    mode.add_argument("--status", action="store_true", help="Print read-only sharing status JSON")
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    share_p.add_argument("--fork", default=None)
    share_p.add_argument("--account", default=None)
    share_p.add_argument("--project-root", default=None)
    share_p.add_argument("--remote-project-root", default=None)
    share_p.add_argument("--visibility", default="public")
    share_p.add_argument("--repository", default="mindie-agent/knowledge-vllm-ascend")
    return parser


def _parsed_dry_run(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "dry_run", False))


def _dry_install(args: argparse.Namespace) -> bool:
    if args.cmd == "install-wb":
        return True
    return args.cmd == "init" and bool(getattr(args, "install_wb", False))


def main(argv: list[str] | None = None) -> int:
    incoming = list(sys.argv[1:] if argv is None else argv)
    if not incoming:
        incoming = ["init"]

    parser = _build_parser()
    args = parser.parse_args(incoming)
    if args.cmd == "sharing" and not (args.status or args.enable or args.disable):
        parser.error("sharing requires one of --status, --enable, or --disable")
    if _parsed_dry_run(args):
        return _emit_json(_dry_plan(args.cmd, _dry_install(args)))
    if args.cmd not in {"init", "install-wb", "sharing"}:
        parser.error("a command is required: init | install-wb | sharing")
    if args.cmd == "sharing" and args.status:
        return _emit_json(sharing_status(args.config))

    apply_enterprise_transport_env()
    if (
        importlib.util.find_spec("mindie_knowledge") is None
        and os.environ.get("_MINDIE_WB_SETUP_BOOTSTRAPPED") != "1"
    ):
        source = _discover_checkout_source() or repo_root(args.config)
        target_py = ensure_runtime_python(source, config=args.config)
        staged_src = stage_source_baseline(source)
        env = apply_enterprise_transport_env(dict(os.environ))
        env["_MINDIE_WB_SETUP_BOOTSTRAPPED"] = "1"
        env["MINDIE_WB_SOURCE_ROOT"] = str(staged_src)
        run_kwargs: dict = {"env": env}
        if os.name == "nt":
            run_kwargs.update(
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=_reexec_stdio(sys.stdin),
                stdout=_reexec_stdio(sys.stdout),
                stderr=_reexec_stdio(sys.stderr),
            )
        proc = subprocess.run(
            [target_py, "-I", "-X", "utf8", str(Path(__file__).resolve()), *incoming],
            timeout=BOOTSTRAP_TIMEOUT + 15.0,
            **run_kwargs,
        )
        return int(proc.returncode)

    if args.cmd == "init":
        result = ensure_default_profile(
            config=args.config,
            workspace_root=args.workspace_root,
            domain=args.domain,
        )
        if args.install_wb:
            result["workbuddy_install"] = install_workbuddy_plugin(
                wb_dir=args.wb_dir,
                config=args.config,
                source_root=result.get("source_root"),
            )
    elif args.cmd == "install-wb":
        prof = ensure_default_profile(config=args.config)
        result = install_workbuddy_plugin(
            wb_dir=args.wb_dir,
            config=args.config,
            source_root=prof.get("source_root"),
        )
    elif args.cmd == "sharing":
        result = configure_community(
            config=args.config,
            enabled=bool(args.enable),
            fork=args.fork,
            account=args.account,
            project_root=args.project_root,
            remote_project_root=args.remote_project_root,
            visibility=args.visibility,
            repository=args.repository,
        )
    code = _emit_json(result)
    if isinstance(result, dict) and result.get("reporting_error"):
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
