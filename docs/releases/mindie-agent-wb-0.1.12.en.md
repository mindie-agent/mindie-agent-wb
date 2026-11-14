# mindie-agent-wb 0.1.12

[中文](mindie-agent-wb-0.1.12.md)

- **Windows 11 (CP936 / PowerShell 5.1 / Smart App Control) & WSL2 Compatibility Hardening and Tri-OS CI Gate (`bounded.py`, `paths.py`, `mindie_launch.py`, `wb_install.py`, `organizer.py`, `wb_transcript.py`)**: Unifies `hooks.json`, `mcp.json`, and `.mcp.json` on `python -I -X utf8` while pinning the verified runtime interpreter on install; adds Win32 Job Object process-tree cleanup, WSL2 `/mnt/<drive>` and 0-byte `WindowsApps` stub filtering, PowerShell 5.1 `& ` invocation hints plus UTF-8 BOM `--input-file` support, pre-send vs post-send HTTPS proxy tunnel transport classification, UNC `file://` and `\\?\UNC\` path canonicalization, and full `[ubuntu-latest, macos-latest, windows-latest]` CI enforcement behind `release-ready`.
