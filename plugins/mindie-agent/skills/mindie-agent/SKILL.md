---
name: mindie-agent
description: Manually enable MindIE L1-L4 graded knowledge, domain skills, feedback, distillation, and background capture for the current Tencent WorkBuddy conversation through /mindie-agent or $mindie-agent. All 34 deterministic remote-dev and component MCP tools remain available without activation.
---

# MindIE Agent (Tencent WorkBuddy Unified Entry)

**Keep the user's task moving.** MindIE knowledge, setup, onboarding, diagnostics and local ledgers support the task. If one is unavailable, report that specific failure and continue independent work or use equivalent authorized host-native tools. Defer optional setup and sharing questions when they would interrupt the task. Retain existing job IDs and confirmed results. Reconcile an uncertain remote write before repeating that operation; do not bypass identity, permissions or actual device/port ownership. Coordinator-managed work keeps its leases; an existing verified host reservation may support equivalent native work. If ownership cannot be established, pause only that device action.

**Task binding and resource checks have separate scopes.** Coordinator `hosts`, `probe`, `container` and `lease` work without a managed task attachment; omit `context_file` for independent environment operations. Missing MindIE attachment or knowledge activation must not stop authorized SSH inspection, container/source/model discovery, CPU-only tests, offline profiler analysis or simulation. Native call identity still applies to plugin tools. Before starting device work, establish actual device/port ownership and obtain a lease or verify an existing reservation. If that cannot be established, pause that device action and continue independent work.

**Binding recovery and updates.** Knowledge activation, native call identity, managed task attachment, and device ownership have separate authorities. Repeating `/mindie-agent` or `$mindie-agent` does not repair a missing managed task attachment or native transport identity. Follow the structured failure and continue independent authorized work. The resident MCP facade publishes catalog changes for the selected runtime; activation commands are not a catalog-refresh mechanism. An already loaded legacy facade or a host without notification support follows its native reload boundary, with refresh state reported separately from installation success.

Use this skill after the user invokes `/mindie-agent` or `$mindie-agent` in the current WorkBuddy conversation.

1. **Manual Session Activation**:
   - The native WorkBuddy `UserPromptSubmit` / `UserPromptExpansion` hook detects `/mindie-agent` in the user prompt and activates the session lease (`entry_enabled = 1`) in `admission.sqlite3` for the current `session_id`, injecting the bound `WB_CONVERSATION_ID` into context.
   - You can also initialize or inspect the session lease directly via MCP tool `mcp__mindie-knowledge__mindie_entry` (`{"op": "init"}` or `{"op": "status"}`).
2. **Complete MCP Tool Surfaces (40 Tools Across 3 Servers)**:
   - **`mindie-remote-dev` (20 tools, always on)**:
     `remote_auth`, `remote_read`, `remote_write`, `remote_edit`, `remote_multi_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_ls`, `remote_apply_patch`, `remote_job_status`, `remote_job_tail`, `remote_job_output`, `remote_job_stop`, `remote_job_stdin`, `remote_artifact_manifest`, `remote_artifact_pull`, `remote_artifact_push`, `remote_context_snapshot`, `remote_probe`.
   - **`mindie-tools` (14 tools, always on)**:
     `mindie_status`, `profiler` (`mindie-profiler`), `sim` (`mindie-sim`), `coordinator` (`mindie-coordinator`), `debugger` (`mindie-debugger`: `triage`, `decode-code`, `precision`, `aclgraph`, `remote-guide`), `npu_top` (`npu-top` CLI), `npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `npu_telemetry_window`, `list_npu_servers`, `diagnostics_status`, `diagnostics_bundle`.
   - **`mindie-knowledge` (6 tools, session-gated by `/mindie-agent`)**:
     `mindie_entry`, `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync` (also callable via `knowledge_cli.py`).
3. **Two-Step Progressive Knowledge Loading & De-Biased Exploration (`mode="explore"`)**:
   - **Step 1 (`knowledge_query`)**: Query with observed `conditions` (`matched` | `mismatched` | `condition_unknown`; never treat `condition_unknown` as a match) and `token_budget` (`1..64000`, default `8000` in reuse/diagnose and `24000` in `mode="explore"`).
   - **Step 2 (`knowledge_explain`)**: Batch-read selected `refs=[...]` with `known_refs=[...]` and `token_budget=16000` (`1..64000`, default `16000` in reuse/diagnose and `24000` in `mode="explore"`).
   - **De-Biased Exploration**: When 3 consecutive attempts stagnate, or when a baseline probe / prior attempt log shows standard `L3` heuristics stall due to a structural bottleneck, call `knowledge_query(..., mode="explore", conditions={...}, token_budget=24000, native_subcontext=True, ledger_entries=[...], bottleneck_summary={...}, goal=...)` and spawn an isolated `Agent` subagent seeded only with `exploration_context.subagent_fact_pack.prompt_brief` (`L1` constraints + `L2` bottleneck evidence + `L4` mutators + `conditional_avoid`, with `L3` workflows stripped). When implementing a multi-factor `L4` mutator, expose optional keyword-only boolean ablation flags (defaulting to the full optimized candidate) directly on the primary operator function returning `(out_tensor, metrics_dict)`, and invoke that primary operator function directly across `full_candidate` and each leave-one-out arm so each arm executes a distinct tensor-level computation path. Use `<plugin_root>/scripts/experiment_ledger.py` and `<plugin_root>/scripts/rsi_loop.py` to record 4-gate RSI outcomes in `experiment-ledger.jsonl` and `PROGRESS.md`.

## Progressive Context Disclosure (Read Only on Demand)

- **Detailed tool schemas (`knowledge_*`, `remote_*`, `debugger`, `npu_top`), dual-layer ledger (`PROGRESS.md` + `experiment-ledger.jsonl`), and NPU timing / profiler discipline**: read [references/domain-skills.md](references/domain-skills.md).
- **Session lifecycle, `/mindie-agent deactivate`, and `Stop` capture rules**: read [references/activation-lifecycle.md](references/activation-lifecycle.md).
- **Install, workspace onboarding (`AskUserQuestion` popup), contribution notice, and GitHub sign-in**:
  - Read [references/workspace-setup.md](references/workspace-setup.md), [references/user-notice.md](references/user-notice.md), and [references/github-authentication.md](references/github-authentication.md).
  - Mount the WorkBuddy Connector and native plugin with `uv run --no-project --python 3.13 python "<plugin_root>/scripts/setup.py" init --install-wb` or `install-wb` (`install_workbuddy_plugin`).
  - Right after installation or when `uv run --no-project --python 3.13 python "<plugin_root>/scripts/setup.py" sharing --status` reports `needs_onboarding_prompt: true`, call WorkBuddy's native **`AskUserQuestion`** tool in the user's language (`multiSelect: false`, first option prefixed with `(Recommended)`) to ask:
    1. Local Development Workspace `vllm-ascend-workspace` (`header: "开发工作区"` / `"Workspace"`)
    2. Community Knowledge Sharing & Automated Fault Reporting (`header: "社区共享"` / `"Sharing"`)
    3. `顺手给 MindIE Agent 点个 GitHub Star 吗？ ⭐` / `Would you like to give mindie-agent a Star on GitHub ⭐?` (`header: "GitHub Star"`, `(Recommended) 好，点个 Star ⭐` / `(Recommended) Sure, star it ⭐` vs `先不用` / `Maybe later`)
  - Resolve `WORKSPACE_ROOT` first, then run `uv run --no-project --python 3.13 python "<plugin_root>/scripts/setup.py" sharing --enable --project-root "$WORKSPACE_ROOT"` (or `--disable`), and star `mindie-agent/mindie-agent` only after an explicit yes.
