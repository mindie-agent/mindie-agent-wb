"""WorkBuddy lifecycle hooks and knowledge RPC transport fallback.
[Role]
Routes native hook payloads (`SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`Stop`) and provides `knowledge_transport` when `mindie_harness` reports
`knowledge_unavailable` for an in-process `mindie_knowledge` checkout.
[Invariants]
- Ordinary prompt/tool hooks create zero SQLite files and never auto-approve a
  host permission prompt; forged `_session_id` / `mindie_activation` keys deny.
- `Stop` re-verifies sharing tier (`contribute` or `local-only`) and project
  scope, preserves explicit `inactive` revocation, and activates `not_before=0.0`.
- Receipt Honesty: a successful primary knowledge call keeps `isError=False` with
  `receipt_warning` + `receipt_unconfirmed=True` if `Admission.finish` fails.
[Timeouts & Locks]
- `capture_hook` is bounded to `6.0s`; knowledge RPC uses `30s` reads and `315s` sync.
[Side-Effect Policy]
- `session-start` writes the pending-update marker; `stop` and `knowledge_*`
  calls may touch admission/engine state within the resolved profile.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import sys
from mindie_harness.api import ProfileRef
from mindie_harness.mcp.receipts import apply_receipt_honesty
from mindie_harness.sharing import sharing_status
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from mindie_launch import PLUGIN_ROOT, SESSION_RE, _config_path, _event, _forged, _read_json, _workspace, _write_pending
MAX_HOOK_BYTES = 1024 * 1024
SUPPRESS = {"suppressOutput": True}
def _emit(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0
def _profile(config=None) -> ProfileRef:
    return ProfileRef.resolve("wb", plugin_root=PLUGIN_ROOT, config_path=Path(config) if config else _config_path())
def _engine_path(profile: ProfileRef) -> Path:
    value = _read_json(profile.config_path).get("engine_config")
    return Path(value) if isinstance(value, str) and os.path.isabs(value) else profile.config_path.parent / "wb.engine.json"
def _error(message: str, reason: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True, "structuredContent": {"stage": "dispatch", "reason": reason, "message": message, "retryable": False}}
def _envelope(raw, *, is_error: bool = False) -> dict:
    if isinstance(raw, dict) and isinstance(raw.get("content"), list):
        return raw
    return {"content": [{"type": "text", "text": json.dumps(raw, ensure_ascii=False, default=str, sort_keys=True)}], "isError": bool(is_error), "structuredContent": raw if isinstance(raw, dict) else {"result": raw}}
def knowledge_transport(ctx, arguments: dict, profile: ProfileRef) -> dict:
    """Fallback RPC bridge to `mindie_knowledge.loop.transport` with Receipt Honesty."""
    try:
        from mindie_knowledge.loop import transport
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.cli import distill_selected, ensure_service
    except TimeoutError:
        raise
    except ImportError:
        return _error("mindie_knowledge transport is unavailable in this interpreter.", "knowledge_unavailable")
    method, engine, admission, token, neutral = ctx.tool_name.removeprefix("knowledge_"), _engine_path(profile), None, None, False
    result = _error("Knowledge transport did not complete.", "knowledge_rpc_failed")
    try:
        admission = Admission(profile.admission_db)
        if admission.inspect(ctx.session_id).get("status") == "inactive":
            return _error("MindIE knowledge is explicitly deactivated for this session.", "knowledge_inactive")
        lease = admission.activate(ctx.session_id, project_root=str(arguments.get("project_root") or ctx.cwd or os.getcwd()), root_session=ctx.session_id, not_before=0.0)
        token = lease.get("token")
        connection = ensure_service(str(engine))
        payload = dict(arguments, _session_id=ctx.session_id, _session_verified=True)
        if method == "distill":
            payload = {key: arguments[key] for key in ("inputs", "task_state", "related_refs", "proposal", "domain", "topic", "target_signature") if key in arguments}
            payload.update(_session_id=ctx.session_id, _activation=token)
            raw = transport.rpc(connection, "distill", payload, timeout=315.0) if any(key in arguments for key in ("related_refs", "proposal", "topic", "target_signature")) else distill_selected(str(engine), payload)
            from mindie_knowledge.graded.distill import result_view
            body, is_error = result_view(raw)
            result = _envelope(body, is_error=is_error)
        else:
            raw = transport.rpc(connection, method, payload, timeout=315.0 if method == "sync" else 30.0)
            body = {"status": "ok", "feeds": raw} if method == "sync" and isinstance(raw, list) else raw
            result = _envelope(body, is_error=isinstance(body, dict) and body.get("status") == "error")
    except TimeoutError:
        neutral = True
        raise
    except Exception as exc:
        neutral = isinstance(exc, ConnectionError)
        reason = "knowledge_outcome_unknown" if neutral and method in {"distill", "feedback", "sync"} else "knowledge_rpc_failed"
        result = _error(f"{type(exc).__name__}: {exc}", reason)
    finally:
        if admission is not None and token is not None and not neutral:
            try:
                admission.finish(ctx.session_id, token, result.get("isError") is not True)
            except TimeoutError as exc:
                if result.get("isError") is not True:
                    result = apply_receipt_honesty(result, exc)
            except Exception as exc:
                if result.get("isError") is not True:
                    result = apply_receipt_honesty(result, exc)
    return result
def _session(event: dict) -> str | None:
    value = event.get("session_id") or event.get("conversationId")
    return value.strip().lower() if isinstance(value, str) and SESSION_RE.fullmatch(value.strip()) else None
def _workbuddy_roots() -> list[Path]:
    explicit = os.environ.get("WORKBUDDY_CONFIG_DIR") or os.environ.get("WORKBUDDY_HOME")
    if explicit:
        return [Path(explicit).expanduser()]
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    if os.name == "nt":
        return [Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "workbuddy"]
    return [home / ".workbuddy", home / ".config" / "workbuddy"]
def _transcript(event: dict, session: str) -> Path | None:
    raw = event.get("transcript_path") or event.get("transcriptPath")
    if isinstance(raw, str) and os.path.isabs(raw) and Path(raw).is_file():
        return Path(raw)
    found = [path for root in _workbuddy_roots() for path in root.glob("projects/*/*.jsonl") if path.stem.lower() == session]
    return max(found, key=lambda path: path.stat().st_mtime_ns) if found else None
def _turn_id(event: dict, session: str, transcript: Path) -> str:
    for key in ("turn_id", "turnId", "message_id", "messageId", "event_id", "eventId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:256]
    try:
        stat = transcript.stat()
        return f"{session}:stop:{stat.st_size}:{stat.st_mtime_ns}"[:256]
    except TimeoutError:
        raise
    except OSError:
        return f"{session}:stop"[:256]
def handle_session_start(event: dict, *, config=None) -> dict:
    _write_pending()
    return dict(SUPPRESS)
def handle_user_prompt_submit(event: dict, *, config=None) -> dict:
    return dict(SUPPRESS)
def handle_pre_tool_use(event: dict, *, config=None) -> dict:
    if _forged(event):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "forged session identity keys are not accepted"}}
    return dict(SUPPRESS)
def handle_stop(event: dict, *, config=None) -> dict:
    """Consent-first `Stop` handoff with explicit-revocation preservation."""
    workspace, session = _workspace(event), _session(event)
    if event.get("stop_hook_active") is True or workspace is None or session is None:
        return dict(SUPPRESS)
    profile = _profile(config)
    status = sharing_status(profile, cwd=workspace)
    transcript, engine_path = _transcript(event, session), _engine_path(profile)
    if status.get("tier") not in {"contribute", "local-only"} or not status.get("cwd_in_scope") or transcript is None or not engine_path.is_file():
        return dict(SUPPRESS)
    try:
        from mindie_knowledge.loop.activation import Admission
        admission = Admission(profile.admission_db)
        if admission.inspect(session).get("status") == "inactive":
            return dict(SUPPRESS)
        lease = admission.activate(session, project_root=workspace, root_session=session, not_before=0.0)
        import wb_transcript
        source = wb_transcript.capture_source(transcript, session, binding=lease)
    except TimeoutError:
        raise
    except Exception as exc:
        print(f"MindIE Stop binding warning: {type(exc).__name__}: {exc}", file=sys.stderr)
        return dict(SUPPRESS)
    try:
        engine = _read_json(engine_path)
        if isinstance(engine.get("root"), str) and isinstance(engine.get("domain"), str):
            from mindie_knowledge.loop.handoff import _store_path, prepare_schema
            if not _store_path(engine).is_file():
                prepare_schema(str(engine_path))
        from mindie_knowledge.loop.cli import capture_hook
        capture_hook(str(engine_path), {
            "hook_event_name": "Stop", "identity_kind": "turn",
            "turn_id": _turn_id(event, session, Path(source["transcript_path"])), "harness": "wb",
            "session_id": session, "mindie_activation": lease.get("token"),
            "transcript_path": source["transcript_path"], "cwd": source["project_root"],
        }, timeout=6.0)
    except TimeoutError:
        raise
    except Exception as exc:
        print(f"MindIE Stop capture warning: {type(exc).__name__}: {exc}", file=sys.stderr)
    return dict(SUPPRESS)
def main(argv: list[str] | None = None, *, raw: bytes | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "session-start"
    handlers = {
        "session-start": handle_session_start, "user-prompt-submit": handle_user_prompt_submit,
        "user-prompt-expansion": handle_user_prompt_submit, "pre-invocation": handle_user_prompt_submit,
        "pre-tool-use": handle_pre_tool_use, "stop": handle_stop,
    }
    if action not in handlers:
        raise ValueError(f"unknown bridge hook action: {action}")
    payload = raw if raw is not None else sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
    return _emit(handlers[action](_event(payload)))
if __name__ == "__main__":
    raise SystemExit(main())
