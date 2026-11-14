# mindie-agent-wb 0.1.12

[English](mindie-agent-wb-0.1.12.en.md)

- **Windows 11（CP936 / PowerShell 5.1 / Smart App Control）与 WSL2 全链路兼容加固及三平台 CI 门禁（`bounded.py`、`paths.py`、`mindie_launch.py`、`wb_install.py`、`organizer.py`、`wb_transcript.py`）**：将 `hooks.json`、`mcp.json` 与 `.mcp.json` 统一为跨平台 `python -I -X utf8` 并在安装期固化已校验解释器路径；新增 Win32 Job Object 孙进程树回收、WSL2 `/mnt/<drive>` 与 `WindowsApps` 0 字节桩过滤、PowerShell 5.1 `& ` 调用提示与 UTF-8 BOM `--input-file` 输入、HTTPS 代理隧道与发送后传输异常精确判别、UNC `file://` 及 `\\?\UNC\` 路径规范化，并将 `ubuntu-latest`、`macos-latest` 与 `windows-latest` 全部纳入 `release-ready` 必过 CI 矩阵。
