"""Isolated Tencent WorkBuddy (`wb`) summary and C6 distillation worker (`organizer.py`).

[Role]
Executes a single bounded model invocation to summarize redacted transcript
blocks (`mindie-summary-request/1`) or perform explicit C6 distillation
(`mindie-distill-request/1`), emitting one `mindie-summary-outcome/1` JSON
envelope on stdout.

[Invariants]
- Exit 0 indicates the outcome envelope was delivered on stdout; never retries
  uncertain/billed model calls (`outcome_unknown`) or substitutes fallback
  summaries.
- Supports `--identity` for `mindie_knowledge.materials.summarizer.validate_identity`.
- Supports loopback summary endpoint (`summary-endpoint.json` / `MINDIE_SUMMARY_ENDPOINT`)
  or native WorkBuddy `codebuddy -p` CLI in an isolated scratch profile
  (`summary_scratch.create_profile`) with `CODEBUDDY_FORCE_LITE_WB_BUNDLE=1` and
  `DISABLE_TELEMETRY=1`.

[Timeouts & Locks]
- Uses `mindie_knowledge.loop.summary_budget.native_timeout()` reserving `5.0s`
  for cleanup inside the parent worker deadline.

[Side-Effect Policy]
- Creates and cleans up one private temporary profile; never modifies user
  workspaces, material stores, or publication state directly.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request

from mindie_knowledge.loop import summary_scratch
from mindie_knowledge.loop.summary_budget import native_timeout
from mindie_knowledge.materials import summarizer

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from bounded import run as bounded_run
from paths import state_dir, workbuddy_cli_bin

SUMMARY_MODEL = os.environ.get("MINDIE_SUMMARY_MODEL") or "deepseek-v3"
SUMMARY_EFFORT = "low"
MAX_REQUEST_BYTES = 128 * 1024
MAX_DISTILL_REQUEST_BYTES = 512 * 1024
DISTILL_REQUEST_SCHEMA = "mindie-distill-request/1"


class NativeFailure(RuntimeError):
    def __init__(self, message: str, *, reason: str = "native_failure"):
        super().__init__(message)
        self.reason = reason


class NativeStartError(OSError):
    pass


def _distill_capability() -> tuple[list[str], Path | None]:
    try:
        from mindie_knowledge.graded import distill
    except ImportError:
        return [], None
    required = ("build_prompt", "decision_schema", "parse_validate_result", "validate_decision")
    if (
        getattr(distill, "REQUEST_SCHEMA", None) != DISTILL_REQUEST_SCHEMA
        or not all(callable(getattr(distill, name, None)) for name in required)
    ):
        return [], None
    return [DISTILL_REQUEST_SCHEMA], Path(distill.__file__)


def identity(model: str | None = None) -> dict:
    selected_model = model or os.environ.get("MINDIE_SUMMARY_MODEL") or SUMMARY_MODEL
    sources = [Path(__file__), Path(summarizer.__file__), Path(summary_scratch.__file__)]
    capabilities, distill_source = _distill_capability()
    if distill_source is not None:
        sources.append(distill_source)
    implementation = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    implementation["native_command"] = os.environ.get("MINDIE_WB_BIN") or workbuddy_cli_bin() or "codebuddy"
    value = summarizer.policy_identity(
        model=selected_model,
        effort=SUMMARY_EFFORT,
        implementation=implementation,
    )
    value["capabilities"] = capabilities
    value["fingerprint"] = summarizer.digest({k: v for k, v in value.items() if k != "fingerprint"})
    return value


def _resolve_endpoint() -> dict | None:
    env_url = os.environ.get("MINDIE_SUMMARY_ENDPOINT")
    if env_url:
        return dict(
            url=env_url,
            token=os.environ.get("MINDIE_SUMMARY_TOKEN", ""),
            model=os.environ.get("MINDIE_SUMMARY_MODEL") or SUMMARY_MODEL,
        )
    ep_file = state_dir() / "summary-endpoint.json"
    if ep_file.is_file():
        try:
            data = json.loads(ep_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("url"), str):
                return data
        except (OSError, ValueError):
            return None
    return None


class NativeSummaryModel:
    """Single-call model bridge for LangMem and C6 distillation; never retries."""

    def __init__(self):
        self.calls = 0
        self.raw_result: str | None = None
        self.usage: dict | None = None
        self.usage_known = False
        self.transmitted = False
        self.cleanup_failed = False

    def _call_once(self, prompt_text: str, schema: dict) -> str:
        self.calls += 1
        if self.calls != 1:
            raise NativeFailure("worker attempted more than one model call", reason="native_extra_turn")
        directory, core_owned = summary_scratch.create_profile(prefix="mindie-wb-summary-")
        try:
            endpoint = _resolve_endpoint()
            if endpoint is not None:
                url = endpoint["url"]
                headers = {"Content-Type": "application/json"}
                if endpoint.get("token"):
                    headers["Authorization"] = f"Bearer {endpoint['token']}"
                payload = json.dumps(
                    dict(
                        model=endpoint.get("model") or SUMMARY_MODEL,
                        prompt=prompt_text,
                        response_schema=schema,
                    ),
                    ensure_ascii=False,
                ).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                budget = native_timeout(max_seconds=120.0)
                opener = (
                    urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    if ("127.0.0.1" in url or "localhost" in url)
                    else urllib.request.build_opener()
                )
                try:
                    self.transmitted = True
                    with opener.open(req, timeout=budget) as resp:
                        raw_bytes = resp.read(summarizer.MAX_RESPONSE_BYTES * 2)
                except urllib.error.HTTPError as exc:
                    raise TimeoutError(f"summary endpoint HTTP {exc.code} after transmission") from exc
                except urllib.error.URLError as exc:
                    if isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
                        self.transmitted = False
                        raise NativeStartError("summary endpoint connection refused") from exc
                    raise TimeoutError("summary endpoint request failed after transmission") from exc

                envelope = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(envelope, dict):
                    raise summarizer.SummaryResultError("invalid endpoint response", reason="result_json_invalid")
                if isinstance(envelope.get("usage"), dict):
                    self.usage = summarizer.clean_usage(envelope["usage"])
                    self.usage_known = True
                text = envelope.get("result")
                if isinstance(text, dict):
                    text = json.dumps(text, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                if not isinstance(text, str) or not text.strip():
                    raise summarizer.SummaryResultError("empty summary response", reason="result_json_invalid")
                self.raw_result = text
                return text

            cli_bin = workbuddy_cli_bin()
            if cli_bin is None:
                raise NativeStartError("WorkBuddy CLI binary is not available and no summary endpoint is configured")
            env = dict(os.environ)
            env["CODEBUDDY_FORCE_LITE_WB_BUNDLE"] = "1"
            env["DISABLE_TELEMETRY"] = "1"
            env["CODEBUDDY_CONFIG_DIR"] = str(directory)
            env["WORKBUDDY_CONFIG_DIR"] = str(directory)
            budget = native_timeout(max_seconds=120.0)
            self.transmitted = True
            proc = bounded_run(
                [
                    cli_bin,
                    "-p",
                    prompt_text,
                    "--output-format",
                    "json",
                    "--json-schema",
                    json.dumps(schema, ensure_ascii=False),
                    "--max-turns",
                    "1",
                    "--tools",
                    "",
                ],
                "",
                timeout=budget,
                max_output=summarizer.MAX_RESPONSE_BYTES * 2,
                env=env,
                cwd=directory,
            )
            self.cleanup_failed = proc.cleanup_failed
            text = proc.checked_stdout().strip()
            if not text:
                raise summarizer.SummaryResultError("empty CLI summary response", reason="result_json_invalid")
            self.raw_result = text
            return text
        finally:
            if not core_owned:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    self.cleanup_failed = True

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        data = [dict(role=m.type, content=m.content) for m in messages]
        prompt = summarizer.render_prompt(data)
        expected_ids = [m.id for m in messages if getattr(m, "id", None)]
        schema = summarizer.output_schema(expected_ids or None)
        raw = self._call_once(prompt, schema)
        return AIMessage(content=raw)


def execute_request(request: dict) -> dict:
    started = time.monotonic()
    model = NativeSummaryModel()
    try:
        summarizer.validate_request(request)
        current_id = identity(request["policy_identity"].get("model"))
        if current_id["fingerprint"] != request["policy_identity"].get("fingerprint"):
            return summarizer.outcome(
                request,
                status="failed",
                model_calls=0,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error="configuration",
                error_reason="worker_policy_changed",
                billing_status="not_called",
            )
        if request.get("schema_version") == DISTILL_REQUEST_SCHEMA:
            from mindie_knowledge.graded import distill

            prompt = distill.build_prompt(request)
            schema = distill.decision_schema(request)
            raw = model._call_once(prompt, schema)
            decision = distill.parse_validate_result(raw, request)
            return summarizer.outcome(
                request,
                status="returned",
                result=decision,
                raw_result=model.raw_result,
                usage=model.usage,
                usage_known=model.usage_known,
                model_calls=model.calls,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                cleanup_failed=model.cleanup_failed,
            )

        result = summarizer.summarize_batch(request, model)
        return summarizer.outcome(
            request,
            status="returned",
            result=result,
            raw_result=model.raw_result,
            usage=model.usage,
            usage_known=model.usage_known,
            model_calls=model.calls,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            cleanup_failed=model.cleanup_failed,
        )
    except NativeStartError:
        return summarizer.outcome(
            request,
            status="failed",
            model_calls=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error="native",
            error_reason="native_start_failed",
            cleanup_failed=model.cleanup_failed,
            billing_status="not_called",
        )
    except summarizer.SummaryResultError as exc:
        return summarizer.outcome(
            request,
            status="failed" if not model.transmitted or model.raw_result is not None else "outcome_unknown",
            raw_result=model.raw_result,
            usage=model.usage,
            usage_known=model.usage_known,
            model_calls=1 if model.transmitted else 0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error="invalid_result",
            error_reason=exc.reason,
            cleanup_failed=model.cleanup_failed,
        )
    except TimeoutError:
        return summarizer.outcome(
            request,
            status="outcome_unknown" if model.transmitted else "failed",
            raw_result=model.raw_result,
            usage=model.usage,
            usage_known=model.usage_known,
            model_calls=1 if model.transmitted else 0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error="deadline",
            error_reason="native_deadline",
            cleanup_failed=model.cleanup_failed,
        )
    except Exception:
        return summarizer.outcome(
            request,
            status="outcome_unknown" if model.transmitted else "failed",
            raw_result=model.raw_result,
            usage=model.usage,
            usage_known=model.usage_known,
            model_calls=1 if model.transmitted else 0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error="native",
            error_reason="native_failure",
            cleanup_failed=model.cleanup_failed,
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--identity" in argv:
        sys.stdout.buffer.write((json.dumps(identity(), ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    raw = sys.stdin.buffer.read(MAX_DISTILL_REQUEST_BYTES + 1)
    if len(raw) > MAX_DISTILL_REQUEST_BYTES:
        raise SystemExit("summary request exceeds byte limit")
    request = json.loads(raw.decode("utf-8"))
    envelope = execute_request(request)
    sys.stdout.buffer.write((json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
