<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for Tencent WorkBuddy (<code>mindie-agent-wb</code>)</h1>

<p align="center">
  <b>腾讯 WorkBuddy（及底层 CodeBuddy Agent CLI）的原生插件与连接器（Connector）适配器，完整暴露 MindIE Agent 全栈 53 项 MCP 工具（含 13 项 WorkBuddy 原生远端开发工具镜像）、生命周期 Hooks 与分级知识飞轮 Skill</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-wb/releases"><img src="https://img.shields.io/badge/release-v0.1.4-0F766E.svg" alt="Release v0.1.4"></a>
</p>

## 1. 架构概览：连接器（Connector）与插件（Plugin）双重兼容

WorkBuddy 桌面端界面中的 **Connector（连接器）** 底层即基于 **Agent Plugins 1.0.0** 规范构建并由内置 `@genie/agent-cli` (`codebuddy`) 运行时承载。本仓库同时实现：

1. **Connector UI 规范兼容**（[`plugins/mindie-agent/plugin.json`](plugins/mindie-agent/plugin.json) + [`plugins/mindie-agent/mcp.json`](plugins/mindie-agent/mcp.json)）：
   - 声明 `extensions["ai.workbuddy"]` 双语展示元数据、示例 Prompt 与超时策略（`660s` 远端/组件预算、`210s` 知识预算）。
2. **Native Agent Plugin 完整能力**（[`.codebuddy-plugin/plugin.json`](plugins/mindie-agent/.codebuddy-plugin/plugin.json) + [`.mcp.json`](plugins/mindie-agent/.mcp.json) + [`hooks/hooks.json`](plugins/mindie-agent/hooks/hooks.json) + [`skills/mindie-agent/SKILL.md`](plugins/mindie-agent/skills/mindie-agent/SKILL.md)）：
   - 挂载 `SessionStart`、`UserPromptExpansion`、`UserPromptSubmit`、`PreToolUse`、`Stop` 五类生命周期 Hook。
   - 默认加载（`alwaysLoad: true`, `defer_loading: false`）全部三个 stdio MCP Server，并启用 `REMOTE_DEV_HARNESS_PROFILE=wb` 原生镜像。

## 2. 完整暴露的 MCP 工具矩阵（共 53 项）

| MCP Server | 表面 | 工具数量 | 暴露工具列表 |
| :--- | :--- | :---: | :--- |
| `mindie-remote-dev` | `remote` (`wb` profile) | **33** | **20 项标准工具**：`remote_auth`, `remote_read`, `remote_write`, `remote_edit`, `remote_multi_edit`, `remote_bash`, `remote_glob`, `remote_grep`, `remote_ls`, `remote_apply_patch`, `remote_job_status`, `remote_job_tail`, `remote_job_output`, `remote_job_stop`, `remote_job_stdin`, `remote_artifact_manifest`, `remote_artifact_pull`, `remote_artifact_push`, `remote_context_snapshot`, `remote_probe`<br>**13 项 WorkBuddy 原生工具镜像**：`remote_Read`, `remote_Write`, `remote_Edit`, `remote_MultiEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_BashOutput`, `remote_NotebookEdit`, `remote_TaskStop`, `remote_KillShell`, `remote_ViewImage` |
| `mindie-tools` | `tools` | **14** | `mindie_status`, `profiler`, `sim`, `coordinator`, `debugger` (`triage` / `decode-code` / `precision` / `aclgraph` / `remote-guide`), `npu_top`, `npu_status`, `server_status`, `list_mounts`, `find_npu_capacity`, `npu_telemetry_window`, `list_npu_servers`, `diagnostics_status`, `diagnostics_bundle` |
| `mindie-knowledge` | `knowledge` | **6** | `mindie_entry`, `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync` |

## 3. 快速安装与验证

```bash
# 初始化 MindIE WorkBuddy 配置并将插件/连接器注册到 ~/.workbuddy/plugins
python3 plugins/mindie-agent/scripts/setup.py init --install-wb

# 使用 WorkBuddy 内置 CLI 校验插件清单
CODEBUDDY_FORCE_LITE_WB_BUNDLE=1 DISABLE_TELEMETRY=1 \
  /Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy \
  plugin validate ./plugins/mindie-agent
```
