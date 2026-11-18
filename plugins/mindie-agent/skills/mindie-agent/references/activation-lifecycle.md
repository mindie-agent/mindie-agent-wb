# Default Session Lifecycle & Capture in Tencent WorkBuddy

- **Default-Enabled Tools**: `mindie-remote-dev`, `mindie-tools`, and `mindie-knowledge` (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) are available out of the box when the plugin is enabled, auto-provisioning a session lease (`not_before=0.0`) on demand.
- **Zero-I/O Ordinary Hooks**: `SessionStart`, `UserPromptSubmit`, and `PreToolUse` pass through without creating SQLite session or bind files.
- **Opt-In Turn-End `Stop` Capture**: `bridge.py stop` checks community sharing consent (`mindie-consent.json` + `mindie-community.json`) and authorized `project_roots` before touching `admission.sqlite3`. When enabled and in scope, it provisions the session lease on demand and hands off the WorkBuddy JSONL transcript to `mindie_knowledge.loop.cli.capture_hook` without blocking or forcing model continuation.
