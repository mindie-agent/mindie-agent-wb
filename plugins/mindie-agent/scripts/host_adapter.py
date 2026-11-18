#!/usr/bin/env python3
"""Tencent WorkBuddy (`wb`) host adapter, seed verifier, and concurrent MCP wire loop.

[Role]
Implements `WBHostAdapter` conforming to `mindie_harness.api.HostAdapter` (`host = "wb"`),
`verify_seed_bundle()` for streaming `64 KiB` SHA-256 verification of bundled
`assets/mindie-harness-seed.pyz`, and `serve_concurrent_stdio()` for concurrent dual-framing
(`Content-Length` + JSONL) MCP stdio serving across `remote`, `tools`, and `knowledge`.

[Invariants]
- Pure Python stdlib at module import time (`< 15ms`, zero device context, zero network).
- `extract_session_identity` reads verified WorkBuddy metadata (`workbuddy.ai/conversationId`)
  or host environment variables only—never model-supplied tool `arguments` (`P2.7`).
- `mcp_tools("knowledge", ...)` exposes all 5 default-enabled `knowledge_*` schemas directly
  on startup without touching `admission.sqlite3`.

[Timeouts & Locks]
- Seed verification streams `64 KiB` chunks in `< 5ms`; resident stdio serving uses separate
  read/control (`16` workers) and mutation (`8` workers) pools with serialized writes.

[Side-Effect Policy]
- Importing and `verify_seed_bundle()` are strictly read-only; CLI execution delegates to
  `mindie_harness.api.run_host(..., adapter=WBHostAdapter())`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
import contextlib
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, BinaryIO

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from paths import config_path, data_home, plugin_root

SEED_LOCK_SCHEMA = "mindie-harness-seed/1"
MAX_WIRE_FRAME_BYTES = 32 * 1024 * 1024
_FAST_REMOTE_SUFFIXES = (
    "job_status", "job_tail", "job_output", "job_stop", "job_list", "job_kill",
    "taskoutput", "taskstop", "tasklist", "bashoutput", "killshell",
    "read", "ls", "glob", "grep", "probe", "stat", "status",
)
_FAST_TOOLS = frozenset({
    "mindie_status", "diagnostics_status", "npu_top", "npu_status", "server_status",
    "list_mounts", "find_npu_capacity", "npu_telemetry_window", "list_npu_servers",
    "knowledge_query", "knowledge_explain",
})
_REMOTE_SESSION_VAR: ContextVar[str | None] = ContextVar("mindie_wb_remote_session", default=None)
_REMOTE_STATE_DIR_VAR: ContextVar[Path | None] = ContextVar("mindie_wb_remote_state_dir", default=None)
_REMOTE_CTX_LOCK = threading.Lock()


def current_remote_session() -> str | None:
    return _REMOTE_SESSION_VAR.get()


def current_remote_state_dir() -> Path | None:
    return _REMOTE_STATE_DIR_VAR.get()


def _install_remote_context_hooks() -> None:
    from remote_dev.core import state_store
    from remote_dev.mcp import tools as remote_tools

    with _REMOTE_CTX_LOCK:
        if not getattr(state_store._effective_ledger_context_id, "_mindie_ctx_wrapped", False):
            orig_eff = state_store._effective_ledger_context_id

            def _ctx_effective(client_context_id: str | None = None) -> str | None:
                if client_context_id:
                    return str(client_context_id)
                bound = _REMOTE_SESSION_VAR.get()
                return bound if bound else orig_eff(client_context_id)

            _ctx_effective._mindie_ctx_wrapped = True  # type: ignore[attr-defined]
            state_store._effective_ledger_context_id = _ctx_effective

        if not getattr(state_store.state_root, "_mindie_ctx_wrapped", False):
            orig_root = state_store.state_root

            def _ctx_state_root() -> Path:
                bound_dir = _REMOTE_STATE_DIR_VAR.get()
                return bound_dir if bound_dir is not None else orig_root()

            _ctx_state_root._mindie_ctx_wrapped = True  # type: ignore[attr-defined]
            state_store.state_root = _ctx_state_root
            remote_tools.state_root = _ctx_state_root


@contextlib.contextmanager
def bind_remote_session(session: str, *, state_root_dir: Path | None = None):
    """Bind per-call `remote_dev` session & state root in `ContextVar`s without `os.environ` races."""
    _install_remote_context_hooks()
    tok_sess = _REMOTE_SESSION_VAR.set(session)
    tok_dir = _REMOTE_STATE_DIR_VAR.set(state_root_dir) if state_root_dir is not None else None
    try:
        yield
    finally:
        if tok_dir is not None:
            _REMOTE_STATE_DIR_VAR.reset(tok_dir)
        _REMOTE_SESSION_VAR.reset(tok_sess)


def seed_bundle_paths(root: str | Path | None = None) -> tuple[Path, Path]:
    base = Path(root).resolve() if root is not None else plugin_root()
    assets = base / "assets"
    return assets / "mindie-harness-seed.pyz", assets / "seed-lock.json"


def _stdlib_verify_seed_lock(pyz_path: Path, lock_path: Path) -> dict[str, Any]:
    if not lock_path.is_file():
        raise ValueError(f"Missing seed lock file: {lock_path}")
    if not pyz_path.is_file():
        raise ValueError(f"Missing seed archive: {pyz_path}")
    lock_doc = json.loads(lock_path.read_bytes().decode("utf-8-sig"))
    if not isinstance(lock_doc, dict) or lock_doc.get("schema") != SEED_LOCK_SCHEMA:
        raise ValueError(f"Invalid seed-lock schema in {lock_path}")
    expected_sha = str(lock_doc.get("sha256") or "").strip().lower()
    expected_size = lock_doc.get("size_bytes")
    if len(expected_sha) != 64 or type(expected_size) is not int or expected_size <= 0:
        raise ValueError("Malformed sha256 or size_bytes in seed-lock.json")
    digest = hashlib.sha256()
    actual_size = 0
    with pyz_path.open("rb") as fh:
        while chunk := fh.read(65536):
            digest.update(chunk)
            actual_size += len(chunk)
    actual_sha = digest.hexdigest()
    if actual_size != expected_size or actual_sha != expected_sha:
        raise ValueError(
            f"Seed integrity mismatch for {pyz_path}: expected {expected_sha} ({expected_size}B), "
            f"got {actual_sha} ({actual_size}B)"
        )
    return lock_doc


def verify_seed_bundle(root: str | Path | None = None) -> dict[str, Any]:
    pyz_path, lock_path = seed_bundle_paths(root)
    try:
        from mindie_harness.bootstrap.seed_main import verify_seed_lock
    except ImportError:
        return _stdlib_verify_seed_lock(pyz_path, lock_path)
    return verify_seed_lock(pyz_path, lock_path)


def read_wire_frame(stream: Any) -> tuple[Any, str, bool]:
    while True:
        first_line = stream.readline(MAX_WIRE_FRAME_BYTES + 1)
        if not first_line:
            return None, "jsonl", True
        if len(first_line) > MAX_WIRE_FRAME_BYTES:
            raise ValueError("MCP frame exceeds 32 MiB byte limit")
        stripped = first_line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            content_length: int | None = None
            hdr = first_line
            while True:
                clean = hdr.strip()
                if not clean:
                    break
                key, sep, val = clean.partition(b":")
                if not sep:
                    raise ValueError("Malformed MCP header line")
                if key.strip().lower() == b"content-length":
                    content_length = int(val.strip().decode("ascii"))
                hdr = stream.readline(8192)
                if not hdr:
                    return None, "content-length", True
            if content_length is None or content_length < 0 or content_length > MAX_WIRE_FRAME_BYTES:
                raise ValueError(f"Invalid Content-Length: {content_length}")
            chunks: list[bytes] = []
            rem = content_length
            while rem > 0:
                piece = stream.read(rem)
                if not piece:
                    raise ValueError("Unexpected EOF inside Content-Length body")
                chunks.append(piece)
                rem -= len(piece)
            return json.loads(b"".join(chunks).decode("utf-8-sig")), "content-length", False
        return json.loads(stripped.decode("utf-8-sig")), "jsonl", False


def write_wire_frame(stream: BinaryIO, payload: dict[str, Any], framing: str) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if framing == "content-length":
        stream.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
        stream.write(raw)
    else:
        stream.write(raw + b"\n")
    stream.flush()


def is_fast_lane_tool(surface: str, name: str, arguments: Mapping[str, Any]) -> bool:
    if surface == "tools":
        return name in _FAST_TOOLS
    if surface == "knowledge":
        return name in {"mindie_entry", "knowledge_query", "knowledge_explain"}
    lowered = name.lower()
    if lowered == "remote_auth":
        return arguments.get("action") == "status"
    return lowered.endswith(_FAST_REMOTE_SUFFIXES)


class _IterStreamAdapter:
    def __init__(self, source: Any) -> None:
        self._it = iter(source)

    def readline(self, _limit: int = -1) -> bytes:
        return next(self._it, b"")

    def read(self, _size: int = -1) -> bytes:
        return next(self._it, b"")


def _file_revision(*files: Path) -> tuple[tuple[int | None, int | None], ...]:
    out: list[tuple[int | None, int | None]] = []
    for f in files:
        for cand in (f, Path(f"{f}-wal")):
            try:
                st = cand.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((None, None))
    return tuple(out)


@contextlib.contextmanager
def hold_combined_runtime_lease(executable: str | Path | None = None):
    """Hold both `mindie_knowledge.storage` and `mindie_harness.locks` generation leases."""
    from storage_lifecycle import hold_runtime_lease

    with hold_runtime_lease(executable) as base_lease:
        harness_lease = None
        if base_lease is not None and hasattr(base_lease, "directory"):
            try:
                from mindie_harness.locks import acquire_generation_lease

                harness_lease = acquire_generation_lease(base_lease.directory)
            except TimeoutError:
                raise
            except Exception:
                harness_lease = None
        try:
            yield base_lease
        finally:
            if harness_lease is not None:
                try:
                    harness_lease.release()
                except TimeoutError:
                    raise
                except OSError:
                    pass


def serve_concurrent_stdio(
    *,
    surface: str,
    rpc_handler: Callable[[Any, threading.Event | None], dict[str, Any] | None],
    watched_files: tuple[Path, ...] = (),
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    watch_interval: float = 0.2,
    eof_drain_seconds: float = 1.5,
) -> int:
    raw_in = sys.stdin.buffer if stdin is None else stdin
    in_stream: Any = raw_in if hasattr(raw_in, "readline") else _IterStreamAdapter(raw_in)
    out_stream = sys.stdout.buffer if stdout is None else stdout
    write_lock = threading.Lock()
    rev_lock = threading.Lock()
    inflight_lock = threading.Lock()
    inflight: dict[object, threading.Event] = {}
    active_futures: set[Future[None]] = set()
    abort_queued = threading.Event()
    initialized_seen = threading.Event()
    watch_stop = threading.Event()
    last_rev = [_file_revision(*watched_files)]
    framing_ref = ["jsonl"]

    def _cancel_all_inflight() -> None:
        abort_queued.set()
        with inflight_lock:
            for ev in inflight.values():
                ev.set()

    def _send(payload: dict[str, Any], framing: str) -> None:
        try:
            with write_lock:
                write_wire_frame(out_stream, payload, framing)
        except BrokenPipeError:
            _cancel_all_inflight()
            raise

    def _check_revision(framing: str) -> None:
        if not watched_files or abort_queued.is_set():
            return
        with rev_lock:
            cur = _file_revision(*watched_files)
            if cur == last_rev[0]:
                return
            last_rev[0] = cur
        _send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}, framing)

    def _watch_loop() -> None:
        while not watch_stop.wait(watch_interval):
            if not initialized_seen.is_set() or abort_queued.is_set():
                continue
            try:
                _check_revision(framing_ref[0])
            except (BrokenPipeError, OSError, ValueError):
                return

    def _dispatch_call(req: dict[str, Any], req_id: object, cancel_ev: threading.Event, framing: str) -> None:
        try:
            if abort_queued.is_set() or cancel_ev.is_set():
                return
            resp = rpc_handler(req, cancel_ev)
            _check_revision(framing)
            if resp is not None:
                _send(resp, framing)
        except BrokenPipeError:
            return
        finally:
            with inflight_lock:
                inflight.pop(req_id, None)

    watcher = None
    if watched_files and watch_interval > 0:
        watcher = threading.Thread(target=_watch_loop, name="mindie-wb-watch", daemon=True)
        watcher.start()

    read_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="mindie-wb-read")
    mut_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mindie-wb-mut")
    try:
        while True:
            try:
                req, framing, is_eof = read_wire_frame(in_stream)
            except (UnicodeDecodeError, ValueError) as exc:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {type(exc).__name__}: {exc}"},
                    },
                    framing_ref[0],
                )
                continue
            if is_eof:
                break
            framing_ref[0] = framing
            if not isinstance(req, dict):
                resp = rpc_handler(req, None)
                if resp is not None:
                    _send(resp, framing)
                continue
            method = req.get("method")
            req_id = req.get("id")
            if method == "notifications/cancelled":
                target_id = (req.get("params") or {}).get("requestId") if isinstance(req.get("params"), dict) else None
                with inflight_lock:
                    ev = inflight.get(target_id)
                if ev is not None:
                    ev.set()
                continue
            if method == "tools/call" and req_id is not None:
                params = req.get("params") if isinstance(req.get("params"), dict) else {}
                tname = params.get("name") if isinstance(params.get("name"), str) else ""
                targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                cancel_ev = threading.Event()
                with inflight_lock:
                    if req_id in inflight:
                        _send(
                            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": "Duplicate in-flight request id"}},
                            framing,
                        )
                        continue
                    inflight[req_id] = cancel_ev
                pool = read_pool if is_fast_lane_tool(surface, tname, targs) else mut_pool
                fut = pool.submit(_dispatch_call, req, req_id, cancel_ev, framing)
                with inflight_lock:
                    active_futures.add(fut)
                fut.add_done_callback(lambda done: active_futures.discard(done))
                continue
            _check_revision(framing)
            resp = rpc_handler(req, None)
            if resp is not None:
                _send(resp, framing)
            if method == "initialize":
                initialized_seen.set()
    except BrokenPipeError:
        _cancel_all_inflight()
    except BaseException:
        _cancel_all_inflight()
        raise
    finally:
        watch_stop.set()
        if watcher is not None:
            watcher.join(timeout=1.0)
        with inflight_lock:
            pending = {f for f in active_futures if not f.done()}
        if pending and not abort_queued.is_set() and eof_drain_seconds > 0:
            _, still_pending = wait_futures(pending, timeout=eof_drain_seconds)
            if still_pending:
                _cancel_all_inflight()
        elif pending:
            _cancel_all_inflight()
        read_pool.shutdown(wait=True, cancel_futures=True)
        mut_pool.shutdown(wait=True, cancel_futures=True)
    return 0


class WBHostAdapter:
    """Thin WorkBuddy (`wb`) adapter over `mindie_harness.api.HostAdapter`."""

    host = "wb"

    def __init__(self, *, config: str | Path | None = None) -> None:
        self._config = config

    def resolve_profile(self, explicit_config: str | Path | None = None):
        from mindie_harness.api import ProfileRef

        cfg = config_path(explicit_config or self._config)
        return ProfileRef.resolve(
            self.host,
            plugin_root=plugin_root(),
            config_path=cfg,
            data_dir=data_home(),
        )

    def extract_session_identity(
        self,
        request: Mapping[str, Any],
        env: Mapping[str, str],
    ) -> str | None:
        from identity import require_session

        params = request.get("params")
        meta = params.get("_meta") if isinstance(params, Mapping) else None
        if isinstance(meta, Mapping):
            for key in ("workbuddy.ai/conversationId", "conversationId", "session_id", "sessionId"):
                raw = meta.get(key)
                if isinstance(raw, str) and raw.strip():
                    try:
                        return require_session(raw)
                    except ValueError:
                        return None
        for env_key in ("WB_CONVERSATION_ID", "WORKBUDDY_SESSION_ID", "CODEBUDDY_SESSION_ID"):
            raw_env = env.get(env_key)
            if isinstance(raw_env, str) and raw_env.strip():
                try:
                    return require_session(raw_env)
                except ValueError:
                    return None
        return None

    def mcp_tools(self, surface: str, profile) -> list[dict[str, Any]]:
        import mcp_server

        _ = profile
        if surface == "remote":
            return mcp_server.remote_tools()
        if surface in {"tools", "components"}:
            return mcp_server.component_tools()
        if surface == "knowledge":
            return mcp_server.knowledge_tools()
        if surface == "all":
            return mcp_server.remote_tools() + mcp_server.component_tools() + mcp_server.knowledge_tools()
        raise ValueError(f"Unsupported WorkBuddy MCP surface: {surface!r}")

    def dispatch_tool(self, ctx, arguments: dict[str, Any], profile) -> dict[str, Any]:
        import mcp_server

        cfg = profile.config_path if profile is not None else config_path(self._config)
        if ctx.surface == "remote":
            return mcp_server.call_remote_surface(
                ctx.tool_name, arguments, rpc_id=ctx.request_id, config=cfg, cancel=ctx.cancel
            )
        if ctx.surface == "knowledge":
            return mcp_server.call_knowledge_surface(
                ctx.tool_name, arguments, rpc_id=ctx.request_id, config=cfg
            )
        return mcp_server.call_tools_surface(
            ctx.tool_name, arguments, rpc_id=ctx.request_id, config=cfg, cancel=ctx.cancel
        )


def main(argv: list[str] | None = None) -> int:
    from mindie_harness.api import run_host

    return int(run_host(argv, adapter=WBHostAdapter()))


if __name__ == "__main__":
    raise SystemExit(main())
