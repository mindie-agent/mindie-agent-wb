# mindie-agent-wb 0.1.18：静默认启用 `knowledge_*` 工具与零命令前缀体验

[English](mindie-agent-wb-0.1.18.en.md)

- **启动默认开放全部 5 项分级知识工具（零会话预绑定门禁）**：`.mcp.json` 中 `mindie-knowledge` 启用 `alwaysLoad: true, defer_loading: false` 并在 `tools/list` 直接暴露 `knowledge_query`、`knowledge_explain`、`knowledge_feedback`、`knowledge_distill`、`knowledge_sync`；缺少宿主会话元数据时自动回退连接级会话标识（CLI 回退 `"wb-cli-default"`）并按需初始化默认会话租约（`not_before=0.0`）。
- **静默语义 `SKILL.md` 与授权优先 `Stop` 采集**：移除 `UserPromptExpansion` Hook 与用户命令前缀，将 `UserPromptSubmit` / `PreToolUse` 简化为轻量级零 SQLite 透传；`Stop` 在访问 `admission.sqlite3` 前先校验社区共享授权与 `project_roots` 范围，关闭或越界时走 `< 100ms` 冷路径且零创建数据库，开启且在授权目录内时自动补齐租约并执行脱敏采集。
