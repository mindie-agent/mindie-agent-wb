# MindIE Agent for WorkBuddy `v0.1.5` Release Notes

<a href="mindie-agent-wb-0.1.5.md">中文</a> | <b>English</b> · <a href="../README.en.md">Back to Documentation Index</a>

`v0.1.5` enforces Windows zero-console-popup guardrails (`subprocess.CREATE_NO_WINDOW`) across `mindie-agent-wb` background subprocesses and upgrades shared runtime components to `mindie-knowledge 0.13.12`, `mindie-diagnostics 0.5.6`, `remote-dev 0.11.5`, and `mindie-coordinator 0.7.4`.

## Highlights (Human-Readable Summary)

1. **Windows Zero-Console-Popup Subprocess Guardrails**:
   - Enforces `subprocess.CREATE_NO_WINDOW` in `plugins/mindie-agent/scripts/bounded.py` and `mcp_bridge.py` so background auto-update checks and component bridges never flash a transient Windows `conhost.exe` window.
2. **Upgraded Shared Component Baseline**:
   - `mindie-knowledge 0.13.12` (`1840bd4e238e718676768489058917818d18cd3c`)
   - `remote-dev 0.11.5` (`131b24533479e9cefc572144adccdcb345f70846`)
   - `mindie-diagnostics 0.5.6` (`9b331956b00737dd6d8c956da3416316f2de1ddf`)
   - `mindie-debugger 0.1.0` (`e9406b2b178335190d5cb59dff4d6985894497ba`)
   - `mindie-profiler 0.5.6` (`79d97e75bff7351b0472b93c03b072c1cf47f5e7`)
   - `mindie-sim 0.9.3` (`dbadff04b60937a0722b0a227cdad01beed083cd`)
   - `mindie-coordinator 0.7.4` (`14d7cec7247bdcbea9b9878db4770e3a05a6ea41`)
   - `npu-top 0.3.1` (`8f0cb3ec66393812e90e118cd73704070c6027a2`)
