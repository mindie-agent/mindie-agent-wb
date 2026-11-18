"""CLI entrypoint (`knowledge_cli.py`) for default-enabled MindIE knowledge in WorkBuddy.

[Role]
Executes default-enabled knowledge operations (`knowledge_query`, `knowledge_explain`,
`knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) plus `describe` and
`mindie_status` for Tencent WorkBuddy conversations and CLI callers.

[Invariants]
- Knowledge operations are available by default without prior session activation:
  missing host session metadata falls back to `"wb-cli-default"` and provisions a
  default lease (`not_before=0.0`) on demand unless muted via `deactivate`.
- Rejects any caller attempt to pass `_session_id`, `_session_verified`,
  `_activation`, or `mindie_activation` inside the JSON arguments payload.

[Timeouts & Locks]
- `KNOWLEDGE_RPC_TIMEOUT = 30.0s` for query/explain/feedback/sync;
  `distill_rpc_timeout()` for C6 distillation; `1 MiB` input byte cap.

[Side-Effect Policy]
- `describe` and `mindie_status` are strictly read-only; `knowledge_*` calls
  provision a default profile/lease on demand and connect to `mindie_knowledge`.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from admission import ensure_default_lease, gate, is_deactivated, knowledge_lease, status_facts
from identity import require_session
from paths import admission_path, config_path, engine_config_path, read_json

KNOWLEDGE_RPC_TIMEOUT = 30.0
KNOWLEDGE_SYNC_TIMEOUT = 315.0
MAX_INPUT_BYTES = 1024 * 1024


class KnowledgeInactive(ValueError):
    """Raised when a knowledge operation is invoked in an explicitly deactivated session."""

    code = "knowledge_inactive"


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
            "domain_path",
            "token_budget",
            "include_unknown",
            "include_archived",
            "navigation_only",
            "run_id",
            "mode",
            "native_subcontext",
            "ledger_entries",
            "bottleneck_summary",
            "goal",
        ],
        inputSchema=dict(
            type="object",
            properties=dict(
                query={"type": "string"},
                limit={"type": "integer", "minimum": 1, "maximum": 20},
                conditions={"type": "object"},
                continuation={"type": "string"},
                domain={"type": "string"},
                domain_path={"type": "string"},
                token_budget={"type": "integer", "minimum": 1, "maximum": 64000},
                include_unknown={"type": "boolean"},
                include_archived={"type": "boolean"},
                navigation_only={"type": "boolean", "default": False},
                run_id={"type": "string"},
                mode={"type": "string", "enum": ["reuse", "diagnose", "explore"]},
                native_subcontext={"type": "boolean"},
                ledger_entries={"type": "array", "items": {"type": "object"}},
                bottleneck_summary={"type": "object"},
                goal={"type": "string"},
            ),
            required=["query"],
            additionalProperties=False,
        ),
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
            "ledger_entries",
            "bottleneck_summary",
            "native_subcontext",
            "goal",
        ],
        inputSchema=dict(
            type="object",
            properties=dict(
                ref={"type": "string"},
                refs={"type": "array", "items": {"type": "string"}},
                conditions={"type": "object"},
                known_refs={"type": "array", "items": {"type": "string"}},
                token_budget={"type": "integer", "minimum": 1, "maximum": 64000},
                run_id={"type": "string"},
                mode={"type": "string", "enum": ["reuse", "diagnose", "explore"]},
                ledger_entries={"type": "array", "items": {"type": "object"}},
                bottleneck_summary={"type": "object"},
                native_subcontext={"type": "boolean"},
                goal={"type": "string"},
            ),
            additionalProperties=False,
        ),
    ),
    dict(
        name="knowledge_feedback",
        description="Record discovered/loaded/applied/evaluated usage evidence for a feedback_ref.",
        required=["ref"],
        optional=["rating", "reason", "event_type", "run_id", "step_id", "outcome", "evidence_source"],
        inputSchema=dict(
            type="object",
            properties=dict(
                ref={"type": "string"},
                rating={"type": "string", "enum": ["up", "down"]},
                reason={"type": "string"},
                event_type={
                    "type": "string",
                    "enum": ["discovered", "loaded", "applied", "evaluated"],
                },
                run_id={"type": "string"},
                step_id={"type": "string"},
                outcome={
                    "type": "string",
                    "enum": ["supported", "refuted", "inconclusive"],
                },
                evidence_source={"type": "string"},
            ),
            required=["ref"],
            additionalProperties=False,
        ),
    ),
    dict(
        name="knowledge_distill",
        description="Distill verified milestone or completed evidence into graded L1-L4 knowledge units.",
        required=["inputs", "task_state"],
        optional=["related_refs", "proposal", "domain", "topic", "target_signature"],
        inputSchema=dict(
            type="object",
            properties=dict(
                inputs={"type": "array", "items": {"type": "object"}},
                task_state={
                    "type": "string",
                    "enum": ["in_flight", "verified_milestone", "completed"],
                },
                domain={"type": "string"},
                related_refs={"type": "array", "items": {"type": "string"}},
                proposal={"type": "object"},
                topic={"type": "string"},
                target_signature={"type": "string"},
            ),
            required=["inputs", "task_state"],
            additionalProperties=False,
        ),
    ),
    dict(
        name="knowledge_sync",
        description="Synchronize configured public knowledge feeds into the local graded store.",
        required=[],
        optional=["domain"],
        inputSchema=dict(
            type="object",
            properties=dict(domain={"type": "string"}),
            additionalProperties=False,
        ),
    ),
]

KNOWLEDGE_NAMES = frozenset(item["name"] for item in PRIVATE_KNOWLEDGE_CATALOG)


def _resolve_session(explicit: str | None = None) -> str:
    if explicit is not None:
        return require_session(explicit)
    for env_key in ("WB_CONVERSATION_ID", "WORKBUDDY_SESSION_ID", "CODEBUDDY_SESSION_ID", "AGY_CONVERSATION_ID"):
        raw = os.environ.get(env_key)
        if isinstance(raw, str) and raw.strip():
            return require_session(raw)
    return "wb-cli-default"


def _select_engine_config(
    domain: str | None = None,
    ref: str | None = None,
    *,
    config: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> tuple[str, dict, bool]:
    cfg_file = config_path(config)
    default_eng = str(engine_config_path(cfg_file))
    if not Path(default_eng).is_file():
        from setup import ensure_default_profile

        ensure_default_profile(config=cfg_file, workspace_root=None)
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


def execute_knowledge_tool(
    tool_name: str,
    args: dict,
    *,
    session: str | None = None,
    project_root: str | Path | None = None,
    config: str | Path | None = None,
) -> dict:
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be a JSON object")
    if FORBIDDEN_IDENTITY_KEYS & set(args):
        raise ValueError("tool arguments must not include native session identity keys")
    if tool_name not in KNOWLEDGE_NAMES:
        raise ValueError(f"unknown knowledge tool: {tool_name}")

    resolved_session = _resolve_session(session)
    if is_deactivated(resolved_session, config=config):
        raise KnowledgeInactive(f"session {resolved_session} is deactivated for MindIE knowledge")

    body = dict(args)
    requested_domain = body.pop("domain", None) if tool_name in {"knowledge_query", "knowledge_distill", "knowledge_sync"} else None
    ref_for_route = None
    if tool_name == "knowledge_explain":
        refs = body.get("refs")
        cand = body.get("ref") or (refs[0] if isinstance(refs, list) and refs else None)
        ref_for_route = cand if isinstance(cand, str) and cand.startswith("mindie://") else None
    elif tool_name == "knowledge_feedback":
        cand = body.get("ref")
        ref_for_route = cand if isinstance(cand, str) and cand.startswith("mindie://") else None

    engine_path, engine_cfg, routed = _select_engine_config(
        domain=requested_domain,
        ref=ref_for_route,
        config=config,
        workspace_root=project_root,
    )
    lease = ensure_default_lease(resolved_session, project_root=project_root, not_before=0.0, config=config)
    adm_store = gate(config)
    eng_adm = engine_cfg.get("admission_path")
    if isinstance(eng_adm, str) and os.path.isabs(eng_adm) and Path(eng_adm).resolve() != admission_path(config).resolve():
        from mindie_knowledge.loop.activation import Admission

        adm_store = Admission(eng_adm)
        if adm_store.inspect(resolved_session).get("status") == "inactive":
            raise KnowledgeInactive(f"session {resolved_session} is deactivated in domain {engine_cfg.get('domain')}")
        adm_store.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lease = adm_store.activate(
            resolved_session,
            project_root=lease.get("project_root") or str(Path(project_root or os.getcwd()).resolve()),
            root_session=resolved_session,
            not_before=0.0,
        )
    token = lease.get("token")

    conn = _ensure_loop_connection(engine_path)
    from mindie_knowledge.loop import summary_budget
    from mindie_knowledge.loop.cli import distill_selected
    from mindie_knowledge.loop.transport import rpc

    method = tool_name.removeprefix("knowledge_")
    succeeded = False
    neutral = False
    out_res = None
    try:
        if method == "distill":
            if not token:
                raise ValueError("knowledge_distill requires an active capture lease in the target domain")
            payload = {
                "inputs": body.get("inputs"),
                "task_state": body.get("task_state"),
                "_session_id": resolved_session,
                "_activation": token,
            }
            for opt_key in ("related_refs", "proposal", "topic", "target_signature"):
                if opt_key in body:
                    payload[opt_key] = body[opt_key]
            if not any(k in body for k in ("related_refs", "proposal", "topic", "target_signature")):
                raw_distill = distill_selected(engine_path, payload)
            else:
                raw_distill = rpc(conn, "distill", payload, timeout=summary_budget.distill_rpc_timeout())
            from mindie_knowledge.graded.distill import result_view

            out_res, distill_err = result_view(raw_distill)
            succeeded = not distill_err
            return out_res

        if method == "query":
            from mindie_knowledge.materials.provenance import query_request

            query_request(body.get("query"), body.get("limit", 5), body.get("conditions"), body.get("continuation"))

        payload = dict(body, _session_id=resolved_session, _session_verified=True)
        if routed:
            payload["domain"] = engine_cfg["domain"]
        rpc_timeout = KNOWLEDGE_SYNC_TIMEOUT if method == "sync" else KNOWLEDGE_RPC_TIMEOUT
        raw_res = rpc(conn, method, payload, timeout=rpc_timeout)
        out_res = {"status": "ok", "feeds": raw_res} if (method == "sync" and isinstance(raw_res, list)) else raw_res
        succeeded = out_res.get("status") != "error" if isinstance(out_res, dict) else True
        return out_res
    finally:
        pending = sys.exc_info()[1]
        if pending is not None and isinstance(pending, (TimeoutError, ConnectionError)):
            neutral = True
        if token is not None and not neutral:
            try:
                adm_store.finish(resolved_session, token, succeeded)
            except Exception as exc:
                if pending is None and isinstance(out_res, dict):
                    out_res["receipt_warning"] = f"{type(exc).__name__}: {exc}"
                    out_res["receipt_unconfirmed"] = True


def invoke(
    tool: str,
    args: dict | None = None,
    *,
    session: str | None = None,
    config: str | Path | None = None,
) -> dict:
    """Programmatically invoke a `knowledge_cli` command (`describe`, `mindie_status`, `knowledge_*`)."""
    if tool == "mindie_status":
        session_opt = None
        try:
            session_opt = _resolve_session(session)
        except ValueError:
            session_opt = None
        return dict(result=status_facts(session_opt, config=config), isError=False)
    resolved_session = _resolve_session(session)
    if is_deactivated(resolved_session, config=config):
        raise KnowledgeInactive(f"session {resolved_session} is deactivated")
    if tool == "describe":
        lease = knowledge_lease(resolved_session, config=config)
        return dict(
            session_id=resolved_session,
            activated=True,
            project_root=lease.get("project_root") if lease else None,
            tools=PRIVATE_KNOWLEDGE_CATALOG,
            isError=False,
        )
    res = execute_knowledge_tool(tool, args or {}, session=resolved_session, config=config)
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
    parser.add_argument("--config", default=None, help="Optional wb.json path")
    parser.add_argument("--json", dest="json_arg", default=None, help="Inline JSON object arguments")
    parser.add_argument("--input-file", dest="input_file", default=None, help="UTF-8 JSON file containing tool arguments")
    parsed = parser.parse_args(argv)

    try:
        if parsed.tool in {"mindie_status", "describe"}:
            out = invoke(parsed.tool, session=parsed.session, config=parsed.config)
            sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            return 0

        session = _resolve_session(parsed.session)
        if parsed.input_file is not None:
            with Path(parsed.input_file).open("rb") as stream:
                raw_bytes = stream.read(MAX_INPUT_BYTES + 1)
            if len(raw_bytes) > MAX_INPUT_BYTES:
                raise ValueError("knowledge arguments exceed 1 MiB byte budget")
            raw_in = raw_bytes.decode("utf-8-sig")
        elif parsed.json_arg is not None:
            if len(parsed.json_arg.encode("utf-8")) > MAX_INPUT_BYTES:
                raise ValueError("knowledge arguments exceed 1 MiB byte budget")
            raw_in = parsed.json_arg.lstrip("\ufeff")
        else:
            raw_bytes = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw_bytes) > MAX_INPUT_BYTES:
                raise ValueError("knowledge arguments exceed 1 MiB byte budget")
            raw_in = raw_bytes.decode("utf-8-sig")
        args = json.loads(raw_in) if raw_in and raw_in.strip() else {}
        result = execute_knowledge_tool(parsed.tool, args, session=session, config=parsed.config)
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
