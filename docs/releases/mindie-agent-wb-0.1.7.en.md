# mindie-agent-wb 0.1.7

[中文](mindie-agent-wb-0.1.7.md)

Align the root marketplace, native plugin, and Connector versions. Installation contracts read the actual plugin version, and PR CI runs all local contracts on Linux and Windows. Networked Tier 3 acceptance requires `--live-host`; tests use the selected interpreter and isolated profiles without discovering another user host installation.

The 0.1.6 candidate failed before a formal Release was created. Its tag and failure evidence are retained; this patch uses a new immutable tag.

## Task continuity and first-time SSH access

- Reuse supplied credentials through shared `remote_auth` to authenticate, install a public key, and verify key access. Missing saved authentication profiles must not block otherwise authorized work.
- Optional knowledge, diagnostics, component setup, and local accounting failures affect their capability. Continue independent authorized native work while preserving task identity, resource constraints, and active jobs.
- Preserve successful primary remote outcomes with explicit warnings for local receipt failures. Reconcile uncertain external writes before retrying them.
- Validation covers local contracts, platform CI, real isolated SSH servers, and release benchmarks. No private deployment-host, NPU inference, or model accuracy acceptance is claimed.
