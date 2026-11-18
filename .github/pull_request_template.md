## 变更摘要 (Summary)

-

## 设计原则 Review (Pre-PR Checklist)

> 对照 `AGENTS.md` 与 `host-plugin` 仓契约完成核验（不适用项须填写 `not-applicable: <具体原因>`）：

- [ ] **HOST-NATIVE** — WorkBuddy Connector（`plugin.json` / `mcp.json`）与 CodeBuddy 原生插件（`.mcp.json` / `hooks/hooks.json`）双表面契约兼容，Windows 子进程零黑框弹窗（`CREATE_NO_WINDOW`）。
- [ ] **HOST-WIRE** — 常驻并发 stdio MCP（同时兼容 `Content-Length` 与 JSONL 帧、读写/控制快慢池隔离、零 `--once` 冷启动阻塞）及超时算术（`HOOK_TOTAL >= HOOK_EXEC_BUDGET + CLEANUP_GRACE`，未激活 `Stop` `< 100ms`）已验证。
- [ ] **HOST-ADMISSION** — 全量工具（含默认启用的 5 项 `knowledge_*`）满足零预绑定门禁（`P2.7`）且共享 `REMOTE_DEV_STATE_DIR`（`AD-02`），`Stop` 先校验社区共享授权与 `project_roots` 再按需创建会话租约，主操作成功后本地记账失败保持 `outcome="success"` + `receipt_warning` + `receipt_unconfirmed=True`（Principle 14）。
- [ ] **HOST-UPGRADE** — 种子包校验（`assets/seed-lock.json` 流式 SHA-256）、`wb.json` 原子切换、`.inuse` 运行时租约保护及历史版本平滑升级兼容性已验证，全仓文本保持 Unix `LF`（`\n`）且单模块 `< 800` 行。

## 自查证据 (Evidence)

- **HOST-NATIVE**:
- **HOST-WIRE**:
- **HOST-ADMISSION**:
- **HOST-UPGRADE**:

## 验证结果 (Verification)

-
