# MindIE Agent for Tencent WorkBuddy (`mindie-agent-wb`)

<p>
  <a href="https://github.com/mindie-agent/mindie-agent-wb/releases"><img src="https://img.shields.io/badge/release-v0.1.2-0F766E.svg" alt="Release v0.1.2"></a>
</p>

Native plugin and UI Connector adapter for **Tencent WorkBuddy** (and its bundled `@genie/agent-cli` / `codebuddy` runtime), exposing the complete suite of **40 MindIE MCP tools**, lifecycle hooks, and L1–L4 graded knowledge skills.

## Complete MCP Tool Matrix (40 Tools Across 3 Servers)

- **`mindie-remote-dev` (20 tools)**: `remote_auth`, `remote_read`, `remote_write`, `remote_edit`, `remote_multi_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_ls`, `remote_apply_patch`, `remote_job_status`, `remote_job_tail`, `remote_job_output`, `remote_job_stop`, `remote_job_stdin`, `remote_artifact_manifest`, `remote_artifact_pull`, `remote_artifact_push`, `remote_context_snapshot`, `remote_probe`.
- **`mindie-tools` (14 tools)**: `mindie_status`, `profiler`, `sim`, `coordinator`, `debugger`, `npu_top`, `npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `npu_telemetry_window`, `list_npu_servers`, `diagnostics_status`, `diagnostics_bundle`.
- **`mindie-knowledge` (6 tools)**: `mindie_entry`, `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`.

## Installation

```bash
python3 plugins/mindie-agent/scripts/setup.py init --install-wb
```
