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
    assert any(p.get("name") == "mindie-agent" and p.get("version") == "0.1.5" for p in mp_entries)


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


def test_help_and_dry_run_do_not_bootstrap(monkeypatch, capsys):
    import setup

    def _boom(*_args, **_kwargs):
        raise AssertionError("read-only setup must not bootstrap or write")

    monkeypatch.delenv("_MINDIE_WB_SETUP_BOOTSTRAPPED", raising=False)
    monkeypatch.setattr(setup.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(setup, "apply_enterprise_transport_env", lambda target=None: {} if target is None else target)
    for name in (
        "stage_source_baseline",
        "ensure_runtime_python",
        "ensure_default_profile",
        "install_workbuddy_plugin",
        "configure_community",
    ):
        monkeypatch.setattr(setup, name, _boom)

    for argv in (["--help"], ["-h"], ["init", "--help"], ["install-wb", "--help"], ["sharing", "--help"]):
        with pytest.raises(SystemExit) as exc:
            setup.main(argv)
        assert exc.value.code == 0
        assert "--dry-run" in capsys.readouterr().out

    cases = (
        (["--dry-run"], None, False),
        (["init", "--dry-run"], "init", False),
        (["init", "--install-wb", "--dry-run"], "init", True),
        (["--dry-run", "install-wb"], "install-wb", True),
        (["sharing", "--enable", "--dry-run"], "sharing", False),
    )
    for argv, cmd, install in cases:
        assert setup.main(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "schema": "mindie-wb-setup/1",
            "status": "dry-run",
            "bootstrap": False,
            "cmd": cmd,
            "uv": payload["uv"],
            "gh": payload["gh"],
            "install_workbuddy_plugin": install,
            "registers_native_plugin": install,
            "registers_connector_if_marketplace_present": install,
            "profile_only": cmd in (None, "init") and not install,
        }
        assert payload["uv"] == __import__("shutil").which("uv")


def test_abbreviated_dry_run_never_mounts_or_enables(monkeypatch, capsys, isolated_mindie_env):
    """``--dry`` is a parsed ``--dry-run``. It must not stage, mount, or enable contribute."""
    import setup

    roots = [
        isolated_mindie_env["config_home"],
        isolated_mindie_env["data_home"],
        isolated_mindie_env["wb_home"],
        isolated_mindie_env["cb_home"],
    ]

    def _listing(root: Path) -> list[str]:
        return sorted(str(path.relative_to(root)) for path in root.rglob("*"))

    before = {str(root): _listing(root) for root in roots}
    calls: list[str] = []

    def _forbid(name: str):
        def _inner(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"abbreviated dry-run must not call {name}")

        return _inner

    monkeypatch.delenv("_MINDIE_WB_SETUP_BOOTSTRAPPED", raising=False)
    monkeypatch.setattr(setup.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(
        setup,
        "apply_enterprise_transport_env",
        lambda target=None: calls.append("apply_enterprise_transport_env") or ({} if target is None else target),
    )
    for name in (
        "stage_source_baseline",
        "ensure_runtime_python",
        "ensure_default_profile",
        "install_workbuddy_plugin",
        "configure_community",
    ):
        monkeypatch.setattr(setup, name, _forbid(name))

    def _reexec(*_args, **_kwargs):
        calls.append("subprocess")
        raise AssertionError("abbreviated dry-run must not re-exec setup")

    monkeypatch.setattr(setup.subprocess, "run", _reexec)

    pairs = (
        (["init", "--install-wb", "--dry-run"], ["init", "--install-wb", "--dry"]),
        (["init", "--install-wb", "--dry-run"], ["init", "--install-wb", "--dry-r"]),
        (["--dry-run", "init", "--install-wb"], ["--dry", "init", "--install-wb"]),
        (["--dry-run", "install-wb"], ["--dry", "install-wb"]),
        (["install-wb", "--dry-run"], ["install-wb", "--dry"]),
        (["init", "--dry-run"], ["init", "--dry"]),
        (["sharing", "--enable", "--dry-run"], ["sharing", "--enable", "--dry"]),
        (["--dry-run", "sharing", "--enable"], ["--dry", "sharing", "--enable"]),
        (["sharing", "--enable", "--dry-run"], ["sharing", "--dry", "--enable"]),
        (["--dry-run"], ["--dry"]),
    )
    for full, short in pairs:
        assert setup.main(full) == 0, full
        full_payload = json.loads(capsys.readouterr().out)
        assert setup.main(short) == 0, short
        short_payload = json.loads(capsys.readouterr().out)
        assert short_payload == full_payload
        assert short_payload["status"] == "dry-run"
        assert short_payload["bootstrap"] is False
        if "sharing" in short:
            assert short_payload["cmd"] == "sharing"
            assert short_payload["install_workbuddy_plugin"] is False
        elif short_payload["cmd"] is None:
            assert short_payload["install_workbuddy_plugin"] is False
        else:
            assert short_payload["cmd"] in {"init", "install-wb"}
            assert short_payload["install_workbuddy_plugin"] is ("--install-wb" in short or "install-wb" in short)

    for argv in (["sharing", "--enable", "--d"], ["init", "--install-wb", "--not-a-flag"]):
        with pytest.raises(SystemExit) as exc:
            setup.main(argv)
        assert exc.value.code == 2, argv
        capsys.readouterr()

    assert calls == []
    assert {str(root): _listing(root) for root in roots} == before


def test_install_wb_flag_is_the_only_mount_path(monkeypatch, capsys):
    import setup

    calls: list[str] = []
    monkeypatch.setenv("_MINDIE_WB_SETUP_BOOTSTRAPPED", "1")
    monkeypatch.setattr(setup, "apply_enterprise_transport_env", lambda target=None: {} if target is None else target)
    monkeypatch.setattr(
        setup,
        "ensure_default_profile",
        lambda **_kwargs: calls.append("profile") or {"harness": "workbuddy"},
    )
    monkeypatch.setattr(
        setup,
        "install_workbuddy_plugin",
        lambda **_kwargs: calls.append("install") or {"status": "installed", "enabled": True},
    )

    assert setup.main(["init"]) == 0
    assert json.loads(capsys.readouterr().out) == {"harness": "workbuddy"}
    assert calls == ["profile"]

    calls.clear()
    assert setup.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"harness": "workbuddy"}
    assert calls == ["profile"]

    calls.clear()
    assert setup.main(["init", "--install-wb"]) == 0
    mounted = json.loads(capsys.readouterr().out)
    assert mounted["workbuddy_install"]["status"] == "installed"
    assert calls == ["profile", "install"]

    calls.clear()
    assert setup.main(["install-wb"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "installed"
    assert calls == ["profile", "install"]


def test_missing_runtime_reexec_is_skipped_for_help(monkeypatch, capsys):
    import sys
    import setup

    seen: dict[str, object] = {}
    monkeypatch.delenv("_MINDIE_WB_SETUP_BOOTSTRAPPED", raising=False)
    monkeypatch.setattr(setup.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(setup, "apply_enterprise_transport_env", lambda target=None: {} if target is None else target)
    monkeypatch.setattr(setup, "stage_source_baseline", lambda *_a, **_k: seen.setdefault("calls", []).append("stage") or Path("."))
    monkeypatch.setattr(
        setup,
        "ensure_runtime_python",
        lambda *_a, **_k: seen.setdefault("calls", []).append("python") or sys.executable,
    )

    def _run(argv, **_kwargs):
        seen["argv"] = list(argv)
        return type("Proc", (), {"returncode": 7})()

    monkeypatch.setattr(setup.subprocess, "run", _run)
    assert setup.main(["init", "--install-wb"]) == 7
    assert seen["calls"] == ["stage", "python"]
    assert seen["argv"][-2:] == ["init", "--install-wb"]

    seen.clear()
    with pytest.raises(SystemExit) as exc:
        setup.main(["--help"])
    assert exc.value.code == 0
    assert seen == {}
    assert "--dry-run" in capsys.readouterr().out


def test_onboarding_references_target_workbuddy():
    refs = PLUGIN_ROOT / "skills" / "mindie-agent" / "references"
    workspace = (refs / "workspace-setup.md").read_text(encoding="utf-8")
    notice = (refs / "user-notice.md").read_text(encoding="utf-8")
    auth = (refs / "github-authentication.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    english = (REPO_ROOT / "README.en.md").read_text(encoding="utf-8")
    assert "WorkBuddy" in workspace and "codebuddy" in workspace
    assert "install_workbuddy_plugin" in workspace
    assert "AskUserQuestion" in workspace
    assert "codex app" not in workspace.lower()
    assert "wb.json" in notice and "mindie-consent.json" in notice
    assert "gh auth login" in auth
    assert "gh auth setup-git" not in readme and "gh auth setup-git" not in english
    assert "credential.https://github.com.helper=!gh auth git-credential" in auth
    assert "GCM_INTERACTIVE=never" in auth
    assert "GIT_CONFIG_COUNT" in auth
    assert "manager" in auth
    for text in (readme, english):
        assert "init --install-wb" in text
        assert "AskUserQuestion" in text
        assert "sharing --status" in text
        assert "user-notice.md" in text
        assert "github-authentication.md" in text
        assert "workspace-setup.md" in text
        assert "mindie-agent/mindie-agent" in text


def test_git_config_count_composition_and_sharing_contract_safety(tmp_path: Path, monkeypatch, capsys):
    import paths
    import setup

    pre_env = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "http.proxy",
        "GIT_CONFIG_VALUE_0": "http://proxy.internal:8080",
        "GIT_CONFIG_KEY_1": "core.longpaths",
        "GIT_CONFIG_VALUE_1": "false",
    }
    composed = paths.apply_enterprise_transport_env(pre_env)
    assert composed["GIT_CONFIG_COUNT"] == "4"
    assert composed["GIT_CONFIG_KEY_0"] == "http.proxy"
    assert composed["GIT_CONFIG_VALUE_0"] == "http://proxy.internal:8080"
    assert composed["GIT_CONFIG_KEY_1"] == "core.longpaths"
    assert composed["GIT_CONFIG_VALUE_1"] == "false"
    assert composed["GIT_CONFIG_KEY_2"] == "core.autocrlf"
    assert composed["GIT_CONFIG_VALUE_2"] == "false"
    assert composed["GIT_CONFIG_KEY_3"] == "credential.https://github.com.provider"
    assert composed["GIT_CONFIG_VALUE_3"] == "github"

    cfg = Path(os.environ["MINDIE_AGENT_CONFIG"])
    consent_file = cfg.parent / "mindie-consent.json"

    for bad_argv in (["sharing"], ["sharing", "--enable", "--disable"], ["sharing", "--status", "--enable"]):
        with pytest.raises(SystemExit) as exc:
            setup.main(bad_argv)
        assert exc.value.code == 2
        capsys.readouterr()
    assert not consent_file.exists()

    assert setup.main(["sharing", "--status"]) == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["schema"] == "mindie-sharing-status/1"
    assert status_out["harness"] == "workbuddy"
    assert status_out["consent_choice"] is None
    assert status_out["sharing_enabled"] is False
    assert status_out["needs_onboarding_prompt"] is True
    assert not consent_file.exists()

    with pytest.raises(ValueError, match="invalid repository format"):
        setup.configure_community(enabled=True, repository="invalid-repo", project_root=tmp_path)
    assert not consent_file.exists()

    with pytest.raises(ValueError, match="visibility='public'"):
        setup.configure_community(enabled=True, visibility="private", project_root=tmp_path)
    assert not consent_file.exists()


def test_temp_checkout_paths_rebind_to_wb_source(tmp_path: Path, monkeypatch):
    import shutil
    import setup

    temp_checkout = tmp_path / "mindie-agent-wb-install"
    temp_plugin = temp_checkout / "plugins" / "mindie-agent"
    shutil.copytree(PLUGIN_ROOT, temp_plugin)
    for name in ("pyproject.toml", "product-contract.json", "harness-compatibility.json"):
        shutil.copy2(REPO_ROOT / name, temp_checkout / name)

    monkeypatch.setenv("WB_PLUGIN_ROOT", str(temp_plugin))
    monkeypatch.setattr(setup, "__file__", str(temp_plugin / "scripts" / "setup.py"))

    cfg_file = tmp_path / "config" / "wb.json"
    wb_dir = tmp_path / "workbuddy"
    prof = setup.ensure_default_profile(config=cfg_file, workspace_root=tmp_path)
    inst = setup.install_workbuddy_plugin(wb_dir=wb_dir, config=cfg_file, source_root=prof.get("source_root"))
    eng_data = json.loads((tmp_path / "config" / "wb.engine.json").read_text(encoding="utf-8"))
    known_mp = json.loads(Path(inst["known_marketplaces"]).read_text(encoding="utf-8"))

    assert "mindie-agent-wb-install" not in prof["runtime_scripts"]
    assert "mindie-agent-wb-install" not in prof["source_root"]
    assert "mindie-agent-wb-install" not in eng_data["transcript_adapter"]
    assert all("mindie-agent-wb-install" not in part for part in eng_data["summary_command"])
    mp_entry = known_mp["mindie-agent-marketplace"]
    assert "mindie-agent-wb-install" not in mp_entry["installLocation"]
    assert "mindie-agent-wb-install" not in mp_entry["source"]["path"]


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

