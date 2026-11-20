"""Bounded incremental reader for Tencent WorkBuddy (`wb`) JSONL transcripts.
[Role]
Per-harness transcript adapter (`FileIdentity`, `identify`, `capture_source`,
`read_material`, `read_increment`) for WorkBuddy SessionStore JSONL files.
[Invariants]
- Supports WorkBuddy `SessionStore` records (`type == "message"`) and
  Claude-compatible `user` / `assistant` records.
- Exports only public user/assistant text: excludes `thinking`, `tool_use`,
  `tool_result`, `isMeta`, and `<system-reminder>` metadata blocks.
- Exact `[start, end)` SHA-256 byte accounting with a 512-byte anchor identity.
[Timeouts & Locks]
- `read_material` enforces `max_scan_bytes=16 MiB`, `max_seconds=2.0s`, and a
  200-record window without holding locks.
[Side-Effect Policy]
- Strictly read-only over the caller-specified transcript path.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
MAX_TEXT, MAX_RECORDS, ANCHOR_BYTES = 48 * 1024, 200, 512
SOURCE_HEAD_BYTES, SOURCE_HEAD_LINES = 1024 * 1024, 64
RECOGNIZED_TYPES = {"message", "session-meta", "user", "assistant", "system", "function_call", "function_call_output", "reasoning", "file-history-snapshot"}
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z")
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
STRIP_TAGS_RE = re.compile(r"<(?:system-reminder|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)\b[^>]*>.*?</(?:system-reminder|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>", re.DOTALL)
GESTURE_PREFIX_RE = re.compile(r"^\s*(?:/mindie-agent(?::mindie-agent)?|\$mindie-agent(?::mindie-agent)?)(?:\s+|$)")
def _strip_win_extended_prefix(raw: str) -> str:
    if os.name != "nt":
        return raw
    return "\\\\" + raw[8:] if raw.startswith("\\\\?\\UNC\\") else (raw[4:] if raw.startswith("\\\\?\\") else raw)
def _canonical_path(path: str | Path) -> str:
    try:
        resolved = str(Path(path).resolve())
    except TimeoutError:
        raise
    except OSError:
        resolved = str(Path(path))
    cleaned = _strip_win_extended_prefix(resolved)
    return os.path.normcase(cleaned) if os.name == "nt" else cleaned
@dataclass(frozen=True)
class FileIdentity:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    anchor_len: int = 0
    anchor_digest: str = ""
    def serialize(self) -> str:
        return json.dumps(dict(dev=self.dev, ino=self.ino, anchor_len=self.anchor_len, anchor_digest=self.anchor_digest), sort_keys=True, separators=(",", ":"))
    @staticmethod
    def unserialize(text: str, path: str) -> FileIdentity | None:
        try:
            parsed = json.loads(text)
            anchor_len, anchor_digest = parsed["anchor_len"], parsed["anchor_digest"]
            if type(anchor_len) is not int or not 0 < anchor_len <= ANCHOR_BYTES or not isinstance(anchor_digest, str) or len(anchor_digest) != 64:
                return None
            return FileIdentity(_canonical_path(path), int(parsed["dev"]), int(parsed["ino"]), 0, 0, anchor_len, anchor_digest)
        except (ValueError, KeyError, TypeError):
            return None
def identify(path: str | Path) -> FileIdentity | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    try:
        stat, anchor = os.fstat(fd), os.read(fd, ANCHOR_BYTES)
    except TimeoutError:
        raise
    except OSError:
        return None
    finally:
        os.close(fd)
    return FileIdentity(_canonical_path(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, len(anchor), hashlib.sha256(anchor).hexdigest())
def to_seconds(raw) -> float | None:
    if type(raw) in (int, float) and raw > 0:
        value = float(raw) / (1000.0 if raw >= 1e12 else 1.0)
        return value if math.isfinite(value) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
def _session_in_path(path: str | Path) -> str | None:
    stem = Path(path).stem
    if SESSION_ID_RE.fullmatch(stem) and stem.lower() not in {"transcript", "transcript_full"}:
        return stem.lower()
    for part in reversed(Path(path).parts[:-1]):
        if SESSION_ID_RE.fullmatch(part) and part.lower() not in {"projects", "subagents", "logs", "brain"}:
            return part.lower()
    return None
def capture_source(path: str | Path, session_id: str, *, binding: dict | None = None) -> dict:
    """Validate a WorkBuddy transcript path and return its capture source descriptor."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id.strip()):
        raise ValueError("native WorkBuddy session_id is missing or invalid")
    norm_session = session_id.strip().lower()
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise ValueError("capture transcript path must be absolute")
    try:
        selected = Path(path).resolve(strict=True)
    except TimeoutError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError("capture transcript is unavailable") from exc
    if not selected.is_file() or _session_in_path(selected) != norm_session:
        raise ValueError("capture transcript does not belong to this session_id")
    with selected.open("rb") as stream:
        head = stream.read(SOURCE_HEAD_BYTES)
    created = None
    for raw in head.split(b"\n")[:SOURCE_HEAD_LINES]:
        try:
            record = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (ValueError, UnicodeDecodeError):
            record = None
        if isinstance(record, dict):
            stamp = to_seconds(record.get("timestamp") or record.get("created_at") or record.get("createdAt"))
            if stamp is not None and math.isfinite(stamp):
                created = stamp
                break
    if created is None:
        raise ValueError("native WorkBuddy task creation timestamp is unavailable")
    if binding is None:
        raise ValueError("native task binding is required to establish project scope")
    scope, boundary = binding.get("project_root"), binding.get("activated_at")
    if (
        str(binding.get("session", "")).lower() != norm_session or type(binding.get("enabled")) not in (bool, int)
        or int(binding["enabled"]) != 1 or not isinstance(scope, str) or not os.path.isabs(scope)
        or type(boundary) not in (int, float) or not math.isfinite(boundary) or boundary < 0
    ):
        raise ValueError("native task binding is invalid")
    return dict(session_id=norm_session, project_root=_strip_win_extended_prefix(str(Path(scope).resolve())), created_at=created, transcript_path=_strip_win_extended_prefix(str(selected)))
def _clean_user_text(raw: str) -> str:
    stripped = STRIP_TAGS_RE.sub("", raw)
    match = USER_REQUEST_RE.search(stripped)
    text = (match.group(1) if match is not None else stripped).strip()
    if GESTURE_PREFIX_RE.match(text):
        text = GESTURE_PREFIX_RE.sub("", text, count=1).strip()
        if text in {"", "deactivate", "status", "init", "recover"}:
            return ""
    return text
def _extract_content_blocks(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        item["text"] for item in content if isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}
        and isinstance(item.get("text"), str) and not item["text"].startswith("<persisted-output>")
    ) if isinstance(content, list) else ""
def _extract(record: dict) -> tuple[str, str] | None:
    pdata = record.get("providerData")
    if record.get("isMeta") is True or (isinstance(pdata, dict) and (pdata.get("isMeta") is True or pdata.get("modLog") is True)):
        return None
    if record.get("type") not in {"message", "user", "assistant"}:
        return None
    message = record.get("message") if isinstance(record.get("message"), dict) else record
    role = message.get("role") or record.get("role")
    raw_text = _extract_content_blocks(message.get("content") if "content" in message else record.get("content"))
    if role == "user":
        cleaned = _clean_user_text(raw_text)
        return ("user", cleaned) if cleaned else None
    if role == "assistant":
        cleaned = STRIP_TAGS_RE.sub("", raw_text).strip()
        return ("assistant", cleaned) if cleaned else None
    return None
def read_material(path: str | Path, start: int, *, session_id: str | None = None, not_before: float | str | None = None,
                  expected: FileIdentity | None = None, max_scan_bytes: int = 16777216, max_seconds: float = 2.0,
                  max_text_bytes: int = MAX_TEXT, scan_until: int | None = None) -> dict:
    if type(start) is not int or start < 0:
        raise ValueError("start must be a nonnegative byte offset")
    if max_scan_bytes <= 0 or max_seconds <= 0 or max_text_bytes <= 0:
        raise ValueError("invalid scan or text budget")
    if scan_until is not None and (type(scan_until) is not int or scan_until < start):
        raise ValueError("scan_until must be an integer byte boundary at or after start")
    boundary = to_seconds(not_before) if not_before is not None else None
    norm_session = session_id.strip().lower() if isinstance(session_id, str) and session_id.strip() else None
    result = dict(status="ok", start=start, end=start, digest=hashlib.sha256(b"").hexdigest(), text="", records=0,
                  skipped_records=0, oversize_records=0, partial=False, more=False, timestamps_reliable=True,
                  session_match=None, coverage_note=None)
    consumed, included, text_size, recognized, begun = hashlib.sha256(), [], 0, 0, time.monotonic()
    try:
        with open(path, "rb") as stream:
            stat = os.fstat(stream.fileno())
            anchor = stream.read(ANCHOR_BYTES)
            current = FileIdentity(_canonical_path(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, len(anchor), hashlib.sha256(anchor).hexdigest())
            result.update(identity=current.serialize(), snapshot_size=stat.st_size)
            if expected is not None:
                stream.seek(0)
                if (expected.path != current.path or expected.dev != stat.st_dev or expected.ino != stat.st_ino
                        or not expected.anchor_len or hashlib.sha256(stream.read(expected.anchor_len)).hexdigest() != expected.anchor_digest):
                    return dict(result, status="replaced", coverage_note="transcript changed before read")
            if stat.st_size < start:
                return dict(result, status="replaced", coverage_note="transcript truncated")
            owner = _session_in_path(path)
            if owner:
                result["session_match"] = not norm_session or owner == norm_session
                if norm_session and owner != norm_session:
                    return dict(result, status="wrong-task", coverage_note="transcript belongs to another task")
            elif norm_session:
                return dict(result, status="unknown-format", coverage_note="task path identity unavailable; no public read")
            stream.seek(max(0, start - 1))
            if start > 0 and stream.read(1) != b"\n":
                return dict(result, status="invalid-boundary", coverage_note="cursor is not a whole-record boundary")
            stream.seek(start)
            effective_end = min(stat.st_size, scan_until) if scan_until is not None else stat.st_size
            end_limit = min(effective_end, start + max_scan_bytes)
            while stream.tell() < end_limit and time.monotonic() - begun < max_seconds:
                offset = stream.tell()
                raw = stream.readline(min(effective_end - offset, end_limit - offset))
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    result["partial"] = offset + len(raw) == stat.st_size
                    break
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    record = None
                if not isinstance(record, dict):
                    consumed.update(raw)
                    result["end"], result["skipped_records"] = stream.tell(), result["skipped_records"] + 1
                    continue
                recognized += 1 if record.get("type") in RECOGNIZED_TYPES or "sessionId" in record else 0
                rec_session = record.get("sessionId")
                if norm_session and isinstance(rec_session, str) and rec_session.strip() and rec_session.strip().lower() != norm_session:
                    return dict(result, status="wrong-task", coverage_note="transcript record belongs to another sessionId")
                stamp, extracted = to_seconds(record.get("timestamp") or record.get("created_at") or record.get("createdAt")), _extract(record)
                if extracted and extracted[1]:
                    if boundary is not None and stamp is None:
                        consumed.update(raw)
                        result["end"], result["skipped_records"] = stream.tell(), result["skipped_records"] + 1
                        continue
                    if boundary is None or stamp >= boundary:
                        rendered = f"### {extracted[0]}\n" + extracted[1].replace("\r\n", "\n").replace("\r", "\n")
                        encoded = rendered.encode("utf-8")
                        if not included and len(encoded) > max_text_bytes:
                            rendered = encoded[:max_text_bytes].decode("utf-8", errors="ignore")
                            encoded = rendered.encode("utf-8")
                            result["oversize_records"] += 1
                        size = len(encoded) + 2
                        if included and (text_size + size > max_text_bytes or len(included) >= MAX_RECORDS):
                            break
                        included.append(rendered)
                        text_size += size
                if not extracted or not extracted[1]:
                    result["skipped_records"] += 1
                consumed.update(raw)
                result["end"] = stream.tell()
            result["more"] = result["end"] < effective_end
    except TimeoutError:
        raise
    except OSError:
        return dict(result, status="missing", coverage_note="transcript unreadable")
    result.update(digest=consumed.hexdigest(), text="\n\n".join(included), records=len(included))
    established = expected is not None or result.get("session_match") is True
    if result["status"] != "ok":
        return result
    if result["end"] == start:
        result["status"] = "unchanged"
    elif not recognized and not (established and result["more"] and result["end"] > result["start"]):
        result.update(status="unknown-format", text="", coverage_note="no recognized native WorkBuddy transcript signature")
    return result
read_increment = read_material
