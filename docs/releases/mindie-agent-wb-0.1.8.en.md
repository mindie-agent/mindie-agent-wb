# mindie-agent-wb 0.1.8

[中文](mindie-agent-wb-0.1.8.md)

## Preserve tasks across installed runtime upgrades

- Launch the interpreter and retained script directory recorded in the same active profile. A newly downloaded native payload can no longer combine new handlers with an older component runtime.
- Probe required component APIs before installation activates the candidate. Reuse the shared builder to prepare a separate runtime when needed; preparation failures preserve the previous profile and task entry point.
- Publish retained scripts atomically into directories identified by version and content digest. Native directory replacement leaves active scripts, job state, and user configuration fields intact. Remove a runtime discovery call to a deleted helper.
- Add Linux/Windows acceptance using actual old plugin setup, native payload replacement, failed preparation, component upgrade, and MCP calls before and after activation. No private deployment-host or NPU accuracy acceptance is claimed.
