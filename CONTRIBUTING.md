# 贡献指南 (`mindie-agent-wb`)

英文版请参阅 [CONTRIBUTING.en.md](CONTRIBUTING.en.md)。

`mindie-agent-wb` 是 MindIE Agent 面向腾讯 WorkBuddy 与 CodeBuddy Agent CLI 的双形态宿主插件仓（仓库类别：`host-plugin`）。

## 1. 本地工作区与分支拓扑

推荐使用扁平同级检出或独立 `.worktrees/` 工作树布局：

```text
code/
  mindie-agent/           # 架构文档、发布清单与跨仓门禁
  harness/                # mindie-harness 公共底座
  mindie-agent-wb/        # WorkBuddy / CodeBuddy 宿主插件仓
  .worktrees/             # 隔离开发分支工作树
```

## 2. 宿主插件核心契约（不可破坏）

1. **Connector 与原生插件双表面兼容**：完整保留 `plugin.json` + `mcp.json`（Connector）与 `.codebuddy-plugin` + `.mcp.json` + `hooks/hooks.json`（原生 CLI 插件）两套入口及 `wb` 原生远端工具镜像。
2. **被动默认与按需暴露知识面（`P2.1` / `P2.2` / `AD-11`）**：`.mcp.json` 中 `mindie-knowledge` 保持 `alwaysLoad: false, defer_loading: true`；未激活 `/mindie-agent` 时 `knowledge` 表面 `tools/list` 仅返回 `mindie_entry`（零暴露 `knowledge_*`）；未激活 `Stop` 在 `100ms` 内返回 `{"suppressOutput": true}`。
3. **确定性工具零预绑定门禁与共享远端任务目录（`P2.7` / `AD-02`）**：`mindie-remote-dev` 与 `mindie-tools` 在无 `_meta`、无 `PreToolUse` 绑定时直接可用，`REMOTE_DEV_STATE_DIR` 共享到 profile 级 `remote/` 目录以支持子智能体查询父会话 `job_id`。
4. **Windows 零弹窗与 Unix LF**：Windows 子进程统一携带 `CREATE_NO_WINDOW`，全仓文本文件保持 Unix `LF`（`\n`）换行。

## 3. 测试隔离与提交前自查

所有测试必须隔离 `HOME`、`USERPROFILE`、`MINDIE_CONFIG_HOME`、`MINDIE_DATA_HOME`、`MINDIE_DIAGNOSTICS_ROOT`、`WORKBUDDY_CONFIG_DIR` 与 `UV_CACHE_DIR`。

提交 PR 前请完成 [`.github/pull_request_template.md`](.github/pull_request_template.md) 中的 `host-plugin` 四项门禁自查（`HOST-NATIVE`、`HOST-WIRE`、`HOST-ADMISSION`、`HOST-UPGRADE`）并运行：

```bash
python3 .github/scripts/check_lf_eol.py
python3 .github/scripts/check_pr_checklist.py --profile host-plugin --body-file <pr_body.md>
pytest -q -m "not live_npu"
```
