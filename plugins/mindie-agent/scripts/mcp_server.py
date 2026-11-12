"""WorkBuddy MCP surfaces with selected-generation discovery and execution.

[Role] Serve remote, deterministic component and explicitly enabled knowledge tools.
[Invariants] Native PreToolUse binds or WorkBuddy call metadata establish identity;
model arguments cannot supply it. Completed operations retain isError=False if
local receipt/bind accounting fails, with receipt_warning/receipt_unconfirmed.
[Timeouts & Locks] SQLite WAL + busy_timeout=5000; remote inner wait + 60s grace;
component capture (16 MiB) remains independent of the response preview budget.
[Side-Effect Policy] Discovery never activates knowledge; tools/call claims the
native bind and executes once. The stable facade follows the selected profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import sysconfig
import time
import uuid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from admission import activate, deactivate, status_facts
from bounded import OutputLimitExceeded, run as bounded_run
from component_catalog import COMPONENT_DESCRIPTIONS
from component_update import trigger_background_update_if_due
from identity import claim_bind, finish_bind, session_context
from knowledge_cli import PRIVATE_KNOWLEDGE_CATALOG, execute_knowledge_tool
from paths import config_path, diagnostics_config_path, read_json, remote_receipts_path, state_dir

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


def _knowledge_mcp_tools() -> list[dict]:
    items = [
        dict(
            name="mindie_entry",
            description="Native MindIE entry control for WorkBuddy (init/activate, status, deactivate, choose, sharing-enable/disable).",
            inputSchema=dict(
                type="object",
                properties=dict(
                    op=dict(type="string"),
                    action=dict(type="string"),
                    choice=dict(type="string", enum=["contribute", "disabled"]),
                    repository=dict(type="string"),
                    account=dict(type="string"),
                    domain=dict(type="string"),
                    project_root=dict(type="string"),
                    arguments=dict(type="string"),
                ),
                additionalProperties=False,
            ),
        )
    ]
    for item in PRIVATE_KNOWLEDGE_CATALOG:
        schema = dict(item["inputSchema"])
        items.append(dict(name=item["name"], description=item["description"], inputSchema=schema))
    return items


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def remote_timeout(
    name: str,
    args: dict | None = None,
    *,
    native_wait_ms: int | float | None = None,
    grace: float = REMOTE_GRACE_SECONDS,
    minimum: float = REMOTE_MIN_BUDGET_SECONDS,
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
    inner_seconds = max(float(native_wait_ms) / 1000.0, explicit_ms / 1000.0)
    return max(float(minimum), inner_seconds + float(grace))


class RemoteReceipts:
    """Lightweight SQLite WAL store tracking in-flight and completed remote calls."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or remote_receipts_path())

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5.0)
        db.execute("PRAGMA busy_timeout=5000;")
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA synchronous=NORMAL;")
        db.execute(
            "CREATE TABLE IF NOT EXISTS receipts("
            "call_id TEXT PRIMARY KEY, "
            "session TEXT NOT NULL, "
            "tool TEXT NOT NULL, "
            "args_sha256 TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "started REAL NOT NULL, "
            "updated REAL NOT NULL)"
        )
        return db

    def begin(self, call_id: str, session: str, tool: str, args: dict) -> None:
        digest = hashlib.sha256(canonical(args or {}).encode("utf-8")).hexdigest()
        now = time.time()
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?,?,?)",
                    (call_id, session, tool, digest, "running", now, now),
                )
        finally:
            db.close()

    def finish(self, call_id: str, status: str) -> None:
        now = time.time()
        db = self._connect()
        try:
            with db:
                db.execute(
                    "UPDATE receipts SET status=?, updated=? WHERE call_id=?",
                    (status, now, call_id),
                )
        finally:
            db.close()


def _attach_accounting_warning(result: dict, warning_key: str, exc: Exception) -> dict:
    structured = dict(result.get("structuredContent") or {})
    structured[warning_key] = f"{type(exc).__name__}: {exc}"
    structured["receipt_unconfirmed"] = True
    result["structuredContent"] = structured
    content = list(result.get("content") or [])
    content.append(
        dict(
            type="text",
            text=f"Operation completed, but local {warning_key} failed ({type(exc).__name__}). Do not repeat the call.",
        )
    )
    result["content"] = content
    return result


def _resolve_remote_profile() -> str:
    from wb_native_compat import ensure_wb_native_support

    ensure_wb_native_support()
    from remote_dev.mcp.harness_profiles import NATIVE_PROFILES

    return "wb" if "wb" in NATIVE_PROFILES else "canonical"


def remote_tools() -> list[dict]:
    profile = _resolve_remote_profile()
    os.environ["REMOTE_DEV_HARNESS_PROFILE"] = profile
    from remote_dev.mcp.tools import list_tools

    return list(list_tools(profile))


def component_tools() -> list[dict]:
    limits = dict(
        cwd=dict(type="string", description="Absolute working directory."),
        timeout_ms=dict(type="integer", minimum=1, maximum=600000, default=120000),
        max_output_tokens=dict(type="integer", minimum=1, maximum=262144, default=16384),
    )
    items = [
        dict(
            name="mindie_status",
            description="Read factual local MindIE configuration and this WorkBuddy conversation's lease status.",
            inputSchema=dict(type="object", properties={}, additionalProperties=False),
            annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        )
    ]
    for name, desc in COMPONENT_DESCRIPTIONS.items():
        items.append(
            dict(
                name=name,
                description=desc,
                inputSchema=dict(
                    type="object",
                    additionalProperties=False,
                    required=["argv"],
                    properties=dict(
                        argv=dict(type="array", items=dict(type="string")),
                        **limits,
                    ),
                ),
                annotations=dict(
                    readOnlyHint=name == "npu_top",
                    destructiveHint=name not in {"npu_top", "debugger"},
                    openWorldHint=True,
                ),
            )
        )
    items.extend(_npu_top_observation_tools())
    items.append(
        dict(
            name="diagnostics_status",
            description="Inspect local MindIE diagnostic/reporting status without starting a worker or contacting GitHub.",
            inputSchema=dict(
                type="object",
                additionalProperties=False,
                properties=dict(config=dict(type="string"), **limits),
            ),
            annotations=dict(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
        )
    )
    items.append(
        dict(
            name="diagnostics_bundle",
            description="Export a bounded, sanitized local diagnostic support bundle without uploading it.",
            inputSchema=dict(
                type="object",
                additionalProperties=False,
                required=["root"],
                properties=dict(
                    root=dict(type="string"),
                    operation_id=dict(type="string"),
                    output=dict(type="string"),
                    **limits,
                ),
            ),
            annotations=dict(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        )
    )
    return items


def knowledge_tools() -> list[dict]:
    return _knowledge_mcp_tools()


def _resolve_claim(
    surface: str,
    tool_name: str,
    arguments: dict,
    *,
    meta: dict | None = None,
    config=None,
) -> dict:
    return claim_bind(surface, tool_name, arguments, meta=meta, config=config)


def _resolve_component_argv(adapter: dict, component: str) -> list[str]:
    configured = adapter.get("component_commands", {}).get(component)
    if isinstance(configured, list) and configured and all(isinstance(x, str) and x for x in configured):
        return list(configured)
    py_exe = adapter.get("python") or sys.executable
    if component == "diagnostics":
        return [str(py_exe), "-I", "-m", "mindie_diagnostics.cli"]
    bin_name = "npu-top" if component == "npu_top" else f"mindie-{component}"
    runtime_dir = str(Path(py_exe).expanduser().parent)
    search_path = os.pathsep.join([runtime_dir, sysconfig.get_path("scripts"), os.environ.get("PATH", "")])
    exe = shutil.which(bin_name, path=search_path)
    if exe is not None:
        return [exe]
    module_map = {
        "profiler": "mindie_profiler",
        "sim": "mindie_sim",
        "coordinator": "mindie_coordinator.cli",
        "debugger": "mindie_debugger.cli",
        "npu_top": "npu_top.cli",
    }
    if component in module_map:
        return [str(py_exe), "-I", "-m", module_map[component]]
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
    name: str,
    arguments: dict | None = None,
    *,
    meta: dict | None = None,
    config=None,
    receipts: RemoteReceipts | None = None,
) -> dict:
    os.environ.setdefault("MINDIE_DIAGNOSTICS_CONFIG", str(diagnostics_config_path(config)))
    profile = _resolve_remote_profile()
    os.environ["REMOTE_DEV_HARNESS_PROFILE"] = profile
    raw_args = dict(arguments or {})
    try:
        claim = _resolve_claim("remote", name, raw_args, meta=meta, config=config)
    except Exception as exc:
        return _admission_failure(exc)
    exec_args = dict(raw_args)
    if "yield_time_ms" in exec_args and type(exec_args["yield_time_ms"]) is int:
        exec_args["yield_time_ms"] = min(exec_args["yield_time_ms"], MAX_YIELD_TIME_MS)
    session, bind_id = claim["session"], claim.get("bind_id")
    receipt_store = receipts or RemoteReceipts(remote_receipts_path(config))
    call_id = str(uuid.uuid4())
    try:
        root = state_dir(config) / "remote" / session
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt_store.begin(call_id, session, name, exec_args)
    except Exception as exc:
        return _admission_failure(exc)
    os.environ["REMOTE_DEV_SESSION_ID"] = session
    os.environ["REMOTE_DEV_STATE_DIR"] = str(root)
    succeeded = False
    outcome = "failed"
    try:
        from remote_dev.mcp.server import tool_text
        from remote_dev.mcp.tools import call_tool
        from remote_dev.mcp.outcome import operation_outcome

        raw = call_tool(name, exec_args, profile=profile)
        outcome = operation_outcome(name, raw)
        is_error = outcome != "succeeded"
        succeeded = not is_error
        blocks = [dict(type="text", text=tool_text(raw))]
        native_blocks = raw.get("content") if isinstance(raw, dict) else None
        if isinstance(native_blocks, list):
            blocks.extend(
                item for item in native_blocks if isinstance(item, dict) and isinstance(item.get("type"), str)
            )
        result = dict(
            content=blocks,
            structuredContent=raw if isinstance(raw, dict) else {"result": raw},
            isError=is_error,
        )
    except Exception as exc:
        from remote_dev.core.errors import error_details, safe_auth_error_details

        details = error_details(exc)
        outcome = "unknown" if details.get("submission_state") == "uncertain" else "failed"
        auth = safe_auth_error_details(details)
        result = dict(
            content=[dict(type="text", text=auth["hint"] if auth else f"{type(exc).__name__}: {exc}")],
            structuredContent=(dict(error_details=auth) if auth else
                               dict(error=str(exc), error_type=type(exc).__name__)),
            isError=True,
        )

    try:
        receipt_store.finish(call_id, outcome)
    except (OSError, sqlite3.Error) as exc:
        if succeeded:
            result = _attach_accounting_warning(result, "receipt_warning", exc)

    if bind_id is not None:
        try:
            finish_bind(bind_id, succeeded, config=config)
        except (OSError, sqlite3.Error) as exc:
            if succeeded:
                result = _attach_accounting_warning(result, "bind_warning", exc)
    return result


def call_tools_surface(
    name: str,
    arguments: dict | None = None,
    *,
    meta: dict | None = None,
    config=None,
) -> dict:
    args = dict(arguments or {})
    if name == "debugger" and "argv" not in args and isinstance(args.get("action"), str):
        action = args.pop("action")
        code = args.pop("code", None)
        args = {"argv": [action, str(code)] if code is not None else [action]}
    try:
        claim = _resolve_claim("tools", name, args, meta=meta, config=config)
    except Exception as exc:
        return _admission_failure(exc)
    session, bind_id = claim["session"], claim.get("bind_id")
    succeeded = False
    try:
        if name == "mindie_status":
            facts = status_facts(session if session != "wb-default-session" else None, config=config)
            succeeded = True
            result = dict(
                content=[dict(type="text", text=canonical(facts))],
                structuredContent=facts,
                isError=False,
            )
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
                if isinstance(args.get("config"), str):
                    argv += ["--config", args["config"]]
            else:
                component = "diagnostics"
                root_arg = args.get("root")
                if not isinstance(root_arg, str) or not root_arg:
                    raise ValueError("diagnostics_bundle requires 'root'")
                argv = ["bundle", "--root", root_arg]
                for k in ("operation_id", "output"):
                    if isinstance(args.get(k), str):
                        argv += ["--" + k.replace("_", "-"), args[k]]

            budget = args.get("max_output_tokens", 16384)
            if type(budget) is not int or not 1 <= budget <= 262144:
                raise ValueError("max_output_tokens must be between 1 and 262144")
            timeout_ms = args.get("timeout_ms", 120000)
            if type(timeout_ms) is not int or not 1 <= timeout_ms <= 600000:
                raise ValueError("timeout_ms must be between 1 and 600000")
            cwd = args.get("cwd") or claim.get("workspace_root") or os.getcwd()

            child_env = dict(os.environ)
            child_env["MINDIE_AGENT_CONFIG"] = str(cfg_file)
            if session and session != "wb-default-session":
                child_env["WORKBUDDY_SESSION_ID"] = session
                child_env["WB_CONVERSATION_ID"] = session
                child_env["CODEBUDDY_SESSION_ID"] = session
                child_env["AGY_CONVERSATION_ID"] = session
            if component == "debugger":
                debugger_repo = Path("/Users/maoxx241/code/debugger")
                if (debugger_repo / "mindie_debugger").is_dir():
                    existing_pp = child_env.get("PYTHONPATH", "")
                    child_env["PYTHONPATH"] = (
                        f"{debugger_repo}{os.pathsep}{existing_pp}" if existing_pp else str(debugger_repo)
                    )
            else:
                child_env.pop("PYTHONPATH", None)

            cmd = _resolve_component_argv(adapter, component) + list(argv)
            proc = bounded_run(
                cmd,
                "",
                timeout=timeout_ms / 1000.0,
                max_output=COMPONENT_CAPTURE_LIMIT_BYTES,
                env=child_env,
                cwd=cwd,
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
            succeeded = proc.returncode == 0
            result = dict(
                content=[dict(type="text", text=canonical(payload))],
                structuredContent=payload,
                isError=proc.returncode != 0,
            )
        else:
            raise ValueError(f"unknown mindie-tools tool: {name}")
    except Exception as exc:
        result = dict(
            content=[dict(type="text", text=f"{type(exc).__name__}: {exc}")],
            structuredContent=dict(error=str(exc), error_type=type(exc).__name__),
            isError=True,
        )

    if bind_id is not None:
        try:
            finish_bind(bind_id, succeeded, config=config)
        except (OSError, sqlite3.Error) as exc:
            if succeeded:
                result = _attach_accounting_warning(result, "bind_warning", exc)
    return result


def call_knowledge_surface(
    name: str,
    arguments: dict | None = None,
    *,
    meta: dict | None = None,
    config=None,
) -> dict:
    args = dict(arguments or {})
    try:
        claim = _resolve_claim("knowledge", name, args, meta=meta, config=config)
        session, bind_id = claim["session"], claim.get("bind_id")
        workspace = (args.get("project_root") or claim.get("workspace_root")
                     or session_context(session, config=config).get("workspace_root") or os.getcwd())
    except Exception as exc:
        return _admission_failure(exc)
    succeeded = False
    try:
        if name == "mindie_entry":
            op = args.get("op") or args.get("action") or "status"
            if op == "status":
                payload = status_facts(session, config=config)
            elif op == "deactivate":
                payload = deactivate(session, config=config)
            elif op in {"init", "activate"}:
                cfg_file = config_path(config)
                if not cfg_file.is_file():
                    from setup import ensure_default_profile

                    ensure_default_profile(config=cfg_file, workspace_root=workspace)
                payload = activate(session, workspace, domain=args.get("domain"), config=config)
            elif op in {"choose", "sharing-enable", "sharing-disable"}:
                from setup import configure_community

                enabled = args.get("choice") == "contribute" if op == "choose" else (op == "sharing-enable")
                payload = configure_community(
                    config=config,
                    enabled=enabled,
                    fork=args.get("repository"),
                    account=args.get("account"),
                    project_root=workspace,
                )
            elif op in {"sharing-status", "reporting-status", "recover"}:
                payload = status_facts(session, config=config)
            else:
                raise ValueError(f"unsupported mindie_entry op: {op}")
            succeeded = payload.get("status") != "partial" and not payload.get("reporting_error")
            result = dict(
                content=[dict(type="text", text=canonical(payload))],
                structuredContent=payload,
                isError=not succeeded,
            )
        else:
            payload = execute_knowledge_tool(name, args, session=session)
            is_error = False
            if name == "knowledge_distill":
                from mindie_knowledge.graded.distill import result_view

                payload, is_error = result_view(payload)
            elif name == "knowledge_sync":
                is_error = payload.get("status") != "ok"
            succeeded = not is_error
            result = dict(
                content=[dict(type="text", text=canonical(payload))],
                structuredContent=payload,
                isError=bool(is_error),
            )
    except Exception as exc:
        result = dict(
            content=[dict(type="text", text=f"{type(exc).__name__}: {exc}")],
            structuredContent=dict(error=str(exc), error_type=type(exc).__name__),
            isError=True,
        )

    if bind_id is not None:
        try:
            finish_bind(bind_id, succeeded, config=config)
        except (OSError, sqlite3.Error) as exc:
            if succeeded:
                result = _attach_accounting_warning(result, "bind_warning", exc)
    return result


def handle_rpc(request: dict, surface: str = "tools", *, config=None) -> dict | None:
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
                capabilities=dict(tools=dict(listChanged=False)),
                serverInfo=dict(name=f"mindie-wb-{surface}",
                                version=read_json(HERE.parent / "plugin.json")["version"]),
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
            out = call_remote_surface(tool_name, arguments, meta=meta, config=config)
        elif surface == "knowledge":
            out = call_knowledge_surface(tool_name, arguments, meta=meta, config=config)
        else:
            out = call_tools_surface(tool_name, arguments, meta=meta, config=config)
        return dict(jsonrpc="2.0", id=req_id, result=out)
    return dict(jsonrpc="2.0", id=req_id, error=dict(code=-32601, message=f"Method not found: {method}"))


def serve(surface: str = "tools") -> int:
    """Run the line-delimited JSON-RPC 2.0 loop over stdin/stdout."""
    if surface == "components":
        surface = "tools"
    if surface not in {"remote", "tools", "knowledge"}:
        raise ValueError(f"unknown MindIE MCP surface: {surface}")
    for raw_line in sys.stdin.buffer:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            req = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            resp = dict(jsonrpc="2.0", id=None, error=dict(code=-32700, message=f"Parse error: {type(exc).__name__}: {exc}"))
            sys.stdout.buffer.write((canonical(resp) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
            continue
        resp = handle_rpc(req, surface)
        if resp is not None:
            sys.stdout.buffer.write((canonical(resp) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = [x for x in argv if x != "--once"]
    surface = args[0] if args else "tools"
    if "--once" not in argv:
        from remote_dev.mcp.catalog_refresh import profile_selection, serve_selected
        return serve_selected(lambda: profile_selection(
            config_path(), HERE, surface, config_env="MINDIE_AGENT_CONFIG", plugin_env="WB_PLUGIN_ROOT",
        ))
    return serve(surface)


if __name__ == "__main__":
    raise SystemExit(main())
