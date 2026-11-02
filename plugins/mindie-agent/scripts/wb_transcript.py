"""Bounded incremental reader for Tencent WorkBuddy (`wb`) JSONL transcripts.

[Role]
Per-Harness transcript adapter (`FileIdentity`, `identify`, `capture_source`,
`read_material`, `read_increment`) for WorkBuddy session JSONL files stored under
`<workbuddy_config_dir>/projects/<compressed-cwd>/<sessionId>.jsonl`.

[Invariants]
- Supports both WorkBuddy `SessionStore` records (`type == "message"`,
  `role in {"user", "assistant"}`, `type == "session-meta"`) and Claude-compatible
  records (`type in {"user", "assistant"}`, `message.role`).
- Extracts only public user and assistant text content; excludes `thinking`,
  `function_call`, `function_call_output`, `tool_use`, `tool_result`,
  `providerData.isMeta == True` internal continuation items, and
  `<system-reminder>` / `<ADDITIONAL_METADATA>` / `<SYSTEM_MESSAGE>` blocks.
- Enforces exact `[start, end)` SHA-256 byte accounting with a 512-byte prefix
  anchor (`FileIdentity`).

[Timeouts & Locks]
- `read_material` enforces `max_scan_bytes=16 MiB` and `max_seconds=2.0s` per
  incremental scan window without holding locks.

[Side-Effect Policy]
- Strictly read-only over the caller-specified transcript path; mutates no files.
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

MAX_WINDOW = 256 * 1024
MAX_TEXT = 48 * 1024
MAX_RECORDS = 200
ANCHOR_BYTES = 512
SOURCE_HEAD_BYTES = 1024 * 1024
SOURCE_HEAD_LINES = 64

RECOGNIZED_TYPES = {
    "message",
    "session-meta",
    "user",
    "assistant",
    "system",
    "function_call",
    "function_call_output",
    "reasoning",
    "file-history-snapshot",
    "USER_INPUT",
    "PLANNER_RESPONSE",
}

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}\Z")
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
STRIP_TAGS_RE = re.compile(
    r"<(?:system-reminder|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)\b[^>]*>.*?</(?:system-reminder|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>",
    re.DOTALL,
)
GESTURE_PREFIX_RE = re.compile(
    r"^\s*(?:/mindie-agent(?::mindie-agent)?|\$mindie-agent(?::mindie-agent)?)(?:\s+|$)"
)


@dataclass(frozen=True)
class FileIdentity:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    anchor_len: int = 0
    anchor_digest: str = ""

    @property
    def key(self) -> str:
        return f"{self.path}|{self.dev}:{self.ino}"

    def serialize(self) -> str:
        return json.dumps(
            dict(
                dev=self.dev,
                ino=self.ino,
                anchor_len=self.anchor_len,
                anchor_digest=self.anchor_digest,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def unserialize(text: str, path: str) -> FileIdentity | None:
        try:
            data = json.loads(text)
            anchor_len = data["anchor_len"]
            anchor_digest = data["anchor_digest"]
            if (
                type(anchor_len) is not int
                or not 0 < anchor_len <= ANCHOR_BYTES
                or not isinstance(anchor_digest, str)
                or len(anchor_digest) != 64
            ):
                return None
            return FileIdentity(
                path,
                int(data["dev"]),
                int(data["ino"]),
                0,
                0,
                anchor_len,
                anchor_digest,
            )
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
        stat = os.fstat(fd)
        anchor = os.read(fd, ANCHOR_BYTES)
    except TimeoutError:
        raise
    except OSError:
        return None
    finally:
        os.close(fd)
    return FileIdentity(
        str(Path(path).resolve()),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        len(anchor),
        hashlib.sha256(anchor).hexdigest(),
    )


def to_seconds(raw) -> float | None:
    if type(raw) in (int, float) and raw > 0:
        value = float(raw)
        if value >= 1e12:
            value /= 1000.0
        return value if math.isfinite(value) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _session_in_path(path: str | Path) -> str | None:
    stem = Path(path).stem
    if SESSION_ID_RE.fullmatch(stem) and stem.lower() not in {"transcript", "transcript_full"}:
        return stem.lower()
    parts = Path(path).parts
    for part in reversed(parts[:-1]):
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
    if not selected.is_file():
        raise ValueError("capture source must be a regular transcript file")
    owner = _session_in_path(selected)
    if owner != norm_session:
        raise ValueError("capture transcript does not belong to this session_id")

    with selected.open("rb") as stream:
        head = stream.read(SOURCE_HEAD_BYTES)
    lines = head.split(b"\n")[:SOURCE_HEAD_LINES]
    created = None
    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        stamp = to_seconds(record.get("timestamp") or record.get("created_at") or record.get("createdAt"))
        if stamp is not None and math.isfinite(stamp):
            created = stamp
            break
    if created is None:
        raise ValueError("native WorkBuddy task creation timestamp is unavailable")

    if binding is not None:
        scope = binding.get("project_root")
        boundary = binding.get("activated_at")
        if (
            str(binding.get("session", "")).lower() != norm_session
            or type(binding.get("enabled")) not in (bool, int)
            or int(binding["enabled"]) != 1
            or not isinstance(scope, str)
            or not os.path.isabs(scope)
            or type(boundary) not in (int, float)
            or not math.isfinite(boundary)
            or boundary < 0
        ):
            raise ValueError("native task binding is invalid")
        return dict(
            session_id=norm_session,
            project_root=str(Path(scope).resolve()),
            created_at=created,
            transcript_path=str(selected),
        )
    raise ValueError("native task binding is required to establish project scope")


def _clean_user_text(raw: str) -> str:
    match = USER_REQUEST_RE.search(raw)
    text = match.group(1) if match is not None else STRIP_TAGS_RE.sub("", raw)
    text = STRIP_TAGS_RE.sub("", text).strip()
    if GESTURE_PREFIX_RE.match(text):
        text = GESTURE_PREFIX_RE.sub("", text, count=1).strip()
        if text in {"", "deactivate", "status", "init", "recover"}:
            return ""
    return text


def _extract_content_blocks(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            btype = item.get("type")
            if btype in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
                text = item["text"]
                if not text.startswith("<persisted-output>"):
                    chunks.append(text)
        return "\n".join(chunks)
    return ""


def _extract(record: dict) -> tuple[str, str] | None:
    if record.get("isMeta") is True:
        return None
    pdata = record.get("providerData")
    if isinstance(pdata, dict) and (pdata.get("isMeta") is True or pdata.get("modLog") is True):
        return None

    rtype = record.get("type")
    if rtype == "message":
        msg = record.get("message") if isinstance(record.get("message"), dict) else record
        role = msg.get("role") or record.get("role")
        raw_content = msg.get("content") if "content" in msg else record.get("content")
        raw_text = _extract_content_blocks(raw_content)
        if role == "user":
            cleaned = _clean_user_text(raw_text)
            return ("user", cleaned) if cleaned else None
        if role == "assistant":
            cleaned = STRIP_TAGS_RE.sub("", raw_text).strip()
            return ("assistant", cleaned) if cleaned else None
        return None

    if rtype in {"user", "assistant"}:
        msg = record.get("message")
        raw_text = _extract_content_blocks(msg.get("content") if isinstance(msg, dict) else record.get("content"))
        if rtype == "user":
            cleaned = _clean_user_text(raw_text)
            return ("user", cleaned) if cleaned else None
        cleaned = STRIP_TAGS_RE.sub("", raw_text).strip()
        return ("assistant", cleaned) if cleaned else None

    if rtype == "USER_INPUT" and record.get("source") == "USER_EXPLICIT":
        raw_text = record.get("content")
        if isinstance(raw_text, str):
            cleaned = _clean_user_text(raw_text)
            return ("user", cleaned) if cleaned else None
    if rtype == "PLANNER_RESPONSE" and record.get("source") == "MODEL":
        raw_text = record.get("content")
        if isinstance(raw_text, str):
            cleaned = STRIP_TAGS_RE.sub("", raw_text).strip()
            return ("assistant", cleaned) if cleaned else None
    return None


def read_material(
    path: str | Path,
    start: int,
    *,
    session_id: str | None = None,
    not_before: float | str | None = None,
    expected: FileIdentity | None = None,
    max_scan_bytes: int = 16777216,
    max_seconds: float = 2.0,
    max_text_bytes: int = MAX_TEXT,
    scan_until: int | None = None,
) -> dict:
    if type(start) is not int or start < 0:
        raise ValueError("start must be a nonnegative byte offset")
    if max_scan_bytes <= 0 or max_seconds <= 0 or max_text_bytes <= 0:
        raise ValueError("invalid scan or text budget")
    if scan_until is not None and (type(scan_until) is not int or scan_until < start):
        raise ValueError("scan_until must be an integer byte boundary at or after start")
    boundary = to_seconds(not_before) if not_before is not None else None
    norm_session = session_id.strip().lower() if isinstance(session_id, str) and session_id.strip() else None

    result = dict(
        status="ok",
        start=start,
        end=start,
        digest=hashlib.sha256(b"").hexdigest(),
        text="",
        records=0,
        skipped_records=0,
        oversize_records=0,
        partial=False,
        more=False,
        timestamps_reliable=True,
        session_match=None,
        coverage_note=None,
        coverage=[],
    )
    consumed = hashlib.sha256()
    recognized = 0
    included: list[str] = []
    text_size = 0
    begun = time.monotonic()

    try:
        with open(path, "rb") as stream:
            stat = os.fstat(stream.fileno())
            anchor = stream.read(ANCHOR_BYTES)
            current = FileIdentity(
                str(Path(path).resolve()),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                len(anchor),
                hashlib.sha256(anchor).hexdigest(),
            )
            result["identity"] = current.serialize()
            result["snapshot_size"] = stat.st_size
            if expected is not None:
                stream.seek(0)
                if (
                    expected.path != current.path
                    or expected.dev != stat.st_dev
                    or expected.ino != stat.st_ino
                    or not expected.anchor_len
                    or hashlib.sha256(stream.read(expected.anchor_len)).hexdigest()
                    != expected.anchor_digest
                ):
                    result.update(status="replaced", coverage_note="transcript changed before read")
                    return result
            if stat.st_size < start:
                result.update(status="replaced", coverage_note="transcript truncated")
                return result
            owner = _session_in_path(path)
            if owner:
                result["session_match"] = not norm_session or owner == norm_session
                if norm_session and owner != norm_session:
                    result.update(status="wrong-task", coverage_note="transcript belongs to another task")
                    return result
            elif norm_session:
                result.update(
                    status="unknown-format",
                    coverage_note="task path identity unavailable; no public read",
                )
                return result

            stream.seek(max(0, start - 1))
            if start > 0 and stream.read(1) != b"\n":
                result.update(
                    status="invalid-boundary",
                    coverage_note="cursor is not a whole-record boundary",
                )
                return result
            stream.seek(start)
            end_limit = min(stat.st_size, start + max_scan_bytes)
            if scan_until is not None:
                end_limit = min(end_limit, scan_until)

            while stream.tell() < end_limit and time.monotonic() - begun < max_seconds:
                offset = stream.tell()
                room = stat.st_size - offset
                if scan_until is not None:
                    room = min(room, scan_until - offset)
                raw = stream.readline(room)
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
                    result.setdefault("discarded_records", []).append(
                        dict(start=offset, end=stream.tell(), reason="invalid record")
                    )
                    consumed.update(raw)
                    result["end"] = stream.tell()
                    result["skipped_records"] += 1
                    continue

                rtype = record.get("type")
                if rtype in RECOGNIZED_TYPES or "sessionId" in record:
                    recognized += 1
                rec_session = record.get("sessionId")
                if (
                    norm_session
                    and isinstance(rec_session, str)
                    and rec_session.strip()
                    and rec_session.strip().lower() != norm_session
                ):
                    result.update(status="wrong-task", coverage_note="transcript record belongs to another sessionId")
                    return result

                stamp = to_seconds(record.get("timestamp") or record.get("created_at") or record.get("createdAt"))
                extracted = _extract(record)
                if extracted and extracted[1]:
                    if boundary is not None and stamp is None:
                        result.setdefault("discarded_records", []).append(
                            dict(start=offset, end=stream.tell(), reason="missing public timestamp")
                        )
                        consumed.update(raw)
                        result["end"] = stream.tell()
                        result["skipped_records"] += 1
                        continue
                    if boundary is not None and stamp < boundary:
                        extracted = None
                    else:
                        kind, text = extracted
                        text = text.replace("\r\n", "\n").replace("\r", "\n")
                        rendered = f"### {kind}\n{text}"
                        size = len(rendered.encode("utf-8")) + 2
                        if included and (text_size + size > max_text_bytes or len(included) >= MAX_RECORDS):
                            break
                        included.append(rendered)
                        text_size += size
                if not extracted or not extracted[1]:
                    result["skipped_records"] += 1
                consumed.update(raw)
                result["end"] = stream.tell()
            result["more"] = result["end"] < stat.st_size
    except TimeoutError:
        raise
    except OSError:
        result.update(status="missing", coverage_note="transcript unreadable")
        return result

    result.update(
        digest=consumed.hexdigest(),
        text="\n\n".join(included),
        records=len(included),
    )
    established = expected is not None or result.get("session_match") is True
    if result["status"] != "ok":
        return result
    if result["end"] == start:
        result["status"] = "unchanged"
    elif not recognized and not (established and result["more"] and result["end"] > result["start"]):
        result.update(
            status="unknown-format",
            text="",
            coverage_note="no recognized native WorkBuddy transcript signature",
        )
    return result


read_increment = read_material
