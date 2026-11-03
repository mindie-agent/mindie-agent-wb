# Domain Tooling & NPU Discipline

## 1. Graded Knowledge Tools (`mindie-knowledge` MCP & `knowledge_cli.py`)

- `mindie_entry`: Manages session lease (`op="init"`, `op="status"`, `op="deactivate"`) and community sharing consent (`op="choose"`, `op="sharing-enable"`, `op="sharing-disable"`).
- `knowledge_query`: Searches graded `L1–L4` units and task blocks (`token_budget` `1..64000`, default `8000` in reuse/diagnose and `24000` in `mode="explore"`). Returns bounded `cards` and deduplicated `foundation_cards`.
- `knowledge_explain`: Reads exact `mindie://<domain>/units/<id>@<rev>` references and expands `requires` once (`token_budget` `1..64000`, default `16000` in reuse/diagnose and `24000` in `mode="explore"`).
- `knowledge_feedback`: Records `discovered`, `loaded`, `applied`, or `evaluated` (`supported` | `refuted` | `inconclusive`) usage events.
- `knowledge_distill`: Distills verified milestone (`task_state="verified_milestone"`) or completed (`task_state="completed"`) evidence into graded `L1–L4` units. `L4` mutators require a verified leave-one-out ablation receipt.
- `knowledge_sync`: Synchronizes configured public knowledge feeds.
- `experiment_ledger.py` & `rsi_loop.py`: Evaluate 4-stage RSI candidate verification gates (`fingerprint`, `gate`, `judge`, `append`, `render`) and manage resumable `profile -> consult -> propose -> measure -> learn` rounds over `experiment-ledger.jsonl` and `PROGRESS.md`.

## 2. Deterministic Component & Fleet Observation Tools (`mindie-tools` MCP)

- `profiler` (`mindie-profiler`): `inventory`, `analyze`, `correlate`, `compare`, `sql`, `camodel`, `kernels`.
- `sim` (`mindie-sim`): Roofline, operator, graph, topology/PD Pareto, and serving performance simulations.
- `coordinator` (`mindie-coordinator`): Acquire NPU and port leases before running device workloads (`vram_free_mb >= Expected VRAM`; respect Ascend A3 910C dual-Die `Phy-ID 0..15` package boundaries).
- `debugger` (`mindie-debugger`): `triage`, `decode-code`, `precision` (`inspect-capsule`, `replay-call`, `check-call`, `compare-traces`, `slim-moe`, `watch`), `aclgraph` (`guard`, `inspect`), and `remote-guide`.
- `npu_top` & Direct Observation Tools (`npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `npu_telemetry_window`, `list_npu_servers`): Observation-only fleet telemetry.
- `diagnostics_status` & `diagnostics_bundle`: Local diagnostic health and sanitized bundle export.
