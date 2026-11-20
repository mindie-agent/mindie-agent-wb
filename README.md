<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for Tencent WorkBuddy (<code>mindie-agent-wb</code>)</h1>

<p align="center">
  <b>腾讯 WorkBuddy（及底层 CodeBuddy Agent CLI）的原生插件与连接器适配器：收敛的 15 项 <code>wb</code> 远端工具、确定性组件工具与 <code>mindie_doctor</code>、默认启用的 L1-L4 分级知识飞轮，以及 6 个薄脚本运行时。</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-wb/releases"><img src="https://img.shields.io/badge/release-v0.1.20-0F766E.svg" alt="Release v0.1.20"></a>
</p>

## 1. 架构概览：连接器（Connector）与原生插件（Plugin）

WorkBuddy 桌面端的 **Connector（连接器）** 基于 **Agent Plugins 1.0.0** 规范构建，由内置 `@genie/agent-cli`（`codebuddy`）运行时承载。本仓库同时实现：

1. **Connector UI 规范兼容**：[`plugin.json`](plugins/mindie-agent/plugin.json) + [`mcp.json`](plugins/mindie-agent/mcp.json) 声明 `extensions["ai.workbuddy"]` 双语展示元数据、示例 Prompt 与超时策略。
2. **Native Agent Plugin 完整能力**：[`.codebuddy-plugin/plugin.json`](plugins/mindie-agent/.codebuddy-plugin/plugin.json) + [`.mcp.json`](plugins/mindie-agent/.mcp.json) + [`hooks/hooks.json`](plugins/mindie-agent/hooks/hooks.json) + [`skills/mindie-agent/SKILL.md`](plugins/mindie-agent/skills/mindie-agent/SKILL.md)，挂载 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`Stop` 四类生命周期 Hook，并默认加载三个 stdio MCP Server（`alwaysLoad: true`, `defer_loading: false`）。
3. **六个薄脚本（`v0.1.20`）**：`mindie_launch.py`、`bridge.py`、`mcp_server.py`、`wb_transcript.py`、`wb_install.py`、`setup.py`。运行时委托 `mindie-harness v0.1.6`；`remote-dev v0.11.15` 原生提供 `wb` 15 工具 profile，不再需要本地兼容层。

→ 直接跳到 [快速安装与验证](#3-快速安装与验证)。

## 2. 工具矩阵（15 + 11 + 5）

| MCP Server | 表面 | 数量 | 工具 |
| :--- | :--- | :---: | :--- |
| `mindie-remote-dev` | `remote`（`wb` profile） | **15** | `remote_Read`, `remote_Write`, `remote_Edit`, `remote_MultiEdit`, `remote_Bash`, `remote_Glob`, `remote_Grep`, `remote_TaskOutput`, `remote_NotebookEdit`, `remote_TaskStop`, `remote_ViewImage`, `remote_auth`, `remote_probe`, `remote_artifact_pull`, `remote_artifact_push` |
| `mindie-tools` | `tools` | **11** | `mindie_status`, `mindie_doctor`, `profiler`, `sim`, `coordinator`, `npu_top`, `debugger`, `rsi_loop`, `experiment_ledger`, `diagnostics_status`, `diagnostics_bundle` |
| `mindie-knowledge` | `knowledge` | **5** | `knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync` |

`mindie-tools` 不含任何 `knowledge_*` 重复项；`mindie-remote-dev` 不含 `remote_BashOutput` / `remote_KillShell`。三个表面都支持零 `_meta`、零 `PreToolUse` 预绑定的标准 MCP `tools/call`。

## 3. 快速安装与验证

把下面这句话发给 WorkBuddy / `codebuddy`（或其他能执行终端的 Agent）：

```text
帮我安装一下这个 https://github.com/mindie-agent/mindie-agent-wb
```

安装脚本入口（`gh` 可选，绝不是阻塞项）：

```bash
uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host wb
```

安装成功后，Agent 应按以下顺序完成一次 3 问动态引导：

1. **开发工作区**：新建并初始化 `vllm-ascend-workspace`（推荐）、保持当前目录，或复用已有目录；记录绝对路径 `WORKSPACE_ROOT`。
2. **社区共享（动态推荐）**：先运行 `gh auth status` / `gh api user --jq .login`。若 `gh` 已登录，推荐 `contribute`；否则推荐 `local-only`。用户可随时改选。
   ```bash
   python plugins/mindie-agent/scripts/setup.py sharing --enable --project-root "$WORKSPACE_ROOT"
   python plugins/mindie-agent/scripts/setup.py sharing --local-only
   python plugins/mindie-agent/scripts/setup.py sharing --disable
   ```
3. **一步连接昇腾 NPU（终端模式）**：用一次 `remote_auth` 调用完成主机、端口、用户与容器绑定：
   ```json
   {"host": "npu.example", "port": 22, "user": "root", "save_default": true, "discover": true}
   ```
   终端交互模式可在该步末尾追加 `--password`，让密码在隐藏提示中输入，避免明文进入对话或日志。非终端模式改用密钥或已有 `remote_auth` 默认绑定。

完成后运行就绪检查并核对安装：

```bash
python plugins/mindie-agent/scripts/setup.py doctor --probe-remote --cwd "$WORKSPACE_ROOT"
```

在 WorkBuddy 内也可直接调用 `mindie_doctor`（参数 `{"probe_remote": true, "cwd": "..."}`）。确认以下信号后再报告安装完成：

- `~/.config/mindie-agent/wb.json` 存在且 `harness` 为 `workbuddy`。
- `~/.workbuddy/plugins/installed_plugins.json` 含 `mindie-agent@mindie-agent-marketplace`，`settings.json` 的 `enabledPlugins` 为 `true`。
- `mindie_remote-dev` 的 `tools/list` 恰好 15 项，`mindie-tools` 含 `mindie_doctor` 且无 `knowledge_*`，`mindie-knowledge` 恰好 5 项。
- `mindie_doctor` 返回 `status: "ok"`（远端探测可选）。

`--help`、`-h` 和 `--dry-run`（含 `--dry` 等唯一前缀）只打印计划，不写入任何配置：

```bash
python plugins/mindie-agent/scripts/setup.py init --install-wb --dry-run
```

## 4. 生命周期与任务连续性

- **静默默认启用**：`UserPromptSubmit` / `PreToolUse` / `SessionStart` 与未配置的 `Stop` 在纯标准库路径内完成，`< 100ms` 返回 `{"suppressOutput": true}`，不创建 SQLite。
- **授权优先采集**：仅当 `mindie-consent.json` 选择 `contribute` / `local-only`、`mindie-community.json` 已启用且工作区位于 `project_roots` 内时，`Stop` 才激活默认租约并调用 `capture_hook(..., timeout=6.0)`；显式撤销（`inactive`）永远保留。
- **非阻塞任务连续性规则**：MindIE 工具（`remote-dev`、`coordinator`、`knowledge`、`mindie_doctor`）是加速器，绝不是路障。任何工具失败只报告一次（先核对不确定的远端写入），随后立即改用原生 `ssh`/`scp`、本地脚本或其他已授权工具继续任务。

## 5. 文档与致谢

- 安装与工作区：[`docs/install/workspace-setup.md`](docs/install/workspace-setup.md)
- GitHub 登录（可选）：[`docs/install/github-authentication.md`](docs/install/github-authentication.md)
- 用户须知与数据范围：[`docs/install/user-notice.md`](docs/install/user-notice.md)
- 版本说明：[`docs/releases/mindie-agent-wb-0.1.20.md`](docs/releases/mindie-agent-wb-0.1.20.md)

如果这个项目帮到了你，欢迎顺手点一个 Star ⭐。
