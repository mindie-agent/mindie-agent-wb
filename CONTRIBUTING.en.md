# Contributing to `mindie-agent-wb`

For the Chinese version, see [CONTRIBUTING.md](CONTRIBUTING.md).

`mindie-agent-wb` is the Tencent WorkBuddy Connector and CodeBuddy CLI native host plugin repository (`host-plugin` profile) in the MindIE Agent organization.

## 1. Workspace Layout

Develop against a flat sibling checkout or an isolated `.worktrees/` checkout:

```text
code/
  mindie-agent/           # Hub architecture docs, channel manifests, and org gates
  harness/                # Shared mindie-harness substrate
  mindie-agent-wb/        # WorkBuddy / CodeBuddy host plugin
  .worktrees/             # Isolated feature/refactoring worktrees
```

## 2. Non-Negotiable Host-Plugin Contracts

1. **Connector + Native CLI Dual Surfaces**: Preserve both `plugin.json` + `mcp.json` (WorkBuddy Connector) and `.codebuddy-plugin` + `.mcp.json` + `hooks/hooks.json` (CodeBuddy CLI plugin), including the `wb` remote-dev profile.
2. **Silent Default-On Knowledge & Consent-Gated `Stop` (`P2.7` / `P2.8`)**: Keep all three servers at `alwaysLoad: true, defer_loading: false` in `.mcp.json` exposing the 5 `knowledge_*` tools on startup, and check community sharing consent + `project_roots` before opening `admission.sqlite3` so passive `Stop` returns `{"suppressOutput": true}` in `< 100ms`.
3. **Zero Pre-Call Binding & Shared Remote State (`P2.7` / `AD-02`)**: `mindie-remote-dev`, `mindie-tools`, and `mindie-knowledge` must work without `_meta` or `PreToolUse` binds, and `REMOTE_DEV_STATE_DIR` must remain shared at `state_dir()/remote` across parent sessions and subagents.
4. **Windows Zero-Popup & Unix LF**: Every Windows subprocess call must include `CREATE_NO_WINDOW`, and all tracked text files must use Unix `LF` (`\n`).

## 3. Test Isolation & Pre-PR Gate

Tests must isolate `HOME`, `USERPROFILE`, `MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`, `WORKBUDDY_CONFIG_DIR`, and `UV_CACHE_DIR`.

Before opening or updating a PR, complete the `host-plugin` checklist (`HOST-NATIVE`, `HOST-WIRE`, `HOST-ADMISSION`, `HOST-UPGRADE`) in [`.github/pull_request_template.md`](.github/pull_request_template.md) and run:

```bash
python3 .github/scripts/check_lf_eol.py
python3 .github/scripts/check_pr_checklist.py --profile host-plugin --body-file <pr_body.md>
pytest -q -m "not live_npu"
```
