# Activation & Capture Lifecycle in Tencent WorkBuddy

- **Manual Entry Only**: Only an explicit `/mindie-agent` or `$mindie-agent` prompt in `UserPromptSubmit` / `UserPromptExpansion` (or `mindie_entry(op="init")`) grants `entry_enabled = 1` in `admission.sqlite3`.
- **Deactivation**: `/mindie-agent deactivate` (or `mindie_entry(op="deactivate")`) revokes the conversation's lease across all registered domains; deterministic tools (`mindie-remote-dev`, `mindie-tools`) remain available.
- **Turn-End `Stop` Capture**: `bridge.py stop` checks `Admission.knowledge_lease(session_id)`, community sharing consent (`mindie-community.json`), and project scope, then hands off the WorkBuddy JSONL transcript to `mindie_knowledge.loop.cli.capture_hook` without blocking or forcing model continuation.
