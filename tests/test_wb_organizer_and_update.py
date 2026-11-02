"""Tier 0 & Tier 1 tests for `organizer.py` (`mindie-summary-outcome/1`) and `component_update.py`."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
import pytest

from mindie_knowledge.materials import summarizer
import component_update
import organizer
import paths
import setup


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sample_request() -> tuple[dict, str]:
    block_id = "a" * 64
    task_id = "b" * 64
    blocks = [
        {
            "block_id": block_id,
            "text": "### user\n排查 A3 HCCL 超时\n\n### assistant\n定位到双 Die Phy-ID 绑定冲突。",
            "source_range": {"start": 0, "end": 120, "digest": "c" * 64},
        }
    ]
    pol = organizer.identity()
    req = summarizer.make_request(
        task_id=task_id,
        body_version="rev-1",
        blocks=blocks,
        prior_navigation=None,
        identity=pol,
    )
    return req, block_id


def test_organizer_identity_passes_core_summarizer_validation():
    ident = organizer.identity()
    validated = summarizer.validate_identity(ident)
    assert validated["fingerprint"] == ident["fingerprint"]
    assert "mindie-distill-request/1" in validated["capabilities"]


def test_organizer_emits_valid_summary_outcome_and_never_retries_unknown(
    monkeypatch: pytest.MonkeyPatch,
):
    req, block_id = _sample_request()
    mode = {"behavior": "ok"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if mode["behavior"] == "timeout":
                self.send_response(504)
                self.end_headers()
                return
            body = json.dumps(
                {
                    "result": {
                        "blocks": [
                            {
                                "block_id": block_id,
                                "title": "A3 双 Die Phy-ID 绑定冲突排查",
                                "summary": "定位昇腾 A3 910C 双 Die 拓扑下 HCCL 通信超时根因为相邻 Die 混绑。",
                            }
                        ],
                        "navigation": {
                            "title": "A3 HCCL 通信超时与双 Die 映射排查",
                            "summary": "排查并解决昇腾 A3 双 Die Phy-ID 绑定冲突导致的 HCCL 超时。",
                        },
                    },
                    "usage": {"input_tokens": 180, "cached_input_tokens": 0, "output_tokens": 65},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("MINDIE_SUMMARY_ENDPOINT", f"http://127.0.0.1:{port}/summarize")

        out = organizer.execute_request(req)
        summarizer.validate_outcome(out, req)
        assert out["status"] == "returned"
        assert out["model_calls"] == 1
        assert out["billing_status"] == "reported"
        assert out["usage"] == {"input_tokens": 180, "cached_input_tokens": 0, "output_tokens": 65}

        # When failure happens after request transmission, status MUST be outcome_unknown (never auto-replayed)
        mode["behavior"] = "timeout"
        out_unknown = organizer.execute_request(req)
        summarizer.validate_outcome(out_unknown, req)
        assert out_unknown["status"] == "outcome_unknown"
        assert out_unknown["model_calls"] == 1
        assert out_unknown["billing_status"] == "unknown"
    finally:
        server.shutdown()
        server.server_close()


def test_flexible_component_update_baseline_and_atomic_rebind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mindie_knowledge.component_runtime import source_baseline

    baseline = source_baseline(REPO_ROOT)
    assert set(baseline["runtime"]) == {"mindie-knowledge", "remote-dev", "mindie-diagnostics"}
    assert all(len(rev) == 40 for rev in baseline["runtime"].values())

    cfg_file = tmp_path / "config" / "wb.json"
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(tmp_path / "data"))
    setup.ensure_default_profile(config=cfg_file, workspace_root=tmp_path)

    fake_py = tmp_path / "new-venv" / "bin" / "python"
    fake_py.parent.mkdir(parents=True, exist_ok=True)
    fake_py.write_text("#!/bin/sh\n", encoding="utf-8")

    fake_artifact = {
        "generation": str(tmp_path / "gen-1"),
        "source": str(REPO_ROOT),
        "python": str(fake_py),
        "components": {
            "schema": "mindie-component-runtime/1",
            "adapter_source_sha256": baseline["source_sha256"],
            "runtime": {
                "mindie-knowledge": {
                    "repository": "mindie-agent/knowledge",
                    "version": "0.13.99",
                    "revision": "a" * 40,
                }
            },
            "lock_sha256": "b" * 64,
        },
        "installed": {
            "mindie-knowledge": {
                "repository": "mindie-agent/knowledge",
                "version": "0.13.99",
                "revision": "a" * 40,
            }
        },
    }
    monkeypatch.setattr(component_update, "prepare", lambda *a, **kw: fake_artifact)
    res = component_update.check(config=cfg_file, source=REPO_ROOT)
    assert res["status"] == "updated"
    assert Path(res["python"]).resolve() == fake_py.resolve()

    rebound = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert Path(rebound["python"]).resolve() == fake_py.resolve()
    assert rebound["component_runtime"]["runtime"]["mindie-knowledge"]["version"] == "0.13.99"


def test_enterprise_transport_env_and_staged_source_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = paths.apply_enterprise_transport_env({})
    assert env["UV_SYSTEM_CERTS"] == "true"
    assert env["UV_NATIVE_TLS"] == "true"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "core.longpaths"
    assert env["GIT_CONFIG_VALUE_0"] == "true"
    assert env["GIT_CONFIG_KEY_1"] == "core.autocrlf"
    assert env["GIT_CONFIG_VALUE_1"] == "false"

    crlf_src = tmp_path / "crlf-src"
    crlf_src.mkdir()
    for name in ("pyproject.toml", "product-contract.json", "harness-compatibility.json"):
        raw = (REPO_ROOT / name).read_bytes().replace(b"\n", b"\r\n")
        (crlf_src / name).write_bytes(raw)
    staged_dst = tmp_path / "data" / "wb-source"
    result_dir = setup.stage_source_baseline(crlf_src, staged_dst)
    assert result_dir == staged_dst.resolve()
    for name in ("pyproject.toml", "product-contract.json", "harness-compatibility.json"):
        assert b"\r\n" not in (staged_dst / name).read_bytes()

    mounted_plugin = tmp_path / "workbuddy" / "plugins" / "cache" / "mindie-agent"
    mounted_plugin.mkdir(parents=True)
    monkeypatch.setenv("CODEBUDDY_PLUGIN_ROOT", str(mounted_plugin))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(tmp_path / "config"))
    assert paths.repo_root() == staged_dst.resolve()
