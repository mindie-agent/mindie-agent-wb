# MindIE Agent for Tencent WorkBuddy `v0.1.2` 发布说明

<b>中文</b> | <a href="mindie-agent-wb-0.1.2.en.md">English</a>

`v0.1.2` 将 `mindie-agent-wb` 升级至最新昇腾 AI Infra 共享组件基线（`knowledge 0.13.10`、`remote-dev 0.11.4`、`diagnostics 0.5.5`、`debugger 0.1.0`、`profiler 0.5.6`、`sim 0.9.2`、`coordinator 0.7.3` 与 `npu-top 0.3.1`），支持 WorkBuddy / CodeBuddy `1.0.0..9.99.99` 前向兼容版本区间，并接入标准化的 GitHub Release 发布与 CPU 微基准门禁。

## 更新亮点（人话版）

1. **宿主版本前向兼容与远端 CANN 故障排查引导增强**：
   - `harness-compatibility.json` 支持 `1.0.0..9.99.99`（`include_prerelease: true`），WorkBuddy 桌面端或 CodeBuddy CLI 升级后无需重装插件即可继续使用全部 40 项 MCP 工具与 Hooks。
   - 优化 `mindie-tools` 中 `debugger` 工具的提示说明，明确引导通过 `remote-guide` + `remote-dev` 扫描远端昇腾容器内的 CANN `plog` 与 ACLGraph 报错。
2. **全量共享组件与知识语料基线升级（含昇腾 950PR / A5 仿真校准）**：
   - `mindie-knowledge 0.13.10`（`7936a4fda67f6808d8fa73824363a4d7345766e5`）与 `knowledge-vllm-ascend`（`a0f890b70e73919ffe5d917febcb2595793422ef`，297 个知识单元）
   - `remote-dev 0.11.4`（`7c67876d716feb3949e4dbf33eb5e06980ff0d36`）
   - `mindie-diagnostics 0.5.5`（`f2f2f946c172afc99342a84c0c1ed244f6d44e01`）
   - `mindie-debugger 0.1.0`（`e9406b2b178335190d5cb59dff4d6985894497ba`）
   - `mindie-profiler 0.5.6`（`79d97e75bff7351b0472b93c03b072c1cf47f5e7`）
   - `mindie-sim 0.9.2`（`ced2dba5741af2d98f3a75b2a9cad77b9b94c40d`，新增 Ascend 950PR / A5 硬件规格与超节点拓扑校准）
   - `mindie-coordinator 0.7.3`（`79a8b28eef72d57f965a01c6660849f024fbb1c0`）
   - `npu-top 0.3.1`（`8f0cb3ec66393812e90e118cd73704070c6027a2`）
3. **自动化发布流水线与微基准门禁**：
   - 新增 `.github/workflows/release.yml` 与 `benchmarks/release.json`，每次正式发版自动执行契约测试并生成 `SHA256SUMS` 与公开对话解析延迟微基准报告。
