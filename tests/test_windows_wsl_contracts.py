"""Tier 0 & Tier 1 regression tests for Windows 11, PowerShell 5.1, CP936, and WSL2 contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import ssl
import sys
import time
import types

import pytest

import bounded
import bridge
import diagnostic_support
import knowledge_cli
import organizer
import paths
import setup
import wb_transcript


SESSION_ID = "454c6ee9-afea-47be-8188-abb9c7fb1f26"


def _nt_os() -> types.SimpleNamespace:
    return types.SimpleNamespace(**{**os.__dict__, "name": "nt"})


def test_which_native_filters_wsl_mnt_c_shims_and_windowsapps_stubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mnt_c = tmp_path / "mnt_c_bin"
    linux_bin = tmp_path / "usr_bin"
    mnt_c.mkdir()
    linux_bin.mkdir()
    # CRLF Windows npm/batch shim on /mnt/c is rejected on WSL2
    win_uv = mnt_c / "uv"
    win_uv.write_bytes(b"#!/bin/sh\r\nexit 0\r\n")
    win_uv.chmod(0o755)
    linux_uv = linux_bin / "uv"
    linux_uv.write_bytes(b"#!/bin/sh\nexit 0\n")
    linux_uv.chmod(0o755)
    # Genuine Linux LF script / ELF binary on /mnt/c is preserved when no Windows shim marker exists
    mnt_linux_tool = mnt_c / "linux-only-tool"
    mnt_linux_tool.write_bytes(b"\x7fELF-linux-binary")
    mnt_linux_tool.chmod(0o755)

    monkeypatch.setattr(paths, "_is_wsl", lambda: True)
    monkeypatch.setattr(paths, "_WSL_MOUNT_RE", __import__("re").compile(f"^{__import__('re').escape(str(mnt_c))}(?:[/\\\\]|$)"))
    if os.name == "nt":
        monkeypatch.setattr(
            paths.shutil,
            "which",
            lambda cmd, path=None: next(
                (str(Path(d) / cmd) for d in (path or "").split(os.pathsep) if d and (Path(d) / cmd).is_file()),
                None,
            ),
        )
    search = os.pathsep.join([str(mnt_c), str(linux_bin)])
    assert paths.which_native("uv", path=search) == str(linux_uv)
    assert paths.which_native("uv", path=str(mnt_c)) is None
    assert paths.which_native("linux-only-tool", path=str(mnt_c)) == str(mnt_linux_tool)

    # WindowsApps 0-byte Store execution alias stub is rejected on Windows without mutating global os.name
    win_apps = tmp_path / "WindowsApps"
    win_apps.mkdir()
    zero_stub = win_apps / "python.exe"
    zero_stub.write_bytes(b"")
    real_winget = tmp_path / "winget" / "uv.exe"
    real_winget.parent.mkdir()
    real_winget.write_bytes(b"MZ-valid-binary")

    monkeypatch.setattr(paths, "_is_wsl", lambda: False)
    monkeypatch.setattr(paths, "os", _nt_os())
    monkeypatch.setattr(paths.shutil, "which", lambda cmd, path=None: str(zero_stub) if cmd == "python" else None)
    monkeypatch.setattr(paths, "_win32_tool_candidates", lambda cmd: [real_winget] if cmd == "uv" else [])
    assert paths.which_native("python", path=str(win_apps)) is None
    assert paths.which_native("uv") == str(real_winget)

    # Windows npm `.cmd` wrapper unwraps to `[node.exe, cli.js]` so `cmd.exe` never parses prompt metacharacters
    npm_dir = tmp_path / "npm_bin"
    pkg_dir = npm_dir / "node_modules" / "codebuddy" / "bin"
    pkg_dir.mkdir(parents=True)
    node_exe = npm_dir / "node.exe"
    node_exe.write_bytes(b"MZ-node")
    cli_js = pkg_dir / "cli.js"
    cli_js.write_text("console.log('ok')\n", encoding="utf-8")
    cb_cmd = npm_dir / "codebuddy.cmd"
    cb_cmd.write_text('@ECHO off\r\n"%~dp0\\node.exe"  "%~dp0\\node_modules\\codebuddy\\bin\\cli.js" %*\r\n', encoding="utf-8")
    assert paths._unwrap_cli_argv(cb_cmd) == [str(node_exe.resolve()), str(cli_js.resolve())]
    opaque_cmd = npm_dir / "opaque.cmd"
    opaque_cmd.write_text("@echo off\r\necho unsafe\r\n", encoding="utf-8")
    assert paths._unwrap_cli_argv(opaque_cmd) is None

    # Smart App Control (WinError 4551): Windows component `.exe` trampolines dispatch via signed `python -I -X utf8 -m`
    import mcp_server

    fake_dbg_exe = tmp_path / "Scripts" / "mindie-debugger.exe"
    fake_dbg_exe.parent.mkdir()
    fake_dbg_exe.write_bytes(b"MZ-unsigned-trampoline")
    monkeypatch.setattr(mcp_server, "os", _nt_os())
    monkeypatch.setattr(mcp_server, "which_native", lambda cmd, path=None: str(fake_dbg_exe))
    assert mcp_server._resolve_component_argv({"python": r"C:\Python\python.exe"}, "debugger") == [
        r"C:\Python\python.exe",
        "-I",
        "-X",
        "utf8",
        "-m",
        "mindie_debugger.cli",
    ]


def test_powershell_activation_hint_and_knowledge_cli_utf8_bom_input_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    ws = tmp_path / "中文工作区"
    ws.mkdir()
    out = bridge.handle_user_prompt_submit(
        {
            "session_id": SESSION_ID,
            "cwd": str(ws),
            "prompt": "/mindie-agent 排查昇腾 A3 双 Die HCCL",
        }
    )
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "& \"" in ctx
    assert "-I -X utf8" in ctx
    assert f'--session "{SESSION_ID}"' in ctx
    assert "--input-file <json_file>" in ctx

    # Simulate PowerShell 5.1 Set-Content -Encoding UTF8 (with UTF-8 BOM \xef\xbb\xbf)
    bom_file = tmp_path / "query_args.json"
    bom_file.write_bytes(b"\xef\xbb\xbf" + json.dumps({"query": "昇腾 A3 双 Die 拓扑", "limit": 2}, ensure_ascii=False).encode("utf-8"))

    seen: dict = {}
    monkeypatch.setattr(
        knowledge_cli,
        "execute_knowledge_tool",
        lambda tool, args, *, session: seen.update(tool=tool, args=args, session=session) or {"hits": []},
    )
    rc = knowledge_cli.main(["knowledge_query", "--session", SESSION_ID, "--input-file", str(bom_file)])
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["isError"] is False
    assert seen["tool"] == "knowledge_query"
    assert seen["session"] == SESSION_ID
    assert seen["args"] == {"query": "昇腾 A3 双 Die 拓扑", "limit": 2}


def test_wb_install_pins_verified_python_into_hooks_and_mcp_manifests(tmp_path: Path):
    cfg = tmp_path / "config" / "wb.json"
    wb_dir = tmp_path / "workbuddy"
    official_mp = wb_dir / "plugins" / "marketplaces" / "workbuddy-connector-plugins-official" / ".codebuddy-plugin"
    official_mp.mkdir(parents=True)
    (official_mp / "marketplace.json").write_text(
        json.dumps({"name": "workbuddy-connector-plugins-official", "plugins": []}),
        encoding="utf-8",
    )

    prof = setup.ensure_default_profile(config=cfg, workspace_root=tmp_path)
    inst = setup.install_workbuddy_plugin(wb_dir=wb_dir, config=cfg, source_root=prof.get("source_root"))
    expected_py = prof["python"]

    for target_dir in (Path(inst["cache_install_path"]), Path(inst["connector_path"])):
        hooks_cfg = json.loads((target_dir / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        for entries in hooks_cfg["hooks"].values():
            cmd_str = entries[0]["hooks"][0]["command"]
            assert "python3 " not in cmd_str
            assert "-I -X utf8" in cmd_str
            assert expected_py in cmd_str

        for mcp_name in ("mcp.json", ".mcp.json"):
            mcp_cfg = json.loads((target_dir / mcp_name).read_text(encoding="utf-8"))
            for srv in mcp_cfg["mcpServers"].values():
                assert srv["command"] == expected_py
                assert srv["args"][:3] == ["-I", "-X", "utf8"]


def test_organizer_proxy_tunnel_vs_post_send_transmission_honesty(monkeypatch: pytest.MonkeyPatch):
    model = organizer.NativeSummaryModel()
    conn = organizer._TrackedHTTPSConnection("api.example.com", 443, owner=model)
    sent_chunks: list[bytes] = []
    monkeypatch.setattr(organizer.http.client.HTTPSConnection, "send", lambda self, data: sent_chunks.append(data))
    monkeypatch.setattr(
        organizer.http.client.HTTPSConnection,
        "_tunnel",
        lambda self: self.send(b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n"),
    )
    conn._tunnel()
    assert model.transmitted is False
    conn.send(b'{"prompt":"hello"}')
    assert model.transmitted is True

    # Mid-stream SSLError after request body transmitted -> outcome_unknown (never retried)
    req, _ = __import__("test_wb_organizer_and_update")._sample_request()
    monkeypatch.setenv("MINDIE_SUMMARY_ENDPOINT", "https://api.example.com/v1/summarize")

    def fake_open(request_obj, timeout=None):
        raise ssl.SSLError("TLS record bad_record_mac mid-response")

    def build_post_send_opener(*_handlers):
        class _Opener:
            def open(self, request_obj, timeout=None):
                for h in _handlers:
                    if hasattr(h, "_owner"):
                        h._owner.transmitted = True
                return fake_open(request_obj, timeout=timeout)

        return _Opener()

    monkeypatch.setattr(organizer.urllib.request, "build_opener", build_post_send_opener)
    post_out = organizer.execute_request(req)
    assert post_out["status"] == "outcome_unknown"
    assert post_out["model_calls"] == 1

    # Pre-send SSLError during handshake -> failed / not_called
    def build_pre_send_opener(*_handlers):
        class _Opener:
            def open(self, request_obj, timeout=None):
                return fake_open(request_obj, timeout=timeout)

        return _Opener()

    monkeypatch.setattr(organizer.urllib.request, "build_opener", build_pre_send_opener)
    pre_out = organizer.execute_request(req)
    assert pre_out["status"] == "failed"
    assert pre_out["model_calls"] == 0
    assert pre_out["billing_status"] == "not_called"


def test_unc_file_urls_and_extended_unc_transcript_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(diagnostic_support, "os", _nt_os())
    unc_path = diagnostic_support._file_url_to_path("file://wsl.localhost/Ubuntu/home/dev/repo")
    assert str(unc_path) in {r"\\wsl.localhost\Ubuntu\home\dev\repo", "//wsl.localhost/Ubuntu/home/dev/repo"}
    drive_path = diagnostic_support._file_url_to_path("file:///C:/Users/dev/code/debugger")
    assert str(drive_path).replace("\\", "/") == "C:/Users/dev/code/debugger"

    monkeypatch.setattr(wb_transcript, "os", _nt_os())
    assert wb_transcript._strip_win_extended_prefix(r"\\?\UNC\server\share\proj\a.jsonl") == r"\\server\share\proj\a.jsonl"
    assert wb_transcript._strip_win_extended_prefix(r"\\?\C:\Users\dev\a.jsonl") == r"C:\Users\dev\a.jsonl"

    # Verify incremental transcript read survives extended-length path differences
    monkeypatch.undo()
    tfile = tmp_path / f"{SESSION_ID}.jsonl"
    line1 = json.dumps(
        {
            "type": "message",
            "timestamp": 1761899000000,
            "message": {"role": "user", "content": "定位昇腾 HCCL 超时"},
        },
        ensure_ascii=False,
    ) + "\n"
    tfile.write_text(line1, encoding="utf-8")
    first = wb_transcript.read_material(tfile, 0, session_id=SESSION_ID)
    assert first["status"] == "ok"
    prefixed = (f"\\\\?\\{tfile.resolve()}" if os.name == "nt" else str(tfile.resolve()))
    expected_id = wb_transcript.FileIdentity.unserialize(first["identity"], prefixed)
    assert expected_id is not None
    second = wb_transcript.read_material(tfile, first["end"], session_id=SESSION_ID, expected=expected_id)
    assert second["status"] == "unchanged"


def test_bounded_run_kills_grandchild_holding_stdout_pipe_and_tracks_taskkill_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        bounded.run(
            [
                sys.executable,
                "-I",
                "-c",
                (
                    "import subprocess, sys, time; "
                    "subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)']); "
                    "time.sleep(30)"
                ),
            ],
            timeout=0.6,
        )
    assert time.monotonic() - started < 5.0

    # Verify non-{0, 128} taskkill exit code sets cleanup_failed=True on Windows fallback path
    class _FakePopen:
        pid = 99999

        def poll(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 0

    fake_proc = _FakePopen()
    monkeypatch.setattr(bounded, "os", _nt_os())
    monkeypatch.setattr(bounded, "_STDLIB_POPEN_CLS", _FakePopen)
    monkeypatch.setattr(
        bounded,
        "_STDLIB_POPEN",
        lambda *_a, **_k: types.SimpleNamespace(wait=lambda timeout=None: 5, kill=lambda: None),
    )
    assert bounded._kill_process_tree(fake_proc, job_handle=None, wait_timeout=1.0) is True

    monkeypatch.setattr(
        bounded,
        "_STDLIB_POPEN",
        lambda *_a, **_k: types.SimpleNamespace(wait=lambda timeout=None: 128, kill=lambda: None),
    )
    assert bounded._kill_process_tree(fake_proc, job_handle=None, wait_timeout=1.0) is False

    # Verify argparse-compatible `--timeout` parsing and background update fast-child race guard
    import component_update
    import mindie_launch

    assert mindie_launch._parse_cli_timeout(["update", "--timeout", "5", "--timeout", "540"]) == 540.0
    assert mindie_launch._parse_cli_timeout(["update", "--time", "600"]) == 600.0

    monkeypatch.delenv("MINDIE_CONFIG_HOME", raising=False)
    cfg = tmp_path / "wb_cfg" / "wb.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    data_dir = tmp_path / "wb_data"
    data_dir.mkdir()
    monkeypatch.setattr(component_update, "data_home", lambda: data_dir)
    status_file = data_dir / "wb-component-update-status.json"

    def _fast_child_popen(_cmd, **kwargs):
        rid = kwargs["env"]["MINDIE_WB_UPDATE_RUN_ID"]
        paths.atomic_write_json(
            status_file,
            {"checked_at": time.time(), "status": "up_to_date", "run_id": rid},
        )
        return types.SimpleNamespace(pid=43210)

    monkeypatch.setattr(component_update.subprocess, "Popen", _fast_child_popen)
    assert component_update.trigger_background_update_if_due(config=cfg) is True
    assert json.loads(status_file.read_text(encoding="utf-8"))["status"] == "up_to_date"
