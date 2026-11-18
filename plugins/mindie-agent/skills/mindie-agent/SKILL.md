---
name: mindie-agent
description: "Ascend NPU, vLLM-Ascend, xLLM, MindIE-SD, and AscendC/Triton engineering workflow: L1-L4 graded knowledge retrieval/distillation, remote NPU development, profiling, Roofline simulation, CANN fault triage, and cluster lease coordination."
---

# MindIE Agent Runtime Workflow (Tencent WorkBuddy)

## 1. Default-Enabled MCP Tooling & Task Continuity

- **Zero-gate default availability**: While the plugin is enabled in WorkBuddy, `mindie-remote-dev` (`remote_*` + WorkBuddy native mirrors), `mindie-tools` (`mindie_status`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, 6 observation tools, `diagnostics_status`, `diagnostics_bundle`), and `mindie-knowledge` (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) work over standard MCP without prior activation, mandatory `PreToolUse` binds, or `context_file`.
- **Keep the user's task moving**: Knowledge, diagnostics, and local ledgers are optional helpers. Report any helper failure once and continue with deterministic MCP or authorized host-native tools; reconcile any `unknown` remote write before repeating it.

## 2. Code-First Investigation, Knowledge & NPU Discipline

- **Inspect code first**: Read target source files and callers before querying knowledge; proceed directly when the codebase already explains the issue.
- **Two-step `L1–L4` retrieval**:
  1. `knowledge_query`: pass concise technical terms, optional `domain`, and observed `conditions` (`token_budget` up to `64000`). Treat `condition_unknown` as unproven—probe missing facts before applying the unit.
  2. `knowledge_explain`: batch-read selected `mindie://<domain>/units/<id>@<rev>` `refs=[...]` with observed `conditions` and visible `known_refs=[...]`.
- **NPU leases & de-biased exploration**: Acquire or verify a `coordinator` lease before running workloads on physical NPUs (respecting A3 dual-Die boundaries), inspect live `coordinator` `execution_id` and `remote-dev` `job_id` handles before resubmitting, pass explicit `--time-window` / `--step-window` / `--full-range` and `--clock-domain` to `profiler`, and use `mode="explore"` plus leave-one-out ablations when repeated `L3` attempts stall.

## 3. Progressive References (Load Only When Needed)

- Detailed schemas, profiler flags & NPU lease discipline: [references/domain-skills.md](references/domain-skills.md)
- Default session lifecycle & opt-in `Stop` capture: [references/activation-lifecycle.md](references/activation-lifecycle.md)
