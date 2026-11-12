<p align="center">
  <img src="plugins/mindie-agent/assets/mindie-agent-logo.png" alt="MindIE Agent logo" width="128" height="128">
</p>

<h1 align="center">MindIE Agent for Tencent WorkBuddy (<code>mindie-agent-wb</code>)</h1>

<p align="center">
  <b>腾讯 WorkBuddy（及底层 CodeBuddy Agent CLI）的原生插件与连接器（Connector）适配器，完整暴露 MindIE Agent 全栈 53 项 MCP 工具（含 13 项 WorkBuddy 原生远端开发工具镜像）、生命周期 Hooks 与分级知识飞轮 Skill</b>
</p>

<p align="center">
  <a href="https://github.com/mindie-agent/mindie-agent-wb/releases"><img src="https://img.shields.io/badge/release-v0.1.9-0F766E.svg" alt="Release v0.1.9"></a>
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

## 3. 安装与首次配置

把下面这句话发给 WorkBuddy / `codebuddy`（或其他能执行终端的 Agent），由它按本节协议完成安装与核验：

```text
帮我安装一下这个 https://github.com/mindie-agent/mindie-agent-wb
```

社区贡献默认关闭。安装成功后阅读 [用户须知](plugins/mindie-agent/skills/mindie-agent/references/user-notice.md)，并按 [工作区配置指南](plugins/mindie-agent/skills/mindie-agent/references/workspace-setup.md) 完成首次工作区引导。GitHub 登录按 [GitHub 登录引导](plugins/mindie-agent/skills/mindie-agent/references/github-authentication.md) 处理。

<details>
<summary>🤖 <b>AI Agent 自动安装执行协议（点击展开 / Agent 读取本页时会自动解析执行）</b></summary>

如果你是收到用户安装请求的 AI Agent，请直接按顺序执行以下步骤。**不要把单独的 `init` 汇报成 WorkBuddy 已安装。**

1. **检查 `git`、`uv` 与 `gh`（硬性指标，零全局 Git 污染）**

   插件拉取、发行版校验、`knowledge-vllm-ascend` 同步、经验闭环 PR 与组件故障上报都依赖已登录的 `gh`。长路径、LF 与 GitHub 凭据提供方仅通过进程级 `GIT_CONFIG_COUNT` 注入，不修改宿主机全局或系统级 Git 配置文件（`~/.gitconfig`）：

   ```sh
   git --version && uv --version && gh --version
   mindie_append_git_config() {
     key="$1"; value="$2"; count="${GIT_CONFIG_COUNT:-0}"; i=0
     while [ "$i" -lt "$count" ]; do
       eval "cur=\${GIT_CONFIG_KEY_$i-}"
       [ "$cur" = "$key" ] && return 0
       i=$((i + 1))
     done
     eval "export GIT_CONFIG_KEY_$count=\"\$key\""
     eval "export GIT_CONFIG_VALUE_$count=\"\$value\""
     GIT_CONFIG_COUNT=$((count + 1)); export GIT_CONFIG_COUNT
   }
   mindie_append_git_config core.longpaths true
   mindie_append_git_config core.autocrlf false
   mindie_append_git_config "credential.https://github.com.provider" github
   mindie_append_git_config "credential.https://github.com.helper" "!gh auth git-credential"
   unset -f mindie_append_git_config
   export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true
   GH_USER=$(gh api --hostname github.com user --jq .login) && echo "GitHub channel verified for account: $GH_USER"
   ```

   未安装 `gh` 时先协助安装（macOS：`brew install gh`；Windows：`winget install --id GitHub.cli`；Ubuntu / WSL：`sudo apt update && sudo apt install -y gh`）。需要登录时按 [GitHub 登录引导](plugins/mindie-agent/skills/mindie-agent/references/github-authentication.md)：普通网络用浏览器，企业网络或浏览器授权受阻时优先 Token。网络、代理或证书失败先修传输，不把它们当成 Token 失效。

2. **保留用户授权**

   安装过程只注册 WorkBuddy 插件与连接器。社区贡献保持关闭，直到用户在第 5 步或 `/mindie-agent` 里明确选择 `contribute`。安装过程不自动创建 Fork，也不静默点 Star。

3. **安装必须调用 `install_workbuddy_plugin`**

   会挂载 Connector 与原生插件的入口只有这两条：

   ```sh
   UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv run --no-project --python 3.13 python plugins/mindie-agent/scripts/setup.py init --install-wb --workspace-root "$PWD"
   UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv run --no-project --python 3.13 python plugins/mindie-agent/scripts/setup.py install-wb
   ```

   `init --install-wb` 会先刷新持久化源码基线（`wb-source`）、写入 MindIE Profile（`wb.json`），再调用 `install_workbuddy_plugin`。单独的 `install-wb` 同样会先准备 Profile，再调用 `install_workbuddy_plugin`。

   单独的 `init`（不带 `--install-wb`）只写 Profile 和运行时，**不**注册 `~/.workbuddy` 里的插件或连接器。

   从锁定标签安装时：

   ```sh
   MINDIE_TMP="${TMPDIR:-/tmp}/mindie-agent-wb-install"
   rm -rf "$MINDIE_TMP"
   git -c core.longpaths=true -c core.autocrlf=false clone --depth 1 --branch v0.1.9 https://github.com/mindie-agent/mindie-agent-wb.git "$MINDIE_TMP"
   UV_SYSTEM_CERTS=true UV_NATIVE_TLS=true uv run --no-project --python 3.13 python \
     "$MINDIE_TMP/plugins/mindie-agent/scripts/setup.py" init --install-wb --workspace-root "$PWD"
   ```

   `--help`、`-h` 和 `--dry-run`（含唯一缩写，例如 `--dry`）只打印说明或计划，不拉取运行时，也不写配置：

   ```sh
   uv run --no-project --python 3.13 python plugins/mindie-agent/scripts/setup.py --help
   uv run --no-project --python 3.13 python plugins/mindie-agent/scripts/setup.py init --install-wb --dry-run
   ```

4. **核对成功信号：原生插件与 Connector 都已挂载**

   - 命令回执里 `workbuddy_install.status` 为 `installed`（单独跑 `install-wb` 时顶层 `status` 为 `installed`），`enabled` 为 `true`，`plugin_id` 为 `mindie-agent@mindie-agent-marketplace`。
   - `~/.workbuddy/plugins/installed_plugins.json` 含有该插件，且 `~/.workbuddy/settings.json` 的 `enabledPlugins["mindie-agent@mindie-agent-marketplace"]` 为 `true`。
   - 当 `~/.workbuddy/plugins/marketplaces/workbuddy-connector-plugins-official` 存在时，回执里的 `connector_path` 非空，且对应账号的 `plugin-account-state/<uid>.json` 中 `mindie-agent@workbuddy-connector-plugins-official` 为 `enabled: true`。该目录不存在时，报告原生插件已挂载、Connector 市场尚未出现，不要把插件缓存写成 Connector 已激活。
   - `~/.config/mindie-agent/wb.json` 已生成，`harness` 为 `workbuddy`。
   - 可选：用内置 CLI 校验清单（这只验证清单，不能代替上面的挂载回执）：

     ```sh
     CODEBUDDY_FORCE_LITE_WB_BUNDLE=1 DISABLE_TELEMETRY=1 \
       /Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy \
       plugin validate ./plugins/mindie-agent
     ```

   向用户汇报已验证的 GitHub 账号（`$GH_USER`）、插件版本，以及上面的插件/连接器挂载结果。

5. **调用 WorkBuddy 原生 `AskUserQuestion` 工具弹出交互式单选框完成首次授权与工作区配置**

   - 先用纯标准库命令读取当前共享状态：

     ```sh
     WB_SOURCE="${MINDIE_DATA_HOME:-$HOME/.local/share/mindie-agent}/wb-source"
     uv run --no-project --python 3.13 python "$WB_SOURCE/plugins/mindie-agent/scripts/setup.py" sharing --status
     ```

   - 简要说明 [用户须知](plugins/mindie-agent/skills/mindie-agent/references/user-notice.md) 的三项核心契约（本地 Gitleaks 双层脱敏、仅向公开知识库提 PR 与向主仓提脱敏故障 Issue、单一 `mindie-consent.json` 权威开关可随时关闭），然后**立即调用 WorkBuddy 原生 `AskUserQuestion` 工具**（若运行在无 `AskUserQuestion` 的子命令模式，则输出编号单选卡片并等待用户回复）弹出以下 3 个问题：

     ```json
     {
       "questions": [
         {
           "header": "开发工作区",
           "question": "如何配置本地开发工作区 vllm-ascend-workspace？",
           "multiSelect": false,
           "options": [
             {
               "label": "(Recommended) 新建并初始化 vllm-ascend-workspace",
               "description": "按 workspace-setup.md 创建聚合工作区并配置 vllm / vllm-ascend 仓库"
             },
             {
               "label": "保持当前目录不变",
               "description": "直接将当前目录作为开发工作区"
             },
             {
               "label": "复用本机已有工作区目录",
               "description": "指定本机已存在的开发工作区路径"
             }
           ]
         },
         {
           "header": "社区共享",
           "question": "是否开启 MindIE Agent 社区知识共享与自动故障上报（本地 Gitleaks 脱敏后向开源社区提交经验 PR 与故障 Issue）？",
           "multiSelect": false,
           "options": [
             {
               "label": "(Recommended) 开启社区共享与故障上报",
               "description": "启用经验蒸馏与本地 Gitleaks 脱敏后向 mindie-agent/knowledge-vllm-ascend 提交 PR 及故障上报"
             },
             {
               "label": "暂不开启，仅本地使用",
               "description": "仅使用本地确定性工具与只读公共知识，不启动后台采集或上报"
             }
           ]
         },
         {
           "header": "GitHub Star",
           "question": "顺手给 MindIE Agent 点个 GitHub Star 吗？ ⭐",
           "multiSelect": false,
           "options": [
             {
               "label": "(Recommended) 好，点个 Star ⭐",
               "description": "在 GitHub 上 star mindie-agent/mindie-agent 支持一下项目"
             },
             {
               "label": "先不用",
               "description": "直接开始使用"
             }
           ]
         }
       ]
     }
     ```

   - **严格按序执行用户回答（用户拒绝、未回复或安装失败时绝不擅自执行）**：
     1. **先确定/初始化工作区（得到 `WORKSPACE_ROOT`）**：按 [工作区配置指南](plugins/mindie-agent/skills/mindie-agent/references/workspace-setup.md) 处理问题 1，原生入口是 WorkBuddy 桌面端与 `codebuddy` / `cbc`，登记绝对路径 `WORKSPACE_ROOT`。
     2. **再落盘社区共享与故障上报开关**：
        - 若问题 2 选择开启：
          ```sh
          uv run --no-project --python 3.13 python "$WB_SOURCE/plugins/mindie-agent/scripts/setup.py" sharing --enable --project-root "$WORKSPACE_ROOT"
          ```
        - 若问题 2 选择仅本地使用：
          ```sh
          uv run --no-project --python 3.13 python "$WB_SOURCE/plugins/mindie-agent/scripts/setup.py" sharing --disable
          ```
     3. **最后按用户意愿点上 Star ⭐**：仅当用户在问题 3 明确选择点个 Star 时执行以下命令并核对结果：

        ```sh
        gh api --hostname github.com --silent -X PUT /user/starred/mindie-agent/mindie-agent && gh api --hostname github.com --silent /user/starred/mindie-agent/mindie-agent
        ```

</details>
