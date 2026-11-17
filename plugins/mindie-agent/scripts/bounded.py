"""Bounded subprocess execution with timeout, output caps, and process-tree kill.

[Role]
Executes local CLI subprocesses (`mindie-profiler`, `mindie-sim`, `mindie-coordinator`,
`mindie-debugger`, `npu-top`, `mindie-diagnostics`, `uv`) using file-backed output capture
and process-group / Win32 Job Object termination on timeout or cancellation before draining.

[Invariants]
- Never calls unbounded `subprocess.communicate()`; every call requires a
  positive finite wall-clock `timeout`.
- Enforces `outer_timeout >= inner_budget + cleanup_grace`: on timeout or
  cancellation, kills the entire child process tree first so grandchildren
  cannot hold stdout/stderr files open indefinitely or mask `TimeoutError` with
  Windows `PermissionError: [WinError 32]`.
- Explicitly re-raises `TimeoutError` before any `except OSError:` branch.

[Timeouts & Locks]
- Default `timeout = 30.0s` (`max_output = 1 MiB`), polling exit every `25ms`
  with a `2.0s` tree-reap ceiling after `SIGKILL` / `TerminateJobObject` / `taskkill /T /F`.

[Side-Effect Policy]
- Spawns one isolated process group / Win32 Job Object with temporary output files in `TMPDIR`.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time

_JOB_OBJECT_EXTENDED_LIMIT_INFO = 9
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_STDLIB_POPEN_CLS = subprocess.Popen
_STDLIB_POPEN = subprocess.Popen
_STDLIB_OS = os
_STDLIB_RMTREE = shutil.rmtree


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class OutputLimitExceeded(ValueError):
    """Child process output exceeded the caller's byte budget."""


class ProcessCancelled(RuntimeError):
    """Child process was cancelled by caller event."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int
    cleanup_failed: bool = False

    @property
    def output(self) -> str:
        return self.stdout

    def checked_stdout(self) -> str:
        if self.returncode != 0:
            detail = self.stderr.strip() or self.stdout.strip() or f"exit code {self.returncode}"
            raise RuntimeError(f"child process failed ({self.returncode}): {detail}")
        return self.stdout


def _win32_kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _close_windows_job(job_handle) -> None:
    if job_handle is None or os.name != "nt":
        return
    try:
        _win32_kernel32().CloseHandle(wintypes.HANDLE(job_handle))
    except Exception:
        pass


def _attach_windows_job(proc, *, kill_on_close: bool = True):
    if os.name != "nt" or not isinstance(getattr(proc, "pid", None), int) or proc.pid <= 0:
        return None
    try:
        k32 = _win32_kernel32()
        hjob = k32.CreateJobObjectW(None, None)
        if not hjob:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        if kill_on_close:
            flags |= _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = flags
        if not k32.SetInformationJobObject(
            hjob,
            _JOB_OBJECT_EXTENDED_LIMIT_INFO,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            k32.CloseHandle(hjob)
            return None
        hproc = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(proc.pid))
        if not hproc:
            k32.CloseHandle(hjob)
            return None
        try:
            assigned = bool(k32.AssignProcessToJobObject(hjob, hproc))
        finally:
            k32.CloseHandle(hproc)
        if not assigned:
            k32.CloseHandle(hjob)
            return None
        return int(hjob)
    except Exception:
        return None


def _kill_process_tree(proc: subprocess.Popen, job_handle=None, *, wait_timeout: float = 2.0) -> bool:
    cleanup_failed = False
    killed_via_job = False
    remaining_budget = max(0.05, float(wait_timeout))
    deadline = time.monotonic() + remaining_budget
    if job_handle is not None and os.name == "nt":
        try:
            k32 = _win32_kernel32()
            terminated = bool(k32.TerminateJobObject(wintypes.HANDLE(job_handle), 1))
            k32.CloseHandle(wintypes.HANDLE(job_handle))
            killed_via_job = terminated
            if not terminated and proc.poll() is None:
                cleanup_failed = True
        except Exception:
            cleanup_failed = True
    if proc.poll() is None:
        if os.name == "nt":
            if not killed_via_job and isinstance(proc, _STDLIB_POPEN_CLS):
                tk_proc = None
                tk_budget = 0.0
                try:
                    tk_proc = _STDLIB_POPEN(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    tk_budget = min(remaining_budget * 0.7, max(0.0, deadline - time.monotonic()))
                    tk_rc = tk_proc.wait(timeout=tk_budget)
                    if tk_rc not in {0, 128}:
                        cleanup_failed = True
                except (subprocess.TimeoutExpired, TimeoutError):
                    cleanup_failed = True
                    remaining_budget = max(0.0, remaining_budget - tk_budget)
                    if tk_proc is not None:
                        kill_budget = min(0.2, remaining_budget, max(0.0, deadline - time.monotonic()))
                        try:
                            tk_proc.kill()
                            if kill_budget > 0:
                                tk_proc.wait(timeout=kill_budget)
                        except (subprocess.TimeoutExpired, TimeoutError):
                            remaining_budget = max(0.0, remaining_budget - kill_budget)
                        except OSError:
                            remaining_budget = max(0.0, remaining_budget - kill_budget)
                except OSError:
                    cleanup_failed = True
            if proc.poll() is None:
                try:
                    proc.kill()
                except TimeoutError:
                    raise
                except OSError:
                    cleanup_failed = True
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except TimeoutError:
                raise
            except OSError:
                cleanup_failed = True
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except TimeoutError:
                raise
            except OSError:
                pass
    try:
        proc.wait(timeout=min(remaining_budget, max(0.0, deadline - time.monotonic())))
    except (subprocess.TimeoutExpired, TimeoutError):
        cleanup_failed = True
    else:
        if os.name != "nt":
            try:
                os.waitpid(proc.pid, os.WNOHANG)
            except TimeoutError:
                raise
            except (ChildProcessError, OSError):
                pass
    return cleanup_failed


def _read_bounded(path: Path, max_bytes: int) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise OutputLimitExceeded(f"process output exceeded {max_bytes} bytes")
    return raw.decode("utf-8", errors="replace")


def run(
    argv: list[str],
    stdin_text: str = "",
    *,
    timeout: float = 30.0,
    max_output: int = 1024 * 1024,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    cancel: threading.Event | None = None,
    allowed_returncodes: set[int] | None = None,
) -> ProcessResult:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output <= 0:
        raise ValueError("max_output must be a positive integer byte limit")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise ValueError("argv must be a nonempty list of strings")

    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    if (
        subprocess.Popen is _STDLIB_POPEN
        and os is _STDLIB_OS
        and shutil.rmtree is _STDLIB_RMTREE
        and max_output >= 1024
        and (env is None or set(os.environ).issubset(child_env))
    ):
        try:
            from mindie_harness.process.supervisor import (
                Deadline,
                OutputLimitError,
                ProcessCancelledError,
                ProcessSpec,
                run_owned,
            )
        except ImportError:
            pass
        else:
            spec = ProcessSpec(
                argv=tuple(argv),
                cwd=Path(cwd) if cwd is not None else None,
                env=child_env,
                stdin_bytes=(stdin_text or "").encode("utf-8"),
                max_output_bytes=max_output,
                mcp_response_cap_bytes=max(256, min(max_output, 1024 * 1024)),
                memory_limit_bytes=None,
                allowed_returncodes=None,
            )
            try:
                outcome = run_owned(
                    spec,
                    deadline=Deadline.from_timeout(float(timeout)),
                    cancel=cancel,
                )
            except OutputLimitError as exc:
                raise OutputLimitExceeded(f"process output exceeded {max_output} bytes") from exc
            except ProcessCancelledError as exc:
                raise ProcessCancelled(f"process cancelled: {argv[0]}") from exc
            except TimeoutError as exc:
                raise TimeoutError(f"process timed out after {timeout:.2f}s: {argv[0]}") from exc
            result = ProcessResult(
                returncode=int(outcome.returncode if outcome.returncode is not None else -1),
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                elapsed_ms=int(outcome.elapsed_ms),
                cleanup_failed=bool(outcome.cleanup_issues),
            )
            if allowed_returncodes is not None and result.returncode not in allowed_returncodes:
                result.checked_stdout()
            return result

    cleanup_failed = False
    tmp_dir = tempfile.mkdtemp(prefix="mindie-wb-proc-")
    try:
        tmp_root = Path(tmp_dir)
        stdin_path = tmp_root / "stdin.txt"
        stdout_path = tmp_root / "stdout.txt"
        stderr_path = tmp_root / "stderr.txt"
        stdin_path.write_bytes((stdin_text or "").encode("utf-8"))

        with stdin_path.open("rb") as fin, stdout_path.open("wb") as fout, stderr_path.open("wb") as ferr:
            popen_kwargs: dict = dict(
                stdin=fin,
                stdout=fout,
                stderr=ferr,
                env=child_env,
                cwd=str(cwd) if cwd is not None else None,
            )
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
            else:
                popen_kwargs["start_new_session"] = True

            started = time.monotonic()
            deadline = started + float(timeout)
            proc = subprocess.Popen(argv, **popen_kwargs)
            job_handle = _attach_windows_job(proc)
            try:
                while True:
                    rc = proc.poll()
                    if rc is not None:
                        break
                    if cancel is not None and cancel.is_set():
                        cleanup_failed = _kill_process_tree(proc, job_handle) or cleanup_failed
                        job_handle = None
                        raise ProcessCancelled(f"process cancelled: {argv[0]}")
                    now = time.monotonic()
                    if now >= deadline:
                        cleanup_failed = _kill_process_tree(proc, job_handle) or cleanup_failed
                        job_handle = None
                        raise TimeoutError(f"process timed out after {timeout:.2f}s: {argv[0]}")
                    if (stdout_path.exists() and stdout_path.stat().st_size > max_output) or (
                        stderr_path.exists() and stderr_path.stat().st_size > max_output
                    ):
                        cleanup_failed = _kill_process_tree(proc, job_handle) or cleanup_failed
                        job_handle = None
                        raise OutputLimitExceeded(f"process output exceeded {max_output} bytes")
                    time.sleep(min(0.025, max(0.001, deadline - now)))
            except BaseException:
                if proc.poll() is None:
                    cleanup_failed = _kill_process_tree(proc, job_handle) or cleanup_failed
                    job_handle = None
                raise
            finally:
                if job_handle is not None:
                    _close_windows_job(job_handle)

        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        stdout_text = _read_bounded(stdout_path, max_output)
        stderr_text = _read_bounded(stderr_path, max_output)
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except TimeoutError:
            raise
        except OSError:
            cleanup_failed = True

    rc_int = int(proc.returncode if proc.returncode is not None else -1)
    if allowed_returncodes is not None and rc_int not in allowed_returncodes:
        raise RuntimeError(f"{argv[0]} exited with status {rc_int}: {stderr_text.strip() or stdout_text.strip()}")
    return ProcessResult(
        returncode=rc_int,
        stdout=stdout_text,
        stderr=stderr_text,
        elapsed_ms=elapsed_ms,
        cleanup_failed=cleanup_failed,
    )
