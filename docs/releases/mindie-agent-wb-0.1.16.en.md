# mindie-agent-wb 0.1.16: `mindie-harness` Shared Substrate, Dual-Surface Concurrent MCP, and `0.1.13` Component Baseline

[中文](mindie-agent-wb-0.1.16.md)

- **Shared `mindie-harness v0.1.0` Substrate Across Connector & CLI Surfaces**: Bundles streaming-verified `plugins/mindie-agent/assets/mindie-harness-seed.pyz` + `seed-lock.json` across both the WorkBuddy Connector and CodeBuddy native CLI plugin, delegating concurrent dual-framing MCP stdio dispatch, symlinked dev-root support (`AD-07`), windowless process supervision, and RSI helpers to `mindie_harness`.
- **Zero-Binding Deterministic Pass-Through & Manual Knowledge Admission (`P2.7` & `P2.1`)**: Scrubs import-time `REMOTE_DEV_SESSION_ID` leakage, guaranteeing hook-free `remote` / `tools` execution while keeping `knowledge_*` gated on explicit `/mindie-agent` session activation.
- **`0.1.13` Formal Component Baseline**: Pins `mindie-harness v0.1.0`, `mindie-knowledge v0.13.23`, `remote-dev v0.11.13`, `mindie-diagnostics v0.5.16`, `mindie-debugger v0.1.4`, `mindie-profiler v0.5.16`, `mindie-sim v0.9.18`, `mindie-coordinator v0.7.13`, and `npu-top v0.3.5` across `pyproject.toml`, `uv.lock`, and `product-contract.json`, aligned with `--profile host-plugin` governance.
