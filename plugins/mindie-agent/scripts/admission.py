"""Session lease admission and factual control-plane status for Tencent WorkBuddy.

[Role]
Wraps `mindie_knowledge.loop.activation.Admission` for default-enabled session
leases (`ensure_default_lease`, `activate`, `deactivate`, `is_deactivated`,
`knowledge_lease`) and read-only `status_facts`.

[Invariants]
- While the WorkBuddy plugin is enabled, knowledge tools are available by default
  and provision a session lease (`not_before=0.0`) on demand unless muted via
  `deactivate` (`enabled == 0`).
- `knowledge_lease`, `is_deactivated`, and `status_facts` are strictly read-only
  and never create `admission.sqlite3` or implicitly start background services.

[Timeouts & Locks]
- Uses `Admission`'s WAL + `5.0s` SQLite busy timeout (`open_database`).

[Side-Effect Policy]
- `ensure_default_lease`, `activate`, and `deactivate` mutate `admission_path`;
  `knowledge_lease`, `is_deactivated`, and `status_facts` are side-effect free.
"""

from __future__ import annotations

import os
from pathlib import Path

from identity import require_session
from paths import (
    admission_path,
    community_config_path,
    config_path,
    consent_path,
    engine_config_path,
    read_json,
)


def gate(config: str | Path | None = None):
    from mindie_knowledge.loop.activation import Admission

    return Admission(str(admission_path(config)))


def knowledge_lease(session: str, *, config: str | Path | None = None) -> dict | None:
    session = require_session(session)
    path = admission_path(config)
    if not path.is_file():
        return None
    return gate(config).knowledge_lease(session)


def is_deactivated(session: str, *, config: str | Path | None = None) -> bool:
    session = require_session(session)
    path = admission_path(config)
    if not path.is_file():
        return False
    return gate(config).inspect(session).get("status") == "inactive"


def ensure_default_lease(
    session: str,
    project_root: str | Path | None = None,
    *,
    not_before: float | None = 0.0,
    config: str | Path | None = None,
) -> dict:
    session = require_session(session)
    if is_deactivated(session, config=config):
        raise ValueError("MindIE knowledge is deactivated for this session")
    existing = knowledge_lease(session, config=config)
    if project_root is not None:
        root = str(Path(project_root).resolve())
        if existing is not None and isinstance(existing.get("project_root"), str):
            if Path(existing["project_root"]).resolve() == Path(root).resolve():
                return dict(
                    existing,
                    enabled=int(bool(existing.get("enabled", 1))),
                    entry_enabled=int(bool(existing.get("entry_enabled", 1))),
                )
    else:
        if existing is not None:
            return dict(
                existing,
                enabled=int(bool(existing.get("enabled", 1))),
                entry_enabled=int(bool(existing.get("entry_enabled", 1))),
            )
        root = str(Path(os.getcwd()).resolve())
    adm = gate(config)
    adm.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lease = adm.activate(session, project_root=root, root_session=session, not_before=not_before)
    return dict(
        lease,
        enabled=int(bool(lease.get("enabled", 1))),
        entry_enabled=int(bool(lease.get("entry_enabled", 1))),
    )


def activate(
    session: str,
    project_root: str | Path,
    *,
    domain: str | None = None,
    not_before: float | None = None,
    config: str | Path | None = None,
) -> dict:
    session = require_session(session)
    root = str(Path(project_root).resolve())
    if not os.path.isabs(root):
        raise ValueError("project_root must be an absolute path")
    cfg_file = config_path(config)
    if domain is not None and cfg_file.is_file():
        adapter = read_json(cfg_file)
        from mindie_knowledge.loop.domain_routes import activate_selected_domain

        selected = activate_selected_domain(
            adapter["engine_config"],
            domain=domain,
            domain_configs=adapter.get("domain_configs"),
            session=session,
            project_root=root,
            root_session=session,
        )
        lease = selected["lease"]
        return dict(
            status="active",
            session=lease["session"],
            domain=selected["domain"],
            engine_config=selected["engine_config"],
            generation=selected["generation"],
            mindie_session_id=lease["session"],
            mindie_activation=lease["token"],
            activated_at=lease["activated_at"],
            project_root=lease["project_root"],
        )
    adm = gate(config)
    adm.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lease = adm.activate(session, project_root=root, root_session=session, not_before=not_before)
    return dict(
        status="active",
        session=lease["session"],
        mindie_session_id=lease["session"],
        mindie_activation=lease["token"],
        activated_at=lease["activated_at"],
        project_root=lease["project_root"],
    )


def deactivate(
    session: str,
    *,
    config: str | Path | None = None,
    project_root: str | Path | None = None,
) -> dict:
    session = require_session(session)
    root = str(Path(project_root or os.getcwd()).resolve())
    adm = gate(config)
    adm.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if adm.inspect(session).get("status") == "missing":
        adm.activate(session, project_root=root, root_session=session, not_before=0.0)
    adm.deactivate(session)
    cfg_file = config_path(config)
    if cfg_file.is_file():
        adapter = read_json(cfg_file)
        domain_configs = adapter.get("domain_configs")
        if isinstance(domain_configs, dict) and domain_configs:
            from mindie_knowledge.loop.domain_routes import deactivate_registered_domains

            return deactivate_registered_domains(
                adapter["engine_config"],
                domain_configs=domain_configs,
                session=session,
            )
    return dict(status="inactive", session=session)


def status_facts(session: str | None = None, *, config: str | Path | None = None) -> dict:
    """Return factual control-plane status without onboarding prompts or activation."""
    cfg_file = config_path(config)
    configured = cfg_file.is_file()
    lease = None
    norm_session = session
    is_active = True
    if session is not None:
        try:
            norm_session = require_session(session)
            is_active = not is_deactivated(norm_session, config=config)
            lease = knowledge_lease(norm_session, config=config)
        except ValueError:
            is_active = False
            lease = None
    consent_state = "unconfigured"
    cpath = consent_path(config)
    if cpath.is_file():
        try:
            cdata = read_json(cpath)
            consent_state = str(cdata.get("choice") or cdata.get("status") or "configured")
        except ValueError:
            consent_state = "invalid"
    community_enabled = False
    comm_path = community_config_path(config)
    if comm_path.is_file():
        try:
            comm_data = read_json(comm_path)
            community_enabled = bool(comm_data.get("enabled"))
        except ValueError:
            community_enabled = False
    return dict(
        harness="workbuddy",
        configured=configured,
        config_path=str(cfg_file),
        engine_config_path=str(engine_config_path(config)),
        activated=bool(is_active),
        active=bool(is_active),
        knowledge_lease=lease,
        session=lease["session"] if lease else norm_session,
        session_id=lease["session"] if lease else norm_session,
        project_root=lease.get("project_root") if lease else None,
        activated_at=lease.get("activated_at") if lease else None,
        consent_choice=consent_state,
        community_sharing_enabled=community_enabled,
    )
