"""Tier 2 (`@pytest.mark.live_npu`) & Tier 3 live WorkBuddy host, MCP subprocess, and auto-update verification."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

pytestmark = pytest.mark.live_host


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"
LAUNCH_SCRIPT = PLUGIN_ROOT / "scripts" / "mindie_launch.py"
WORKBUDDY_CLI = Path("/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy")
LIVE_SESSION = "9c6fa414-9fc6-49e0-8238-939cb9128812"


def _run_launch(args: list[str], stdin_text: str = "", *, env: dict | None = None, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    run_env = dict(os.environ if env is None else env)
    run_env.pop("_MINDIE_WB_REEXEC", None)
    return subprocess.run(
        [sys.executable, "-I", str(LAUNCH_SCRIPT), *args],
        input=stdin_text,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_live_mindie_launch_hooks_and_all_three_mcp_stdio_servers(tmp_path: Path):
    """End-to-end Tier 3 verification of `mindie_launch.py` across hooks and all 3 MCP stdio servers."""
    ws = tmp_path / "ws"
    ws.mkdir()

    # 1. UserPromptSubmit hook with `/mindie-agent`
    hook_in = json.dumps(
        {
            "session_id": LIVE_SESSION,
            "cwd": str(ws),
            "prompt": "/mindie-agent Verify live WorkBuddy MCP and knowledge loop",
        }
    )
    hook_proc = _run_launch(["hook", "user-prompt-submit"], hook_in)
    assert hook_proc.returncode == 0, hook_proc.stderr
    hook_out = json.loads(hook_proc.stdout)
    assert LIVE_SESSION in hook_out["hookSpecificOutput"]["additionalContext"]

    # 2. Live stdio `mcp remote` (`initialize` + `tools/list`)
    remote_reqs = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    remote_proc = _run_launch(["mcp", "remote"], remote_reqs)
    assert remote_proc.returncode == 0, remote_proc.stderr
    remote_lines = [json.loads(line) for line in remote_proc.stdout.strip().splitlines()]
    assert remote_lines[0]["result"]["serverInfo"]["name"] == "mindie-wb-remote"
    remote_tool_names = {t["name"] for t in remote_lines[1]["result"]["tools"]}
    assert len(remote_tool_names) == 33
    assert {"remote_MultiEdit", "remote_ViewImage", "remote_BashOutput", "remote_KillShell"}.issubset(remote_tool_names)

    # 3. Live stdio `mcp tools` (`tools/list` + `mindie_status` + `debugger` + `sim` + `profiler` + `coordinator`)
    tools_reqs = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "mindie_status",
                        "arguments": {},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "debugger",
                        "arguments": {"argv": ["decode-code", "507011"]},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "sim",
                        "arguments": {"argv": ["--help"]},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "profiler",
                        "arguments": {"argv": ["--help"]},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "coordinator",
                        "arguments": {"argv": ["--help"]},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
        ]
    ) + "\n"
    tools_proc = _run_launch(["mcp", "tools"], tools_reqs, timeout=45.0)
    assert tools_proc.returncode == 0, tools_proc.stderr
    tools_lines = [json.loads(line) for line in tools_proc.stdout.strip().splitlines()]
    assert len(tools_lines[0]["result"]["tools"]) == 14
    for idx in range(1, 6):
        assert tools_lines[idx]["result"]["isError"] is False, f"Tool call id={idx+1} failed: {tools_lines[idx]}"

    # 4. Live stdio `mcp knowledge` (`tools/list` + `mindie_entry` + `knowledge_sync` + `knowledge_query`)
    local_feed_repo = Path(
        "/Users/maoxx241/.local/share/mindie-agent/kimi/vllm-ascend/state-v1/feed/"
        "b7c5bd4d68f997d5c714d1f0a783fa2b0be6150dd7bd7de372da2e74ae0abe84/repo.git"
    )
    if local_feed_repo.is_dir():
        eng_cfg_file = Path(os.environ["MINDIE_CONFIG_HOME"]) / "mindie-agent" / "wb.engine.json"
        if eng_cfg_file.is_file():
            eng_cfg = json.loads(eng_cfg_file.read_text(encoding="utf-8"))
            eng_cfg["feeds"] = [
                {
                    "repository": "mindie-agent/knowledge-vllm-ascend",
                    "ref": "main",
                    "domain": "vllm-ascend",
                    "url": str(local_feed_repo),
                }
            ]
            eng_cfg_file.write_text(json.dumps(eng_cfg, indent=2) + "\n", encoding="utf-8")

    know_reqs = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "mindie_entry",
                        "arguments": {"op": "status"},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "knowledge_sync",
                        "arguments": {},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "knowledge_query",
                        "arguments": {"query": "A3 HCCL雙Die", "limit": 3},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
        ]
    ) + "\n"
    know_proc = _run_launch(["mcp", "knowledge"], know_reqs, timeout=60.0)
    assert know_proc.returncode == 0, know_proc.stderr
    know_lines = [json.loads(line) for line in know_proc.stdout.strip().splitlines()]
    assert len(know_lines[0]["result"]["tools"]) == 6
    assert know_lines[1]["result"]["isError"] is False
    assert know_lines[1]["result"]["structuredContent"]["activated"] is True
    assert know_lines[2]["result"]["isError"] is False, f"knowledge_sync failed: {know_lines[2]}"
    assert know_lines[3]["result"]["isError"] is False, f"knowledge_query failed: {know_lines[3]}"


def test_live_component_update_check_verifies_up_to_date(tmp_path: Path):
    """Verify `mindie_launch.py update --check` against live component releases and installed inventory."""
    proc = _run_launch(["update", "--check"], timeout=60.0)
    assert proc.returncode == 0, f"update --check failed: {proc.stderr}\n{proc.stdout}"
    payload = json.loads(proc.stdout.strip())
    assert payload["schema"] == "mindie-component-update/1"
    assert payload["harness"] == "workbuddy"
    assert payload["status"] in {"up_to_date", "updated"}
    assert "mindie-knowledge" in payload["installed"]
    assert "remote-dev" in payload["installed"]
    assert "mindie-diagnostics" in payload["installed"]


@pytest.mark.live_npu
def test_live_npu_cluster_observation_and_remote_probe():
    """Tier 2 (`@pytest.mark.live_npu`): Non-destructive live NPU host check via WorkBuddy MCP."""
    reqs = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "coordinator",
                        "arguments": {"argv": ["hosts", "--action", "list"]},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "npu_top",
                        "arguments": {"argv": ["inventory", "--help"]},
                        "_meta": {"workbuddy.ai/conversationId": LIVE_SESSION},
                    },
                }
            ),
        ]
    ) + "\n"
    proc = _run_launch(["mcp", "tools"], reqs, timeout=25.0)
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines()]
    assert lines[0]["result"]["isError"] is False
    assert lines[1]["result"]["isError"] is False
