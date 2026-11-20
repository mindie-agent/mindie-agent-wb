# mindie-agent-wb 0.1.20: Cold `--managed` Re-Entry Fix, Read-Only Zero-Lease Compliance & 3-OS Wire Gate

[中文](mindie-agent-wb-0.1.20.md)

- **Deleted Shadow Knowledge Transport & Read-Path Lease Creation (`HAR-STATE`)**: Removed `knowledge_transport` in `scripts/bridge.py` (which previously minted `Admission` leases on public `knowledge_query`/`explain` reads) and the local override in `scripts/mcp_server.py` (`45` lines), delegating strictly to `mindie-harness v0.1.5`.
- **Silent Cold Seed Provisioning & Stable Manifest Launchers**: Fixed cold `mindie_launch.py` startup so post-seed execution re-enters `WBManagedAdapter` and `Stop` (exposing all 15 `remote`, 11 `tools`, and 5 `knowledge` tools on Turn 1); eliminated bare `"python"` entries in `.mcp.json` / `mcp.json` / `hooks.json` and stopped `wb_install.py` from hard-pinning transient generation interpreters into cached manifests.
- **9-Domain Provisioning & 3-OS Native Wire Gate**: Wired single-lock `ensure_knowledge_binding` and `run_stop_capture` (`identity_kind="notification"` when native `turn_id` is absent), refreshed `mindie-harness-seed.pyz` and 9-package pins, and enabled `verify-host-plugin.yml` across Ubuntu, macOS, and Windows.
