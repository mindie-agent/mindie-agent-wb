# mindie-agent-wb 0.1.20：冷启动 `--managed` 重入修复、清除公开读取租约与三平台原生协议门禁

[English](mindie-agent-wb-0.1.20.en.md)

- **清除本地知识影子回退与公开读取建库缺陷（`HAR-STATE`）**：删除 `scripts/bridge.py` 中的 `knowledge_transport`（此前在公开 `knowledge_query`/`explain` 读取时违规调用 `admission.activate` 创建 `admission.sqlite3`）及 `scripts/mcp_server.py` 旁路覆盖（精简至 `45` 行），统一交由 `mindie-harness v0.1.8` 零建库只读分发。
- **冷启动静默自举重入与稳定启动器清单**：修复 `mindie_launch.py` 冷启动路径（静默完成 `seed.pyz` 自举后重新进入本插件原生 `WBManagedAdapter` 与 `Stop` 处理器，确保冷启动完整暴露 15 项 `remote`、11 项 `tools` 与 5 项 `knowledge` 工具）；移除 `.mcp.json` / `mcp.json` / `hooks.json` 中的裸 `"python"` 调用，并停止 `wb_install.py` 将瞬态运行代解释器写死进缓存清单。
- **9 域引擎绑定与三平台原生协议门禁**：单锁接入 `ensure_knowledge_binding` 与 `run_stop_capture`（无原生 `turn_id` 时使用 `identity_kind="notification"`），更新 `mindie-harness-seed.pyz` 与 9 大共享组件锁，并接入 `verify-host-plugin.yml` 三平台验收。
