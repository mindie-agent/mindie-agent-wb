# Default Session Lifecycle & Capture in Tencent WorkBuddy

- **Default-on tools**: `mindie-remote-dev`, `mindie-tools`, and
  `mindie-knowledge` are available whenever the plugin is enabled. They need no
  onboarding command, activation command, `_meta`, or `PreToolUse` bind. A
  default lease (`not_before=0.0`) is provisioned on demand when a knowledge or
  capture operation needs one.
- **Explicit revocation wins**: `Admission.inspect(session).status ==
  "inactive"` is preserved. Never silently re-activate a revoked session, and
  never treat a missing lease as revocation.
- **Consent-first `Stop`**: the hook reads `mindie-consent.json`
  (`choice in {"contribute", "local-only"}`) and `mindie-community.json`
  (`enabled is True` plus non-empty absolute `project_roots`). When sharing is
  unconfigured, disabled, or the workspace is out of scope, it returns
  `{"suppressOutput": true}` immediately with zero SQLite creation.
- **In-scope capture**: when enabled and in scope, `Stop` re-verifies the
  sharing tier and scope, activates the default lease, validates the session
  transcript with `wb_transcript.capture_source`, prepares the domain schema if
  absent, and calls `capture_hook(..., timeout=6.0)` with the native turn id and
  activation token. A capture failure is reported once and never blocks the
  host turn.
- **No command prefix**: ordinary prompts are not intercepted and do not need a
  slash or dollar invocation prefix. Share or mute collection through
  `setup.py sharing --enable | --local-only | --disable`.
