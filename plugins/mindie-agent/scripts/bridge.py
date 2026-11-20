"""WorkBuddy native lifecycle hook handlers (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `Stop`).
[Role]
Routes native WorkBuddy & CodeBuddy hook payloads and delegates authorized `Stop`
transcript capture to `mindie_harness.runtime.knowledge_binding.run_stop_capture`.
[Invariants]
- Ordinary prompt/tool hooks create zero SQLite files and never auto-approve a
  host permission prompt; forged `_session_id` / `mindie_activation` keys deny.
- `Stop` exits `< 100ms` with zero SQLite creation and zero engine provisioning when
  unconfigured, disabled, or outside `project_roots`, preserves explicit `inactive`
  revocation, and uses `identity_kind="notification"` when native `turn_id` is absent.
[Timeouts & Locks]
- `run_stop_capture` is bounded by `timeout_sec=6.0`; passive hooks acquire no locks.
[Side-Effect Policy]
- `session-start` writes the pending-update marker; authorized `stop` delegates
  consent check, lazy provisioning, and capture admission to `run_stop_capture`.
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from mindie_harness.api import ProfileRef
from mindie_harness.runtime.knowledge_binding import run_stop_capture
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path: sys.path.insert(0, str(_HERE))
from mindie_launch import PLUGIN_ROOT, SESSION_RE, _config_path, _event, _forged, _may_capture_stop, _workspace, _write_pending
MAX_HOOK_BYTES = 1024 * 1024
SUPPRESS = {"suppressOutput": True}
def _emit(payload: dict) -> int:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0
def _profile(config=None) -> ProfileRef:
    return ProfileRef.resolve("wb", plugin_root=PLUGIN_ROOT, config_path=Path(config) if config else _config_path())
def _session(event: dict) -> str | None:
    value = event.get("session_id") or event.get("conversationId")
    return value.strip().lower() if isinstance(value, str) and SESSION_RE.fullmatch(value.strip()) else None
def _workbuddy_roots() -> list[Path]:
    explicit = os.environ.get("WORKBUDDY_CONFIG_DIR") or os.environ.get("WORKBUDDY_HOME")
    if explicit: return [Path(explicit).expanduser()]
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    if os.name == "nt":
        return [Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "workbuddy"]
    return [home / ".workbuddy", home / ".config" / "workbuddy"]
def _transcript(event: dict, session: str) -> Path | None:
    raw = event.get("transcript_path") or event.get("transcriptPath")
    if isinstance(raw, str) and os.path.isabs(raw) and Path(raw).is_file(): return Path(raw)
    found = [path for root in _workbuddy_roots() for path in root.glob("projects/*/*.jsonl") if path.stem.lower() == session]
    return max(found, key=lambda path: path.stat().st_mtime_ns) if found else None
def _stop_identity(event: dict, session: str, transcript: Path) -> dict[str, str]:
    for key in ("turn_id", "turnId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip(): return {"identity_kind": "turn", "turn_id": value.strip()[:256]}
    for key in ("event_id", "eventId", "message_id", "messageId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip(): return {"identity_kind": "notification", "event_id": value.strip()[:256]}
    try:
        stat = transcript.stat()
        event_id = f"{session}:stop:{stat.st_size}:{stat.st_mtime_ns}"[:256]
    except TimeoutError: raise
    except OSError: event_id = f"{session}:stop"[:256]
    return {"identity_kind": "notification", "event_id": event_id}
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
    """Consent-first `Stop` handoff delegating directly to `run_stop_capture`."""
    workspace, session = _workspace(event), _session(event)
    if workspace is None or session is None or not _may_capture_stop(event, config=config):
        return dict(SUPPRESS)
    transcript = _transcript(event, session)
    if transcript is None: return dict(SUPPRESS)
    profile = _profile(config)
    payload = {"hook_event_name": "Stop", "harness": "wb", "session_id": session, "cwd": str(Path(workspace).resolve()), "transcript_path": str(transcript), **_stop_identity(event, session, transcript)}
    try:
        run_stop_capture(profile, payload, plugin_root=PLUGIN_ROOT, timeout_sec=6.0)
    except TimeoutError: raise
    except Exception as exc:
        print(f"MindIE Stop capture warning: {type(exc).__name__}: {exc}", file=sys.stderr)
    return dict(SUPPRESS)
def main(argv: list[str] | None = None, *, raw: bytes | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "session-start"
    if action == "session-start": return _emit(handle_session_start({}))
    if action in {"user-prompt-submit", "user-prompt-expansion", "pre-invocation"}: return _emit(handle_user_prompt_submit({}))
    handlers = {"pre-tool-use": handle_pre_tool_use, "stop": handle_stop}
    if action not in handlers: raise ValueError(f"unknown bridge hook action: {action}")
    payload = raw if raw is not None else sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
    return _emit(handlers[action](_event(payload)))
if __name__ == "__main__":
    raise SystemExit(main())
