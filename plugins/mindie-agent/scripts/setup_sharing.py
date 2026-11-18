"""Community knowledge sharing and fault-reporting configuration for WorkBuddy (`setup_sharing.py`).

[Role]
Implements stdlib-only sharing status inspection (`sharing_status`), GitHub fork
discovery/provisioning (`resolve_github_fork`), and consent-backed community
sharing configuration (`configure_community_impl`) for `setup.py`.

[Invariants]
- `sharing_status` is strictly read-only and never bootstraps a virtualenv or
  imports third-party packages; corrupt consent/community/diagnostics files set
  `repair_required=True` rather than fabricating healthy defaults.
- `configure_community_impl` validates repository/account/visibility inputs before
  touching disk, refuses to overwrite corrupt consent files, and surfaces
  reporting activation errors via `status="partial"` + `reporting_error`.
- Explicitly re-raises `TimeoutError` before any `except OSError:` branch.

[Timeouts & Locks]
- `gh api` / `gh repo fork` subprocess calls use `bounded.run` (`10.0s`–`15.0s`);
  delegated interpreter re-exec uses `30.0s`.

[Side-Effect Policy]
- `sharing_status` performs zero writes; `configure_community_impl` writes
  `mindie-community.json`, `mindie-consent.json`, and `diagnostics.json`.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable

from bounded import run as bounded_run
from paths import (
    apply_enterprise_transport_env,
    community_config_path,
    config_path,
    consent_path,
    diagnostics_config_path,
    read_json,
    which_native,
)

_REPO_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def read_optional_json(path: Path, *, read_json_fn: Callable = read_json) -> tuple[str, dict]:
    try:
        path.stat()
        return "ok", read_json_fn(path)
    except TimeoutError:
        raise
    except FileNotFoundError:
        return "missing", {}
    except ValueError:
        return "corrupt", {}
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return "missing", {}
        return "unreadable", {}


def sharing_status(config: str | Path | None = None, *, read_json_fn: Callable = read_json) -> dict:
    """Return current community sharing and fault reporting status using stdlib only."""
    cfg_file = config_path(config)
    consent_state, consent_data = read_optional_json(consent_path(cfg_file), read_json_fn=read_json_fn)
    comm_state, comm_data = read_optional_json(community_config_path(cfg_file), read_json_fn=read_json_fn)
    diag_state, diag_data = read_optional_json(diagnostics_config_path(cfg_file), read_json_fn=read_json_fn)

    choice = consent_data.get("choice") if consent_state == "ok" else None
    if consent_state == "ok" and choice not in (None, "contribute", "read-only", "later", "disabled"):
        consent_state, choice = "corrupt", None
    choice_revision = consent_data.get("choice_revision") if consent_state == "ok" else None
    repair_required = any(
        state in {"corrupt", "unreadable"} for state in (consent_state, comm_state, diag_state)
    )
    sharing_enabled = comm_state == "ok" and bool(comm_data.get("enabled", False)) and choice == "contribute"
    reporting_sub = (
        diag_data.get("reporting")
        if diag_state == "ok" and isinstance(diag_data.get("reporting"), dict)
        else {}
    )
    reporting_enabled = diag_state == "ok" and bool(
        reporting_sub.get("enabled", False)
        or diag_data.get("decision") == "enabled"
        or diag_data.get("enabled") is True
    )
    if comm_state == "ok":
        repository = comm_data.get("repository", "mindie-agent/knowledge-vllm-ascend")
        fork = comm_data.get("fork")
        account = comm_data.get("account")
        project_roots = list(comm_data.get("project_roots") or [])
    elif comm_state == "missing":
        repository = "mindie-agent/knowledge-vllm-ascend"
        fork = None
        account = None
        project_roots = []
    else:
        repository = None
        fork = None
        account = None
        project_roots = []
    return {
        "schema": "mindie-sharing-status/1",
        "harness": "workbuddy",
        "consent_state": consent_state,
        "consent_choice": choice,
        "choice_revision": choice_revision,
        "community_state": comm_state,
        "diagnostics_state": diag_state,
        "sharing_enabled": sharing_enabled,
        "community_enabled": sharing_enabled,
        "reporting_enabled": reporting_enabled,
        "repository": repository,
        "fork": fork,
        "account": account,
        "project_roots": project_roots,
        "needs_onboarding_prompt": (not repair_required) and (choice not in {"contribute", "disabled"}),
        "repair_required": repair_required,
    }


def validate_sharing_inputs(
    *,
    repository: str,
    visibility: str,
    account: str | None,
    fork: str | None,
    project_root: str | Path | None,
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


def resolve_github_fork(repository: str, account: str | None) -> tuple[str | None, str | None]:
    gh = which_native("gh")
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


def configure_community_impl(
    *,
    ensure_profile_fn: Callable,
    ensure_python_fn: Callable,
    setup_script: Path,
    config: str | Path | None = None,
    enabled: bool,
    fork: str | None = None,
    account: str | None = None,
    project_root: str | Path | None = None,
    remote_project_root: str | None = None,
    visibility: str = "public",
    repository: str = "mindie-agent/knowledge-vllm-ascend",
) -> dict:
    resolved_root_path = validate_sharing_inputs(
        repository=repository,
        visibility=visibility,
        account=account,
        fork=fork,
        project_root=project_root,
    )
    cfg_file = config_path(config)
    if not cfg_file.is_file():
        ensure_profile_fn(config=cfg_file, workspace_root=resolved_root_path)
    if importlib.util.find_spec("mindie_knowledge") is None:
        interpreter = read_json(cfg_file).get("python") or ensure_python_fn()
        argv = [str(interpreter), "-I", "-X", "utf8", str(setup_script), "sharing", "--config", str(cfg_file)]
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
            f"consent file is {consent_info['state']}; explicit user repair is required",
            state=consent_info["state"],
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
    if enabled and resolved_fork is None and which_native("gh"):
        resolved_account, resolved_fork = resolve_github_fork(repository, resolved_account)

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
        from paths import engine_config_path as _eng_cfg_path

        eng_file = _eng_cfg_path(cfg_file)
        if eng_file.is_file():
            try:
                eng_doc = read_json(eng_file)
                if isinstance(eng_doc.get("root"), str) and isinstance(eng_doc.get("domain"), str):
                    from mindie_knowledge.loop.handoff import _store_path, prepare_schema

                    if not _store_path(eng_doc).is_file():
                        prepare_schema(str(eng_file))
            except TimeoutError:
                raise
            except Exception as exc:
                print(f"MindIE WorkBuddy store schema prepare warning: {type(exc).__name__}: {exc}", file=sys.stderr)
    else:
        consent_store.record_choice(cfile, "disabled")
        updated = settings_mod.write(comm_file, **write_kwargs)

    payload = updated.as_dict()
    if hasattr(consent_store, "activate_reporting"):
        try:
            reporting = consent_store.activate_reporting(
                cfile,
                config=diagnostics_config_path(cfg_file),
                roots=roots or None,
                ensure=False,
            )
        except TimeoutError:
            raise
        except Exception as exc:
            reporting = {"status": f"{type(exc).__name__}: {exc}"}
        expected = {"configured", "running"} if enabled else {"disabled"}
        if not isinstance(reporting, dict) or reporting.get("status") not in expected:
            err = reporting.get("status") if isinstance(reporting, dict) else type(reporting).__name__
            payload["reporting_error"] = str(err)
            payload["status"] = "partial"
            print(f"MindIE WorkBuddy reporting activation failed: {err}", file=sys.stderr)
    return payload
