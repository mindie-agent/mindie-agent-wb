# MindIE Agent for Tencent WorkBuddy `v0.1.3` Release Notes

<a href="mindie-agent-wb-0.1.3.md">中文</a> | <b>English</b>

`v0.1.3` upgrades `mindie-agent-wb` to `mindie-knowledge 0.13.11` and `mindie-sim 0.9.3`, unifies the Recursive Self-Improvement (`rsi_loop` / `experiment_ledger`) state machine onto a single authoritative `ExperimentLedger`, and raises default graded retrieval token budgets (`explore` `24000`, `query` `8000`, `explain` `16000`, ceiling `64000`).

## Highlights (Human-Readable Summary)

1. **Single-Source RSI Loop & Experiment Ledger (`mindie-knowledge 0.13.11`)**:
   - Exposes `rsi_loop` (`orient` / `propose` / `verdict`) and `experiment_ledger` in `knowledge_mcp.py` backed by `ExperimentLedger`, surfacing `active_rsi_session` directly in `knowledge_explore`.
2. **Higher Graded Retrieval Token Budgets**:
   - Raises default `token_budget` to `24000` for `knowledge_explore`, `8000` for `knowledge_query`, and `16000` for `knowledge_explain` (ceiling `64000`).
3. **Upgraded Shared Component & Corpus Baseline**:
   - `mindie-knowledge 0.13.11` (`1392a5c326605225fc6d89334842ae7700c0dce8`) and `knowledge-vllm-ascend` (`a0f890b70e73919ffe5d917febcb2595793422ef`, 297 verified units)
   - `remote-dev 0.11.4` (`7c67876d716feb3949e4dbf33eb5e06980ff0d36`)
   - `mindie-diagnostics 0.5.5` (`f2f2f946c172afc99342a84c0c1ed244f6d44e01`)
   - `mindie-debugger 0.1.0` (`e9406b2b178335190d5cb59dff4d6985894497ba`)
   - `mindie-profiler 0.5.6` (`79d97e75bff7351b0472b93c03b072c1cf47f5e7`)
   - `mindie-sim 0.9.3` (`dbadff04b60937a0722b0a227cdad01beed083cd`)
   - `mindie-coordinator 0.7.3` (`79a8b28eef72d57f965a01c6660849f024fbb1c0`)
   - `npu-top 0.3.1` (`8f0cb3ec66393812e90e118cd73704070c6027a2`)
