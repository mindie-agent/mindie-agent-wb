---
name: mindie-agent
description: Manually enable MindIE L1-L4 graded knowledge, domain skills, feedback, distillation, and background capture for the current Tencent WorkBuddy conversation through /mindie-agent or $mindie-agent. All 34 deterministic remote-dev and component MCP tools remain available without activation.
---

# MindIE Agent (Tencent WorkBuddy Unified Entry)

## 1. Core Operating Stance

- **Keep the user's task moving**: Knowledge, onboarding, diagnostics, and local ledgers are optional helpers. If any helper fails, report that specific error once and continue with deterministic MCP tools or authorized host-native tools. Reconcile any `unknown` remote write before repeating it.
- **Zero-gate deterministic tools (34 tools always on)**: `mindie-remote-dev` (20 `remote_*` tools) and `mindie-tools` (14 tools: `mindie_status`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, 6 read-only `npu-top` observation tools, `diagnostics_status`, `diagnostics_bundle`) work in every conversation and subagent over standard MCP without `/mindie-agent`, mandatory `PreToolUse` binds, or `context_file`; `coordinator` runs are addressed directly by `execution_id`. Obtain a `coordinator` lease (or verify an existing reservation) before running workloads on physical NPUs, pausing only that device step if ownership cannot be confirmed.
- **Explicit `/mindie-agent` knowledge gate (6 tools)**: `UserPromptSubmit` / `UserPromptExpansion` (or `mcp__mindie-knowledge__mindie_entry` with `{"op": "init"}`) activates the session lease when the user invokes `/mindie-agent` or `$mindie-agent`. `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`, and `Stop` capture require an active lease in the current session.

## 2. Advisory Knowledge & Domain Tooling

- **Inspect code first**: Read the relevant code and callers before querying knowledge; proceed directly when the codebase already explains the issue.
- **Two-step retrieval (`L1–L4` graded units)**:
  1. `knowledge_query`: pass concise technical terms, optional `domain`, and observed `conditions` (`token_budget` default `8000`, up to `64000`). Treat `condition_unknown` as unproven—probe missing facts before applying the unit.
  2. `knowledge_explain`: batch-read selected `mindie://<domain>/units/<id>@<revision>` `refs=[...]` with observed `conditions`, visible `known_refs=[...]`, and `token_budget` (default `16000`).
- **Long-horizon runs & de-biased exploration**:
  - `coordinator` (`execution_id`) and `remote-dev` (`job_id`) persist authoritative run state—inspect existing handles before resubmitting after an interruption. Pass explicit `--time-window`, `--step-window`, or `--full-range` and `--clock-domain` to `mindie_profiler`.
  - For multi-round optimization sweeps, you may optionally track hypotheses via `<plugin_root>/scripts/experiment_ledger.py`. When repeated `L3` attempts stall on a structural bottleneck, query `L4` structural mutators and optionally spawn an isolated `Agent` subagent seeded with `exploration_context.subagent_fact_pack.prompt_brief`, verifying multi-factor changes with leave-one-out ablations on the operator's real interface.

## 3. Non-Intrusive First-Use Onboarding

- Defer optional onboarding when solving an active user problem; reuse saved choices (`contribute`, `disabled`, `read-only`, `later`) without prompting again.
- During initial setup (`setup.py init --install-wb`) or when `setup.py sharing --status` reports `needs_onboarding_prompt: true`, briefly reference [references/user-notice.md](references/user-notice.md) and ask via `AskUserQuestion` in the user's language:
  1. **Workspace**: Keep the current directory when already inside a repository checkout, or initialize/select `vllm-ascend-workspace` when starting fresh;
  2. **Community Sharing & Sanitized Fault Reporting**: Enable (`setup.py sharing --enable --project-root "$WORKSPACE_ROOT"`) vs. Keep Local-Only (`--disable`);
  3. **Optional GitHub Star**: Ask lightly whether to star `mindie-agent/mindie-agent` (only star on an explicit yes).

## 4. Progressive References (Load Only When Needed)

- Detailed schemas, profiler flags & optional ledger helpers: [references/domain-skills.md](references/domain-skills.md)
- Session lifecycle, `/mindie-agent deactivate` & `Stop` capture: [references/activation-lifecycle.md](references/activation-lifecycle.md)
- Workspace setup & GitHub sign-in: [references/workspace-setup.md](references/workspace-setup.md), [references/github-authentication.md](references/github-authentication.md)
