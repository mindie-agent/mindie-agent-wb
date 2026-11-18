"""WorkBuddy MCP surfaces with selected-generation discovery and execution.

[Role]
Serves `remote` (`wb` profile), deterministic `tools`, and default-enabled
`knowledge` MCP surfaces over concurrent dual-framing stdio.

[Invariants]
- Session identity is never taken from model arguments. All three surfaces
  treat `PreToolUse` binds and WorkBuddy `_meta` as optional hints, falling back
  cleanly to `_CONNECTION_SESSION_ID` with on-demand default leases (`not_before=0.0`).
- Completed operations keep `isError=False` with `receipt_warning` and
  `receipt_unconfirmed=True` if local SQLite accounting fails.

[Timeouts & Locks]
- SQLite WAL + `busy_timeout=5000`; remote inner wait + `60s` grace;
  component capture (`16 MiB`) remains independent of the response preview budget.

[Side-Effect Policy]
- `initialize` and `tools/list` are read-only and never create `admission.sqlite3`;
  `tools/call` executes with zero pre-call session-binding gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import sysconfig
import threading
import time
from typing import BinaryIO
import uuid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from admission import activate, deactivate, status_facts
from bounded import run as bounded_run
from component_catalog import COMPONENT_DESCRIPTIONS
from component_update import trigger_background_update_if_due
from identity import claim_bind, finish_bind, session_context
from knowledge_cli import KNOWLEDGE_NAMES, PRIVATE_KNOWLEDGE_CATALOG, execute_knowledge_tool
from paths import config_path, diagnostics_config_path, read_json, remote_receipts_path, state_dir, which_native
from storage_lifecycle import hold_runtime_lease

MAX_YIELD_TIME_MS = 30000
REMOTE_GRACE_SECONDS = 60.0
REMOTE_MIN_BUDGET_SECONDS = 180.0
COMPONENT_CAPTURE_LIMIT_BYTES = 16 * 1024 * 1024
REMOTE_NATIVE_MAX_WAIT_MS = {
    "remote_write_stdin": 300000,
    "remote_job_stdin": 300000,
    "remote_bash": 600000,
    "remote_Bash": 600000,
    "remote_TaskOutput": 600000,
    "remote_run_command": 600000,
    "remote_exec_command": 30000,
}

NPU_TOP_OBSERVATION_NAMES = frozenset(
    {"npu_status", "server_status", "list_mounts", "find_npu_capacity", "npu_telemetry_window", "list_npu_servers"}
)


def _npu_top_observation_tools() -> list[dict]:
    try:
        from npu_top.mcp import TOOLS as npu_tools

        return list(npu_tools)
    except ImportError:
        return [
            dict(
                name=name,
                description=f"Observe Ascend NPU telemetry via npu-top ({name}).",
                inputSchema=dict(type="object", properties={}, additionalProperties=True),
                annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
            )
            for name in sorted(NPU_TOP_OBSERVATION_NAMES)
        ]


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def remote_timeout(
    name: str, args: dict | None = None, *, native_wait_ms: int | float | None = None,
    grace: float = REMOTE_GRACE_SECONDS, minimum: float = REMOTE_MIN_BUDGET_SECONDS,
) -> float:
    """Compute `outer_timeout >= max(minimum, inner_wait + grace)`."""
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not math.isfinite(grace) or grace <= 0:
        raise ValueError("remote grace must be a positive finite number")
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum <= 0:
        raise ValueError("remote minimum budget must be a positive finite number")
    if native_wait_ms is None:
        native_wait_ms = REMOTE_NATIVE_MAX_WAIT_MS.get(name, 0)
    explicit_ms = 0.0
    if isinstance(args, dict):
        for key in ("yield_time_ms", "timeout_ms", "WaitMsBeforeAsync"):
            value = args.get(key)
            if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                explicit_ms = max(explicit_ms, float(value))
    return max(float(minimum), max(float(native_wait_ms), explicit_ms) / 1000.0 + float(grace))


class RemoteReceipts:
    """Lightweight SQLite WAL store tracking in-flight and completed MCP calls."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or remote_receipts_path())
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        if not self._initialized or not self.path.is_file():
            with self._init_lock:
                if not self._initialized or not self.path.is_file():
                    self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    init_db = sqlite3.connect(self.path, timeout=5.0)
                    try:
                        for stmt in (
                            "PRAGMA busy_timeout=5000;", "PRAGMA journal_mode=WAL;", "PRAGMA synchronous=NORMAL;",
                            "CREATE TABLE IF NOT EXISTS receipts(call_id TEXT PRIMARY KEY, session TEXT NOT NULL, "
                            "tool TEXT NOT NULL, args_sha256 TEXT NOT NULL, status TEXT NOT NULL, "
                            "started REAL NOT NULL, updated REAL NOT NULL)",
                        ):
                            init_db.execute(stmt)
                        init_db.commit()
                    finally:
                        init_db.close()
                    self._initialized = True
        db = sqlite3.connect(self.path, timeout=5.0)
        db.execute("PRAGMA busy_timeout=5000;")
        db.execute("PRAGMA synchronous=NORMAL;")
        return db

    def begin(self, call_id: str, session: str, tool: str, args: dict) -> None:
        digest = hashlib.sha256(canonical(args or {}).encode("utf-8")).hexdigest()
        now = time.time()
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM receipts WHERE call_id=?", (call_id,)).fetchone() is not None:
                    raise ValueError("Duplicate MCP request; replay rejected")
                db.execute(
                    "INSERT INTO receipts VALUES(?,?,?,?,?,?,?)",
                    (call_id, session, tool, digest, "running", now, now),
                )
        finally:
            db.close()

    def set_session(self, call_id: str, session: str) -> None:
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE receipts SET session=? WHERE call_id=?", (session, call_id))
        finally:
            db.close()

    def finish(self, call_id: str, status: str) -> None:
        now = time.time()
        db = self._connect()
        try:
            with db:
                db.execute("UPDATE receipts SET status=?, updated=? WHERE call_id=?", (status, now, call_id))
        finally:
            db.close()


def _finalize_accounting(result: dict, succeeded: bool, *, receipt_store: RemoteReceipts | None = None,
                         call_id: str | None = None, outcome: str | None = None,
                         bind_id: str | None = None, config=None) -> dict:
    if receipt_store is not None and call_id is not None:
        try:
            receipt_store.finish(call_id, outcome or ("succeeded" if succeeded else "failed"))
        except (TimeoutError, OSError, sqlite3.Error) as exc:
            if not succeeded and isinstance(exc, TimeoutError):
                raise
            if succeeded:
                result = _attach_accounting_warning(result, "receipt_warning", exc)
    if bind_id is not None:
        try:
            finish_bind(bind_id, succeeded, config=config)
        except (TimeoutError, OSError, sqlite3.Error) as exc:
            if not succeeded and isinstance(exc, TimeoutError):
                raise
            if succeeded:
                result = _attach_accounting_warning(result, "bind_warning", exc)
    return result


def _attach_accounting_warning(result: dict, warning_key: str, exc: Exception) -> dict:
    structured = dict(result.get("structuredContent") or {})
    msg = f"{type(exc).__name__}: {exc}"
    structured[warning_key] = msg
    structured.setdefault("receipt_warning", msg)
    structured["receipt_unconfirmed"] = True
    result["structuredContent"] = structured
    text = f"Operation completed, but local {warning_key} failed ({type(exc).__name__}). Do not repeat the call."
    result["content"] = [*(result.get("content") or []), dict(type="text", text=text)]
    return result


def _resolve_remote_profile() -> str:
    from wb_native_compat import ensure_wb_native_support

    ensure_wb_native_support()
    from remote_dev.mcp.harness_profiles import NATIVE_PROFILES

    return "wb" if "wb" in NATIVE_PROFILES else "canonical"


def remote_tools() -> list[dict]:
    profile = _resolve_remote_profile()
    os.environ.setdefault("REMOTE_DEV_HARNESS_PROFILE", profile)
    from remote_dev.mcp.tools import list_tools

    return list(list_tools(profile))


def component_tools() -> list[dict]:
    limits = dict(
        cwd=dict(type="string", description="Absolute working directory."),
        timeout_ms=dict(type="integer", minimum=1, maximum=600000, default=120000),
        max_output_tokens=dict(type="integer", minimum=1, maximum=262144, default=16384),
    )
    items = [dict(
        name="mindie_status",
        description="Read factual local MindIE configuration and this WorkBuddy conversation's lease status.",
        inputSchema=dict(type="object", properties={}, additionalProperties=False),
        annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )]
    for name, desc in COMPONENT_DESCRIPTIONS.items():
        items.append(dict(
            name=name, description=desc,
            inputSchema=dict(type="object", additionalProperties=False, required=["argv"],
                             properties=dict(argv=dict(type="array", items=dict(type="string")), **limits)),
            annotations=dict(readOnlyHint=name == "npu_top", destructiveHint=name not in {"npu_top", "debugger"}, openWorldHint=True),
        ))
    items.extend(_npu_top_observation_tools())
    items.append(dict(
        name="diagnostics_status",
        description="Inspect local MindIE diagnostic/reporting status without starting a worker or contacting GitHub.",
        inputSchema=dict(type="object", additionalProperties=False, properties=dict(config=dict(type="string"), **limits)),
        annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    ))
    items.append(dict(
        name="diagnostics_bundle",
        description="Export a bounded, sanitized local diagnostic support bundle without uploading it.",
        inputSchema=dict(type="object", additionalProperties=False, required=[], properties=dict(
            root=dict(type="string"), operation_id=dict(type="string"), incident_id=dict(type="string"), output=dict(type="string"), **limits)),
        annotations=dict(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    ))
    return items


def knowledge_tools() -> list[dict]:
    return [
        dict(
            name=item["name"],
            description=item["description"],
            inputSchema=dict(item["inputSchema"]),
            annotations=dict(
                readOnlyHint=item["name"] in {"knowledge_query", "knowledge_explain"},
                destructiveHint=False,
                openWorldHint=item["name"] == "knowledge_sync",
            ),
        )
        for item in PRIVATE_KNOWLEDGE_CATALOG
    ]


_CONNECTION_SESSION_ID = os.environ.get("MINDIE_MCP_CONNECTION_ID") or f"wb-mcp-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _rpc_receipt_id(surface: str, rpc_id) -> str:
    return str(uuid.uuid4()) if rpc_id is None else f"{_CONNECTION_SESSION_ID}:{surface}:rpc:{json.dumps(rpc_id, sort_keys=True)}"


def _resolve_claim(surface: str, tool_name: str, arguments: dict, *, meta: dict | None = None, config=None) -> dict:
    try:
        return claim_bind(surface, tool_name, arguments, meta=meta, config=config)
    except TimeoutError:
        raise
    except ValueError as exc:
        if "no native PreToolUse bind" in str(exc) or "no active PreToolUse bind" in str(exc):
            return {"session": _CONNECTION_SESSION_ID, "bind_id": None, "workspace_root": None, "transcript_path": None}
        raise


def _resolve_component_argv(adapter: dict, component: str, *, allow_pythonpath: bool = False) -> list[str]:
    configured = adapter.get("component_commands", {}).get(component)
    if isinstance(configured, list) and configured and all(isinstance(x, str) and x for x in configured):
        return list(configured)
    py_exe = adapter.get("python") or sys.executable
    py_flags = ["-X", "utf8"] if allow_pythonpath else ["-I", "-X", "utf8"]
    if component == "diagnostics":
        return [str(py_exe), *py_flags, "-m", "mindie_diagnostics.cli"]
    module_map = {
        "profiler": "mindie_profiler",
        "sim": "mindie_sim",
        "coordinator": "mindie_coordinator.cli",
        "debugger": "mindie_debugger.cli",
        "npu_top": "npu_top",
    }
    if allow_pythonpath and component in module_map:
        return [str(py_exe), *py_flags, "-m", module_map[component]]
    bin_name = "npu-top" if component == "npu_top" else f"mindie-{component}"
    runtime_dir = str(Path(py_exe).expanduser().parent)
    search_path = os.pathsep.join([runtime_dir, sysconfig.get_path("scripts"), os.environ.get("PATH", "")])
    exe = which_native(bin_name, path=search_path)
    if exe is not None:
        if os.name == "nt" and component in module_map and exe.lower().endswith(".exe"):
            return [str(py_exe), *py_flags, "-m", module_map[component]]
        return [exe]
    if component in module_map:
        return [str(py_exe), *py_flags, "-m", module_map[component]]
    raise FileNotFoundError(f"{bin_name} is not installed in the active MindIE runtime")


def _npu_top_tool_error(message: str, extra: dict | None = None) -> dict:
    payload = dict(error=message, **(extra or {}))
    return dict(content=[dict(type="text", text=message)], structuredContent=payload, isError=True)


def _execute_npu_top_observation(name: str, args: dict) -> dict:
    from npu_top.client import ClientError, VawsTopClient
    from npu_top.mcp import handle_request

    try:
        client = VawsTopClient()
    except ClientError as exc:
        return _npu_top_tool_error(str(exc), {"status": "monitor_unavailable"})
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    resp = handle_request(req, client)
    if not isinstance(resp, dict):
        return _npu_top_tool_error(f"npu-top MCP returned {type(resp).__name__} instead of a JSON-RPC object")
    if "error" in resp:
        err = resp["error"]
        msg = err.get("message", "npu-top MCP call failed") if isinstance(err, dict) else str(err)
        return _npu_top_tool_error(msg)
    result = resp.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        kind = "missing" if "result" not in resp else ("empty object" if result == {} else type(result).__name__)
        return _npu_top_tool_error(f"npu-top MCP result is {kind} without content")
    return result


def _admission_failure(exc):
    return dict(isError=True, structuredContent=dict(
        code="admission_unavailable", execution="not_started", automatic_retry=False,
        error_type=type(exc).__name__), content=[dict(type="text", text=
        "This MindIE call did not start because local admission is unavailable. "
        "Continue independent task steps or use equivalent authorized host-native tools; "
        "preserve task identity and resource ownership. Do not repeat this call unchanged.")])


def call_remote_surface(
    name: str, arguments: dict | None = None, *, meta: dict | None = None,
    rpc_id=None, config=None, receipts: RemoteReceipts | None = None, cancel: threading.Event | None = None,
) -> dict:
    os.environ.setdefault("MINDIE_DIAGNOSTICS_CONFIG", str(diagnostics_config_path(config)))
    profile = _resolve_remote_profile()
    os.environ.setdefault("REMOTE_DEV_HARNESS_PROFILE", profile)
    raw_args = dict(arguments or {})
    exec_args = dict(raw_args)
    if "yield_time_ms" in exec_args and type(exec_args["yield_time_ms"]) is int:
        exec_args["yield_time_ms"] = min(exec_args["yield_time_ms"], MAX_YIELD_TIME_MS)
    receipt_store = receipts or RemoteReceipts(remote_receipts_path(config))
    call_id = _rpc_receipt_id("remote", rpc_id)
    reserved = False
    try:
        receipt_store.begin(call_id, _CONNECTION_SESSION_ID, name, exec_args)
        reserved = True
        claim = _resolve_claim("remote", name, raw_args, meta=meta, config=config)
        session, bind_id = claim["session"], claim.get("bind_id")
        if session != _CONNECTION_SESSION_ID and hasattr(receipt_store, "set_session"):
            receipt_store.set_session(call_id, session)
        explicit_remote = os.environ.get("REMOTE_DEV_STATE_DIR")
        root = Path(explicit_remote) if explicit_remote else (state_dir(config) / "remote")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except Exception as exc:
        if reserved:
            try:
                receipt_store.finish(call_id, "failed")
            except TimeoutError:
                raise
            except (OSError, sqlite3.Error):
                pass
        if "claim" in locals() and claim.get("bind_id") is not None:
            try:
                finish_bind(claim["bind_id"], False, config=config)
            except TimeoutError:
                raise
            except (OSError, sqlite3.Error):
                pass
        return _admission_failure(exc)
    succeeded = False
    outcome = "failed"
    try:
        from contextlib import nullcontext
        from host_adapter import bind_remote_session
        from remote_dev.core.cancellation import request_context
        _had_remote_session_env = "REMOTE_DEV_SESSION_ID" in os.environ
        from remote_dev.mcp.server import tool_text
        if not _had_remote_session_env and os.environ.get("REMOTE_DEV_SESSION_ID", "").startswith("mcp-"):
            os.environ.pop("REMOTE_DEV_SESSION_ID", None)
        from remote_dev.mcp.tools import call_tool
        from remote_dev.mcp.outcome import operation_outcome

        ctx_mgr = request_context(cancel) if cancel is not None else nullcontext()
        with bind_remote_session(session, state_root_dir=root), ctx_mgr:
            raw = call_tool(name, exec_args, profile=profile)
        outcome = operation_outcome(name, raw)
        is_error = outcome != "succeeded"
        succeeded = not is_error
        blocks = [dict(type="text", text=tool_text(raw))]
        native_blocks = raw.get("content") if isinstance(raw, dict) else None
        if isinstance(native_blocks, list):
            blocks.extend(item for item in native_blocks if isinstance(item, dict) and isinstance(item.get("type"), str))
        result = dict(content=blocks, structuredContent=raw if isinstance(raw, dict) else {"result": raw}, isError=is_error)
    except Exception as exc:
        from remote_dev.core.errors import error_details, safe_auth_error_details

        details = error_details(exc)
        outcome = "unknown" if details.get("submission_state") == "uncertain" else "failed"
        auth = safe_auth_error_details(details)
        result = dict(
            content=[dict(type="text", text=auth["hint"] if auth else f"{type(exc).__name__}: {exc}")],
            structuredContent=(dict(error_details=auth) if auth else dict(error=str(exc), error_type=type(exc).__name__)),
            isError=True,
        )

    return _finalize_accounting(
        result, succeeded, receipt_store=receipt_store, call_id=call_id,
        outcome=outcome, bind_id=bind_id, config=config,
    )


def call_tools_surface(
    name: str, arguments: dict | None = None, *, meta: dict | None = None,
    rpc_id=None, config=None, receipts: RemoteReceipts | None = None, cancel: threading.Event | None = None,
) -> dict:
    if name in KNOWLEDGE_NAMES:
        return call_knowledge_surface(name, arguments, meta=meta, rpc_id=rpc_id, config=config, receipts=receipts)
    args = dict(arguments or {})
    if name == "debugger" and "argv" not in args and isinstance(args.get("action"), str):
        action = args.pop("action")
        code = args.pop("code", None)
        args = {"argv": [action, str(code)] if code is not None else [action]}
    receipt_store = receipts or (RemoteReceipts(remote_receipts_path(config)) if rpc_id is not None else None)
    call_id = _rpc_receipt_id("tools", rpc_id)
    reserved = False
    try:
        if receipt_store is not None:
            receipt_store.begin(call_id, _CONNECTION_SESSION_ID, name, args)
            reserved = True
        claim = _resolve_claim("tools", name, args, meta=meta, config=config)
        if receipt_store is not None and claim["session"] != _CONNECTION_SESSION_ID and hasattr(receipt_store, "set_session"):
            receipt_store.set_session(call_id, claim["session"])
    except Exception as exc:
        if reserved and receipt_store is not None:
            try:
                receipt_store.finish(call_id, "failed")
            except TimeoutError:
                raise
            except (OSError, sqlite3.Error):
                pass
        return _admission_failure(exc)
    session, bind_id = claim["session"], claim.get("bind_id")
    succeeded = False
    try:
        if name == "mindie_status":
            facts = status_facts(session if session != "wb-default-session" else None, config=config)
            succeeded = True
            result = dict(content=[dict(type="text", text=canonical(facts))], structuredContent=facts, isError=False)
        elif name in NPU_TOP_OBSERVATION_NAMES:
            result = _execute_npu_top_observation(name, args)
            succeeded = result.get("isError") is not True
        elif name in COMPONENT_DESCRIPTIONS or name in {"diagnostics_status", "diagnostics_bundle"}:
            cfg_file = config_path(config)
            adapter = read_json(cfg_file) if cfg_file.is_file() else {"python": sys.executable}
            if name in COMPONENT_DESCRIPTIONS:
                component = name
                argv = args.get("argv")
                if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
                    raise ValueError("argv must be a list of strings")
            elif name == "diagnostics_status":
                component, argv = "diagnostics", ["reporting", "status"]
                diag_cfg = args.get("config") if isinstance(args.get("config"), str) else str(diagnostics_config_path(config))
                argv += ["--config", diag_cfg]
            else:
                import diagnostic_support

                component = "diagnostics"
                root_arg = args.get("root")
                if root_arg is not None and (not isinstance(root_arg, str) or not root_arg):
                    raise ValueError("diagnostics_bundle 'root' must be a non-empty string")
                argv = ["bundle", "--root", root_arg or diagnostic_support.default_diagnostics_root()]
                op_id = args.get("operation_id") if isinstance(args.get("operation_id"), str) else args.get("incident_id")
                if isinstance(op_id, str) and op_id:
                    argv += ["--operation-id", op_id]
                if isinstance(args.get("output"), str) and args["output"]:
                    argv += ["--output", args["output"]]

            budget = args.get("max_output_tokens", 16384)
            if type(budget) is not int or not 1 <= budget <= 262144:
                raise ValueError("max_output_tokens must be between 1 and 262144")
            timeout_ms = args.get("timeout_ms", 120000)
            if type(timeout_ms) is not int or not 1 <= timeout_ms <= 600000:
                raise ValueError("timeout_ms must be between 1 and 600000")
            cwd = args.get("cwd") or claim.get("workspace_root") or os.getcwd()

            child_env = dict(os.environ, MINDIE_AGENT_CONFIG=str(cfg_file))
            child_env.setdefault("MINDIE_DIAGNOSTICS_CONFIG", str(diagnostics_config_path(config)))
            if session and session != "wb-default-session":
                child_env["WORKBUDDY_SESSION_ID"] = session
                child_env["WB_CONVERSATION_ID"] = session
                child_env["CODEBUDDY_SESSION_ID"] = session
                if component == "coordinator":
                    child_env["MINDIE_COORDINATOR_NATIVE_CALL"] = canonical({"workbuddy.ai/conversationId": session})
            use_debugger_repo = False
            if component == "debugger":
                dbg_env = os.environ.get("MINDIE_DEBUGGER_REPO")
                if dbg_env and (Path(dbg_env) / "mindie_debugger").is_dir():
                    existing_pp = child_env.get("PYTHONPATH", "")
                    child_env["PYTHONPATH"] = f"{dbg_env}{os.pathsep}{existing_pp}" if existing_pp else dbg_env
                    use_debugger_repo = True
                else:
                    child_env.pop("PYTHONPATH", None)
            else:
                child_env.pop("PYTHONPATH", None)

            invocation_token = os.urandom(16).hex() if name in COMPONENT_DESCRIPTIONS else None
            if invocation_token is not None:
                child_env["MINDIE_DIAGNOSTICS_INVOCATION_TOKEN"] = invocation_token
            cmd = _resolve_component_argv(adapter, component, allow_pythonpath=use_debugger_repo) + list(argv)
            proc = bounded_run(
                cmd, "", timeout=timeout_ms / 1000.0, max_output=COMPONENT_CAPTURE_LIMIT_BYTES,
                env=child_env, cwd=cwd, cancel=cancel,
            )
            reply_limit = budget * 4
            stdout_bytes = proc.stdout.encode("utf-8", errors="replace")
            stderr_bytes = proc.stderr.encode("utf-8", errors="replace")
            truncated = len(stdout_bytes) + len(stderr_bytes) > reply_limit
            err_budget = min(len(stderr_bytes), reply_limit // 2)
            out_budget = min(len(stdout_bytes), reply_limit - err_budget)
            err_budget = min(len(stderr_bytes), reply_limit - out_budget)
            payload = dict(
                component=component,
                exit_code=proc.returncode,
                stdout=stdout_bytes[:out_budget].decode("utf-8", errors="replace"),
                stderr=stderr_bytes[:err_budget].decode("utf-8", errors="replace"),
                output_truncated=truncated,
            )
            if not truncated and proc.stdout:
                try:
                    payload["result"] = json.loads(proc.stdout)
                except ValueError:
                    pass
            if name in COMPONENT_DESCRIPTIONS and proc.returncode not in (0, 2):
                import diagnostic_support

                diag = diagnostic_support.record_component_crash(
                    component, argv, proc.returncode, proc.stderr,
                    parsed_result=payload.get("result"), config=config, executable=cmd[0],
                    invocation_token=invocation_token,
                )
                if diag is not None:
                    payload["diagnostic"] = diag
            succeeded = proc.returncode == 0
            result = dict(content=[dict(type="text", text=canonical(payload))], structuredContent=payload, isError=proc.returncode != 0)
        else:
            raise ValueError(f"unknown mindie-tools tool: {name}")
    except Exception as exc:
        import diagnostic_support

        err_struct = dict(error=str(exc), error_type=type(exc).__name__)
        if diagnostic_support.is_internal_mcp_exception(exc):
            err_struct["diagnostic"] = diagnostic_support.failure(
                "mcp.tools", str(name or "dispatch"), "internal", exception=exc, config=config
            )
        result = dict(content=[dict(type="text", text=f"{type(exc).__name__}: {exc}")], structuredContent=err_struct, isError=True)

    return _finalize_accounting(
        result, succeeded, receipt_store=receipt_store, call_id=call_id, bind_id=bind_id, config=config,
    )


def call_knowledge_surface(
    name: str, arguments: dict | None = None, *, meta: dict | None = None,
    rpc_id=None, config=None, receipts: RemoteReceipts | None = None,
) -> dict:
    args = dict(arguments or {})
    receipt_store = receipts or (RemoteReceipts(remote_receipts_path(config)) if rpc_id is not None else None)
    call_id = _rpc_receipt_id("knowledge", rpc_id)
    reserved = False
    try:
        if receipt_store is not None:
            receipt_store.begin(call_id, _CONNECTION_SESSION_ID, name, args)
            reserved = True
        claim = _resolve_claim("knowledge", name, args, meta=meta, config=config)
        session, bind_id = claim["session"], claim.get("bind_id")
        if receipt_store is not None and session != _CONNECTION_SESSION_ID and hasattr(receipt_store, "set_session"):
            receipt_store.set_session(call_id, session)
        workspace = (args.get("project_root") or claim.get("workspace_root")
                     or session_context(session, config=config).get("workspace_root") or os.getcwd())
    except Exception as exc:
        if reserved and receipt_store is not None:
            try:
                receipt_store.finish(call_id, "failed")
            except TimeoutError:
                raise
            except (OSError, sqlite3.Error):
                pass
        return _admission_failure(exc)
    succeeded = False
    try:
        if name == "mindie_entry":
            op = args.get("op") or args.get("action") or "status"
            if op in {"status", "sharing-status", "reporting-status", "recover"}:
                payload = status_facts(session, config=config)
            elif op == "deactivate":
                payload = deactivate(session, config=config, project_root=workspace)
            elif op in {"init", "activate"}:
                cfg_file = config_path(config)
                if not cfg_file.is_file():
                    from setup import ensure_default_profile

                    ensure_default_profile(config=cfg_file, workspace_root=workspace)
                payload = activate(session, workspace, domain=args.get("domain"), config=config)
            elif op in {"choose", "sharing-enable", "sharing-disable"}:
                from setup import configure_community

                enabled = args.get("choice") == "contribute" if op == "choose" else (op == "sharing-enable")
                payload = configure_community(config=config, enabled=enabled, fork=args.get("repository"),
                                              account=args.get("account"), project_root=workspace)
            else:
                raise ValueError(f"unsupported mindie_entry op: {op}")
            succeeded = payload.get("status") != "partial" and not payload.get("reporting_error")
            result = dict(content=[dict(type="text", text=canonical(payload))], structuredContent=payload, isError=not succeeded)
        else:
            payload = execute_knowledge_tool(name, args, session=session, project_root=workspace, config=config)
            is_error = False
            if name == "knowledge_distill":
                from mindie_knowledge.graded.distill import result_view

                payload, is_error = result_view(payload)
            elif name == "knowledge_sync":
                is_error = payload.get("status") != "ok"
            succeeded = not is_error
            result = dict(content=[dict(type="text", text=canonical(payload))], structuredContent=payload, isError=bool(is_error))
    except Exception as exc:
        result = dict(content=[dict(type="text", text=f"{type(exc).__name__}: {exc}")],
                      structuredContent=dict(error=str(exc), error_type=type(exc).__name__), isError=True)

    return _finalize_accounting(
        result, succeeded, receipt_store=receipt_store, call_id=call_id, bind_id=bind_id, config=config,
    )


def handle_rpc(
    request: dict, surface: str = "tools", *, config=None,
    receipts: RemoteReceipts | None = None, cancel: threading.Event | None = None,
) -> dict | None:
    """Process one JSON-RPC 2.0 request or notification for `remote`, `tools`, or `knowledge`."""
    if not isinstance(request, dict):
        return dict(jsonrpc="2.0", id=None, error=dict(code=-32600, message="Invalid Request: JSON-RPC request must be an object"))
    method = request.get("method")
    req_id = request.get("id")
    if req_id is None:
        return None
    if method == "initialize":
        try:
            trigger_background_update_if_due(config=config)
        except Exception as exc:
            print(f"MindIE WorkBuddy background update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return dict(
            jsonrpc="2.0",
            id=req_id,
            result=dict(
                protocolVersion="2025-11-25",
                capabilities=dict(tools=dict(listChanged=True)),
                serverInfo=dict(name=f"mindie-wb-{surface}", version=read_json(HERE.parent / "plugin.json")["version"]),
            ),
        )
    if method == "ping":
        return dict(jsonrpc="2.0", id=req_id, result={})
    if method == "tools/list":
        if surface == "remote":
            tools = remote_tools()
        elif surface == "knowledge":
            tools = knowledge_tools()
        else:
            tools = component_tools()
        return dict(jsonrpc="2.0", id=req_id, result=dict(tools=tools))
    if method == "tools/call":
        params = request.get("params")
        if params is not None and not isinstance(params, dict):
            return dict(jsonrpc="2.0", id=req_id, error=dict(code=-32602, message="Invalid params"))
        params = params or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
        if not isinstance(tool_name, str) or not tool_name:
            return dict(jsonrpc="2.0", id=req_id, error=dict(code=-32602, message="Invalid tool name"))
        if surface == "remote":
            out = call_remote_surface(tool_name, arguments, meta=meta, rpc_id=req_id, config=config, receipts=receipts, cancel=cancel)
        elif surface == "knowledge":
            out = call_knowledge_surface(tool_name, arguments, meta=meta, rpc_id=req_id, config=config, receipts=receipts)
        else:
            out = call_tools_surface(tool_name, arguments, meta=meta, rpc_id=req_id, config=config, receipts=receipts, cancel=cancel)
        return dict(jsonrpc="2.0", id=req_id, result=out)
    return dict(jsonrpc="2.0", id=req_id, error=dict(code=-32601, message=f"Method not found: {method}"))


def serve(surface: str = "tools", *, config=None, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """Run the concurrent dual-framing JSON-RPC 2.0 loop over stdin/stdout."""
    if surface == "components":
        surface = "tools"
    if surface not in {"remote", "tools", "knowledge"}:
        raise ValueError(f"unknown MindIE MCP surface: {surface}")
    from host_adapter import hold_combined_runtime_lease, serve_concurrent_stdio

    receipts = RemoteReceipts(remote_receipts_path(config))
    with hold_combined_runtime_lease():
        return serve_concurrent_stdio(
            surface=surface,
            rpc_handler=lambda req, cancel_ev: handle_rpc(req, surface, config=config, receipts=receipts, cancel=cancel_ev),
            watched_files=(config_path(config),),
            stdin=stdin,
            stdout=stdout,
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = [x for x in argv if x != "--once"]
    surface = args[0] if args else "tools"
    with hold_runtime_lease():
        return serve(surface)


if __name__ == "__main__":
    raise SystemExit(main())
