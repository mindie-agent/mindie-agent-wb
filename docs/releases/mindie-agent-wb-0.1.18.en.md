# mindie-agent-wb 0.1.18: Silent Default-Enabled `knowledge_*` Tools and Consent-First `Stop` Capture

[中文](mindie-agent-wb-0.1.18.md)

- **Default-Enabled `knowledge_*` Tools with Zero Session-Binding Gate**: Sets `alwaysLoad: true, defer_loading: false` on `mindie-knowledge` in `.mcp.json` and exposes `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, and `knowledge_sync` directly on `tools/list` at startup, falling back cleanly to connection-scoped session identity (`"wb-cli-default"` in `knowledge_cli.py`) and provisioning default leases (`not_before=0.0`) on demand.
- **Silent Semantic `SKILL.md` and Consent-First `Stop` Fast-Path**: Removes `UserPromptExpansion` and user slash/dollar command prefixes, converts `UserPromptSubmit` and `PreToolUse` into zero-SQLite pass-throughs, and checks community sharing consent plus authorized `project_roots` in `Stop` before opening `admission.sqlite3` (`< 100ms` passive cold path with zero SQLite creation when off or out of scope, and automatic lease provisioning + redacted transcript capture when enabled and in scope).
