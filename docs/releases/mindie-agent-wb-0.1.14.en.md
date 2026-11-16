# mindie-agent-wb 0.1.14: Storage Lifecycle Governance and External Cache Unpinning

[中文](mindie-agent-wb-0.1.14.md)

- **Cross-process update mutex and WorkBuddy external cache unpinning (`component_update.py`, `wb_install.py`, `storage_lifecycle.py`)**: Component updates take a cross-process lock at `<data_home>/wb-component-update.lock`; a contender returns `status="busy"` instead of overwriting state or building again. Install and upgrade flows scan only existing MindIE WorkBuddy plugin caches, atomically rebind pinned Python interpreters in `hooks.json`, `mcp.json`, and `.mcp.json`, and retain the active version plus one rollback generation. Superseded cache generations leave through a two-phase rename without touching unregistered account state.
- **Silent reclamation of historical baggage**: After a successful switch, bounded `mindie_knowledge.storage.prune_component_runtimes` and `sweep_legacy_installations` calls reclaim incomplete failed `.venv` generations without `components.json`, excess `wb-state/component-runtimes/*`, old `wb-source/*`, and leftover `.trash-*`. Cross-plugin borrowed sibling-host interpreters, an external Git checkout referenced by `wb.json["source_root"]`, live lease-holding children, and Git metadata are always preserved. Local occupancy or budget expiry only adds `receipt_warning`/`receipt_unconfirmed=True`; the primary result remains `status="updated"`.
- **Resident MCP runtime lease and cold-start cache placement (`mcp_server.py`, `setup.py`)**: The resident MCP facade and its single-request children hold a shared `acquire_runtime_lease(...)` for `owning_dir(sys.executable)`, so reclamation cannot retire a generation that is still serving. A first `wb-venv` bootstrap that has no explicit `UV_CACHE_DIR` and crosses volumes now uses the same-volume `<data_home>/.uv-cache`.
- **Test isolation and temporary-directory governance (`tests/conftest.py`, `tests/test_runtime_upgrade.py`, `tests/test_storage_lifecycle_wb.py`)**: Tests now fully isolate `HOME`, `USERPROFILE`, `MINDIE_*`, `TMPDIR`, and `UV_CACHE_DIR`; Windows `mwb-*` directories are removed in `finally` with retries; the old-runtime acceptance run uses pytest's isolated `tmp_path` and no longer writes multi-generation virtualenvs next to the checkout. New cross-platform storage lifecycle regressions cover failed `.venv` trees, multiple runtime/source generations, old plugin caches, interpreter rebinding, borrowed interpreters, and live-lease protection, and assert that `mcp_server.py` stays under 800 lines.

## Component baseline

| Component | Version | Pinned commit |
| :--- | :--- | :--- |
| `mindie-knowledge` | `0.13.22` | `c991b64a1ead277b5ce91b7835337b01aed03998` |
| `remote-dev` | `0.11.12` | `c4d38f99c1ef5aace1c088af07647941adc2d394` |
| `mindie-diagnostics` | `0.5.14` | `cd16baa406070bcd41bbe39a9e488cc8753c60c5` |
| `mindie-coordinator` | `0.7.11` | `33e2c6966d21d62969a2148145e74885dc826e3c` |
| `mindie-profiler` | `0.5.15` | `889b6ef161862938cc07b1b4123c57e302965d48` |
| `mindie-sim` | `0.9.15` | `9dbab02f10843c7b829d6fda7e7b9ad89e4f92e5` |
| `mindie-debugger` | `0.1.3` | `33052a2fc10c7475e4b819577870ca822efcebd0` |
| `npu-top` | `0.3.4` | `4341003c6637e2b8b9512c7b076f80f45019e252` |
