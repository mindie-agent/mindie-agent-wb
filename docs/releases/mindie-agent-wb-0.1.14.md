# mindie-agent-wb 0.1.14：存储生命周期治理与外部缓存解绑

[English](mindie-agent-wb-0.1.14.en.md)

- **组件更新互斥与 WorkBuddy 外部缓存解绑（`component_update.py`、`wb_install.py`、`storage_lifecycle.py`）**：组件更新在 `<data_home>/wb-component-update.lock` 上获取跨进程互斥锁，竞争方返回 `status="busy"` 而不是覆盖状态或重复构建；安装与升级后仅扫描已存在的 MindIE WorkBuddy 插件缓存，原子刷新 `hooks.json`、`mcp.json`、`.mcp.json` 中已钉死的 Python 解释器路径，并保留当前活跃版本与最近 1 个回滚版本，其余旧缓存目录经两阶段重命名静默回收，不触碰未注册账号状态。
- **存量历史包袱静默回收**：切换成功后按有界预算调用 `mindie_knowledge.storage.prune_component_runtimes` 与 `sweep_legacy_installations`，自动回收无 `components.json` 的半成品失败 `.venv`、超量 `wb-state/component-runtimes/*`、旧 `wb-source/*` 及遗留 `.trash-*`。跨插件借用的兄弟宿主解释器、`wb.json["source_root"]` 外部 Git checkout、活租约子进程和 Git 元数据始终保留；局部占用或预算到期只记录 `receipt_warning`/`receipt_unconfirmed=True`，主流程仍为 `status="updated"`。
- **常驻 MCP 共享运行租约与冷启动缓存落盘（`mcp_server.py`、`setup.py`）**：常驻 MCP facade 与单请求子进程对 `owning_dir(sys.executable)` 持有 `acquire_runtime_lease(...)` 共享租约，避免回收器误删正在服务的组件代；首次构建 `wb-venv` 时若未显式设置 `UV_CACHE_DIR` 且默认缓存跨卷，则自动使用同卷 `<data_home>/.uv-cache`。
- **测试隔离与临时目录治理（`tests/conftest.py`、`tests/test_runtime_upgrade.py`、`tests/test_storage_lifecycle_wb.py`）**：新增 `HOME`、`USERPROFILE`、`MINDIE_*`、`TMPDIR`、`UV_CACHE_DIR` 全隔离；Windows `mwb-*` 临时目录在 `finally` 中带重试清理；旧运行时验收改用 pytest 隔离 `tmp_path`，不再向仓库父目录写入多代虚拟环境。新增跨平台存储生命周期回归，覆盖失败 `.venv`、多代运行时/源码、旧插件缓存、解释器指针刷新、借用解释器与活租约保护，并断言 `mcp_server.py` 保持 `< 800` 行。

## 组件基线

| 组件 | 版本 | 锁定 commit |
| :--- | :--- | :--- |
| `mindie-knowledge` | `0.13.22` | `c991b64a1ead277b5ce91b7835337b01aed03998` |
| `remote-dev` | `0.11.12` | `c4d38f99c1ef5aace1c088af07647941adc2d394` |
| `mindie-diagnostics` | `0.5.14` | `cd16baa406070bcd41bbe39a9e488cc8753c60c5` |
| `mindie-coordinator` | `0.7.11` | `33e2c6966d21d62969a2148145e74885dc826e3c` |
| `mindie-profiler` | `0.5.15` | `889b6ef161862938cc07b1b4123c57e302965d48` |
| `mindie-sim` | `0.9.15` | `9dbab02f10843c7b829d6fda7e7b9ad89e4f92e5` |
| `mindie-debugger` | `0.1.3` | `33052a2fc10c7475e4b819577870ca822efcebd0` |
| `npu-top` | `0.3.4` | `4341003c6637e2b8b9512c7b076f80f45019e252` |
