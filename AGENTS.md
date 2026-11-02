# MindIE Agent — Tencent WorkBuddy Adapter (`mindie-agent-wb`)

This repository contains the native Tencent WorkBuddy (`WorkBuddy.app` / `codebuddy`) plugin and UI Connector adapter for MindIE Agent.

## Core Invariants

- **Dual Native Plugin & Connector Compatibility**: `plugins/mindie-agent/plugin.json` and `mcp.json` conform strictly to `https://agent-plugins.org/schemas/1.0.0/*.schema.json` with `extensions["ai.workbuddy"]`, while `.mcp.json`, `hooks/hooks.json`, and `skills/mindie-agent/SKILL.md` wire the full Agent CLI lifecycle.
- **Complete 40-Tool MCP Exposure**: Exposes `mindie-remote-dev` (20 tools), `mindie-tools` (14 tools including `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, 6 direct `npu-top` observation tools, and `diagnostics`), and `mindie-knowledge` (6 tools).
- **State & Receipt Honesty**: Never accepts session identity from `params.arguments`; claims verified binds from `PreToolUse` or `params._meta["workbuddy.ai/conversationId"]`, and preserves `isError=False` with `receipt_warning` + `receipt_unconfirmed=True` if local SQLite accounting fails after a primary operation completes.
- **Zero Host Pollution in Tests**: All tests isolate `MINDIE_CONFIG_HOME`, `MINDIE_DATA_HOME`, `MINDIE_DIAGNOSTICS_ROOT`, `WORKBUDDY_CONFIG_DIR`, `CODEBUDDY_CONFIG_DIR`, and `HOME`.
