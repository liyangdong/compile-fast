"""Shared process, hashing and path utilities; no third-party dependencies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import contextvars
import signal
import tempfile
import time
from contextlib import contextmanager

_cancel = contextvars.ContextVar("compile_fast_cancel", default=None)
_metrics = contextvars.ContextVar("compile_fast_metrics", default=None)


@contextmanager
def process_metrics():
    measurements = []
    token = _metrics.set(measurements)
    try:
        yield measurements
    finally:
        _metrics.reset(token)


def process_usage(process):
    """Best-effort leader-process counters; child process accounting is not implied."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            times = [wintypes.FILETIME() for _ in range(4)]
            get_times = kernel.GetProcessTimes
            get_times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            if not get_times(int(process._handle), *(ctypes.byref(t) for t in times)):
                return {}
            cpu_ms = sum((t.dwHighDateTime << 32) + t.dwLowDateTime for t in times[2:]) / 10000
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ("peak", "working", "peakPaged", "paged", "peakNonPaged", "nonPaged", "pagefile", "peakPagefile")]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            get_memory = kernel.K32GetProcessMemoryInfo
            get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            success = get_memory(int(process._handle), ctypes.byref(counters), counters.cb)
            return {"cpuMs": round(cpu_ms, 2), "peakRssBytes": counters.peak if success else None}
        stat = Path(f"/proc/{process.pid}/stat").read_text()
        values = stat[stat.rfind(")") + 2:].split()
        cpu_ms = (int(values[11]) + int(values[12])) * 1000 / os.sysconf("SC_CLK_TCK")
        status = Path(f"/proc/{process.pid}/status").read_text()
        peak = next((int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith("VmHWM:")), None)
        return {"cpuMs": round(cpu_ms, 2), "peakRssBytes": peak}
    except (OSError, ValueError, AttributeError, IndexError):
        return {}


@contextmanager
def cancellation(event):
    token = _cancel.set(event)
    try:
        yield
    finally:
        _cancel.reset(token)


def checkpoint():
    event = _cancel.get()
    if event is not None and event.is_set():
        raise EngineError("Verification was cancelled or superseded.", code="CANCELLED")


def terminate(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait(timeout=10)


class EngineError(Exception):
    def __init__(self, message: str, status: str = "UNKNOWN", code: str = "CONTEXT_MISSING"):
        super().__init__(message)
        self.status, self.code = status, code


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_hash(path: Path) -> str:
    if path.is_file():
        return file_hash(path)
    if path.is_dir():
        return digest([(p.relative_to(path).as_posix(), file_hash(p))
                       for p in sorted(path.rglob("*")) if p.is_file()])
    raise EngineError(f"Classpath entry does not exist: {path}", code="MISSING_DEPENDENCY")


def inside(root: Path, value: str) -> Path:
    result = (root / value).resolve()
    if not result.is_relative_to(root.resolve()):
        raise EngineError(f"Path escapes workspace: {value}", code="INVALID_PATH")
    return result


def tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise EngineError(f"Executable not found: {name}", "INFRA_ERROR", "TOOL_MISSING")
    return found


def run(args: list[str], cwd: Path, timeout: float = 120) -> subprocess.CompletedProcess:
    checkpoint()
    env = os.environ.copy()
    # Hidden compiler injection would invalidate the recorded context.
    for key in ("JDK_JAVAC_OPTIONS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH"):
        env.pop(key, None)
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(args, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                       start_new_session=os.name != "nt",
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            started = time.monotonic()
            usage = {}
            try:
                while process.poll() is None:
                    usage.update(process_usage(process))
                    checkpoint()
                    if time.monotonic() - started > timeout:
                        raise EngineError(f"Command timed out after {timeout}s: {args[0]}", "INFRA_ERROR", "TIMEOUT")
                    time.sleep(0.05)
                checkpoint()
                stdout.seek(0)
                stderr.seek(0)
                return subprocess.CompletedProcess(args, process.returncode,
                    stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace"))
            finally:
                usage.update(process_usage(process))
                terminate(process)
                measurements = _metrics.get()
                if measurements is not None:
                    measurements.append({"executable": args[0], "durationMs": round((time.monotonic() - started) * 1000), **usage})
    except OSError as exc:
        raise EngineError(str(exc), "INFRA_ERROR", "PROCESS_ERROR") from exc


def build_files(root: Path) -> dict[str, str]:
    names = {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
             "settings.gradle.kts", "gradle.properties", "maven.config", "jvm.config",
             "extensions.xml", "toolchains.xml"}
    excluded = {".git", ".compile-fast", "target", "build", ".venv", "node_modules"}
    result = {}
    for parent, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in excluded]
        for name in files:
            if name in names:
                path = Path(parent) / name
                result[path.relative_to(root).as_posix()] = file_hash(path)
    return result


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement avoids exposing partial evidence/model/index files to concurrent agents.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
