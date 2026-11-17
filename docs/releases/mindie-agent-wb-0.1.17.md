# mindie-agent-wb 0.1.17：显式禁用模型自动唤醒 `SKILL.md` 与剥离运行时安装引导边界

[English](mindie-agent-wb-0.1.17.en.md)

- **显式禁用语义自动唤醒（`disable-model-invocation: true`）**：在 `plugins/mindie-agent/skills/mindie-agent/SKILL.md` Frontmatter 中声明 `disable-model-invocation: true`，确保分级知识飞轮仅响应用户显式发起的 `/mindie-agent` 或 `$mindie-agent` 指令，绝不由宿主按话题语义静默加载。
- **安装引导与运行时技能严格分离**：将首次工作区配置、GitHub 登录引导与用户须知从运行时 `skills/mindie-agent/references/` 迁出至仓库级 `docs/install/`（`workspace-setup.md`、`github-authentication.md`、`user-notice.md`），并清理 `SKILL.md` 中的全部安装、授权弹窗与 Star 提示内容。
