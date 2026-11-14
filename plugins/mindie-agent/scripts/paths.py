"""Canonical MindIE-owned path resolution for the Tencent WorkBuddy (`wb`) adapter.

[Role]
Resolves MindIE-owned configuration, state, admission, consent, and runtime
paths (`wb.json`, `wb.engine.json`, `mindie-consent.json`, `admission.sqlite3`,
`mcp-binds.sqlite3`, `remote-receipts.sqlite3`), plus read-only discovery of the
active WorkBuddy configuration directory (`~/.workbuddy`).

[Invariants]
- All MindIE runtime state, session leases, and SQLite databases live under
  MindIE-owned directories (`config_home`, `data_home`, `state_dir`).
- Atomic file writes use `os.replace()` on regular files in the same directory.
- `which_native` filters Windows `/mnt/<drive>` shims on WSL2 and 0-byte
  `WindowsApps` Store stubs on Windows.

[Timeouts & Locks]
- Pure path resolution and bounded local JSON reads (`<= 64 KiB`); no network
  or subprocess calls.

[Side-Effect Policy]
- Read-only path resolution except when `atomic_write_json` is explicitly called
  on a target path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

MAX_CONFIG_BYTES = 65536
_WSL_MOUNT_RE = re.compile(r"^/mnt/[a-zA-Z](?:/|$)")


def _is_wsl() -> bool:
    return sys.platform == "linux" and (
        "WSL_DISTRO_NAME" in os.environ or Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()
    )


def _win32_tool_candidates(cmd: str) -> list[Path]:
    base = cmd.lower().removesuffix(".exe").removesuffix(".cmd")
    home = Path.home()
    local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    prog_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
    out: list[Path] = []
    if base == "uv":
        out.extend([home / ".local" / "bin" / "uv.exe", local_app / "Microsoft" / "WinGet" / "Links" / "uv.exe"])
        winget_pkgs = local_app / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.is_dir():
            out.extend(sorted(winget_pkgs.glob("astral-sh.uv_*/uv.exe")))
    elif base == "git":
        out.extend([prog_files / "Git" / "cmd" / "git.exe", local_app / "Programs" / "Git" / "cmd" / "git.exe"])
    elif base == "gh":
        out.extend([prog_files / "GitHub CLI" / "gh.exe", local_app / "Programs" / "GitHub CLI" / "gh.exe"])
    elif base == "gitleaks":
        out.append(local_app / "Microsoft" / "WinGet" / "Links" / "gitleaks.exe")
        winget_pkgs = local_app / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.is_dir():
            out.extend(sorted(winget_pkgs.glob("Gitleaks.Gitleaks_*/gitleaks.exe")))
    return out


def which_native(cmd: str, path: str | None = None) -> str | None:
    """Resolve `cmd` without picking Windows `/mnt/<drive>` shims on WSL2 or WindowsApps 0-byte stubs."""
    search_path = os.environ.get("PATH", "") if path is None else path
    if _is_wsl():
        filtered = os.pathsep.join(
            entry for entry in search_path.split(os.pathsep) if entry and not _WSL_MOUNT_RE.match(entry)
        )
        found = shutil.which(cmd, path=filtered)
        if found and (found.lower().endswith((".exe", ".cmd", ".bat", ".ps1")) or _WSL_MOUNT_RE.match(found)):
            return None
        return found
    found = shutil.which(cmd, path=search_path)
    if os.name == "nt" and found:
        try:
            if "windowsapps" in found.replace("/", "\\").lower() and Path(found).stat().st_size == 0:
                found = None
        except TimeoutError:
            raise
        except OSError:
            found = None
    if os.name == "nt" and found is None and path is None:
        for cand in _win32_tool_candidates(cmd):
            if cand.is_file():
                return str(cand)
    return found


def plugin_root() -> Path:
    for env_key in ("WB_PLUGIN_ROOT", "CODEBUDDY_PLUGIN_ROOT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        explicit = os.environ.get(env_key)
        if explicit and os.path.isabs(explicit):
            return Path(explicit).resolve()
    return Path(__file__).resolve().parent.parent


def repo_root(config: str | Path | None = None) -> Path:
    explicit = os.environ.get("MINDIE_WB_SOURCE_ROOT")
    if explicit and os.path.isabs(explicit):
        ep = Path(explicit).resolve()
        if (ep / "pyproject.toml").is_file():
            return ep
    candidate = plugin_root().parent.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate
    cfg = config_path(config)
    if cfg.is_file():
        try:
            data = read_json(cfg)
            sr = data.get("source_root")
            if isinstance(sr, str) and os.path.isabs(sr) and (Path(sr) / "pyproject.toml").is_file():
                return Path(sr).resolve()
        except ValueError:
            pass
    retained = data_home() / "wb-source"
    if (retained / "pyproject.toml").is_file():
        return retained
    return candidate


def apply_enterprise_transport_env(target_env: dict[str, str] | None = None) -> dict[str, str]:
    """Configure enterprise TLS trust, non-interactive GCM fast-path, and Windows Git longpaths/LF."""
    env = os.environ if target_env is None else target_env
    env.setdefault("UV_SYSTEM_CERTS", "true")
    env.setdefault("UV_NATIVE_TLS", "true")
    env.setdefault("UV_NO_PROGRESS", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "never")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    git_pairs = (
        ("core.longpaths", "true"),
        ("core.autocrlf", "false"),
        ("credential.https://github.com.provider", "github"),
    )
    if "GIT_CONFIG_COUNT" not in env:
        env["GIT_CONFIG_COUNT"] = str(len(git_pairs))
        for idx, (k, v) in enumerate(git_pairs):
            env[f"GIT_CONFIG_KEY_{idx}"] = k
            env[f"GIT_CONFIG_VALUE_{idx}"] = v
    else:
        try:
            count = max(0, int(env.get("GIT_CONFIG_COUNT", "0")))
        except ValueError:
            count = 0
        existing_keys = {env.get(f"GIT_CONFIG_KEY_{i}") for i in range(count)}
        for k, v in git_pairs:
            if k not in existing_keys:
                env[f"GIT_CONFIG_KEY_{count}"] = k
                env[f"GIT_CONFIG_VALUE_{count}"] = v
                existing_keys.add(k)
                count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
    return dict(env)


def config_home() -> Path:
    explicit = os.environ.get("MINDIE_CONFIG_HOME")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    return (Path.home() / ".config" / "mindie-agent").resolve()


def data_home() -> Path:
    explicit = os.environ.get("MINDIE_DATA_HOME")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    return (Path.home() / ".local" / "share" / "mindie-agent").resolve()


def config_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env_path = os.environ.get("MINDIE_AGENT_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return config_home() / "wb.json"


def engine_config_path(config: str | Path | None = None) -> Path:
    cfg = config_path(config)
    if cfg.is_file():
        try:
            data = read_json(cfg)
            engine = data.get("engine_config")
            if isinstance(engine, str) and os.path.isabs(engine):
                return Path(engine)
        except ValueError:
            pass
    return cfg.with_name(cfg.stem + ".engine.json")


def consent_path(config: str | Path | None = None) -> Path:
    return config_path(config).parent / "mindie-consent.json"


def community_config_path(config: str | Path | None = None) -> Path:
    return config_path(config).parent / "mindie-community.json"


def diagnostics_config_path(config: str | Path | None = None) -> Path:
    explicit = os.environ.get("MINDIE_DIAGNOSTICS_CONFIG")
    if explicit and os.path.isabs(explicit):
        return Path(explicit).resolve()
    return config_path(config).parent / "diagnostics.json"


def state_dir(config: str | Path | None = None) -> Path:
    cfg = config_path(config)
    if cfg.is_file():
        try:
            data = read_json(cfg)
            sd = data.get("state_dir")
            if isinstance(sd, str) and os.path.isabs(sd):
                return Path(sd)
        except ValueError:
            pass
    return data_home() / "wb-state"


def admission_path(config: str | Path | None = None) -> Path:
    cfg = config_path(config)
    if cfg.is_file():
        try:
            data = read_json(cfg)
            adm = data.get("admission_path")
            if isinstance(adm, str) and os.path.isabs(adm):
                return Path(adm)
        except ValueError:
            pass
    return state_dir(config) / "admission.sqlite3"


def bind_db_path(config: str | Path | None = None) -> Path:
    return state_dir(config) / "mcp-binds.sqlite3"


def remote_receipts_path(config: str | Path | None = None) -> Path:
    return state_dir(config) / "remote-receipts.sqlite3"


def workbuddy_config_dir(config: str | Path | None = None) -> Path:
    """Return the active WorkBuddy configuration root (`~/.workbuddy`)."""
    for env_key in ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR"):
        env_dir = os.environ.get(env_key)
        if env_dir and os.path.isabs(env_dir):
            return Path(env_dir).resolve()
    if config is not None or config_path(config).is_file():
        try:
            data = read_json(config_path(config))
            custom = data.get("workbuddy_config_dir")
            if isinstance(custom, str) and os.path.isabs(custom):
                return Path(custom).resolve()
        except ValueError:
            pass
    return (Path.home() / ".workbuddy").resolve()


def workbuddy_cli_bin() -> str | None:
    """Discover the bundled WorkBuddy / CodeBuddy CLI binary path across macOS, Linux, and Windows."""
    explicit = os.environ.get("MINDIE_WB_BIN")
    if explicit and os.path.isabs(explicit) and Path(explicit).is_file():
        return str(Path(explicit).resolve())
    for tool_name in ("codebuddy", "cbc", "workbuddy"):
        found = which_native(tool_name)
        if found:
            return str(Path(found).resolve())
    home = Path.home()
    candidates = [
        Path("/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"),
        home / "Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
    ]
    if os.name == "nt":
        local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        prog_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        for base in (local_app / "Programs" / "WorkBuddy", prog_files / "WorkBuddy"):
            candidates.extend(
                [
                    base / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy.cmd",
                    base / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy.exe",
                    base / "bin" / "codebuddy.cmd",
                    base / "bin" / "codebuddy.exe",
                ]
            )
    for cand in candidates:
        if cand.is_file():
            return str(cand.resolve())
    return None


def read_json(path: str | Path) -> dict:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"MindIE metadata file not found: {target}")
    with target.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"MindIE metadata exceeds {MAX_CONFIG_BYTES} byte limit: {target}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"MindIE metadata is malformed JSON: {target}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"MindIE metadata must be a JSON object: {target}")
    return parsed


def atomic_write_json(path: str | Path, payload: dict) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(serialized)
            stream.flush()
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except TimeoutError:
            raise
        except OSError:
            pass
        raise
    return target
