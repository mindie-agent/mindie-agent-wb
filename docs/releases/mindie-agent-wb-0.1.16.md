# mindie-agent-wb 0.1.16：接入 `mindie-harness` 共享底座、双入口并发 MCP 收敛与 `0.1.13` 组件基线对齐

[English](mindie-agent-wb-0.1.16.en.md)

- **接入 `mindie-harness v0.1.0` 共享底座（WorkBuddy Connector + CodeBuddy CLI 双入口一致）**：打包并流式校验 `plugins/mindie-agent/assets/mindie-harness-seed.pyz` + `seed-lock.json`，将并发双帧 stdio MCP 路由、安全符号链接挂载（`AD-07`）、无窗子进程监管与单状态源 RSI 闭环统一收敛至 `mindie_harness`。
- **严守确定性零绑定直通与手动知识准入（`P2.7` & `P2.1`）**：彻底清理隐式 `REMOTE_DEV_SESSION_ID` 导入残留，确保 `remote` 与 `tools` 表面在零前置 Hook 下直接可用，而 `knowledge_*` 严格受 `/mindie-agent` 显式激活门控。
- **`0.1.13` 全量组件正式版锁定**：在 `pyproject.toml`、`uv.lock` 与 `product-contract.json` 中同步锁定 `mindie-harness v0.1.0`、`mindie-knowledge v0.13.23`、`remote-dev v0.11.13`、`mindie-diagnostics v0.5.16`、`mindie-debugger v0.1.4`、`mindie-profiler v0.5.16`、`mindie-sim v0.9.18`、`mindie-coordinator v0.7.13` 与 `npu-top v0.3.5`，并启用 `--profile host-plugin` PR 门禁。
