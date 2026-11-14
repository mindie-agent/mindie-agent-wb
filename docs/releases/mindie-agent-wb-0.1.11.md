# mindie-agent-wb 0.1.11

[English](mindie-agent-wb-0.1.11.en.md)

- **Diagnostics 2.0 统一诊断支持、崩溃收据鉴权与收据诚实性升级（`diagnostic_support.py`、`mcp_server.py`、`component_update.py`）**：接入 WorkBuddy / CodeBuddy 宿主统一故障捕获与脱敏上报、40-SHA `diagnostic-build.json` 源码溯源、子进程崩溃收据校验及主操作成功后的 `receipt_warning`/`receipt_unconfirmed=True` 收据诚实性保护，同步升级 `diagnostics v0.5.11`、`debugger v0.1.2`、`knowledge v0.13.17`、`remote-dev v0.11.10`、`npu-top v0.3.3` 与 `coordinator v0.7.9`。
