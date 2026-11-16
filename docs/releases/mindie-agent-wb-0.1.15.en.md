# mindie-agent-wb 0.1.15

- **One-shot RPC receipt reservation & native WB coordinator identity (`P2.7`)**: Reserves one-shot RPC receipts before claiming `PreToolUse` binds on `remote` and `tools` surfaces, propagates `MINDIE_MCP_CONNECTION_ID`, removes conflicting `AGY_CONVERSATION_ID` export, and forwards `workbuddy.ai/conversationId` to `coordinator`.
- **Skill and guide polish**: Aligns `SKILL.md` and `AGENTS.md` with the 5 Iron Rules.
