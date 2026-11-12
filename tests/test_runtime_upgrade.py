"""Tier 3: a formal old runtime keeps serving until a compatible pair activates."""
import asyncio
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
import setup
from bounded import run
from paths import config_path, state_dir

pytestmark = pytest.mark.skipif(
    os.environ.get("MINDIE_TEST_RUNTIME_UPGRADE") != "1",
    reason="explicit old-runtime acceptance requires MINDIE_TEST_RUNTIME_UPGRADE=1")

ROOT = Path(__file__).resolve().parents[1]
IS_AGY = ROOT.name.endswith("agy")
HOST = "AGY" if IS_AGY else "WB"
OLD_TAG = "v0.1.6" if IS_AGY else "v0.1.5"
OLD_TARGETS = {
    "mindie-knowledge": ("knowledge", "1840bd4e238e718676768489058917818d18cd3c"),
    "remote-dev": ("remote-dev", "131b24533479e9cefc572144adccdcb345f70846"),
    "mindie-coordinator": ("coordinator", "14d7cec7247bdcbea9b9878db4770e3a05a6ea41"),
    "mindie-sim": ("sim", "dbadff04b60937a0722b0a227cdad01beed083cd"),
}


def test_old_runtime_pair_survives_prepare_failure_and_upgrades(tmp_path, monkeypatch, request):
    deadline = time.monotonic() + 85.0
    tmp_path = Path(tempfile.mkdtemp(prefix="mindie-upgrade-", dir=ROOT.parent))

    def clean_native_directory():
        resolved = tmp_path.resolve()
        assert resolved.parent == ROOT.parent.resolve() and resolved.name.startswith("mindie-upgrade-")
        # Component generations contain long third-party package paths on Windows.
        shutil.rmtree("\\\\?\\" + str(resolved) if os.name == "nt" else resolved)

    request.addfinalizer(clean_native_directory)
    home, config_home, data_home = (tmp_path / name for name in ("home", "config", "data"))
    for path in (home, config_home, data_home):
        path.mkdir()
    for name, value in {
        "HOME": home, "USERPROFILE": home, "MINDIE_CONFIG_HOME": config_home,
        "MINDIE_DATA_HOME": data_home, "MINDIE_AGENT_CONFIG": config_home / f"{HOST.lower()}.json",
        "MINDIE_DIAGNOSTICS_ROOT": data_home / "diagnostics",
        "MINDIE_DIAGNOSTICS_CONFIG": config_home / "diagnostics.json",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("MINDIE_AGY_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("MINDIE_WB_SOURCE_ROOT", raising=False)

    def invoke(command, stdin="", *, cwd=None):
        remaining = deadline - time.monotonic()
        assert remaining > 0, "old-runtime acceptance exceeded its 85s budget"
        result = run([str(arg) for arg in command], stdin, timeout=remaining,
                     max_output=256 * 1024, env=dict(os.environ), cwd=cwd)
        assert result.returncode == 0, result.stderr + result.stdout
        return result.stdout

    monkeypatch.delenv(f"_MINDIE_{HOST}_REEXEC", raising=False)
    monkeypatch.delenv(f"_MINDIE_{HOST}_SETUP_BOOTSTRAPPED", raising=False)
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path / "remote"))
    monkeypatch.setenv(f"{HOST}_CONVERSATION_ID", "454c6ee9-afea-47be-8188-abb9c7fb1f26")
    if os.name == "nt":
        count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        for offset, (key, value) in enumerate((("http.sslBackend", "schannel"), ("core.longpaths", "true"))):
            monkeypatch.setenv(f"GIT_CONFIG_KEY_{count + offset}", key)
            monkeypatch.setenv(f"GIT_CONFIG_VALUE_{count + offset}", value)
        monkeypatch.setenv("GIT_CONFIG_COUNT", str(count + 2))
    old_python = os.environ.get("MINDIE_TEST_OLD_RUNTIME")
    if not old_python:
        # Use real formal component sources with the CI interpreter's pinned
        # third-party cohort. No installed user host supplies the test runtime.
        frozen = invoke(["uv", "pip", "freeze", "--python", sys.executable]).splitlines()
        requirements = []
        for line in frozen:
            name = re.split(r"[ =@<>&]", line, maxsplit=1)[0].lower()
            if name.startswith("mindie-agent-") or name == "mindie-bench":
                continue
            if name in OLD_TARGETS:
                repository, sha = OLD_TARGETS[name]
                line = f"{name} @ git+https://github.com/mindie-agent/{repository}@{sha}"
            requirements.append(line)
        req = tmp_path / "old-requirements.txt"
        req.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        # The native plugins already override older transitive declarations.
        overrides = tmp_path / "old-overrides.txt"
        overrides.write_text("\n".join(line for line in requirements if " @ git+" in line) + "\n",
                             encoding="utf-8")
        venv = tmp_path / "old-venv"
        invoke(["uv", "venv", str(venv), "--python", sys.executable])
        old_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        invoke(["uv", "--no-config", "pip", "install", "--python", old_python,
                "-r", req, "--overrides", overrides])
    version = invoke([old_python, "-I", "-c",
                      "import importlib.metadata as m; print(m.version('remote-dev'))"]).strip()
    assert version == "0.11.5"
    archive = subprocess.check_output(["git", "archive", OLD_TAG], cwd=ROOT, timeout=10)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as packed:
        packed.extractall(legacy, filter="data")
    # A real global native directory is outside the OS temp tree: the old
    # installer deliberately used different script bindings for temp installs.
    native = home / ".gemini/config/plugins/mindie-agent"
    shutil.copytree(legacy / "plugins/mindie-agent", native)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv(f"{HOST}_PLUGIN_ROOT", str(native))
    scripts = native / "scripts"
    config = config_path()
    invoke([old_python, "-I", scripts / "setup.py", "init", "--config", config], cwd=legacy)
    old_profile = json.loads(config.read_text(encoding="utf-8"))
    old_profile["user_setting"] = "preserve"
    config.write_text(json.dumps(old_profile), encoding="utf-8")
    if IS_AGY:
        assert Path(old_profile["runtime_scripts"]).resolve() == scripts.resolve()
    retained_scripts = Path(old_profile["source_root"]) / "plugins/mindie-agent/scripts"
    before = config.read_bytes()
    previous_payload = (retained_scripts / "mcp_server.py").read_bytes()
    job_state = tmp_path / "remote/existing-job"
    job_state.parent.mkdir(parents=True, exist_ok=True)
    job_state.write_text("owned job remains unchanged", encoding="utf-8")
    new_launcher = scripts / "mindie_launch.py"

    def status(launcher):
        async def conversation():
            remaining = deadline - time.monotonic()
            assert remaining > 0, "old-runtime acceptance exceeded its 85s budget"
            parameters = StdioServerParameters(
                command=str(old_python), args=["-I", str(launcher), "mcp", "remote"],
                env=dict(os.environ))
            with anyio.fail_after(remaining):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "remote_auth", dict(host="auth.example", port=22, action="status"),
                            meta={"workbuddy.ai/conversationId": "454c6ee9-afea-47be-8188-abb9c7fb1f26"})
                        assert result.isError is False, result
                        await session.send_ping()
        asyncio.run(conversation())

    status(scripts / "mindie_launch.py")
    shutil.copytree(ROOT / "plugins/mindie-agent", native, dirs_exist_ok=True)
    status(new_launcher)  # New native entry serves the profile's previous pair.

    def reject_candidate(*args, **kwargs):
        raise RuntimeError("candidate preparation rejected")
    with monkeypatch.context() as fault:
        fault.setattr(setup, "ensure_runtime_python", reject_candidate)
        with pytest.raises(RuntimeError, match="candidate preparation rejected"):
            setup.ensure_default_profile(config=config)
    assert config.read_bytes() == before
    assert (retained_scripts / "mcp_server.py").read_bytes() == previous_payload
    status(new_launcher)

    invoke([old_python, "-I", ROOT / "plugins/mindie-agent/scripts/setup.py",
            "init", "--config", config])
    active = json.loads(config.read_text(encoding="utf-8"))
    assert Path(active["python"]) != Path(old_python)
    assert active["runtime_scripts"] != str(scripts)
    assert active["user_setting"] == "preserve"
    assert active["state_dir"] == old_profile["state_dir"]
    assert (retained_scripts / "mcp_server.py").read_bytes() == previous_payload
    assert job_state.read_text(encoding="utf-8") == "owned job remains unchanged"
    invoke([active["python"], "-I", "-c",
            "from remote_dev.mcp.outcome import operation_outcome"])
    status(new_launcher)

