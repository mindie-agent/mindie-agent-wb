"""Tier 0 & Tier 1 tests for WorkBuddy native plugin + Connector layout, contracts, and installer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
WORKBUDDY_CLI = Path("/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy")


def test_plugin_manifests_and_connector_mcp_layout():
    # 1. Agent Plugins 1.0.0 root manifest (WorkBuddy UI Connector / Plugin loader)
    plugin_json = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert plugin_json["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert plugin_json["name"] == "mindie-agent"
    assert isinstance(plugin_json["repository"], str)
    assert isinstance(plugin_json["author"], dict)
    assert "logo" not in plugin_json
    assert (PLUGIN_ROOT / "assets" / "logo.svg").is_file()
    assert (PLUGIN_ROOT / "assets" / "icon.svg").is_file()
    assert (PLUGIN_ROOT / "assets" / "mindie-agent-icon.png").is_file()
    assert (PLUGIN_ROOT / "assets" / "mindie-agent-logo.png").is_file()
    assert (PLUGIN_ROOT / "ai.workbuddy" / "assets" / "icon.svg").is_file()
    for comp in ("remote-dev", "knowledge", "diagnostics", "profiler", "sim", "npu-top", "coordinator", "mindie-bench"):
        assert (PLUGIN_ROOT / "assets" / "icons" / f"{comp}-icon.png").is_file()
        assert (PLUGIN_ROOT / "assets" / "icons" / f"{comp}-icon.svg").is_file()
    wb_ext = plugin_json["extensions"]["ai.workbuddy"]
    assert wb_ext["display"]["name"] == "MindIE Agent"

    # 2. CodeBuddy / WorkBuddy CLI manifests
    for rel_dir in (".codebuddy-plugin", ".workbuddy-plugin", ".claude-plugin"):
        sub_manifest = json.loads((PLUGIN_ROOT / rel_dir / "plugin.json").read_text(encoding="utf-8"))
        assert sub_manifest["name"] == "mindie-agent"
        assert sub_manifest["version"] == plugin_json["version"]

    # 3. Marketplace manifest
    mp_json = json.loads((REPO_ROOT / ".codebuddy-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert mp_json["name"] == "mindie-agent-marketplace"
    assert mp_json["plugins"][0]["name"] == "mindie-agent"

    # 4. Hooks layout (`hooks/hooks.json`)
    hooks_file = PLUGIN_ROOT / "hooks" / "hooks.json"
    assert hooks_file.is_file()
    hooks_cfg = json.loads(hooks_file.read_text(encoding="utf-8"))
    events = hooks_cfg["hooks"]
    assert set(events) == {"SessionStart", "UserPromptExpansion", "UserPromptSubmit", "PreToolUse", "Stop"}

    # 5. Dual MCP configs (`.mcp.json` for Agent CLI + `mcp.json` for Agent Plugins 1.0.0 Connector)
    dot_mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert set(dot_mcp["mcpServers"]) == {"mindie-remote-dev", "mindie-tools", "mindie-knowledge"}
    for srv in dot_mcp["mcpServers"].values():
        assert srv["type"] == "stdio"
        assert srv["alwaysLoad"] is True
        assert srv["defer_loading"] is False

    connector_mcp = json.loads((PLUGIN_ROOT / "mcp.json").read_text(encoding="utf-8"))
    assert connector_mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert set(connector_mcp["mcpServers"]) == {"mindie-remote-dev", "mindie-tools", "mindie-knowledge"}
    allowed_connector_keys = {"type", "command", "args", "env", "cwd"}
    for srv in connector_mcp["mcpServers"].values():
        assert srv["type"] == "stdio"
        assert set(srv).issubset(allowed_connector_keys)


def test_product_and_harness_compatibility_contracts_and_module_invariants(monkeypatch: pytest.MonkeyPatch):
    product = json.loads((REPO_ROOT / "product-contract.json").read_text(encoding="utf-8"))
    assert product["schema"] == "mindie-product-contract/1"
    assert product["harness"] == "workbuddy"
    assert set(product["components"]) == {
        "mindie-knowledge",
        "remote-dev",
        "mindie-diagnostics",
        "mindie-coordinator",
        "mindie-profiler",
        "mindie-sim",
        "mindie-debugger",
        "npu-top",
    }
    assert (PLUGIN_ROOT / "skills" / "mindie-agent" / "SKILL.md").is_file()
    assert (SCRIPTS_DIR / "organizer.py").is_file()

    compat = json.loads((REPO_ROOT / "harness-compatibility.json").read_text(encoding="utf-8"))
    assert compat["schema"] == "mindie-harness-compatibility/1"
    assert set(compat["used_interfaces"]) == set(compat["interface_registry"])
    import mindie_knowledge.releases as releases_mod

    assert releases_mod.REPOSITORIES["workbuddy"] == "mindie-agent/mindie-agent-wb"
    releases_mod.validate_compatibility(compat, harness_name="workbuddy")
    assert releases_mod.supports_harness(compat, harness_name="workbuddy", harness_version="1.2.15")
    assert releases_mod.supports_harness(compat, harness_name="workbuddy", harness_version="2.104.3")
    assert releases_mod.supports_harness(compat, harness_name="workbuddy", harness_version="5.3.0")

    for iface_id, entry in compat["interface_registry"].items():
        assert entry["code_paths"], f"{iface_id} missing code_paths"
        assert entry["test_ids"], f"{iface_id} missing test_ids"
        for code_ref in entry["code_paths"]:
            rel_path = code_ref.split(":", 1)[0]
            assert (REPO_ROOT / rel_path).is_file(), f"Missing code path {rel_path}"
        for test_id in entry["test_ids"]:
            test_rel, func_name = test_id.split("::", 1)
            test_file = REPO_ROOT / test_rel
            assert test_file.is_file(), f"Missing test file {test_rel}"
            assert f"def {func_name}(" in test_file.read_text(encoding="utf-8")

    required_sections = ("[Role]", "[Invariants]", "[Timeouts & Locks]", "[Side-Effect Policy]")
    for script in sorted(SCRIPTS_DIR.glob("*.py")):
        text = script.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert len(lines) < 800, f"{script.name} exceeds 800 lines ({len(lines)})"
        for section in required_sections:
            assert section in text, f"{script.name} missing mandatory header section {section}"

    for fpath in REPO_ROOT.rglob("*"):
        if (
            fpath.is_file()
            and fpath.suffix in {".py", ".json", ".md", ".toml", ".svg"}
            and not any(part in {".pytest_cache", ".venv", ".git"} for part in fpath.parts)
        ):
            raw = fpath.read_bytes()
            assert b"\r\n" not in raw, f"CRLF line endings found in {fpath.relative_to(REPO_ROOT)}"


def test_install_workbuddy_plugin_registers_marketplace_cache_and_connector(tmp_path: Path):
    import setup

    wb_dir = tmp_path / "workbuddy"
    official_mp = wb_dir / "plugins" / "marketplaces" / "workbuddy-connector-plugins-official" / ".codebuddy-plugin"
    official_mp.mkdir(parents=True)
    (official_mp / "marketplace.json").write_text(
        json.dumps({"name": "workbuddy-connector-plugins-official", "plugins": []}),
        encoding="utf-8",
    )
    snap_dir = wb_dir / "storage" / "skeleton"
    snap_dir.mkdir(parents=True)
    test_uid = "4a4a56dc-94d7-4f41-af55-02b3dbbbfb59"
    (snap_dir / "account-snapshot.json").write_text(
        json.dumps({"primary": {"version": 1, "uid": test_uid, "type": "enterprise"}}),
        encoding="utf-8",
    )
    conn_state_dir = wb_dir / "connectors" / test_uid
    conn_state_dir.mkdir(parents=True)
    (conn_state_dir / "connector-states.json").write_text(
        json.dumps({"accountIdentityKey": f"{test_uid}|ent-01"}),
        encoding="utf-8",
    )

    res = setup.install_workbuddy_plugin(wb_dir=wb_dir)
    assert res["status"] == "installed"
    assert res["enabled"] is True
    assert res["plugin_id"] == "mindie-agent@mindie-agent-marketplace"
    cache_path = Path(res["cache_install_path"])
    assert (cache_path / "plugin.json").is_file()
    assert (cache_path / "mcp.json").is_file()
    assert (cache_path / ".mcp.json").is_file()
    assert (cache_path / "hooks" / "hooks.json").is_file()
    assert (cache_path / "assets" / "logo.svg").is_file()
    assert (cache_path / "assets" / "icon.svg").is_file()
    assert (cache_path / "assets" / "mindie-agent-icon.png").is_file()
    assert (cache_path / "assets" / "mindie-agent-logo.png").is_file()
    assert (cache_path / "assets" / "icons" / "remote-dev-icon.png").is_file()
    assert (cache_path / "assets" / "icons" / "knowledge-icon.png").is_file()
    assert (cache_path / "ai.workbuddy" / "assets" / "icon.svg").is_file()

    known_mp = json.loads(Path(res["known_marketplaces"]).read_text(encoding="utf-8"))
    assert "mindie-agent-marketplace" in known_mp

    installed = json.loads(Path(res["installed_plugins"]).read_text(encoding="utf-8"))
    assert "mindie-agent@mindie-agent-marketplace" in installed["plugins"]
    assert "mindie-agent@workbuddy-connector-plugins-official" in installed["plugins"]

    settings = json.loads(Path(res["settings_file"]).read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["mindie-agent@mindie-agent-marketplace"] is True
    assert settings["enabledPlugins"]["mindie-agent@workbuddy-connector-plugins-official"] is False

    acct_state = json.loads((wb_dir / "plugin-account-state" / f"{test_uid}.json").read_text(encoding="utf-8"))
    assert acct_state["version"] == 1
    assert acct_state["identityKey"] == f"{test_uid}|ent-01"
    assert acct_state["plugins"]["mindie-agent@workbuddy-connector-plugins-official"]["enabled"] is True

    connector_path = Path(res["connector_path"])
    assert (connector_path / "plugin.json").is_file()
    assert (connector_path / "ai.workbuddy" / "assets" / "icon.svg").is_file()
    mp_entries = json.loads((official_mp / "marketplace.json").read_text(encoding="utf-8"))["plugins"]
    assert any(p.get("name") == "mindie-agent" and p.get("version") == "0.1.4" for p in mp_entries)


@pytest.mark.skipif(not WORKBUDDY_CLI.is_file(), reason="WorkBuddy CLI not installed on this machine")
def test_live_workbuddy_cli_plugin_validate():
    env = dict(os.environ)
    env["CODEBUDDY_FORCE_LITE_WB_BUNDLE"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    for target in (REPO_ROOT, PLUGIN_ROOT):
        proc = subprocess.run(
            [str(WORKBUDDY_CLI), "plugin", "validate", str(target)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        assert proc.returncode == 0, f"codebuddy plugin validate failed on {target}: {proc.stderr}\n{proc.stdout}"
        assert "Validation passed" in proc.stdout


def test_wb_knowledge_cli_explore_budget_and_rsi_wrappers(tmp_path: Path):
    import experiment_ledger
    import knowledge_cli
    import rsi_loop

    q_tool = next(t for t in knowledge_cli.PRIVATE_KNOWLEDGE_CATALOG if t["name"] == "knowledge_query")
    e_tool = next(t for t in knowledge_cli.PRIVATE_KNOWLEDGE_CATALOG if t["name"] == "knowledge_explain")
    assert q_tool["inputSchema"]["properties"]["token_budget"]["maximum"] == 64000
    assert e_tool["inputSchema"]["properties"]["token_budget"]["maximum"] == 64000

    ledger_path = tmp_path / "ledger.jsonl"
    assert experiment_ledger.main(["render", "--ledger", str(ledger_path), "--task-id", "test-rsi"]) == 0
    assert (
        rsi_loop.main(
            [
                "init",
                "--state",
                str(tmp_path / "state.json"),
                "--task-id",
                "test-rsi",
                "--ledger",
                str(ledger_path),
                "--baseline-repo",
                str(REPO_ROOT),
                "--direction",
                "minimize",
                "--repetitions",
                "3",
            ]
        )
        == 0
    )

