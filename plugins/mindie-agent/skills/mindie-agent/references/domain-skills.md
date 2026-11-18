# Domain Tooling & NPU Discipline

## 1. `mindie-remote-dev` (15-tool `wb` profile)

WorkBuddy binds `REMOTE_DEV_HARNESS_PROFILE=wb`, so the remote surface is the
converged native profile rather than the canonical 20-tool catalog:

- Native mirrors: `remote_Read`, `remote_Write`, `remote_Edit`,
  `remote_MultiEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`,
  `remote_TaskOutput`, `remote_NotebookEdit`, `remote_TaskStop`,
  `remote_ViewImage`.
- Remote-only support: `remote_auth`, `remote_probe`, `remote_artifact_pull`,
  `remote_artifact_push`.

`remote_TaskOutput` owns background output/job inspection for this profile.
`remote_BashOutput` and `remote_KillShell` are not part of `wb`; do not assume
they exist.

Bind once with saved discovery, then reuse the default endpoint:

```json
{"host": "npu.example", "port": 22, "user": "root", "alias": "a3-lab",
 "save_default": true, "discover": true}
```

Call `remote_auth(..., save_default=True, discover=True)` with those
arguments. Subsequent tools may omit endpoint fields and recover the saved
default; a `job_*` tool also recovers its endpoint from the stored job record.
Use `remote_probe` for a cheap reachability check.

## 2. Deterministic Components (`mindie-tools`)

`mindie_status`, `mindie_doctor`, `profiler`, `sim`, `coordinator`, `npu_top`,
`debugger`, `rsi_loop`, `experiment_ledger`, `diagnostics_status`, and
`diagnostics_bundle`. This surface contains no `knowledge_*` duplicates.

`mindie_doctor` is the readiness check for the active profile, runtime,
component bindings, and optional remote reachability:

```json
{"probe_remote": true, "cwd": "/workspace/vllm-ascend", "timeout_ms": 30000}
```

Run it once before profiling or multi-rank work. A failed doctor result is
diagnostic guidance; continue through native tools when the task does not
depend on the failed check.

## 3. Graded Knowledge (`mindie-knowledge`, 5 default-on tools)

`knowledge_query`, `knowledge_explain`, `knowledge_feedback`,
`knowledge_distill`, and `knowledge_sync`. They are available at startup with
zero `_meta`, zero pre-call binding, and zero onboarding command.

- `knowledge_query`: concise technical terms, optional `domain`, observed
  `conditions`, `token_budget` (1–64000), `mode` (`reuse` / `diagnose` /
  `explore`).
- `knowledge_explain`: batch `refs`, observed `conditions`, visible
  `known_refs`.
- `knowledge_distill`: only `verified_milestone` or `completed` evidence with
  `inputs`, `task_state`, and a native lease supplied by the runtime.
- `knowledge_sync`: configured public feeds only.

## 4. Sharing & Fault Reporting

Run `setup.py sharing --enable | --local-only | --disable` (and read-only
`--status`) from the plugin root:

```bash
python scripts/setup.py sharing --enable
python scripts/setup.py sharing --local-only
python scripts/setup.py sharing --disable
python scripts/setup.py sharing --status
```

`--enable` (`contribute`) authorizes redacted knowledge PRs plus sanitized
fault reporting. `--local-only` captures local transcripts without reporting.
`--disable` stops new capture and reporting. `--status` is strictly read-only.

## 5. NPU Lease Discipline

Acquire or verify a `coordinator` lease before physical-NPU workloads, respect
A3 dual-Die package boundaries, and inspect live `execution_id` / `job_id`
handles before resubmitting. Pass explicit `--time-window`,
`--step-window` / `--full-range`, and `--clock-domain` to `profiler`; use
`mode="explore"` plus leave-one-out ablations when repeated `L3` attempts stall.
