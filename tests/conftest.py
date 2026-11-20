"""Pytest isolation fixtures and pre-0.1.5 substrate test shim for `mindie-agent-wb`."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import types
from typing import Any
import pytest

REAL_HOME = Path.home()
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "mindie-agent" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if importlib.util.find_spec("mindie_harness") is None:
    for sibling in ("harness-ux", "knowledge-ux", "remote-dev-ux", "diagnostics-ux"):
        cand = REPO_ROOT.parent / sibling
        if cand.is_dir() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))


def _install_pre_015_knowledge_binding_shim() -> None:
    """Register faithful `mindie_harness.runtime.knowledge_binding` + loop RPC bridge when `mindie-harness < 0.1.5`."""
    try:
        if importlib.util.find_spec("mindie_harness.runtime.knowledge_binding") is not None:
            return
    except (ImportError, ValueError):
        return
    try:
        import mindie_harness.mcp.gateway as mh_gateway
        import mindie_harness.mcp.knowledge_rpc as mh_krpc
        import mindie_harness.runtime as mh_runtime
        from mindie_harness.atomic import atomic_write_json, read_json_safe
        from mindie_harness.mcp.receipts import apply_receipt_honesty
        from mindie_harness.paths import ProfileRef
        from mindie_harness.process.supervisor import Deadline
    except ImportError:
        return

    adapter_names = {
        "codex": "codex_transcript.py",
        "cc": "transcript.py",
        "kimi": "kimi_transcript.py",
        "dsh": "dsh_transcript.py",
        "agy": "agy_transcript.py",
        "wb": "wb_transcript.py",
        "oc": "opencode_transcript.py",
    }

    def _resolve_transcript_adapter(profile: ProfileRef, plugin_root: Path | None) -> Path:
        target_name = adapter_names.get(profile.host, f"{profile.host}_transcript.py")
        for root in (Path(plugin_root) if plugin_root else None, profile.plugin_root):
            if root is None:
                continue
            for cand in (root / "scripts" / target_name, root / target_name, root / "plugins" / "mindie-agent" / "scripts" / target_name):
                if cand.is_file():
                    return cand.resolve()
        noop = profile.state_dir / "noop_transcript_adapter.py"
        if not noop.is_file():
            noop.parent.mkdir(parents=True, exist_ok=True)
            noop.write_text(
                "from dataclasses import dataclass\n"
                "@dataclass(frozen=True)\n"
                "class FileIdentity:\n"
                "    path: str = ''\n"
                "    def serialize(self) -> str: return '{}'\n"
                "    @staticmethod\n"
                "    def unserialize(text: str, path: str): return None\n"
                "def identify(path): return None\n"
                "def read_material(path, start, **kw): return {'status': 'unchanged', 'start': start, 'end': start, 'text': '', 'records': 0, 'digest': ''}\n",
                encoding="utf-8",
            )
        return noop.resolve()

    def _resolve_redactor(profile: ProfileRef) -> str:
        found = shutil.which("gitleaks")
        if found:
            return str(Path(found).resolve())
        for cache_root in (
            Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")),
            Path.home() / ".cache",
            profile.data_dir / "cache",
        ):
            gdir = cache_root / "mindie-agent" / "gitleaks"
            if gdir.is_dir():
                hit = next((p for p in sorted(gdir.rglob("gitleaks*")) if p.is_file() and p.name.lower() in {"gitleaks", "gitleaks.exe"}), None)
                if hit is not None:
                    return str(hit.resolve())
        exe = "gitleaks.exe" if os.name == "nt" else "gitleaks"
        return str((profile.data_dir / "cache" / "gitleaks" / "8.30.1" / exe).resolve(strict=False))

    def ensure_knowledge_binding(
        profile: ProfileRef,
        *,
        plugin_root: Path | None = None,
        python_executable: str | Path | None = None,
        install_redactor: bool = False,
        retire_previous: bool = False,
        include_default_feed: bool = False,
        already_locked: bool = False,
    ) -> dict[str, Any]:
        from mindie_harness.locks import FileLock
        from mindie_knowledge.loop import domain_catalog
        from mindie_knowledge.loop.store import Store

        lock = None
        if not already_locked:
            profile.config_path.parent.mkdir(parents=True, exist_ok=True)
            lock = FileLock(profile.config_path.with_name(f".{profile.config_path.name}.update.lock"), exclusive=True, timeout=5.0)
            lock.acquire()
        try:
            prev_cfg = read_json_safe(profile.config_path)
            prev = dict(prev_cfg) if isinstance(prev_cfg, Mapping) else {}
            cfg_parent = profile.config_path.parent
            engines_dir = (cfg_parent / "engines" / profile.host).resolve(strict=False)
            auth_dir = (cfg_parent / "domains").resolve(strict=False)
            domains_root = (profile.data_dir / "domains").resolve(strict=False)
            transcript_path = _resolve_transcript_adapter(profile, Path(plugin_root) if plugin_root else None)
            redactor_path = _resolve_redactor(profile)
            default_domain = "vllm-ascend"
            comm_path = (cfg_parent / "mindie-community.json").resolve(strict=False)
            consent_path = profile.consent_path.resolve(strict=False)
            admission_path = profile.admission_db.resolve(strict=False)
            default_engine: dict[str, Any] = {
                "schema": "mindie-engine-config/1",
                "harness": profile.host,
                "domain": default_domain,
                "root": str(domains_root),
                "state_dir": str((profile.state_dir / "engine").resolve(strict=False)),
                "admission_path": str(admission_path),
                "community_config": str(comm_path),
                "settings_path": str(comm_path),
                "capture_mode": "public-transcript",
                "transcript_adapter": str(transcript_path),
                "redactor_executable": redactor_path,
            }
            if include_default_feed or prev.get("feeds"):
                default_engine["feeds"] = [domain_catalog.get_domain_config(default_domain).to_feed()]
            default_text = json.dumps(default_engine, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            default_digest = hashlib.sha256(default_text.encode("utf-8")).hexdigest()[:20]
            default_path = engines_dir / default_digest / f"{default_domain}.engine.json"
            default_path.parent.mkdir(parents=True, exist_ok=True)
            default_path.write_text(default_text, encoding="utf-8")
            store = Store(str(domains_root), default_domain)
            store.close()

            seed_adapter = {
                "engine_config": str(default_path),
                "domain_configs": {
                    k: v
                    for k, v in (prev.get("domain_configs") or {}).items()
                    if isinstance(k, str) and isinstance(v, str) and Path(v).is_file()
                },
            }
            catalog_res = domain_catalog.prepare(
                seed_adapter,
                default_engine,
                config_directory=str(engines_dir),
                authority_directory=str(auth_dir),
                consent_config=str(consent_path),
            )
            domain_configs = dict(catalog_res["domain_configs"])
            domain_configs[default_domain] = str(default_path)
            binding_fields = {
                "host": profile.host,
                "engine_config": str(default_path),
                "domain_configs": domain_configs,
                "community_config": str(comm_path),
                "consent_config": str(consent_path),
                "admission_path": str(admission_path),
            }
            candidate = dict(prev, **binding_fields)
            if python_executable is not None:
                candidate["python"] = str(python_executable)
            if retire_previous and prev.get("engine_config") and Path(str(prev["engine_config"])).is_file():
                try:
                    domain_catalog.retire_changed(prev, candidate, include_default=True)
                except TimeoutError:
                    raise
                except Exception:
                    pass
            if not already_locked:
                atomic_write_json(profile.config_path, candidate)
            return {
                "status": "configured",
                "engine_config": str(default_path),
                "domain_configs": domain_configs,
                "domains": sorted(domain_configs),
                "transcript_adapter": str(transcript_path),
                "redactor_executable": redactor_path,
                "binding_fields": binding_fields,
            }
        finally:
            if lock is not None:
                lock.release()

    def _scope_allows(profile: ProfileRef, cwd: str | None) -> str | None:
        if not cwd or not os.path.isabs(cwd):
            return None
        cfg = read_json_safe(profile.config_path)
        cfg_doc = cfg if isinstance(cfg, Mapping) else {}
        comm_raw = cfg_doc.get("community_config")
        comm_path = Path(comm_raw) if isinstance(comm_raw, str) and os.path.isabs(comm_raw) else profile.config_path.parent / "mindie-community.json"
        comm = read_json_safe(comm_path)
        if not isinstance(comm, Mapping) or comm.get("schema") != "mindie-community-config/1" or comm.get("enabled") is not True:
            return None
        cons_raw = comm.get("consent_config") or cfg_doc.get("consent_config")
        cons_path = Path(cons_raw) if isinstance(cons_raw, str) and os.path.isabs(cons_raw) else profile.consent_path
        cons = read_json_safe(cons_path)
        if (
            not isinstance(cons, Mapping)
            or cons.get("schema") != "mindie-consent/1"
            or cons.get("choice") not in {"contribute", "local-only"}
            or not isinstance(cons.get("choice_revision"), str)
            or not cons["choice_revision"].strip()
        ):
            return None
        roots = comm.get("project_roots")
        if not isinstance(roots, list) or not roots:
            return None
        cand_norm = os.path.normcase(os.path.realpath(cwd))
        for root in roots:
            if isinstance(root, str) and os.path.isabs(root):
                base_norm = os.path.normcase(os.path.realpath(root))
                try:
                    if os.path.commonpath([cand_norm, base_norm]) == base_norm:
                        return str(Path(root).resolve(strict=False))
                except ValueError:
                    continue
        return None

    def run_stop_capture(
        profile: ProfileRef,
        event: Mapping[str, Any],
        *,
        plugin_root: Path | None = None,
        timeout_sec: float = 6.0,
    ) -> dict[str, Any]:
        deadline = Deadline.from_timeout(timeout_sec)
        cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
        session_id = event.get("session_id") if isinstance(event.get("session_id"), str) else None
        transcript_raw = event.get("transcript_path") if isinstance(event.get("transcript_path"), str) else None
        matched_root = _scope_allows(profile, cwd)
        if not session_id or not matched_root or not transcript_raw or not Path(transcript_raw).is_file():
            return {"stage": "inert", "reason": "sharing-disabled"}
        cfg = read_json_safe(profile.config_path)
        if not isinstance(cfg, Mapping) or not isinstance(cfg.get("engine_config"), str) or not Path(cfg["engine_config"]).is_file():
            ensure_knowledge_binding(profile, plugin_root=plugin_root)
            cfg = read_json_safe(profile.config_path)
        engine_path = Path(str((cfg or {}).get("engine_config") or ""))
        if not engine_path.is_file():
            return {"stage": "unavailable", "reason": "configuration"}

        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.cli import capture_hook, load_transcript_adapter
        from mindie_knowledge.loop.handoff import _store_path, prepare_schema

        admission = Admission(profile.admission_db)
        inspected = admission.inspect(session_id)
        if inspected.get("status") == "inactive":
            return {"stage": "inert", "reason": "not-activated"}
        lease = admission.activate(session_id, project_root=str(Path(cwd).resolve(strict=False)), root_session=session_id, not_before=0.0)
        engine_doc = read_json_safe(engine_path) or {}
        adapter_mod = load_transcript_adapter(engine_doc) if engine_doc.get("transcript_adapter") else None
        if adapter_mod is not None and hasattr(adapter_mod, "capture_source"):
            source = adapter_mod.capture_source(transcript_raw, session_id, binding=lease)
            transcript_clean, scope_clean = source["transcript_path"], source["project_root"]
        else:
            transcript_clean, scope_clean = str(Path(transcript_raw).resolve()), str(Path(cwd).resolve(strict=False))
        if isinstance(engine_doc.get("root"), str) and isinstance(engine_doc.get("domain"), str) and not _store_path(engine_doc).is_file():
            prepare_schema(str(engine_path))
        forwarded = dict(event)
        forwarded.update(
            hook_event_name="Stop",
            harness=str(event.get("harness") or profile.host),
            session_id=session_id,
            mindie_activation=lease.get("token"),
            transcript_path=transcript_clean,
            cwd=scope_clean,
        )
        rem = max(0.1, deadline.remaining())
        return capture_hook(str(engine_path), forwarded, timeout=rem)

    def _shim_invoke_knowledge_tool(
        operation: str,
        arguments: Mapping[str, Any] | None,
        *,
        profile: ProfileRef,
        session_id: str,
        cwd: Path | None = None,
        deadline: Deadline | None = None,
    ) -> dict[str, Any]:
        from mindie_knowledge.graded.distill import result_view
        from mindie_knowledge.loop import transport
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.cli import distill_selected, ensure_service
        from mindie_knowledge.loop.domain_routes import select_engine_config

        if operation not in mh_krpc.KNOWLEDGE_NAMES:
            return mh_krpc._error_result("unknown_operation", f"Unknown knowledge operation {operation!r}.")
        args = dict(arguments or {})
        if any(str(k).startswith("_") for k in args):
            return mh_krpc._error_result("forged_identity_argument", "Internal underscore-prefixed arguments are forbidden.")
        cfg = read_json_safe(profile.config_path)
        if not isinstance(cfg, Mapping) or not isinstance(cfg.get("engine_config"), str) or not Path(cfg["engine_config"]).is_file():
            ensure_knowledge_binding(profile, plugin_root=profile.plugin_root)
            cfg = read_json_safe(profile.config_path) or {}
        try:
            engine_path = select_engine_config(
                cfg["engine_config"],
                domain=args.get("domain"),
                ref=args.get("ref"),
                domain_configs=cfg.get("domain_configs"),
            )
        except Exception as exc:
            return mh_krpc._error_result("knowledge_config_invalid", f"{type(exc).__name__}: {exc}")

        method = operation.removeprefix("knowledge_")
        if method in {"query", "explain", "feedback", "sync"}:
            try:
                conn = ensure_service(str(engine_path))
                payload = dict(args, _session_id=session_id, _session_verified=True)
                raw = transport.rpc(conn, method, payload, timeout=315.0 if method == "sync" else 30.0)
                body = {"status": "ok", "feeds": raw} if method == "sync" and isinstance(raw, list) else raw
                return mh_krpc._normalize_result(body)
            except TimeoutError:
                raise
            except Exception as exc:
                return mh_krpc._error_result("knowledge_rpc_failed", f"{type(exc).__name__}: {exc}")

        admission = Admission(profile.admission_db)
        if admission.inspect(session_id).get("status") == "inactive":
            return mh_krpc._error_result("knowledge_inactive", "MindIE knowledge is explicitly deactivated for this session.")
        lease = admission.active_lease(session_id)
        if lease is None or not lease.get("entry_enabled"):
            eff_cwd = str(cwd) if cwd is not None else os.getcwd()
            if _scope_allows(profile, eff_cwd):
                lease = admission.activate(session_id, project_root=eff_cwd, root_session=session_id, not_before=0.0)
            else:
                return mh_krpc._error_result("knowledge_authorization_required", "knowledge_distill requires an active session lease or authorized project_roots consent.")
        token = lease["token"]
        neutral = False
        try:
            conn = ensure_service(str(engine_path))
            distill_payload = {k: args[k] for k in ("inputs", "task_state", "related_refs", "proposal", "domain", "topic", "target_signature") if k in args}
            distill_payload.update(_session_id=session_id, _activation=token)
            if any(k in args for k in ("related_refs", "proposal", "domain", "topic", "target_signature")):
                raw = transport.rpc(conn, "distill", distill_payload, timeout=315.0)
            else:
                raw = distill_selected(str(engine_path), distill_payload)
            body, is_err = result_view(raw)
            result = {
                "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)}],
                "isError": bool(is_err),
                "structuredContent": body,
            }
        except TimeoutError:
            neutral = True
            raise
        except Exception as exc:
            neutral = isinstance(exc, ConnectionError)
            reason = "knowledge_outcome_unknown" if neutral else "knowledge_rpc_failed"
            result = mh_krpc._error_result(reason, f"{type(exc).__name__}: {exc}")
        finally:
            if not neutral:
                try:
                    admission.finish(session_id, token, result.get("isError") is not True)
                except TimeoutError as exc:
                    if result.get("isError") is not True:
                        result = apply_receipt_honesty(result, exc)
                except (OSError, sqlite3.Error) as exc:
                    if result.get("isError") is not True:
                        result = apply_receipt_honesty(result, exc)
        return result

    kb_mod = types.ModuleType("mindie_harness.runtime.knowledge_binding")
    kb_mod.ensure_knowledge_binding = ensure_knowledge_binding  # type: ignore[attr-defined]
    kb_mod.run_stop_capture = run_stop_capture  # type: ignore[attr-defined]
    kb_mod.__all__ = ["ensure_knowledge_binding", "run_stop_capture"]  # type: ignore[attr-defined]
    sys.modules["mindie_harness.runtime.knowledge_binding"] = kb_mod
    setattr(mh_runtime, "knowledge_binding", kb_mod)
    mh_krpc.invoke_knowledge_tool = _shim_invoke_knowledge_tool
    mh_gateway.invoke_knowledge_tool = _shim_invoke_knowledge_tool


_install_pre_015_knowledge_binding_shim()


def pytest_addoption(parser):
    parser.addoption("--live-host", action="store_true", default=False,
                     help="Run explicit Tier 3 host and network acceptance in isolated profiles")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live-host"):
        return
    skip = pytest.mark.skip(reason="Tier 3 acceptance requires --live-host")
    for item in items:
        if item.get_closest_marker("live_host"):
            item.add_marker(skip)


def _force_writable(func, target_path, _exc):
    try:
        os.chmod(target_path, stat.S_IWRITE | stat.S_IREAD)
        func(target_path)
    except TimeoutError:
        raise
    except OSError:
        pass


def _rmtree_exact(path: Path) -> None:
    """Remove one test-owned tree, retrying Windows read-only/locked leftovers."""
    raw = str(path.resolve())
    target = raw if raw.startswith("\\\\?\\") else f"\\\\?\\{raw}"
    for attempt in range(5):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(target, onexc=_force_writable)
            else:
                shutil.rmtree(target, onerror=_force_writable)
            return
        except FileNotFoundError:
            return
        except TimeoutError:
            raise
        except OSError:
            if attempt == 4:
                shutil.rmtree(target, ignore_errors=True)
            else:
                time.sleep(0.05 * (attempt + 1))


@pytest.fixture(autouse=True)
def isolated_mindie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Strictly isolate HOME, AppData, XDG, and all MindIE and WorkBuddy/CodeBuddy paths per test."""
    base_dir = Path(tempfile.mkdtemp(prefix="mwb-")) if os.name == "nt" else tmp_path
    home_dir = base_dir / "home"
    cfg_home = base_dir / "config"
    data_home = base_dir / "data"
    state_home = base_dir / "state"
    cache_home = base_dir / "cache"
    appdata = base_dir / "appdata" / "Roaming"
    localappdata = base_dir / "appdata" / "Local"
    wb_home = base_dir / "workbuddy"
    wb_user_home = base_dir / "workbuddy-home"
    cb_home = base_dir / "codebuddy"
    codex_home = base_dir / "codex"
    claude_home = base_dir / "claude"
    kimi_home = base_dir / "kimi"
    dsh_home = base_dir / "dsh"
    oc_home = base_dir / "opencode"
    tmp_dir = base_dir / "tmp"
    uv_cache = cache_home / "uv"
    for folder in (home_dir, cfg_home, data_home, state_home, cache_home, appdata,
                   localappdata, wb_home, wb_user_home, cb_home, codex_home,
                   claude_home, kimi_home, dsh_home, oc_home, tmp_dir, uv_cache):
        folder.mkdir(parents=True, exist_ok=True)

    gitleaks_dir = home_dir / ".cache" / "mindie-agent" / "gitleaks"
    real_gitleaks = REAL_HOME / ".cache" / "mindie-agent" / "gitleaks"
    if real_gitleaks.is_dir():
        shutil.copytree(real_gitleaks, gitleaks_dir, dirs_exist_ok=True)
    else:
        gitleaks_dir.mkdir(parents=True, exist_ok=True)
        gitleaks_stub = gitleaks_dir / ("gitleaks.exe" if sys.platform == "win32" else "gitleaks")
        gitleaks_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        try:
            gitleaks_stub.chmod(0o755)
        except TimeoutError:
            raise
        except OSError:
            pass

    monkeypatch.setenv("MINDIE_WB_PYTHON", os.environ.get("MINDIE_WB_TEST_PYTHON", sys.executable))
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("MINDIE_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("MINDIE_DATA_HOME", str(data_home))
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(data_home / "remote"))
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(data_home / "remote-auth"))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(cfg_home / "wb.json"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(cfg_home / "diagnostics.json"))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(data_home / "diagnostics"))
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(wb_home))
    monkeypatch.setenv("WORKBUDDY_HOME", str(wb_user_home))
    monkeypatch.setenv("CODEBUDDY_CONFIG_DIR", str(cb_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("KIMI_SHARE_DIR", str(kimi_home))
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(oc_home))
    monkeypatch.setenv("TMPDIR", str(tmp_dir))
    monkeypatch.setenv("TEMP", str(tmp_dir))
    monkeypatch.setenv("TMP", str(tmp_dir))
    monkeypatch.setenv("UV_CACHE_DIR", str(uv_cache))
    monkeypatch.setenv("_MINDIE_WB_REEXEC", "1")
    monkeypatch.delenv("WB_CONVERSATION_ID", raising=False)
    monkeypatch.delenv("CODEBUDDY_SESSION_ID", raising=False)
    try:
        yield dict(
            home=home_dir,
            config_home=cfg_home,
            data_home=data_home,
            wb_home=wb_home,
            cb_home=cb_home,
            config_path=cfg_home / "wb.json",
        )
    finally:
        if os.name == "nt" and base_dir != tmp_path:
            _rmtree_exact(base_dir)
