"""CLI entrypoint (`knowledge_cli.py`) for MindIE knowledge operations in WorkBuddy.

[Role]
Executes session-gated knowledge operations (`knowledge_query`, `knowledge_explain`,
`knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) plus `describe` and
`mindie_status` for an explicitly activated WorkBuddy conversation (`WB_CONVERSATION_ID`).

[Invariants]
- Every `knowledge_*` call requires a valid `WB_CONVERSATION_ID` (from `--session`
  or `WB_CONVERSATION_ID` env var) with an active `Admission.knowledge_lease(session)`.
- Rejects any caller attempt to pass `_session_id`, `_session_verified`,
  `_activation`, or `mindie_activation` inside the JSON arguments payload.

[Timeouts & Locks]
- `KNOWLEDGE_RPC_TIMEOUT = 30.0s` for query/explain/feedback; `distill_rpc_timeout()`
  for C6 distillation.

[Side-Effect Policy]
- Reads/updates the local `mindie_knowledge` loop service for the active domain;
  never activates a session lease implicitly.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import URLError

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from admission import knowledge_lease, status_facts
from identity import require_session
from paths import config_path, engine_config_path, read_json

KNOWLEDGE_RPC_TIMEOUT = 30.0


class KnowledgeInactive(ValueError):
    """Raised when a knowledge operation is invoked in a session without an active lease."""

FORBIDDEN_IDENTITY_KEYS = frozenset(
    {
        "sessionId",
        "conversationId",
        "thread_id",
        "session_id",
        "_session_id",
        "_session_verified",
        "_activation",
        "mindie_activation",
    }
)

PRIVATE_KNOWLEDGE_CATALOG = [
    dict(
        name="knowledge_query",
        description=(
            "Search graded L1-L4 rules and task blocks in the active domain. "
            "Supports mode='explore' for de-biased RSI exploration."
        ),
        required=["query"],
        optional=[
            "limit",
            "conditions",
            "continuation",
            "domain",
            "token_budget",
            "include_unknown",
            "include_archived",
            "navigation_only",
            "run_id",
            "mode",
            "current_approaches",
            "exclude_refs",
            "native_subcontext",
        ],
    ),
    dict(
        name="knowledge_explain",
        description="Read exact mindie:// unit or task block references and expand requires once.",
        required=[],
        optional=[
            "ref",
            "refs",
            "conditions",
            "known_refs",
            "token_budget",
            "run_id",
            "mode",
            "current_approaches",
            "exclude_refs",
            "native_subcontext",
        ],
    ),
    dict(
        name="knowledge_feedback",
        description="Record discovered/loaded/applied/evaluated usage evidence for a feedback_ref.",
        required=["ref"],
        optional=["rating", "reason", "event_type", "run_id", "step_id", "outcome", "evidence_source"],
    ),
    dict(
        name="knowledge_distill",
        description="Distill verified milestone or completed evidence into graded L1-L4 knowledge units.",
        required=["inputs", "task_state"],
        optional=["related_refs", "proposal", "domain"],
    ),
    dict(
        name="knowledge_sync",
        description="Synchronize configured public knowledge feeds into the local graded store.",
        required=[],
        optional=["domain"],
    ),
]


def _resolve_session(explicit: str | None) -> str:
    raw = (
        explicit
        or os.environ.get("WB_CONVERSATION_ID")
        or os.environ.get("CODEBUDDY_SESSION_ID")
        or os.environ.get("AGY_CONVERSATION_ID")
    )
    return require_session(raw)


def _select_engine_config(domain: str | None = None, ref: str | None = None) -> tuple[str, dict, bool]:
    cfg_file = config_path()
    default_eng = str(engine_config_path(cfg_file))
    if not Path(default_eng).is_file():
        from setup import ensure_default_profile

        ensure_default_profile(config=cfg_file)
    adapter = read_json(cfg_file) if cfg_file.is_file() else {}
    domain_cfgs = adapter.get("domain_configs") if isinstance(adapter.get("domain_configs"), dict) else {}
    if domain is None and ref is None:
        return default_eng, read_json(default_eng), False
    from mindie_knowledge.loop.domain_routes import select_engine_config

    selected_path = select_engine_config(default_eng, domain=domain, ref=ref, domain_configs=domain_cfgs)
    return str(selected_path), read_json(selected_path), str(selected_path) != str(default_eng)


def _ensure_loop_connection(engine_path: str) -> dict:
    from mindie_knowledge.loop.cli import ensure_service

    return ensure_service(engine_path)


def execute_knowledge_tool(tool_name: str, args: dict, *, session: str) -> dict:
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be a JSON object")
    if FORBIDDEN_IDENTITY_KEYS & set(args):
        raise ValueError("tool arguments must not include native session identity keys")

    session = require_session(session)
    body = dict(args)
    requested_domain = body.pop("domain", None) if tool_name in {"knowledge_query", "knowledge_distill", "knowledge_sync"} else None
    ref_for_route = None
    if tool_name == "knowledge_explain":
        refs = body.get("refs")
        ref_for_route = body.get("ref") or (refs[0] if isinstance(refs, list) and refs else None)
    elif tool_name == "knowledge_feedback":
        ref_for_route = body.get("ref")

    engine_path, engine_cfg, routed = _select_engine_config(domain=requested_domain, ref=ref_for_route)
    from mindie_knowledge.loop.activation import Admission

    lease = Admission(engine_cfg["admission_path"]).knowledge_lease(session)
    read_bound = False
    if lease is None and routed and tool_name in {"knowledge_query", "knowledge_explain"}:
        default_lease = knowledge_lease(session)
        read_bound = default_lease is not None
    if lease is None and not read_bound:
        raise KnowledgeInactive(
            f"session {session} is not activated for MindIE knowledge; "
            "invoke /mindie-agent in the WorkBuddy prompt first"
        )

    conn = _ensure_loop_connection(engine_path)
    from mindie_knowledge.loop import summary_budget
    from mindie_knowledge.loop.cli import distill_selected
    from mindie_knowledge.loop.transport import rpc

    method = tool_name.removeprefix("knowledge_")
    if method == "distill":
        if lease is None or not lease.get("token"):
            raise ValueError("knowledge_distill requires an active capture lease in the target domain")
        payload = {
            "inputs": body.get("inputs"),
            "task_state": body.get("task_state"),
            "_session_id": session,
            "_activation": lease["token"],
        }
        for opt_key in ("related_refs", "proposal"):
            if opt_key in body:
                payload[opt_key] = body[opt_key]
        if "related_refs" not in body and "proposal" not in body:
            return distill_selected(engine_path, payload)
        return rpc(conn, "distill", payload, timeout=summary_budget.distill_rpc_timeout())

    if method == "query":
        from mindie_knowledge.materials.provenance import query_request

        query_request(body.get("query"), body.get("limit", 5), body.get("conditions"), body.get("continuation"))

    payload = dict(body, _session_id=session, _session_verified=True)
    if routed:
        payload["domain"] = engine_cfg["domain"]
    raw_res = rpc(conn, method, payload, timeout=KNOWLEDGE_RPC_TIMEOUT)
    if method == "sync" and isinstance(raw_res, list):
        return {"status": "ok", "feeds": raw_res}
    return raw_res


def invoke(tool: str, args: dict | None = None, *, session: str | None = None) -> dict:
    """Programmatically invoke a `knowledge_cli` command (`describe`, `mindie_status`, `knowledge_*`)."""
    if tool == "mindie_status":
        session_opt = None
        try:
            session_opt = _resolve_session(session)
        except ValueError:
            session_opt = None
        return dict(result=status_facts(session_opt), isError=False)
    resolved_session = _resolve_session(session)
    if tool == "describe":
        lease = knowledge_lease(resolved_session)
        if lease is None:
            raise KnowledgeInactive(
                f"session {resolved_session} is not activated; invoke /mindie-agent in WorkBuddy first"
            )
        return dict(
            session_id=resolved_session,
            activated=True,
            project_root=lease.get("project_root"),
            tools=PRIVATE_KNOWLEDGE_CATALOG,
            isError=False,
        )
    res = execute_knowledge_tool(tool, args or {}, session=resolved_session)
    return dict(result=res, isError=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge_cli.py", description="MindIE knowledge CLI for WorkBuddy")
    parser.add_argument(
        "tool",
        choices=[
            "describe",
            "mindie_status",
            "knowledge_query",
            "knowledge_explain",
            "knowledge_feedback",
            "knowledge_distill",
            "knowledge_sync",
        ],
    )
    parser.add_argument("--session", default=None, help="WorkBuddy conversationId / session_id")
    parser.add_argument("--json", dest="json_arg", default=None, help="Inline JSON object arguments")
    parsed = parser.parse_args(argv)

    try:
        if parsed.tool == "mindie_status":
            session_opt = None
            try:
                session_opt = _resolve_session(parsed.session)
            except ValueError:
                session_opt = None
            out = dict(result=status_facts(session_opt), isError=False)
            sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            return 0

        session = _resolve_session(parsed.session)
        if parsed.tool == "describe":
            lease = knowledge_lease(session)
            if lease is None:
                raise ValueError(
                    f"session {session} is not activated; invoke /mindie-agent in WorkBuddy first"
                )
            out = dict(
                session_id=session,
                activated=True,
                project_root=lease.get("project_root"),
                tools=PRIVATE_KNOWLEDGE_CATALOG,
                isError=False,
            )
            sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            return 0

        raw_in = parsed.json_arg if parsed.json_arg is not None else sys.stdin.read()
        args = json.loads(raw_in) if raw_in and raw_in.strip() else {}
        result = execute_knowledge_tool(parsed.tool, args, session=session)
        is_error = False
        if parsed.tool == "knowledge_distill":
            from mindie_knowledge.graded.distill import result_view

            result, is_error = result_view(result)
        elif parsed.tool == "knowledge_sync":
            is_error = result.get("status") != "ok"
        envelope = dict(result=result, isError=bool(is_error))
        sys.stdout.buffer.write((json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        return 1 if is_error else 0
    except Exception as exc:
        err = dict(error=f"{type(exc).__name__}: {exc}", isError=True)
        sys.stdout.buffer.write((json.dumps(err, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
