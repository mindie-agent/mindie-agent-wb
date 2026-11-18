# mindie-agent-wb 0.1.19：大减法与 WorkBuddy UX 升级

[English](mindie-agent-wb-0.1.19.en.md)

- **大减法：删除 16 个重复运行时模块，仅保留 6 个薄脚本**：`plugins/mindie-agent/scripts/` 现在只含 `mindie_launch.py`、`bridge.py`、`mcp_server.py`、`wb_transcript.py`、`wb_install.py`、`setup.py`（合计约 1,060 行，硬上限 1,200 行）。`host_adapter.py`、`wb_native_compat.py`、`knowledge_cli.py`、`admission.py`、`identity.py`、`paths.py`、`storage_lifecycle.py` 等全部移除；运行时直接委托 `mindie-harness v0.1.3`。
- **收敛为 15 项 `wb` 远端工具**：`remote-dev v0.11.14` 原生提供 `remote_Read`、`remote_Write`、`remote_Edit`、`remote_MultiEdit`、`remote_Bash`、`remote_Glob`、`remote_Grep`、`remote_TaskOutput`、`remote_NotebookEdit`、`remote_TaskStop`、`remote_ViewImage`，以及 `remote_auth`、`remote_probe`、`remote_artifact_pull`、`remote_artifact_push`。不再暴露 `remote_BashOutput` / `remote_KillShell`。
- **三个 MCP 表面严格不相交**：`mindie-remote-dev`（15 项 `wb` 工具）、`mindie-tools`（11 项确定性组件 + `mindie_doctor`，零 `knowledge_*` 重复）、`mindie-knowledge`（5 项 `knowledge_*`）。三者均支持零 `_meta`、零 `PreToolUse` 预绑定的标准 `tools/call`。
- **`mindie_doctor` 就绪检查**：新增对 profile、活动运行时、组件绑定与可选远端可达性的统一只读检查，安装后应运行一次；失败只报告一次，不阻塞后续任务。
- **静默 Hook 与 `<100ms` 快速路径**：`UserPromptSubmit` / `PreToolUse` / `SessionStart` 与未配置的 `Stop` 在纯标准库路径内完成，返回 `{"suppressOutput": true}`，零 SQLite 创建；`PreToolUse` 对伪造 `_session_id` / `mindie_activation` 明确 `permissionDeny`，绝不自动批准宿主权限提示。
- **授权优先 `Stop` 与撤销保留**：仅当 `mindie-consent.json` 选择 `contribute` / `local-only`、`mindie-community.json` 已启用且工作区位于绝对 `project_roots` 内时，才激活默认租约（`not_before=0.0`）并调用 `capture_hook(..., timeout=6.0)`；显式 `inactive` 撤销永久保留。
- **Receipt Honesty**：knowledge 调用在 harness 报 `knowledge_unavailable` 时回退到 `mindie_knowledge.loop.transport`，对 `distill_selected` 与本地 `Admission.finish` 的 `OSError` / `sqlite3.Error` 保留 `isError=False` 并附 `receipt_warning` + `receipt_unconfirmed=True`。
- **安装与首启 UX**：新增 `uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host wb` 单行安装；`gh` 可选、绝不是阻塞项；3 问动态 `AskUserQuestion` 引导（工作区、按 `gh` 登录态动态推荐 `contribute` 或 `local-only`、一步 `remote_auth(..., save_default=True, discover=True)` SSH NPU 连接，终端模式可用 `--password` 隐藏输入）；成功后运行 `mindie_doctor`，并以一行礼貌 Star 提示收尾。
- **安装器与冷启动**：新增 `plugins/mindie-agent/runtime-requirements.txt`（10 行，与 `pyproject.toml` 组件 Git pin 一致），使 `~/.workbuddy/plugins/cache/...` 缓存副本也能冷引导；`wb_install.py` 自带 LF 清理、验证 Python 固定、Connector 市场注册与旧版本目录清理。
- **非阻塞任务连续性规则**：`remote-dev`、`coordinator`、`knowledge`、`mindie_doctor` 是加速器，绝不是路障；任何工具失败只报告一次（先核对不确定写入），随后立即改用原生 `ssh`/`scp`、本地脚本或其他已授权工具继续任务。
