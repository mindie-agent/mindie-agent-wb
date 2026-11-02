"""Tencent WorkBuddy native lifecycle hook dispatcher (`bridge.py`).

[Role]
Handles WorkBuddy `hooks.json` events (`SessionStart`, `UserPromptExpansion`,
`UserPromptSubmit`, `PreToolUse`, `Stop`), translating WorkBuddy hook JSON
envelopes into MindIE session lease management, one-shot MCP tool binds, and
turn-end transcript capture.

[Invariants]
- Passive Default: `SessionStart` never activates a session lease, starts the
  background knowledge loop, or injects `additionalContext`.
- Manual Entry Only: `UserPromptSubmit` / `UserPromptExpansion` activates
  `admission.sqlite3` only when the prompt begins with `/mindie-agent` or
  `$mindie-agent` (or `command_name` is `mindie-agent`).
- `Stop` returns `{}` (exit 0) and runs `capture_hook` only when the current
  `session_id` has an active lease and community sharing is enabled.

[Timeouts & Locks]
- Bounded by `mindie_launch.py`'s `10.0s` hook budget; spawns at most one
  detached background component-update check (`300s` throttle).

[Side-Effect Policy]
- Updates `admission.sqlite3` on explicit `/mindie-agent` gesture, writes one-shot
  binds to `mcp-binds.sqlite3` on `PreToolUse`, and hands off redacted transcript
  slices on `Stop`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from admission import activate, deactivate, knowledge_lease, status_facts
from component_update import trigger_background_update_if_due
from identity import (
    parse_hook,
    parse_mindie_gesture,
    parse_wb_mcp_tool,
    publish_bind,
    remember_session_context,
    session_context,
    session_from_hook,
    transcript_from_hook,
    workspace_from_hook,
)
from paths import community_config_path, config_path, engine_config_path, plugin_root, read_json
import wb_transcript


def _emit(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def handle_session_start(event: dict) -> dict:
    """Passive `SessionStart` hook: records session context and triggers non-blocking update check if due."""
    try:
        session = session_from_hook(event)
        try:
            ws = workspace_from_hook(event)
        except ValueError:
            ws = None
        tp = transcript_from_hook(event)
        if ws or tp:
            remember_session_context(session, workspace_root=ws, transcript_path=tp)
    except ValueError:
        pass
    trigger_background_update_if_due()
    return {"suppressOutput": True}


def handle_user_prompt(event: dict, *, hook_event_name: str = "UserPromptSubmit") -> dict:
    """`UserPromptSubmit` / `UserPromptExpansion` hook: activates or deactivates `/mindie-agent`."""
    try:
        session = session_from_hook(event)
        try:
            ws = workspace_from_hook(event)
        except ValueError:
            ws = None
        tp = transcript_from_hook(event)
        if ws or tp:
            remember_session_context(session, workspace_root=ws, transcript_path=tp)
    except ValueError:
        session = None

    is_gesture, body = parse_mindie_gesture(event)
    if not is_gesture:
        return {"suppressOutput": True}

    session = session or session_from_hook(event)
    try:
        workspace = workspace_from_hook(event)
    except ValueError:
        ctx = session_context(session)
        workspace = ctx.get("workspace_root") or os.getcwd()
    tokens = body.split()
    head = tokens[0].lower() if tokens else ""

    if head in {"deactivate", "off", "disable"}:
        info = deactivate(session)
        return dict(
            hookSpecificOutput=dict(
                hookEventName=hook_event_name,
                additionalContext=(
                    f"[MindIE Agent] Session `{session}` deactivated (`status={info.get('status')}`). "
                    "Knowledge retrieval and background transcript capture are disabled for the remainder "
                    "of this conversation; deterministic MCP tools (`mindie-remote-dev`, `mindie-tools`) "
                    "remain available."
                ),
            )
        )

    if head == "status":
        facts = status_facts(session)
        return dict(
            hookSpecificOutput=dict(
                hookEventName=hook_event_name,
                additionalContext=(
                    f"[MindIE Agent Status] `{json.dumps(facts, ensure_ascii=False, sort_keys=True)}`"
                ),
            )
        )

    cfg_file = config_path()
    if not cfg_file.is_file():
        from setup import ensure_default_profile

        ensure_default_profile(workspace_root=workspace)

    domain = None
    if head == "domain" and len(tokens) >= 2:
        domain = tokens[1]

    lease_info = activate(session, workspace, domain=domain)
    cli_script = plugin_root() / "scripts" / "knowledge_cli.py"
    adapter = read_json(cfg_file)
    py_exe = adapter.get("python") or sys.executable
    context_msg = (
        f"[MindIE Agent Activated] Session `{session}` is bound to workspace `{workspace}` "
        f"(domain: `{lease_info.get('domain', adapter.get('domain', 'vllm-ascend'))}`). "
        f"You can invoke MindIE knowledge tools directly via the `mindie-knowledge` MCP server "
        f"(`mcp__mindie-knowledge__knowledge_query`, `mcp__mindie-knowledge__knowledge_explain`, "
        f"`mcp__mindie-knowledge__knowledge_feedback`, `mcp__mindie-knowledge__knowledge_distill`, "
        f"`mcp__mindie-knowledge__knowledge_sync`, `mcp__mindie-knowledge__mindie_entry`) "
        f"or via CLI: `WB_CONVERSATION_ID={session} {py_exe} -I {cli_script} <tool_name>`."
    )
    return dict(
        hookSpecificOutput=dict(
            hookEventName=hook_event_name,
            additionalContext=context_msg,
        )
    )


handle_user_prompt_submit = handle_user_prompt


def handle_pre_tool_use(event: dict) -> dict:
    """`PreToolUse` hook: records a one-shot SQLite bind for WorkBuddy MCP tool invocations."""
    parsed = parse_wb_mcp_tool(event)
    if parsed is None:
        return {"suppressOutput": True}
    surface, local_tool, args = parsed
    session = session_from_hook(event)
    try:
        workspace = workspace_from_hook(event)
    except ValueError:
        workspace = None
    transcript = transcript_from_hook(event)
    step_idx = event.get("stepIdx") if isinstance(event.get("stepIdx"), int) else 0
    tool_use_id = event.get("tool_use_id") or event.get("call_id")
    publish_bind(
        session,
        surface,
        local_tool,
        args,
        step_idx=step_idx,
        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
        workspace_root=workspace,
        transcript_path=transcript,
    )
    return dict(
        hookSpecificOutput=dict(
            hookEventName="PreToolUse",
            permissionDecision="allow",
        ),
        suppressOutput=True,
    )


def handle_stop(event: dict) -> dict:
    """`Stop` hook: triggers non-blocking redacted transcript capture for active, consented sessions."""
    try:
        session = session_from_hook(event)
    except ValueError:
        return {"suppressOutput": True}
    lease = knowledge_lease(session)
    if lease is None or int(lease.get("enabled", 0)) != 1:
        return {"suppressOutput": True}

    comm_file = community_config_path()
    if not comm_file.is_file():
        return {"suppressOutput": True}
    try:
        comm_data = read_json(comm_file)
    except ValueError:
        return {"suppressOutput": True}
    if not comm_data.get("enabled"):
        return {"suppressOutput": True}

    tpath = transcript_from_hook(event)
    if not tpath:
        tpath = session_context(session).get("transcript_path")
    if not tpath:
        return {"suppressOutput": True}
    try:
        source = wb_transcript.capture_source(tpath, session, binding=lease)
    except ValueError:
        return {"suppressOutput": True}

    eng_file = engine_config_path()
    if not eng_file.is_file():
        return {"suppressOutput": True}

    try:
        from mindie_knowledge.loop.cli import capture_hook

        capture_hook(
            str(eng_file),
            dict(
                harness="wb",
                session_id=session,
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
    if action == "user-prompt-expansion":
        return _emit(handle_user_prompt(event, hook_event_name="UserPromptExpansion"))
    if action in {"user-prompt-submit", "pre-invocation"}:
        return _emit(handle_user_prompt(event, hook_event_name="UserPromptSubmit"))
    if action == "pre-tool-use":
        return _emit(handle_pre_tool_use(event))
    if action == "stop":
        return _emit(handle_stop(event))
    raise ValueError(f"unknown bridge hook action: {action}")


if __name__ == "__main__":
    raise SystemExit(main())
