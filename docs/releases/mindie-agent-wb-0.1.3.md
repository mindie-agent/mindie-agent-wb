# MindIE Agent for Tencent WorkBuddy `v0.1.3` 发布说明

<b>中文</b> | <a href="mindie-agent-wb-0.1.3.en.md">English</a>

`v0.1.3` 将 `mindie-agent-wb` 升级至 `mindie-knowledge 0.13.11` 与 `mindie-sim 0.9.3`，统一单状态源 RSI 闭环引擎（`rsi_loop` / `experiment_ledger`），并将分级知识检索默认 Token 预算提升至 `explore 24000` / `query 8000` / `explain 16000`（上限 `64000`）。

## 更新亮点（人话版）

1. **统一单状态源 RSI 闭环引擎与实验账本（`mindie-knowledge 0.13.11`）**：
   - 在 `knowledge_mcp.py` 与 `skills/mindie-agent` 中接入统一 `ExperimentLedger` 单状态源，支持 `rsi_loop`（`orient` / `propose` / `verdict`）与 `experiment_ledger` MCP 工具，并在 `knowledge_explore` 响应中直接返回 `active_rsi_session`。
2. **分级知识检索默认预算提升**：
   - 将 `knowledge_explore` 默认 `token_budget` 提升至 `24000`、`knowledge_query` 提升至 `8000`、`knowledge_explain` 提升至 `16000`（上限 `64000`）。
3. **共享组件同步升级**：
   - `mindie-knowledge 0.13.11`（`1392a5c326605225fc6d89334842ae7700c0dce8`）与 `knowledge-vllm-ascend`（`a0f890b70e73919ffe5d917febcb2595793422ef`，297 个知识单元）
   - `remote-dev 0.11.4`（`7c67876d716feb3949e4dbf33eb5e06980ff0d36`）
   - `mindie-diagnostics 0.5.5`（`f2f2f946c172afc99342a84c0c1ed244f6d44e01`）
   - `mindie-debugger 0.1.0`（`e9406b2b178335190d5cb59dff4d6985894497ba`）
   - `mindie-profiler 0.5.6`（`79d97e75bff7351b0472b93c03b072c1cf47f5e7`）
   - `mindie-sim 0.9.3`（`dbadff04b60937a0722b0a227cdad01beed083cd`）
   - `mindie-coordinator 0.7.3`（`79a8b28eef72d57f965a01c6660849f024fbb1c0`）
   - `npu-top 0.3.1`（`8f0cb3ec66393812e90e118cd73704070c6027a2`）
