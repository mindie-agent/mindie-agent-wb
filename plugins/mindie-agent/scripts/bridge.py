"""Tencent WorkBuddy native lifecycle hook dispatcher (`bridge.py`).

[Role]
Handles WorkBuddy `hooks.json` events (`SessionStart`, `UserPromptSubmit`,
`PreToolUse`, `Stop`) with silent pass-through prompt/tool hooks and opt-in
turn-end transcript capture gated by community consent and `project_roots`.

[Invariants]
- Zero-I/O Ordinary Hooks: `SessionStart`, `UserPromptSubmit`, and `PreToolUse`
  never create or mutate `admission.sqlite3` or `mcp-binds.sqlite3`.
- Consent-First `Stop`: `Stop` checks `mindie-community.json` (`enabled=true` +
  authorized `project_roots`) and `mindie-consent.json` (`choice="contribute"`)
  before opening `admission.sqlite3`; when off or out of scope, zero SQLite
  files are created and `{"suppressOutput": true}` returns immediately.

[Timeouts & Locks]
- Bounded by `mindie_launch.py`'s `8.0s` hook execution budget (`10.0s` total);
  spawns at most one detached background component-update check (`300s` throttle).

[Side-Effect Policy]
- Auto-provisions `admission.sqlite3` (`not_before=0.0`) and invokes
  `capture_hook` on `Stop` exclusively when community sharing is enabled and the
  session workspace is inside authorized `project_roots`.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from admission import ensure_default_lease, is_deactivated, knowledge_lease
from component_update import trigger_background_update_if_due
from identity import (
    _reject_identity_arguments,
    parse_hook,
    parse_wb_mcp_tool,
    session_context,
    session_from_hook,
    transcript_from_hook,
    workspace_from_hook,
)
from paths import admission_path, community_config_path, data_home, engine_config_path, read_json
import wb_transcript


def _emit(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _path_is_within_stdlib(candidate: str | Path, root: str | Path) -> bool:
    try:
        cand_res = str(Path(candidate).expanduser().resolve())
        root_res = str(Path(root).expanduser().resolve())
    except TimeoutError:
        raise
    except OSError:
        return False
    if os.name == "nt":
        cand_res = os.path.normcase(cand_res)
        root_res = os.path.normcase(root_res)
    if cand_res == root_res:
        return True
    try:
        rel = os.path.relpath(cand_res, root_res)
    except ValueError:
        return False
    return rel != ".." and not rel.startswith(".." + os.sep) and not os.path.isabs(rel)


def may_capture_community_stop(event: dict, *, config=None) -> tuple[bool, str | None]:
    """Pure-stdlib pre-gate checking community consent and `project_roots` before touching SQLite."""
    try:
        session = session_from_hook(event)
    except ValueError:
        return False, None

    comm_file = community_config_path(config)
    if not comm_file.is_file():
        return False, None
    try:
        comm_data = read_json(comm_file)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False, None

    if (
        comm_data.get("schema") != "mindie-community-config/1"
        or comm_data.get("enabled") is not True
        or not isinstance(comm_data.get("generation"), str)
        or not comm_data["generation"]
        or type(comm_data.get("enabled_at")) not in (int, float)
        or not math.isfinite(comm_data["enabled_at"])
        or comm_data["enabled_at"] <= 0
    ):
        return False, None

    consent_ref = comm_data.get("consent_config")
    if not isinstance(consent_ref, str) or not os.path.isabs(consent_ref):
        return False, None
    cfile = Path(consent_ref)
    if not cfile.is_file():
        return False, None
    try:
        consent_data = read_json(cfile)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return False, None
    if consent_data.get("choice") != "contribute":
        return False, None

    roots = comm_data.get("project_roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and os.path.isabs(r) for r in roots):
        return False, None

    workspace_root: str | None = None
    try:
        workspace_root = workspace_from_hook(event)
    except ValueError:
        if admission_path(config).is_file():
            existing = knowledge_lease(session, config=config)
            if existing is not None and isinstance(existing.get("project_root"), str):
                workspace_root = existing["project_root"]
    if not workspace_root:
        return False, None

    if not any(_path_is_within_stdlib(workspace_root, root) for root in roots):
        return False, None
    return True, workspace_root


def handle_session_start(event: dict, *, config=None) -> dict:
    """Passive `SessionStart` hook: triggers non-blocking update check if due with zero SQLite I/O."""
    _ = event
    spawned = trigger_background_update_if_due(config=config)
    if spawned is None:
        detail = "spawn failed"
        try:
            recorded = read_json(data_home() / "wb-component-update-status.json")
            error = recorded.get("error") if isinstance(recorded, dict) else None
            if isinstance(error, str) and error.strip():
                detail = error
        except TimeoutError:
            raise
        except (OSError, ValueError):
            pass
        return dict(
            hookSpecificOutput=dict(
                hookEventName="SessionStart",
                systemMessage=f"MindIE WorkBuddy background component update failed to start: {detail}",
            )
        )
    return {"suppressOutput": True}


def handle_user_prompt(event: dict, *, hook_event_name: str = "UserPromptSubmit") -> dict:
    """Lightweight pass-through `UserPromptSubmit` hook (zero command interception or SQLite I/O)."""
    _ = event, hook_event_name
    return {"suppressOutput": True}


handle_user_prompt_submit = handle_user_prompt


def handle_pre_tool_use(event: dict) -> dict:
    """Lightweight pass-through `PreToolUse` hook validating argument hygiene without SQLite writes."""
    parsed = parse_wb_mcp_tool(event)
    if parsed is None:
        return {"suppressOutput": True}
    surface, _local_tool, args = parsed
    _reject_identity_arguments(args, surface)
    return dict(
        hookSpecificOutput=dict(
            hookEventName="PreToolUse",
            permissionDecision="allow",
        ),
        suppressOutput=True,
    )


def handle_stop(event: dict, *, config=None) -> dict:
    """`Stop` hook: checks consent + `project_roots` before auto-provisioning lease and capturing."""
    allowed, workspace_root = may_capture_community_stop(event, config=config)
    if not allowed or not workspace_root:
        return {"suppressOutput": True}

    from mindie_knowledge.loop import settings as settings_mod

    settings = settings_mod.load(community_config_path(config))
    if not settings.allows_capture() or not settings.in_scope(workspace_root):
        return {"suppressOutput": True}

    session = session_from_hook(event)
    if is_deactivated(session, config=config):
        return {"suppressOutput": True}

    tpath = transcript_from_hook(event)
    if not tpath:
        tpath = session_context(session, config=config).get("transcript_path")
    if not tpath:
        return {"suppressOutput": True}

    eng_file = engine_config_path(config)
    if not eng_file.is_file():
        return {"suppressOutput": True}

    try:
        lease = ensure_default_lease(session, workspace_root, not_before=0.0, config=config)
        source = wb_transcript.capture_source(tpath, session, binding=lease)
    except ValueError:
        return {"suppressOutput": True}

    try:
        from mindie_knowledge.loop.cli import capture_hook

        capture_hook(
            str(eng_file),
            dict(
                harness="wb",
                session_id=session,
                mindie_activation=lease.get("token"),
                transcript_path=source["transcript_path"],
                cwd=source["project_root"],
            ),
        )
    except Exception as exc:
        print(f"MindIE Stop capture warning: {type(exc).__name__}: {exc}", file=sys.stderr)
    return {"suppressOutput": True}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "session-start"
    raw = sys.stdin.buffer.read()
    event = parse_hook(raw)
    if action == "session-start":
        return _emit(handle_session_start(event))
    if action in {"user-prompt-submit", "user-prompt-expansion", "pre-invocation"}:
        return _emit(handle_user_prompt(event))
    if action == "pre-tool-use":
        return _emit(handle_pre_tool_use(event))
    if action == "stop":
        return _emit(handle_stop(event))
    raise ValueError(f"unknown bridge hook action: {action}")


if __name__ == "__main__":
    raise SystemExit(main())
