"""Fast isolated tests for WorkBuddy manifests, installer modes, setup CLI, and contracts."""

from __future__ import annotations

import io
import ast
import json
from pathlib import Path
import sys

import pytest

import mindie_launch
import setup
import wb_install

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mindie-agent"
SCRIPTS = PLUGIN_ROOT / "scripts"
VERSION = "0.1.19"


class _Stdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, text: str) -> int:
        self.buffer.write(text.encode("utf-8"))
        return len(text)

    def flush(self) -> None:
        pass


def _run_main(monkeypatch, module, argv):
    sink = _Stdout()
    monkeypatch.setattr(sys, "stdout", sink)
    code = module.main(argv)
    return code, sink.buffer.getvalue().decode("utf-8")


def _json_output(raw: str) -> dict:
    return json.loads(raw.strip().splitlines()[-1])


def test_manifests_versions_and_disjoint_mcp_layout():
    assert 'version = "0.1.19"' in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "mindie-agent-wb"\nversion = "0.1.19"' in (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    root_manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert root_manifest["version"] == VERSION
    for relative in (".codebuddy-plugin/plugin.json", ".workbuddy-plugin/plugin.json", ".claude-plugin/plugin.json"):
        assert json.loads((PLUGIN_ROOT / relative).read_text(encoding="utf-8"))["version"] == VERSION
    marketplace = json.loads((REPO_ROOT / ".codebuddy-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["plugins"][0]["version"] == VERSION
    assert json.loads((REPO_ROOT / "product-contract.json").read_text(encoding="utf-8"))["native_version"] == VERSION

    display = root_manifest["extensions"]["ai.workbuddy"]["display"]
    assert "15" in display["description_en"] and "mindie_doctor" in display["description_en"]
    assert "15" in display["description_zh"] and "mindie_doctor" in display["description_zh"]

    dot_mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert set(dot_mcp["mcpServers"]) == {"mindie-remote-dev", "mindie-tools", "mindie-knowledge"}
    for server in dot_mcp["mcpServers"].values():
        assert server["type"] == "stdio" and server["alwaysLoad"] is True and server["defer_loading"] is False
    assert dot_mcp["mcpServers"]["mindie-remote-dev"]["env"]["REMOTE_DEV_HARNESS_PROFILE"] == "wb"
    connector_mcp = json.loads((PLUGIN_ROOT / "mcp.json").read_text(encoding="utf-8"))
    assert set(connector_mcp["mcpServers"]) == {"mindie-remote-dev", "mindie-tools", "mindie-knowledge"}
    assert connector_mcp["mcpServers"]["mindie-remote-dev"]["env"]["REMOTE_DEV_HARNESS_PROFILE"] == "wb"

    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"}
    assert (PLUGIN_ROOT / "runtime-requirements.txt").is_file()


def test_seed_bundle_and_installer_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_mindie_env):
    assert mindie_launch.verify_seed_bundle(PLUGIN_ROOT)["schema"] == "mindie-harness-seed/1"
    wb_root = isolated_mindie_env["wb_home"]
    stale = wb_root / "plugins" / "cache" / "mindie-agent-marketplace" / "mindie-agent" / "0.0.1"
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("stale", encoding="utf-8")
    config = isolated_mindie_env["config_path"]
    config.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")

    standalone = wb_install.install_workbuddy_plugin(wb_dir=wb_root)
    cache = Path(standalone["cache_install_path"])
    assert standalone["status"] == "installed" and standalone["version"] == VERSION
    assert (cache / "plugin.json").is_file() and (cache / "runtime-requirements.txt").is_file()
    assert (cache / ".mcp.json").is_file() and (cache / "mcp.json").is_file()
    assert not stale.exists()
    installed_mcp = json.loads((cache / ".mcp.json").read_text(encoding="utf-8"))
    assert installed_mcp["mcpServers"]["mindie-remote-dev"]["command"] == sys.executable
    hooks = json.loads((cache / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert sys.executable in hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
    installed = json.loads((wb_root / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    assert "mindie-agent@mindie-agent-marketplace" in installed["plugins"]
    settings = json.loads((wb_root / "settings.json").read_text(encoding="utf-8"))
    assert settings["enabledPlugins"]["mindie-agent@mindie-agent-marketplace"] is True
    assert "mindie-agent-marketplace" in json.loads((wb_root / "plugins" / "known_marketplaces.json").read_text(encoding="utf-8"))

    official = wb_root / "plugins" / "marketplaces" / "workbuddy-connector-plugins-official"
    (official / ".codebuddy-plugin").mkdir(parents=True)
    (official / ".codebuddy-plugin" / "marketplace.json").write_text(json.dumps({"name": "workbuddy-connector-plugins-official", "plugins": []}), encoding="utf-8")
    uid = "4a4a56dc-94d7-4f41-af55-02b3dbbbfb59"
    snapshot = wb_root / "storage" / "skeleton" / "account-snapshot.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({"primary": {"uid": uid, "enterpriseId": "ent-01"}}), encoding="utf-8")
    connector_state = wb_root / "connectors" / uid / "connector-states.json"
    connector_state.parent.mkdir(parents=True)
    connector_state.write_text(json.dumps({"accountIdentityKey": f"{uid}|ent-01"}), encoding="utf-8")

    connector = wb_install.install_workbuddy_plugin(wb_dir=wb_root)
    assert Path(connector["connector_path"]).is_dir() and Path(connector["connector_cache_path"]).is_dir()
    catalog = json.loads((official / ".codebuddy-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert any(item.get("name") == "mindie-agent" and item.get("version") == VERSION for item in catalog["plugins"])
    account = json.loads((wb_root / "plugin-account-state" / f"{uid}.json").read_text(encoding="utf-8"))
    assert account["identityKey"] == f"{uid}|ent-01"
    assert account["plugins"]["mindie-agent@workbuddy-connector-plugins-official"]["enabled"] is True


def test_setup_dry_run_init_install_and_sharing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_mindie_env):
    cfg_file = isolated_mindie_env["config_path"]
    wb_root = isolated_mindie_env["wb_home"]
    assert not cfg_file.exists()
    for argv in (["--dry-run"], ["init", "--dry-run"], ["init", "--install-wb", "--dry"], ["install-wb", "--dry-run"], ["sharing", "--enable", "--dry-run"]):
        code, output = _run_main(monkeypatch, setup, argv)
        assert code == 0
        plan = _json_output(output)
        assert plan["status"] == "dry-run" and plan["bootstrap"] is False
        assert not cfg_file.exists() and not any(wb_root.iterdir())
    with pytest.raises(SystemExit) as exc:
        setup.main(["--help"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc:
        setup.main(["init", "--help"])
    assert exc.value.code == 0

    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, output = _run_main(monkeypatch, setup, ["init", "--install-wb", "--workspace-root", str(workspace), "--wb-dir", str(wb_root)])
    assert code == 0
    receipt = _json_output(output)
    assert receipt["workbuddy_install"]["status"] == "installed"
    assert (cfg_file).is_file() and (cfg_file.with_name("wb.engine.json")).is_file()
    engine = json.loads(cfg_file.with_name("wb.engine.json").read_text(encoding="utf-8"))
    assert engine["transcript_adapter"].endswith("wb_transcript.py") and "redactor_executable" in engine

    for argv, expected in (
        (["sharing", "--enable", "--project-root", str(workspace)], "contribute"),
        (["sharing", "--status"], "contribute"),
        (["sharing", "--local-only"], "local-only"),
        (["sharing", "--disable"], "disabled"),
    ):
        code, output = _run_main(monkeypatch, setup, argv)
        assert code == 0
        assert _json_output(output)["tier"] == expected


def test_harness_compatibility_code_paths_tests_and_module_invariants():
    compatibility = json.loads((REPO_ROOT / "harness-compatibility.json").read_text(encoding="utf-8"))
    assert compatibility["schema"] == "mindie-harness-compatibility/1"
    assert "summary.structured-output" not in compatibility["used_interfaces"]
    assert set(compatibility["used_interfaces"]) == set(compatibility["interface_registry"])
    from mindie_knowledge.releases import validate_compatibility

    validate_compatibility(compatibility, harness_name="workbuddy")
    for entry in compatibility["interface_registry"].values():
        for reference in entry["code_paths"]:
            target, _, symbol = reference.partition(":")
            assert (REPO_ROOT / target).is_file()
            if symbol:
                tree = ast.parse((REPO_ROOT / target).read_text(encoding="utf-8"))
                assert symbol in {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for reference in entry["test_ids"]:
            target, _, symbol = reference.partition("::")
            assert (REPO_ROOT / target).is_file()
            assert f"def {symbol}(" in (REPO_ROOT / target).read_text(encoding="utf-8")
    assert setup.release_metadata()["status"] == "validated"

    scripts = sorted(SCRIPTS.glob("*.py"))
    assert {path.name for path in scripts} == {"mindie_launch.py", "bridge.py", "mcp_server.py", "wb_transcript.py", "wb_install.py", "setup.py"}
    assert sum(len(path.read_text(encoding="utf-8").splitlines()) for path in scripts) <= 1200
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert len(text.splitlines()) < 800
        head = "\n".join(text.splitlines()[:30])
        for section in ("[Role]", "[Invariants]", "[Timeouts & Locks]", "[Side-Effect Policy]"):
            assert section in head
        assert b"\r\n" not in script.read_bytes()
