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
  `--dry`) do not bootstrap a runtime or write config. Read-only is decided
  from parsed options before staging, bootstrap, or any profile write.

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
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run
from wb_native_compat import ensure_wb_native_support

ensure_wb_native_support()
from wb_install import _is_temp_checkout_path, install_workbuddy_plugin
from paths import (
    admission_path, apply_enterprise_transport_env, atomic_write_json,
    community_config_path, config_path, consent_path, data_home,
    diagnostics_config_path, engine_config_path, plugin_root, read_json,
    repo_root, state_dir,
)

BOOTSTRAP_TIMEOUT = 540.0
_REPO_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
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
            # Concurrent publication of the same content identity is complete.
        return dst


def _probe_runtime_python(py_exe: str | Path) -> bool:
    cand = Path(py_exe)
    if not cand.is_file():
        return False
    probe = bounded_run(
        [
            str(cand),
            "-I",
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
    scripts_dir = Path(__file__).resolve().parent
    data_root = data_home() / "domains"
    sdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    resolved_source = stage_source_baseline(workspace_root=workspace_root)
    scripts_dir = (resolved_source / "plugins/mindie-agent/scripts").resolve()

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
        summary_command=[interpreter, "-I", str(scripts_dir / "organizer.py")],
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
    if not path.exists():
        return "missing", {}
    try:
        return "ok", read_json(path)
    except FileNotFoundError:
        return "missing", {}
    except ValueError:
        return "corrupt", {}
    except OSError:
        return "unreadable", {}


def sharing_status(config: str | Path | None = None) -> dict:
    """Return current community sharing and fault reporting status using stdlib only."""
    cfg_file = config_path(config)
    consent_state, consent_data = _read_optional_json(consent_path(cfg_file))
    _, comm_data = _read_optional_json(community_config_path(cfg_file))
    _, diag_data = _read_optional_json(diagnostics_config_path(cfg_file))

    choice = consent_data.get("choice") if consent_state == "ok" else None
    if consent_state == "ok" and choice not in (None, "contribute", "read-only", "later", "disabled"):
        consent_state, choice = "corrupt", None
    choice_revision = consent_data.get("choice_revision") if consent_state == "ok" else None
    repair_required = consent_state in {"corrupt", "unreadable"}
    sharing_enabled = bool(comm_data.get("enabled", False)) and choice == "contribute"
    reporting_sub = diag_data.get("reporting") if isinstance(diag_data.get("reporting"), dict) else {}
    reporting_enabled = bool(
        reporting_sub.get("enabled", False) or diag_data.get("decision") == "enabled" or diag_data.get("enabled") is True
    )
    return {
        "schema": "mindie-sharing-status/1",
        "harness": "workbuddy",
        "consent_state": consent_state,
        "consent_choice": choice,
        "choice_revision": choice_revision,
        "sharing_enabled": sharing_enabled,
        "community_enabled": sharing_enabled,
        "reporting_enabled": reporting_enabled,
        "repository": comm_data.get("repository", "mindie-agent/knowledge-vllm-ascend"),
        "fork": comm_data.get("fork"),
        "account": comm_data.get("account"),
        "project_roots": list(comm_data.get("project_roots") or []),
        "needs_onboarding_prompt": (not repair_required) and (choice not in {"contribute", "disabled"}),
        "repair_required": repair_required,
    }


def _validate_sharing_inputs(
    *, repository: str, visibility: str, account: str | None, fork: str | None, project_root: str | Path | None
) -> Path | None:
    if not isinstance(repository, str) or not _REPO_SLUG_RE.fullmatch(repository):
        raise ValueError("invalid repository format; expected owner/repo")
    if visibility != "public":
        raise ValueError("community sharing requires visibility='public'")
    if account is not None and (
        not isinstance(account, str) or not account.strip() or "/" in account or any(c.isspace() for c in account)
    ):
        raise ValueError("invalid GitHub account name")
    if fork is not None and (not isinstance(fork, str) or not _REPO_SLUG_RE.fullmatch(fork)):
        raise ValueError("invalid fork format; expected owner/repo")
    return Path(project_root).expanduser().resolve() if project_root is not None else None


def _resolve_github_fork(repository: str, account: str | None) -> tuple[str | None, str | None]:
    gh = shutil.which("gh")
    if not gh:
        return account, None
    env = apply_enterprise_transport_env(dict(os.environ))
    resolved_account = account
    if not resolved_account:
        proc = bounded_run([gh, "api", "--hostname", "github.com", "user", "--jq", ".login"], "", timeout=10.0, env=env)
        login = (proc.stdout or "").strip()
        if proc.returncode == 0 and login and "/" not in login and not any(c.isspace() for c in login):
            resolved_account = login
    if not resolved_account:
        return None, None
    candidate_fork = f"{resolved_account}/{repository.split('/', 1)[1]}"
    probe = bounded_run(
        [gh, "api", "--hostname", "github.com", f"repos/{candidate_fork}", "--jq", ".full_name"], "", timeout=10.0, env=env
    )
    if probe.returncode == 0 and (probe.stdout or "").strip():
        return resolved_account, candidate_fork
    fork_proc = bounded_run([gh, "repo", "fork", repository, "--clone=false"], "", timeout=15.0, env=env)
    return resolved_account, (candidate_fork if fork_proc.returncode == 0 else None)


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
    resolved_root_path = _validate_sharing_inputs(
        repository=repository, visibility=visibility, account=account, fork=fork, project_root=project_root
    )
    cfg_file = config_path(config)
    if not cfg_file.is_file():
        ensure_default_profile(config=cfg_file, workspace_root=resolved_root_path)
    if importlib.util.find_spec("mindie_knowledge") is None:
        interpreter = read_json(cfg_file).get("python") or ensure_runtime_python()
        argv = [str(interpreter), "-I", str(Path(__file__).resolve()), "sharing", "--config", str(cfg_file)]
        argv.append("--enable" if enabled else "--disable")
        for flag, val in (
            ("--fork", fork),
            ("--account", account),
            ("--project-root", str(resolved_root_path) if resolved_root_path is not None else None),
            ("--remote-project-root", remote_project_root),
            ("--visibility", visibility),
            ("--repository", repository),
        ):
            if val:
                argv.extend([flag, val])
        proc = bounded_run(argv, "", timeout=30.0)
        if proc.returncode != 0:
            raise RuntimeError(f"failed to configure community sharing via {interpreter}: {proc.stderr or proc.stdout}")
        return json.loads(proc.stdout.strip())

    from mindie_knowledge import consent_store
    from mindie_knowledge.loop import settings as settings_mod

    comm_file = community_config_path(cfg_file)
    cfile = consent_path(cfg_file).resolve()
    consent_info = consent_store.read(cfile)
    if consent_info["state"] in ("corrupt", "unreadable"):
        raise consent_store.ConsentError(
            f"consent file is {consent_info['state']}; explicit user repair is required", state=consent_info["state"]
        )

    current = settings_mod.load(comm_file)
    roots = [str(p) for p in current.project_roots]
    if resolved_root_path is not None and str(resolved_root_path) not in roots:
        roots.insert(0, str(resolved_root_path))
    resolved_fork = fork if fork is not None else current.raw.get("fork")
    resolved_account = (
        account
        if account is not None
        else current.raw.get("account")
        or (resolved_fork.split("/", 1)[0] if isinstance(resolved_fork, str) and "/" in resolved_fork else None)
    )
    if enabled and resolved_fork is None and shutil.which("gh"):
        resolved_account, resolved_fork = _resolve_github_fork(repository, resolved_account)

    write_kwargs = dict(
        enabled=bool(enabled),
        repository=repository,
        project_roots=roots,
        consent_config=str(cfile),
        fork=resolved_fork,
        account=resolved_account,
        visibility=visibility,
        remote_project_root=remote_project_root if remote_project_root is not None else current.raw.get("remote_project_root"),
    )
    if enabled:
        updated = settings_mod.write(comm_file, **write_kwargs)
        consent_store.record_choice(cfile, "contribute")
    else:
        consent_store.record_choice(cfile, "disabled")
        updated = settings_mod.write(comm_file, **write_kwargs)

    if hasattr(consent_store, "activate_reporting"):
        try:
            consent_store.activate_reporting(cfile, config=diagnostics_config_path(cfg_file), roots=roots or None, ensure=False)
        except Exception:
            pass
    return updated.as_dict()


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
        "uv": shutil.which("uv"),
        "gh": shutil.which("gh"),
        "install_workbuddy_plugin": install,
        "registers_native_plugin": install,
        "registers_connector_if_marketplace_present": install,
        "profile_only": cmd in (None, "init") and not install,
    }


def _add_dry_run(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    """Register ``--dry-run``.

    Subparsers suppress the ``store_true`` default. A shared default of False
    would overwrite a parent ``--dry-run`` or ``--dry`` already stored on the
    same destination.
    """
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
    """True when argparse accepted ``--dry-run``, including a unique prefix such as ``--dry``."""
    return bool(getattr(args, "dry_run", False))


def _dry_install(args: argparse.Namespace) -> bool:
    if args.cmd == "install-wb":
        return True
    return args.cmd == "init" and bool(getattr(args, "install_wb", False))


def main(argv: list[str] | None = None) -> int:
    incoming = list(sys.argv[1:] if argv is None else argv)
    if not incoming:
        incoming = ["init"]

    # Parse before staging or bootstrap. Help and usage errors exit here.
    # Read-only follows the parsed flag, so ``--dry`` cannot fall through to writes.
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
        proc = subprocess.run([target_py, "-I", str(Path(__file__).resolve()), *incoming], **run_kwargs)
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
    return _emit_json(result)


if __name__ == "__main__":
    raise SystemExit(main())
