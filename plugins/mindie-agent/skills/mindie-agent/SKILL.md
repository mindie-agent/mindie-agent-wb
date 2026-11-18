---
name: "mindie-agent"
description: "Ascend NPU, vLLM-Ascend, xLLM, MindIE-SD, and AscendC/Triton engineering workflow: default-on L1-L4 graded knowledge, a converged 15-tool WorkBuddy remote profile, deterministic component tools, mindie_doctor readiness checks, profiling, simulation, debugging, and cluster lease coordination."
user-invocable: false
---

# MindIE Agent Runtime Workflow (Tencent WorkBuddy)

## 1. Default-On Tooling & Non-Blocking Task Continuity

- **Silent default-on**: `mindie-remote-dev` (the 15-tool `wb` profile), `mindie-tools` (`mindie_status`, `mindie_doctor`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, `rsi_loop`, `experiment_ledger`, `diagnostics_status`, `diagnostics_bundle`), and `mindie-knowledge` (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) are available through standard MCP without onboarding, activation, `_meta`, or pre-call session binding.
- **Non-Blocking Task Continuity Rule**: MindIE tools (`remote-dev`, `coordinator`, `knowledge`, `mindie_doctor`) are accelerators, NEVER roadblocks. If any tool fails, report the failure once (reconciling uncertain remote writes first) and continue immediately through native `ssh`/`scp`, local scripts, or other authorized host tools. Never stall or abandon the user's task because an accelerator is unavailable.

## 2. Remote Development & Readiness

- **15-tool `wb` profile**: `remote_Read`, `remote_Write`, `remote_Edit`, `remote_MultiEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_NotebookEdit`, `remote_TaskStop`, `remote_ViewImage`, plus `remote_auth`, `remote_probe`, `remote_artifact_pull`, and `remote_artifact_push`. Bind endpoints with `remote_auth(..., save_default=True, discover=True)` and reuse the saved default.
- **Readiness before heavy work**: call `mindie_doctor` once to check the profile, active runtime, and optional remote reachability. Treat a failed check as guidance, not a gate.
- **NPU discipline**: acquire or verify a `coordinator` lease before physical-NPU workloads (respecting A3 dual-Die boundaries), and inspect live `execution_id` / `job_id` handles before resubmitting.

## 3. Knowledge & Investigation Discipline

- **Inspect code first**: read target source and callers before querying knowledge; proceed directly when the codebase explains the issue.
- **Two-step `L1-L4` retrieval**: `knowledge_query` with concise technical terms, optional `domain`, observed `conditions`, and `token_budget`; then `knowledge_explain` with selected `refs`, observed `conditions`, and visible `known_refs`. Treat `condition_unknown` as unproven.
- **Receipt honesty**: when a primary call succeeds but local receipt accounting fails, keep the successful result and do not repeat the call.

## 4. Sharing Controls

- `setup.py sharing --enable` opts into redacted community knowledge PRs and fault reporting.
- `setup.py sharing --local-only` keeps local transcript capture without reporting.
- `setup.py sharing --disable` stops new capture and reporting; `--status` is always read-only.

## 5. Progressive References (Load Only When Needed)

- Tool surfaces, `remote_auth` defaults, `mindie_doctor`, and sharing commands: [references/domain-skills.md](references/domain-skills.md)
- Default session lifecycle and consent-gated `Stop` capture: [references/activation-lifecycle.md](references/activation-lifecycle.md)
