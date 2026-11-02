"""Tencent WorkBuddy native hook payload validator and one-shot MCP bind store.

[Role]
Validates WorkBuddy / CodeBuddy hook payloads (`session_id`, `conversationId`,
`cwd`, `transcript_path`, `prompt`, `command_name`, `arguments`, `tool_name`,
`tool_input`, `tool_use_id`, `call_id`) and manages one-shot SQLite WAL binds
connecting `PreToolUse` (and `params._meta["workbuddy.ai/conversationId"]`) to
`mcp_server.py`.

[Invariants]
- Native task identity comes strictly from `session_id` / `conversationId` in
  hook payloads, `params._meta["workbuddy.ai/conversationId"]` in the WorkBuddy
  MCP transport envelope, or `WB_CONVERSATION_ID` in the environment—never from
  model-controlled tool arguments (`params.arguments`).
- `PreToolUse` binds `(surface, local_tool, args_digest)` to `(session_id,
  step_idx, workspace_root, transcript_path)` with a 120s TTL; `claim_bind`
  consumes each bind at most once under `BEGIN IMMEDIATE`.

[Timeouts & Locks]
- Opens `mcp-binds.sqlite3` with `PRAGMA journal_mode=WAL` and
  `PRAGMA busy_timeout=5000` (`timeout=5.0`) once per connection; no `.owner`
  `lstat` or schema reflection on hot paths.

[Side-Effect Policy]
- Mutates only `state_dir() / "mcp-binds.sqlite3"`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from paths import bind_db_path

SESSION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z"
)
GESTURE_RE = re.compile(r"^\s*(?:/mindie-agent(?::mindie-agent)?|\$mindie-agent(?::mindie-agent)?)(?=\s|$)")
WB_MCP_TOOL_RE = re.compile(
    r"^mcp__?(?:plugin[-_][A-Za-z0-9_-]+[-_])?(mindie[-_]remote[-_]dev|mindie[-_]tools|mindie[-_]knowledge)__?([A-Za-z0-9_.]+)\Z"
)
SURFACES = frozenset({"remote", "tools", "knowledge"})
BIND_TTL_SECONDS = 120.0
MAX_HOOK_BYTES = 32 * 1024 * 1024
FORBIDDEN_IDENTITY_KEYS = frozenset(
    {
        "sessionId",
        "conversationId",
        "thread_id",
        "_session_id",
        "_session_verified",
        "_activation",
        "mindie_activation",
    }
)


def _reject_identity_arguments(arguments: dict | None, surface: str) -> None:
    if not isinstance(arguments, dict):
        return
    if FORBIDDEN_IDENTITY_KEYS & set(arguments):
        raise ValueError("tool arguments must not declare session identity")
    if "session_id" in arguments and surface != "remote":
        raise ValueError("tool arguments must not declare session identity")


def parse_hook(raw: bytes) -> dict:
    if len(raw) > MAX_HOOK_BYTES:
        raise ValueError("native hook payload exceeds 32 MiB byte budget")
    if not raw.strip():
        return {}
    event = json.loads(raw.decode("utf-8"))
    if not isinstance(event, dict):
        raise ValueError("native hook payload must be a JSON object")
    return event


def require_session(value) -> str:
    if not isinstance(value, str) or not SESSION_RE.fullmatch(value.strip()):
        raise ValueError("native WorkBuddy session_id / conversationId is missing or invalid")
    return value.strip().lower()


def session_from_hook(event: dict) -> str:
    return require_session(event.get("session_id") or event.get("conversationId"))


def workspace_from_hook(event: dict) -> str:
    cwd = event.get("cwd")
    if isinstance(cwd, str) and os.path.isabs(cwd):
        return str(Path(cwd).resolve())
    paths = event.get("workspacePaths")
    if isinstance(paths, list) and paths:
        first = paths[0]
        if isinstance(first, str) and os.path.isabs(first):
            return str(Path(first).resolve())
    raise ValueError("native hook payload has no absolute cwd or workspacePaths entry")


def transcript_from_hook(event: dict) -> str | None:
    value = event.get("transcript_path") or event.get("transcriptPath")
    if isinstance(value, str) and os.path.isabs(value):
        return str(Path(value))
    return None


def parse_mindie_gesture(event: dict) -> tuple[bool, str]:
    """Return `(is_gesture, subcommand_or_body)` from `UserPromptSubmit` or `UserPromptExpansion`."""
    cmd_name = event.get("command_name")
    if isinstance(cmd_name, str) and cmd_name.strip() in {"mindie-agent", "mindie-agent:mindie-agent"}:
        args = event.get("arguments")
        return True, args.strip() if isinstance(args, str) else ""
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        prompt = event.get("lastUserInput")
    if not isinstance(prompt, str):
        return False, ""
    match = GESTURE_RE.match(prompt)
    if match is None:
        return False, ""
    remainder = prompt[match.end():].strip()
    return True, remainder


def parse_wb_mcp_tool(event_or_call: dict) -> tuple[str, str, dict] | None:
    """Resolve a WorkBuddy `PreToolUse` event (or `toolCall` dict) to `(surface, local_tool, args)`."""
    if not isinstance(event_or_call, dict):
        return None
    name = event_or_call.get("tool_name")
    args = event_or_call.get("tool_input")
    if not isinstance(name, str):
        tc = event_or_call.get("toolCall")
        if isinstance(tc, dict):
            name = tc.get("name")
            args = tc.get("args")
        else:
            name = event_or_call.get("name")
            args = event_or_call.get("args")
    if not isinstance(name, str):
        return None
    if not isinstance(args, dict):
        args = {}
    if name == "call_mcp_tool":
        server = args.get("ServerName") or args.get("server")
        tool_name = args.get("ToolName") or args.get("tool")
        inner_args = args.get("Arguments") if isinstance(args.get("Arguments"), dict) else args.get("arguments")
        if not isinstance(server, str) or not isinstance(tool_name, str):
            return None
        norm_server = server.replace("_", "-")
        if "remote" in norm_server:
            return "remote", tool_name, inner_args if isinstance(inner_args, dict) else {}
        if "knowledge" in norm_server:
            return "knowledge", tool_name, inner_args if isinstance(inner_args, dict) else {}
        if "tools" in norm_server:
            return "tools", tool_name, inner_args if isinstance(inner_args, dict) else {}
        return None
    match = WB_MCP_TOOL_RE.match(name)
    if match is None:
        return None
    raw_server, local_tool = match.group(1), match.group(2)
    norm_server = raw_server.replace("_", "-")
    if "remote" in norm_server:
        surface = "remote"
    elif "knowledge" in norm_server:
        surface = "knowledge"
    else:
        surface = "tools"
    return surface, local_tool, args


def canonical_args(arguments: dict | None) -> str:
    return json.dumps(
        arguments if isinstance(arguments, dict) else {},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def args_digest(arguments: dict | None) -> str:
    return hashlib.sha256(canonical_args(arguments).encode("utf-8")).hexdigest()


def _connect(path: Path, *, create: bool) -> sqlite3.Connection:
    if not path.exists() and not create:
        raise FileNotFoundError(str(path))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5.0)
    db.execute("PRAGMA busy_timeout=5000;")
    if create:
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA synchronous=NORMAL;")
    return db


def _ensure_tables(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS binds("
        "bind_id TEXT PRIMARY KEY, "
        "session TEXT NOT NULL, "
        "surface TEXT NOT NULL, "
        "local_tool TEXT NOT NULL, "
        "args_digest TEXT NOT NULL, "
        "step_idx INTEGER NOT NULL, "
        "tool_use_id TEXT, "
        "workspace_root TEXT, "
        "transcript_path TEXT, "
        "status TEXT NOT NULL, "
        "created REAL NOT NULL, "
        "updated REAL NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS sessions("
        "session TEXT PRIMARY KEY, "
        "workspace_root TEXT, "
        "transcript_path TEXT, "
        "updated REAL NOT NULL)"
    )


def remember_session_context(
    session_id: str,
    *,
    workspace_root: str | None = None,
    transcript_path: str | None = None,
    config=None,
) -> None:
    """Record or update `(workspace_root, transcript_path)` for a verified WorkBuddy session."""
    session_id = require_session(session_id)
    now = time.time()
    db = _connect(bind_db_path(config), create=True)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            _ensure_tables(db)
            row = db.execute(
                "SELECT workspace_root, transcript_path FROM sessions WHERE session=?",
                (session_id,),
            ).fetchone()
            ws = workspace_root or (row[0] if row else None)
            tp = transcript_path or (row[1] if row else None)
            db.execute(
                "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)",
                (session_id, ws, tp, now),
            )
    finally:
        db.close()


def session_context(session_id: str, *, config=None) -> dict:
    """Read stored `(workspace_root, transcript_path)` for a verified WorkBuddy session."""
    session_id = require_session(session_id)
    path = bind_db_path(config)
    if not path.is_file():
        return {"session": session_id, "workspace_root": None, "transcript_path": None}
    db = _connect(path, create=False)
    try:
        _ensure_tables(db)
        row = db.execute(
            "SELECT workspace_root, transcript_path FROM sessions WHERE session=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return {"session": session_id, "workspace_root": None, "transcript_path": None}
        return {"session": session_id, "workspace_root": row[0], "transcript_path": row[1]}
    finally:
        db.close()


def publish_bind(
    session_id: str,
    surface: str,
    local_tool: str,
    arguments: dict,
    *,
    step_idx: int = 0,
    tool_use_id: str | None = None,
    workspace_root: str | None = None,
    transcript_path: str | None = None,
    config=None,
) -> str:
    """Record a one-shot `PreToolUse` bind for a WorkBuddy MCP tool call."""
    session_id = require_session(session_id)
    if surface not in SURFACES:
        raise ValueError(f"unknown MindIE MCP surface: {surface}")
    if not isinstance(local_tool, str) or not local_tool:
        raise ValueError("local_tool must be a nonempty string")
    _reject_identity_arguments(arguments, surface)
    adigest = args_digest(arguments)
    step = int(step_idx) if isinstance(step_idx, int) and not isinstance(step_idx, bool) else 0
    now = time.time()
    bind_id = hashlib.sha256(
        f"{session_id}|{surface}|{local_tool}|{adigest}|{step}|{now:.6f}".encode("utf-8")
    ).hexdigest()
    db = _connect(bind_db_path(config), create=True)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            _ensure_tables(db)
            if workspace_root or transcript_path:
                row = db.execute(
                    "SELECT workspace_root, transcript_path FROM sessions WHERE session=?",
                    (session_id,),
                ).fetchone()
                ws = workspace_root or (row[0] if row else None)
                tp = transcript_path or (row[1] if row else None)
                db.execute(
                    "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)",
                    (session_id, ws, tp, now),
                )
            db.execute(
                "INSERT OR REPLACE INTO binds VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bind_id,
                    session_id,
                    surface,
                    local_tool,
                    adigest,
                    step,
                    tool_use_id,
                    workspace_root,
                    transcript_path,
                    "bound",
                    now,
                    now,
                ),
            )
            return bind_id
    finally:
        db.close()


def _extract_verified_meta_session(meta: dict | None) -> str | None:
    if isinstance(meta, dict):
        for key in ("workbuddy.ai/conversationId", "conversationId"):
            raw = meta.get(key)
            if raw is not None:
                if not isinstance(raw, str) or not SESSION_RE.fullmatch(raw.strip()):
                    raise ValueError(f"invalid {key} in MCP metadata")
                return raw.strip().lower()
    for env_key in ("WB_CONVERSATION_ID", "CODEBUDDY_SESSION_ID", "AGY_CONVERSATION_ID"):
        raw_env = os.environ.get(env_key)
        if isinstance(raw_env, str) and raw_env.strip():
            if not SESSION_RE.fullmatch(raw_env.strip()):
                raise ValueError(f"invalid {env_key} environment variable")
            return raw_env.strip().lower()
    return None


def claim_bind(
    surface: str,
    local_tool: str,
    arguments: dict,
    *,
    meta: dict | None = None,
    config=None,
) -> dict:
    """Atomically claim the freshest unexpired `PreToolUse` bind or verified WorkBuddy `_meta` envelope."""
    if surface not in SURFACES:
        raise ValueError(f"unknown MindIE MCP surface: {surface}")
    _reject_identity_arguments(arguments, surface)
    adigest = args_digest(arguments)
    path = bind_db_path(config)
    now = time.time()
    cutoff = now - BIND_TTL_SECONDS
    meta_session = _extract_verified_meta_session(meta)

    if path.is_file():
        db = _connect(path, create=False)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                _ensure_tables(db)
                if meta_session is not None:
                    row = db.execute(
                        "SELECT bind_id, session, workspace_root, transcript_path, step_idx, tool_use_id "
                        "FROM binds "
                        "WHERE surface=? AND local_tool=? AND args_digest=? AND status='bound' "
                        "AND created>=? AND session=? "
                        "ORDER BY created DESC, step_idx DESC LIMIT 1",
                        (surface, local_tool, adigest, cutoff, meta_session),
                    ).fetchone()
                else:
                    sessions = db.execute(
                        "SELECT DISTINCT session FROM binds "
                        "WHERE surface=? AND local_tool=? AND args_digest=? AND status='bound' AND created>=?",
                        (surface, local_tool, adigest, cutoff),
                    ).fetchall()
                    if len(sessions) > 1:
                        raise ValueError(
                            f"ambiguous PreToolUse binds across {len(sessions)} conversations for "
                            f"{surface}.{local_tool}; verified conversationId metadata is required"
                        )
                    row = db.execute(
                        "SELECT bind_id, session, workspace_root, transcript_path, step_idx, tool_use_id "
                        "FROM binds "
                        "WHERE surface=? AND local_tool=? AND args_digest=? AND status='bound' AND created>=? "
                        "ORDER BY created DESC, step_idx DESC LIMIT 1",
                        (surface, local_tool, adigest, cutoff),
                    ).fetchone()
                if row is not None:
                    bind_id, session, workspace_root, transcript_path, step_idx, tool_use_id = row
                    db.execute(
                        "UPDATE binds SET status='claimed', updated=? WHERE bind_id=?",
                        (now, bind_id),
                    )
                    if not workspace_root or not transcript_path:
                        srow = db.execute(
                            "SELECT workspace_root, transcript_path FROM sessions WHERE session=?",
                            (session,),
                        ).fetchone()
                        if srow is not None:
                            workspace_root = workspace_root or srow[0]
                            transcript_path = transcript_path or srow[1]
                    return dict(
                        bind_id=bind_id,
                        session=session,
                        workspace_root=workspace_root,
                        transcript_path=transcript_path,
                        step_idx=step_idx,
                        tool_use_id=tool_use_id,
                        surface=surface,
                        local_tool=local_tool,
                    )
        finally:
            db.close()

    if meta_session is not None:
        publish_bind(
            meta_session,
            surface,
            local_tool,
            arguments,
            config=config,
        )
        return claim_bind(
            surface,
            local_tool,
            arguments,
            meta={"workbuddy.ai/conversationId": meta_session},
            config=config,
        )

    if not path.is_file():
        raise ValueError("no native PreToolUse bind database exists; call rejected")
    raise ValueError(
        f"no active PreToolUse bind found for {surface}.{local_tool}; "
        "unauthorized or replayed MCP tool call"
    )


def finish_bind(bind_id: str, succeeded: bool, *, config=None) -> None:
    """Mark a claimed bind as `succeeded` or `failed`."""
    if not isinstance(bind_id, str) or not bind_id:
        return
    path = bind_db_path(config)
    if not path.is_file():
        return
    db = _connect(path, create=False)
    try:
        with db:
            db.execute(
                "UPDATE binds SET status=?, updated=? WHERE bind_id=? AND status='claimed'",
                ("succeeded" if succeeded else "failed", time.time(), bind_id),
            )
    finally:
        db.close()
