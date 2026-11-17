# mindie-agent-wb 0.1.17: Explicit Manual-Trigger `SKILL.md` Guard and Runtime/Install Boundary Separation

[中文](mindie-agent-wb-0.1.17.md)

- **Explicit Manual-Trigger Enforcement (`disable-model-invocation: true`)**: Sets `disable-model-invocation: true` in `plugins/mindie-agent/skills/mindie-agent/SKILL.md` frontmatter so the graded knowledge flywheel activates strictly on explicit `/mindie-agent` or `$mindie-agent` user invocation rather than semantic auto-invocation.
- **Clean Runtime Skill vs. Installation Doc Boundary**: Relocates `workspace-setup.md`, `github-authentication.md`, and `user-notice.md` out of `plugins/mindie-agent/skills/mindie-agent/references/` into `docs/install/`, purging all installation, onboarding, consent-prompt, and GitHub Star instructions from runtime `SKILL.md` and skill references.
