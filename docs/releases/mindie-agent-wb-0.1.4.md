# MindIE Agent for WorkBuddy `v0.1.4` 发布说明

<b>中文</b> | <a href="mindie-agent-wb-0.1.4.en.md">English</a> · <a href="../README.md">返回文档索引</a>

`v0.1.4` 为 `mindie-agent-wb` 全面补齐 Windows 零控制台弹窗防护（`subprocess.CREATE_NO_WINDOW`），并将共享组件升级至 `mindie-knowledge 0.13.12`、`mindie-diagnostics 0.5.6`、`remote-dev 0.11.5` 与 `mindie-coordinator 0.7.4`。

## 更新亮点（人话版）

1. **Windows 后台子进程零控制台弹窗防护**：
   - 在 `plugins/mindie-agent/scripts/bounded.py` 与 `mcp_bridge.py` 中统一注入 `subprocess.CREATE_NO_WINDOW`，彻底消除后台自动更新与组件检查在 Windows 上闪现 `conhost.exe` 窗口的问题。
2. **共享组件同步升级**：
   - `mindie-knowledge 0.13.12`（`1840bd4e238e718676768489058917818d18cd3c`）
   - `remote-dev 0.11.5`（`131b24533479e9cefc572144adccdcb345f70846`）
   - `mindie-diagnostics 0.5.6`（`9b331956b00737dd6d8c956da3416316f2de1ddf`）
   - `mindie-debugger 0.1.0`（`e9406b2b178335190d5cb59dff4d6985894497ba`）
   - `mindie-profiler 0.5.6`（`79d97e75bff7351b0472b93c03b072c1cf47f5e7`）
   - `mindie-sim 0.9.3`（`dbadff04b60937a0722b0a227cdad01beed083cd`）
   - `mindie-coordinator 0.7.4`（`14d7cec7247bdcbea9b9878db4770e3a05a6ea41`）
   - `npu-top 0.3.1`（`8f0cb3ec66393812e90e118cd73704070c6027a2`）
