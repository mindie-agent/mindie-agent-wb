"""WorkBuddy (`wb`) native profile and release registry bridge (`wb_native_compat.py`).

[Role]
Ensures that both pinned baseline releases (`remote-dev==0.11.2`,
`mindie-knowledge==0.13.8`) and newer native releases expose the 13 WorkBuddy
native remote tools (`remote_Read`..`remote_ViewImage`) under profile `"wb"`
and register `"workbuddy"` in `mindie_knowledge.releases`.

[Invariants]
- Idempotent: when the installed `remote_dev` already includes `"wb"` in
  `NATIVE_PROFILES` and `mindie_knowledge.releases.REPOSITORIES` already maps
  `"workbuddy"`, registration is a no-op.
- Preserves incremental vs `full=true` output cursors and case-insensitive
  regex `filter` semantics on `remote_TaskOutput` and `remote_BashOutput`.

[Timeouts & Locks]
- Pure in-memory module registration; zero I/O at import time.

[Side-Effect Policy]
- Updates in-process `remote_dev.mcp` and `mindie_knowledge.releases` registries
  only when running against pre-wb component revisions.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping

_WB_CURSORS: dict[str, dict[str, Any]] = {}


def ensure_wb_knowledge_registry() -> None:
    """Ensure `workbuddy` is registered in `mindie_knowledge.releases` and `releases_io`."""
    try:
        from mindie_knowledge import releases, releases_io
    except ImportError:
        return
    if "workbuddy" not in releases.REPOSITORIES:
        releases.REPOSITORIES["workbuddy"] = "mindie-agent/mindie-agent-wb"
    compat_paths = getattr(releases_io, "_COMPATIBILITY_PATHS", None)
    if isinstance(compat_paths, dict) and "workbuddy" not in compat_paths:
        compat_paths["workbuddy"] = "harness-compatibility.json"


def _build_wb_specs() -> dict[str, Any]:
    from remote_dev.core.errors import caller_error
    from remote_dev.mcp import native_spec as _ns
    from remote_dev.mcp.native_spec import NativeToolSpec
    from remote_dev.mcp.profiles_claude import (
        CLAUDE_BASH,
        CLAUDE_EDIT,
        CLAUDE_GREP,
        CLAUDE_NOTEBOOK_EDIT,
        CLAUDE_READ,
        CLAUDE_TASK_STOP,
        CLAUDE_WRITE,
    )

    _ep_args = getattr(_ns, "_endpoint_args", lambda a: {})

    def _default_require_int(val: Any, field: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
        if isinstance(val, bool) or not isinstance(val, int):
            raise caller_error(f"{field} must be an integer", field=field)
        if minimum is not None and val < minimum:
            raise caller_error(f"{field} must be >= {minimum}", field=field)
        if maximum is not None and val > maximum:
            raise caller_error(f"{field} must be <= {maximum}", field=field)
        return int(val)

    def _default_forbid_fields(base_norm: Any, forbidden: Mapping[str, str]) -> Any:
        def _wrapped(args: Mapping[str, Any], *extra: Any) -> dict[str, Any]:
            for key, msg in forbidden.items():
                if args.get(key) is not None:
                    raise caller_error(msg, field=key)
            if base_norm is None:
                return dict(args)
            try:
                return base_norm(args, *extra)
            except TypeError:
                return base_norm(args)

        return _wrapped

    _require_int = getattr(_ns, "_require_int", _default_require_int)
    _forbid_fields = getattr(_ns, "_forbid_fields", _default_forbid_fields)

    def _wb_multi_edit(args: Mapping[str, Any], _spec: NativeToolSpec | None = None) -> dict[str, Any]:
        edits_raw = args.get("edits")
        if not isinstance(edits_raw, list) or not edits_raw:
            raise caller_error("edits must be a non-empty list", field="edits")
        normalized_edits: list[dict[str, Any]] = []
        for idx, item in enumerate(edits_raw):
            if not isinstance(item, Mapping):
                raise caller_error(f"edits[{idx}] must be an object", field="edits")
            if item.get("old_string") == "":
                raise caller_error("old_string must be non-empty", field="old_string")
            normalized_edits.append(
                {
                    "old_string": item["old_string"],
                    "new_string": item["new_string"],
                    "replace_all": bool(item.get("replace_all", False)),
                }
            )
        return {**_ep_args(args), "file_path": args["file_path"], "edits": normalized_edits}

    def _wb_glob(args: Mapping[str, Any], _spec: NativeToolSpec | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {**_ep_args(args), "pattern": args["pattern"]}
        if args.get("path") is not None:
            out["path"] = args["path"]
        if args.get("limit") is not None:
            out["limit"] = _require_int(args["limit"], "limit", minimum=1)
        if args.get("offset") is not None:
            out["offset"] = _require_int(args["offset"], "offset", minimum=0)
        return out

    def _validate_regex(pattern: Any, field: str = "filter") -> None:
        if pattern is None:
            return
        if not isinstance(pattern, str):
            raise caller_error(f"{field} must be a string", field=field)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise caller_error(f"Invalid regular expression in {field}: {exc}", field=field) from exc

    def _wb_task_output_norm(args: Mapping[str, Any], _spec: NativeToolSpec | None = None) -> dict[str, Any]:
        timeout = _require_int(args.get("timeout", 60000), "timeout", minimum=0, maximum=600000)
        _validate_regex(args.get("filter"), "filter")
        return {
            **_ep_args(args),
            "job_id": args["task_id"],
            "yield_time_ms": min(timeout, 30000) if args.get("block", True) is not False else 0,
        }

    def _wb_bash_output_norm(args: Mapping[str, Any], _spec: NativeToolSpec | None = None) -> dict[str, Any]:
        _validate_regex(args.get("filter"), "filter")
        return {**_ep_args(args), "job_id": args["shell_id"], "yield_time_ms": 0}

    def _wb_kill_shell_norm(args: Mapping[str, Any], _spec: NativeToolSpec | None = None) -> dict[str, Any]:
        return {**_ep_args(args), "job_id": args["shell_id"]}

    def _wb_view_image_norm(args: Mapping[str, Any], _spec: NativeToolSpec | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {**_ep_args(args), "file_path": args["image_path"], "path": args["image_path"]}
        if args.get("query") is not None:
            out["query"] = str(args["query"])
        return out

    wb_bash_props = dict(CLAUDE_BASH.properties)
    wb_bash_props["work_dir"] = {
        "type": "string",
        "description": "Local host working directory override; rejected on remote target.",
    }
    wb_bash = NativeToolSpec(
        native_name="Bash",
        canonical="remote.bash",
        description=CLAUDE_BASH.description,
        properties=wb_bash_props,
        required=CLAUDE_BASH.required,
        handler=CLAUDE_BASH.handler,
        normalize=_forbid_fields(
            CLAUDE_BASH.normalize,
            {"work_dir": "work_dir is a local host override; pass cwd on canonical remote_bash instead"},
        ),
    )
    wb_multi_edit = NativeToolSpec(
        native_name="MultiEdit",
        canonical="remote.multi_edit",
        description="Apply multiple sequential exact string replacements to a single file (WorkBuddy MultiEdit).",
        properties={
            "file_path": {"type": "string", "description": "The path to the file to modify."},
            "edits": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["old_string", "new_string"],
                    "additionalProperties": False,
                },
            },
        },
        required=("file_path", "edits"),
        normalize=_wb_multi_edit,
    )
    wb_glob = NativeToolSpec(
        native_name="Glob",
        canonical="remote.glob",
        description="Find files by glob pattern with optional pagination (WorkBuddy Glob; pattern/path/limit/offset).",
        properties={
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
        },
        required=("pattern",),
        normalize=_wb_glob,
    )
    wb_task_output = NativeToolSpec(
        native_name="TaskOutput",
        canonical="remote.job_output",
        description="Retrieve output from a background task (WorkBuddy TaskOutput; default timeout 60000ms, supports regex filter and full snapshot).",
        properties={
            "task_id": {"type": "string"},
            "block": {"type": "boolean", "default": True},
            "timeout": {"type": "integer", "default": 60000, "minimum": 0, "maximum": 600000},
            "filter": {"type": "string"},
            "full": {"type": "boolean"},
        },
        required=("task_id",),
        defaults={"block": True, "timeout": 60000},
        handler="wb_task_output",
        normalize=_wb_task_output_norm,
    )
    wb_bash_output = NativeToolSpec(
        native_name="BashOutput",
        canonical="remote.job_output",
        description="Retrieve output from a running or completed background shell (WorkBuddy BashOutput; shell_id/filter).",
        properties={"shell_id": {"type": "string"}, "filter": {"type": "string"}},
        required=("shell_id",),
        defaults={"block": False, "timeout": 0},
        handler="wb_task_output",
        normalize=_wb_bash_output_norm,
    )
    wb_kill_shell = NativeToolSpec(
        native_name="KillShell",
        canonical="remote.job_stop",
        description="Stop a running background shell by ID (WorkBuddy KillShell).",
        properties={"shell_id": {"type": "string"}},
        required=("shell_id",),
        handler="claude_task_stop",
        normalize=_wb_kill_shell_norm,
    )
    wb_view_image = NativeToolSpec(
        native_name="ViewImage",
        canonical="remote.read",
        description="Read an image file from the remote filesystem (WorkBuddy ViewImage; PNG/JPEG/GIF/WebP).",
        properties={"image_path": {"type": "string"}, "query": {"type": "string"}},
        required=("image_path",),
        handler="wb_view_image",
        normalize=_wb_view_image_norm,
    )
    specs = (
        CLAUDE_READ,
        CLAUDE_WRITE,
        CLAUDE_EDIT,
        wb_multi_edit,
        wb_bash,
        wb_glob,
        CLAUDE_GREP,
        wb_task_output,
        wb_bash_output,
        CLAUDE_NOTEBOOK_EDIT,
        CLAUDE_TASK_STOP,
        wb_kill_shell,
        wb_view_image,
    )
    return {spec.wire: spec for spec in specs}


def _register_wb_handlers() -> None:
    from remote_dev.core.errors import caller_error
    from remote_dev.mcp.native_common import _read_media_payload
    from remote_dev.mcp.native_handlers_claude import claude_task_output
    from remote_dev.mcp.native_runtime import _HANDLERS, handler

    if "wb_task_output" in _HANDLERS and "wb_view_image" in _HANDLERS:
        return

    def _read_stream_delta(
        record_path: Path | None,
        stream: str,
        prev_offset: int,
        prev_head: str,
        *,
        full_snapshot: bool,
        full_preview_text: str,
    ) -> tuple[str, int, str]:
        from remote_dev.core.job_logs import preview_stream_text
        from remote_dev.mcp import native_handlers_claude as claude_handlers

        max_delta = 4 * 1024 * 1024
        if record_path is not None:
            log_path = Path(claude_handlers._job_file(record_path, stream))
            if log_path.is_file():
                try:
                    size = log_path.stat().st_size
                    with log_path.open("rb") as fh:
                        head_str = fh.read(64).decode("latin1")
                        offset = prev_offset
                        if offset < 0 or offset > size or (prev_head and not head_str.startswith(prev_head)):
                            offset = 0
                        if full_snapshot:
                            return full_preview_text, size, head_str
                        read_len = size - offset
                        if read_len <= 0:
                            return "", size, head_str
                        if read_len > max_delta:
                            fh.seek(size - max_delta)
                            raw_delta = fh.read(max_delta)
                        else:
                            fh.seek(offset)
                            raw_delta = fh.read(read_len)
                    return preview_stream_text(raw_delta.decode("utf-8", errors="replace"))["text"], size, head_str
                except OSError:
                    pass
            raw_text, _ = claude_handlers._tail_text(log_path)
        else:
            raw_text = full_preview_text

        raw_bytes = raw_text.encode("utf-8")
        size = len(raw_bytes)
        head_str = raw_bytes[:64].decode("latin1")
        offset = prev_offset
        if offset < 0 or offset > size or (prev_head and not head_str.startswith(prev_head)):
            offset = 0
        if full_snapshot:
            return full_preview_text, size, head_str
        if offset >= size:
            return "", size, head_str
        return preview_stream_text(raw_bytes[offset:].decode("utf-8", errors="replace"))["text"], size, head_str

    @handler("wb_task_output")
    def wb_task_output_fn(profile, spec, native, normalized, call_canonical):
        from remote_dev.core.locking import record_lock
        from remote_dev.core.state_store import atomic_write_json, find_job_record, read_json
        from remote_dev.result import recording_failure

        adapted = dict(native)
        if "task_id" not in adapted and "shell_id" in adapted:
            adapted["task_id"] = adapted["shell_id"]
        if "timeout" not in adapted:
            adapted["timeout"] = 60000
        pattern_raw = native.get("filter")
        regex = None
        if pattern_raw is not None and str(pattern_raw) != "":
            try:
                regex = re.compile(str(pattern_raw), re.IGNORECASE)
            except re.error as exc:
                raise caller_error(f"Invalid filter regex {pattern_raw!r}: {exc}", field="filter") from exc

        job_id = str(adapted["task_id"])
        found = find_job_record(job_id)
        record_path = Path(found[0]) if found else None
        record = found[1] if found else None

        payload = claude_task_output(profile, spec, adapted, normalized, call_canonical)
        result = payload.get("result") or {}
        task_info = result.get("task")
        preview = result.get("preview")
        if not isinstance(task_info, dict) or not isinstance(preview, dict):
            return payload

        stdout_full = str(preview.get("stdout") or "")
        stderr_full = str(preview.get("stderr") or "")
        full_snapshot = native.get("full") is True
        key = f"{record_path or ''}:{job_id}"
        saved_offsets = None
        if record_path is not None and record_path.is_file():
            try:
                latest_in = read_json(record_path)
                if isinstance(latest_in, dict) and isinstance(latest_in.get("wb_output_offsets"), dict):
                    saved_offsets = latest_in["wb_output_offsets"]
            except (OSError, ValueError):
                pass
        cursor = dict(
            saved_offsets
            or (record.get("wb_output_offsets") if isinstance(record, dict) else None)
            or _WB_CURSORS.get(
                key,
                {"stdout_offset": 0, "stderr_offset": 0, "stdout_head": "", "stderr_head": ""},
            )
        )
        stdout_out, next_stdout_off, next_stdout_head = _read_stream_delta(
            record_path,
            "stdout",
            int(cursor.get("stdout_offset", 0)),
            str(cursor.get("stdout_head", "")),
            full_snapshot=full_snapshot,
            full_preview_text=stdout_full,
        )
        stderr_out, next_stderr_off, next_stderr_head = _read_stream_delta(
            record_path,
            "stderr",
            int(cursor.get("stderr_offset", 0)),
            str(cursor.get("stderr_head", "")),
            full_snapshot=full_snapshot,
            full_preview_text=stderr_full,
        )
        next_cursor = {
            "stdout_offset": next_stdout_off,
            "stderr_offset": next_stderr_off,
            "stdout_head": next_stdout_head,
            "stderr_head": next_stderr_head,
        }

        combined = stdout_out
        if stderr_out:
            if combined and not combined.endswith("\n"):
                combined += "\n"
            combined += stderr_out
        combined = combined.strip("\n")
        if regex is not None:
            combined = "\n".join(line for line in combined.splitlines() if regex.search(line))
            task_info["filter"] = str(pattern_raw)
        preview["stdout"] = stdout_out
        preview["stderr"] = stderr_out
        task_info["output"] = combined
        task_info["full"] = full_snapshot
        text = str(payload.get("text") or "")
        if "<output>" in text and "</output>" in text:
            before, rest = text.split("<output>", 1)
            _mid, after = rest.split("</output>", 1)
            payload["text"] = (
                f"{before}<output>\n{combined.rstrip()}\n</output>{after}"
                if combined.strip()
                else (before.rstrip() + after).strip()
            )

        try:
            if record_path is not None and record_path.is_file():
                with record_lock(record_path):
                    latest = read_json(record_path)
                    if not isinstance(latest, dict):
                        latest = dict(record or {})
                    latest["wb_output_offsets"] = dict(next_cursor)
                    atomic_write_json(record_path, latest)
                    if isinstance(record, dict):
                        record.clear()
                        record.update(latest)
            elif isinstance(record, dict):
                record["wb_output_offsets"] = dict(next_cursor)
            _WB_CURSORS[key] = dict(next_cursor)
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            result.setdefault("outcome", "success")
            result.setdefault("status", "ok")
            result.setdefault("summary", f"Task output for {job_id}.")
            return recording_failure(payload, exc, stage="wb_cursor_commit", operation_completed=True)

        return payload

    @handler("wb_view_image")
    def wb_view_image_fn(profile, spec, native, normalized, call_canonical):
        payload = _read_media_payload(normalized, spec_wire=spec.wire)
        result = payload.get("result")
        query = str(native.get("query") or "")
        if isinstance(result, dict) and query:
            result["query"] = query
            if result.get("outcome") == "success":
                summary = f"{result.get('summary', '').rstrip('.')} [query: {query}]."
                result["summary"] = summary
                payload["text"] = summary + "\n"
        return payload


def ensure_wb_remote_profile() -> None:
    """Idempotently register the `"wb"` native profile into `remote_dev.mcp`."""
    try:
        from remote_dev.mcp import harness_profiles, native_spec, schemas
    except ImportError:
        return
    if "wb" in native_spec.NATIVE_PROFILES and "wb" in harness_profiles.NATIVE_TOOLS and harness_profiles.NATIVE_TOOLS["wb"]:
        return

    wb_specs = _build_wb_specs()
    _register_wb_handlers()

    if "wb" not in native_spec.NATIVE_PROFILES:
        native_spec.NATIVE_PROFILES = tuple(list(native_spec.NATIVE_PROFILES) + ["wb"])
        native_spec.PROFILES = (native_spec.CANONICAL_PROFILE,) + native_spec.NATIVE_PROFILES
        harness_profiles.NATIVE_PROFILES = native_spec.NATIVE_PROFILES
        harness_profiles.PROFILES = native_spec.PROFILES

    orig_resolve = native_spec.resolve_profile

    def _resolve_with_wb(value: str | None = None) -> str:
        if value is not None and value.strip().lower() == "workbuddy":
            value = "wb"
        return orig_resolve(value)

    orig_validate_value = native_spec._validate_value

    def _validate_value_with_min_items(value: Any, schema: Mapping[str, Any], path: str) -> None:
        if isinstance(value, list) and "minItems" in schema and len(value) < schema["minItems"]:
            raise native_spec.HarnessProfileError(f"{path} must have at least {schema['minItems']} item(s)")
        return orig_validate_value(value, schema, path)

    native_spec._validate_value = _validate_value_with_min_items
    native_spec.resolve_profile = _resolve_with_wb
    harness_profiles.resolve_profile = _resolve_with_wb
    native_spec.NATIVE_TOOLS["wb"] = wb_specs
    harness_profiles.NATIVE_TOOLS["wb"] = wb_specs
    if hasattr(schemas, "_READ_METADATA_NAMES"):
        schemas._READ_METADATA_NAMES = set(schemas._READ_METADATA_NAMES) | {"remote.ViewImage", "remote_ViewImage"}


def ensure_wb_native_support() -> None:
    """Ensure both `mindie_knowledge` and `remote_dev` expose native WorkBuddy support."""
    ensure_wb_knowledge_registry()
    ensure_wb_remote_profile()
