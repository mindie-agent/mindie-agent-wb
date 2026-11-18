# MindIE Agent — Tencent WorkBuddy & CodeBuddy Host Plugin (`mindie-agent-wb`)

`mindie-agent-wb` is a **`host-plugin`** repository implementing both the Tencent WorkBuddy Connector (`plugin.json`, `mcp.json`, `ai.workbuddy`) and the CodeBuddy native CLI plugin (`.codebuddy-plugin/plugin.json`, `.workbuddy-plugin/plugin.json`, `.claude-plugin/plugin.json`, `.mcp.json`, `hooks/hooks.json`), backed by `mindie-harness` (`v0.1.0`) and shared MindIE runtime components.

## 1. Core Architectural & Contract Rules

- **Dual Surface Preservation**: Preserve both Connector UI (`plugin.json` + `mcp.json`) and CodeBuddy native plugin (`.mcp.json` + `hooks/hooks.json`) surfaces (`33` remote tools under `wb` profile + `14` deterministic component/observation tools + `5` default-enabled `knowledge_*` tools).
- **Silent Default-On Knowledge & Consent-Gated `Stop` (`P2.7`, `P2.8`)**:
  - `.mcp.json` sets `"alwaysLoad": true, "defer_loading": false` across `mindie-remote-dev`, `mindie-tools`, and `mindie-knowledge`; `tools/list` on `mindie-knowledge` returns the 5 `knowledge_*` tools directly without creating `admission.sqlite3`.
  - `Stop` (`hook stop`) checks community sharing consent (`mindie-consent.json` + `mindie-community.json`) and authorized `project_roots` before opening `admission.sqlite3`, returning `{"suppressOutput": true}` in `< 100ms` with zero child re-exec or SQLite creation when off or out of scope, and auto-provisioning a default lease (`not_before=0.0`) to capture when enabled and in scope.
- **Zero Pre-Call Session-Binding Gates & Shared Remote State (`P2.7`, `AD-02`)**:
  - All `mindie-remote-dev`, `mindie-tools`, and `mindie-knowledge` calls succeed over standard MCP `tools/call` with zero `_meta`, zero `PreToolUse` binds, and zero `WB_CONVERSATION_ID`, falling back to `_CONNECTION_SESSION_ID` (`"wb-cli-default"` in `knowledge_cli.py`).
  - `REMOTE_DEV_STATE_DIR` resolves to the shared profile-level `state_dir()/remote` directory so Subagents and reconnected clients can inspect and stop parent `job_id`s.
- **Concurrent Resident Stdio MCP & `mindie-harness` Substrate (`AD-03`, `AD-07`, `AD-10`)**:
  - `mcp_server.py` and `host_adapter.py` (`WBHostAdapter`) serve `Content-Length` and JSONL frames concurrently across separate read/control and mutation pools while holding `.inuse` generation leases (`hold_combined_runtime_lease`), with zero `--once` per-call cold spawns.
  - `assets/mindie-harness-seed.pyz` is verified in `64 KiB` streaming chunks against `assets/seed-lock.json` (`mindie-harness-seed/1`).
  - `rsi_loop.py` and `experiment_ledger.py` delegate to `mindie_harness.rsi`.
  - Never use blanket `O_NOFOLLOW` on metadata/config reads (`AD-07`) and never leak benchmark evaluation tuples or ledger gates into `SKILL.md` (`AD-10`).
- **State & Receipt Honesty (Principles 9, 10, 14, 17)**:
  - Keep every module in `plugins/mindie-agent/scripts/` `< 800` lines with `[Role]`, `[Invariants]`, `[Timeouts & Locks]`, and `[Side-Effect Policy]` in the top 30 lines.
  - Always place `except TimeoutError: raise` before `except OSError:`. Once a primary remote/component/knowledge operation succeeds, local receipt/cursor/bind failures keep `isError=False` (`outcome="success"`) and attach `receipt_warning` + `receipt_unconfirmed=True`.

## 2. Plugin Script Map (`plugins/mindie-agent/scripts/`)

| Module | Role |
| :--- | :--- |
| `mindie_launch.py` | Entrypoint for `hook`, `mcp`, `setup`, and `update`; `< 100ms` passive `Stop` fast-path and paired interpreter re-exec. |
| `host_adapter.py` | `WBHostAdapter` (`mindie_harness.api.HostAdapter`), `verify_seed_bundle()`, and concurrent dual-framing stdio loop (`serve_concurrent_stdio`). |
| `bridge.py` / `admission.py` / `identity.py` | WorkBuddy `SessionStart`, silent `UserPromptSubmit`/`PreToolUse` pass-throughs, and consent-gated `Stop` handoff. |
| `mcp_server.py` / `wb_native_compat.py` / `knowledge_cli.py` | `remote` (`wb` profile), `tools`, and default-enabled `knowledge` MCP & CLI surfaces. |
| `setup.py` / `wb_install.py` / `component_update.py` / `storage_lifecycle.py` | WorkBuddy plugin/Connector installer, atomic `wb.json` switching, and bounded generation/cache reclamation. |
| `wb_transcript.py` / `organizer.py` | Read-only WorkBuddy JSONL transcript projection and isolated summary/C6 worker. |
| `bounded.py` / `diagnostic_support.py` / `rsi_loop.py` | `mindie_harness.process` subprocess delegation, symlink-safe sanitized fault recording, and `mindie_harness.rsi` entrypoints. |

## 3. Local Verification

```bash
python3 .github/scripts/check_lf_eol.py
pytest -q -m "not live_npu"
```
