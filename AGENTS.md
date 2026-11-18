# MindIE Agent — Tencent WorkBuddy & CodeBuddy Host Plugin (`mindie-agent-wb`)

`mindie-agent-wb` is a **`host-plugin`** repository implementing both the Tencent WorkBuddy Connector (`plugin.json`, `mcp.json`, `ai.workbuddy`) and the CodeBuddy native CLI plugin (`.codebuddy-plugin/plugin.json`, `.workbuddy-plugin/plugin.json`, `.claude-plugin/plugin.json`, `.mcp.json`, `hooks/hooks.json`), backed by `mindie-harness` (`v0.1.3`) and shared MindIE runtime components.

## 1. Core Architectural & Contract Rules

- **Six-Script Thin Plugin (`v0.1.19`)**: `plugins/mindie-agent/scripts/` contains only `mindie_launch.py`, `bridge.py`, `mcp_server.py`, `wb_transcript.py`, `wb_install.py`, and `setup.py` (`<= 1,200` total lines; target `850–1,020`). Legacy duplicated runtime modules (`host_adapter.py`, `wb_native_compat.py`, `knowledge_cli.py`, `admission.py`, `identity.py`, `paths.py`, etc.) are deleted because `remote-dev v0.11.14` natively serves the 15-tool `wb` profile.
- **Converged `wb` Remote Profile**: `mindie-remote-dev` exposes exactly 15 tools: `remote_Read`, `remote_Write`, `remote_Edit`, `remote_MultiEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_NotebookEdit`, `remote_TaskStop`, `remote_ViewImage`, plus `remote_auth`, `remote_probe`, `remote_artifact_pull`, and `remote_artifact_push`. `remote_BashOutput` and `remote_KillShell` are absent from this profile.
- **Disjoint MCP Surfaces**: `.mcp.json` mounts `mindie-remote-dev` (`remote`), `mindie-tools` (`tools`), and `mindie-knowledge` (`knowledge`) with `"alwaysLoad": true, "defer_loading": false`; `tools` exposes deterministic components plus `mindie_doctor` and zero `knowledge_*` duplicates; `knowledge` exposes the 5 `knowledge_*` tools; `remote` preserves `REMOTE_DEV_HARNESS_PROFILE=wb`.
- **Silent Default-On & Zero Binding (`P2.7`, `P2.8`, `AD-02`)**: Standard MCP `tools/call` works with zero `_meta`, zero `PreToolUse` binds, and zero `WB_CONVERSATION_ID`. `mindie_launch.py` fast-paths `UserPromptSubmit` / `PreToolUse` / `SessionStart` / unconfigured `Stop` in pure stdlib before third-party imports, with zero SQLite creation.
- **Consent-Gated `Stop`**: `Stop` checks `mindie-consent.json` (`choice in {"contribute", "local-only"}`), `mindie-community.json` (`enabled is True` plus non-empty absolute `project_roots`), and workspace scope before opening `admission.sqlite3`. It preserves explicit `inactive` revocation, activates a default lease (`not_before=0.0`), validates `wb_transcript.capture_source`, and calls `capture_hook(..., timeout=6.0)` only when enabled and in scope.
- **Resident Stdio & Seed Bootstrap**: `mcp_server.py` delegates to `mindie_harness` (`ManagedMCPAdapter`, `serve_mcp`, `rebind_native_plugin_if_needed`) with no legacy `_resolve_claim`; `mindie_launch.py` verifies `assets/mindie-harness-seed.pyz` in `64 KiB` chunks against `assets/seed-lock.json` (`mindie-harness-seed/1`) before cold-provisioning.
- **State & Receipt Honesty (Principles 9, 10, 14, 17)**: Keep every script `< 800` lines with `[Role]`, `[Invariants]`, `[Timeouts & Locks]`, and `[Side-Effect Policy]` in the top 30 lines. Always place `except TimeoutError: raise` before `except OSError:` / broad `except Exception:`. A successful primary remote/component/knowledge result keeps `isError=False` and attaches `receipt_warning` + `receipt_unconfirmed=True` when local receipt accounting fails.
- **Non-Blocking Task Continuity**: MindIE tools (`remote-dev`, `coordinator`, `knowledge`, `mindie_doctor`) are accelerators, NEVER roadblocks. Report a failure once (reconciling uncertain writes first) and continue immediately via native `ssh`/`scp`, local scripts, or other authorized host tools.

## 2. Plugin Script Map (`plugins/mindie-agent/scripts/`)

| Module | Role |
| :--- | :--- |
| `mindie_launch.py` | Pure-stdlib entrypoint for `hook`, `mcp`, `setup`, `update`, `doctor`, and `sharing`; `< 100ms` passive hook fast paths, `verify_seed_bundle()`, active-python re-exec, and cold-seed handoff. |
| `bridge.py` | `SessionStart`, silent `UserPromptSubmit` / `PreToolUse` pass-throughs, forged-identity denial, and consent-first `Stop` capture with explicit-revocation preservation. |
| `mcp_server.py` | Disjoint `remote` / `tools` / `knowledge` `ManagedMCPAdapter` surfaces and a direct `mindie_knowledge.loop.transport` fallback with Receipt Honesty. |
| `wb_transcript.py` | Read-only incremental WorkBuddy SessionStore JSONL projection (`FileIdentity`, `identify`, `capture_source`, `read_material`, `read_increment`). |
| `wb_install.py` | Self-contained WorkBuddy plugin/Connector installer with verified interpreter pinning, registry updates, and stale-version pruning. |
| `setup.py` | Read-only `--help` / `--dry-run`, `init`, `install-wb`, `sharing`, `doctor`, `update`, and AST-checked `release-metadata`. |

## 3. Local Verification

```bash
python3 .github/scripts/check_lf_eol.py
pytest -q -m "not live_npu"
wc -l plugins/mindie-agent/scripts/*.py   # must be <= 1,200 total
```
