# mindie-agent-wb 0.1.15

- **确定性表面收据防重放与原生会话转发（`P2.7`）**：在 `remote` 与 `tools` 表面解析 `PreToolUse` 绑定前先行原子占位一次性 RPC 收据，跨 `serve_selected` 传递 `MINDIE_MCP_CONNECTION_ID`，移除冲突的 `AGY_CONVERSATION_ID` 导出并向 `coordinator` 传递 `workbuddy.ai/conversationId` 原生标识。
- **文档与技能润色**：对齐 `SKILL.md` 与 `AGENTS.md` 至五大铁律。
