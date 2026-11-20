# MindIE Agent for Tencent WorkBuddy (`mindie-agent-wb`)

<p>
  <a href="https://github.com/mindie-agent/mindie-agent-wb/releases"><img src="https://img.shields.io/badge/release-v0.1.20-0F766E.svg" alt="Release v0.1.20"></a>
</p>

Native plugin and UI Connector adapter for **Tencent WorkBuddy** (and its bundled `@genie/agent-cli` / `codebuddy` runtime): a converged 15-tool `wb` remote profile, deterministic component tools plus `mindie_doctor`, a default-enabled L1-L4 knowledge flywheel, and a six-script thin runtime.

## 1. Architecture at a Glance

- **Connector UI surface**: [`plugin.json`](plugins/mindie-agent/plugin.json) + [`mcp.json`](plugins/mindie-agent/mcp.json) with bilingual `extensions["ai.workbuddy"]` metadata.
- **Native plugin surface**: [`.codebuddy-plugin/plugin.json`](plugins/mindie-agent/.codebuddy-plugin/plugin.json) + [`.mcp.json`](plugins/mindie-agent/.mcp.json) + [`hooks/hooks.json`](plugins/mindie-agent/hooks/hooks.json) + [`SKILL.md`](plugins/mindie-agent/skills/mindie-agent/SKILL.md).
- **Six thin scripts (`v0.1.20`)**: `mindie_launch.py`, `bridge.py`, `mcp_server.py`, `wb_transcript.py`, `wb_install.py`, and `setup.py`. They delegate to `mindie-harness v0.1.7`; `remote-dev v0.11.15` natively serves the `wb` 15-tool profile, so no local compatibility layer is retained.

## 2. Tool Matrix (15 + 11 + 5)

| MCP Server | Surface | Count | Tools |
| :--- | :--- | :---: | :--- |
| `mindie-remote-dev` | `remote` (`wb` profile) | **15** | `remote_Read`, `remote_Write`, `remote_Edit`, `remote_MultiEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_NotebookEdit`, `remote_TaskStop`, `remote_ViewImage`, `remote_auth`, `remote_probe`, `remote_artifact_pull`, `remote_artifact_push` |
| `mindie-tools` | `tools` | **11** | `mindie_status`, `mindie_doctor`, `profiler`, `sim`, `coordinator`, `npu_top`, `debugger`, `rsi_loop`, `experiment_ledger`, `diagnostics_status`, `diagnostics_bundle` |
| `mindie-knowledge` | `knowledge` | **5** | `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync` |

`mindie-tools` contains zero `knowledge_*` duplicates; `mindie-remote-dev` contains no `remote_BashOutput` or `remote_KillShell`. All three surfaces support standard MCP `tools/call` with zero `_meta` and zero `PreToolUse` pre-binding.

## 3. Quick Install and Verification

Ask WorkBuddy or any terminal-capable agent:

```text
Install this for me: https://github.com/mindie-agent/mindie-agent-wb
```

Installer entry (`gh` is optional and never a blocker):

```bash
uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host wb
```

After installation the agent performs one three-question, `gh`-aware onboarding flow:

1. **Development workspace**: initialize `vllm-ascend-workspace` (recommended), keep the current directory, or reuse an existing directory; record the absolute `WORKSPACE_ROOT`.
2. **Community sharing (dynamic recommendation)**: run `gh auth status` / `gh api user --jq .login`. Recommend `contribute` when `gh` is logged in, otherwise `local-only`. The user may choose either tier.
   ```bash
   python plugins/mindie-agent/scripts/setup.py sharing --enable --project-root "$WORKSPACE_ROOT"
   python plugins/mindie-agent/scripts/setup.py sharing --local-only
   python plugins/mindie-agent/scripts/setup.py sharing --disable
   ```
3. **One-step Ascend NPU connection (terminal mode)**: bind host, port, user, and container with one `remote_auth` call:
   ```json
   {"host": "npu.example", "port": 22, "user": "root", "save_default": true, "discover": true}
   ```
   In interactive terminal mode, append `--password` at the end of this step so the password is entered at a hidden prompt instead of chat or logs. In non-terminal mode, use keys or an existing saved `remote_auth` default.

Then run the readiness check and verify the install:

```bash
python plugins/mindie-agent/scripts/setup.py doctor --probe-remote --cwd "$WORKSPACE_ROOT"
```

Inside WorkBuddy, call `mindie_doctor` directly with `{"probe_remote": true, "cwd": "..."}`. Report success only after confirming:

- `~/.config/mindie-agent/wb.json` exists with `harness` = `workbuddy`.
- `~/.workbuddy/plugins/installed_plugins.json` lists `mindie-agent@mindie-agent-marketplace`, and `settings.json` enables it.
- `mindie-remote-dev` lists exactly 15 tools; `mindie-tools` includes `mindie_doctor` and no `knowledge_*`; `mindie-knowledge` lists exactly 5 tools.
- `mindie_doctor` returns `status: "ok"` (remote probing is optional).

`--help`, `-h`, and `--dry-run` (including unique prefixes such as `--dry`) only print a plan and never write config:

```bash
python plugins/mindie-agent/scripts/setup.py init --install-wb --dry-run
```

## 4. Lifecycle and Task Continuity

- **Silent default-on**: `UserPromptSubmit`, `PreToolUse`, `SessionStart`, and unconfigured `Stop` complete in a pure-stdlib path and return `{"suppressOutput": true}` in `< 100ms` with zero SQLite creation.
- **Consent-first capture**: `Stop` activates the default lease and calls `capture_hook(..., timeout=6.0)` only when `mindie-consent.json` selects `contribute` / `local-only`, `mindie-community.json` is enabled, and the workspace is inside `project_roots`. Explicit `inactive` revocation always wins.
- **Non-Blocking Task Continuity Rule**: MindIE tools (`remote-dev`, `coordinator`, `knowledge`, `mindie_doctor`) are accelerators, NEVER roadblocks. If a tool fails, report it once (reconciling uncertain remote writes first), then continue immediately through native `ssh`/`scp`, local scripts, or other authorized host tools.

## 5. Docs and Thanks

- Workspace setup: [`docs/install/workspace-setup.md`](docs/install/workspace-setup.md)
- Optional GitHub sign-in: [`docs/install/github-authentication.md`](docs/install/github-authentication.md)
- User notice and data scope: [`docs/install/user-notice.md`](docs/install/user-notice.md)
- Release notes: [`docs/releases/mindie-agent-wb-0.1.20.en.md`](docs/releases/mindie-agent-wb-0.1.20.en.md)

If this project helps you, a one-line Star ⭐ is appreciated.
