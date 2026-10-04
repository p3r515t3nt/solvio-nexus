"""A bound offline Python file tool, separate from Core and its authority.

Runtime paths come from local operator configuration, never from model output.
``bind_runtime`` measures an existing installation; it installs nothing. The
caller must retain that binding with its tested tool. Every invocation checks
the actual runtime bytes before and after execution. Output is untrusted JSON,
not a task grant, delivery receipt, or proof that a user's objective succeeded.
"""
from __future__ import annotations

import asyncio
import ctypes
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from solvio.agent_runtime import extension_process as E

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_SCRATCH_BYTES = 32 * 1024 * 1024
MAX_SCRATCH_FILES = 16
MAX_TIMEOUT = 60.0
MAX_RUNTIME_FILES = 50_000
MAX_RUNTIME_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024
MIN_MEMORY_BYTES = 64 * 1024 * 1024
MAX_MEMORY_BYTES = 1024 * 1024 * 1024
MEMORY_POLL_SECONDS = 0.025


@dataclass(frozen=True)
class FileToolRuntime:
    python: str
    prefix: str
    site_packages: str
    fingerprint: str


@dataclass(frozen=True)
class FileToolInvocation:
    artifact_dir: str
    entrypoint: str
    files: dict[str, str]
    runtime: FileToolRuntime
    timeout_s: float = 30.0
    max_input_bytes: int = MAX_INPUT_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    scratch_bytes: int = MAX_SCRATCH_BYTES
    scratch_files: int = 4
    memory_bytes: int = DEFAULT_MEMORY_BYTES


def _paths(python: str, prefix: str, site_packages: str) -> tuple[Path, Path, Path]:
    values = (python, prefix, site_packages)
    if any(not isinstance(v, str) or not os.path.isabs(v) for v in values):
        raise E._Refused("invalid_runtime")
    binary, root, packages = (Path(os.path.realpath(v)) for v in values)
    if (str(root) != prefix or str(packages) != site_packages or not root.is_dir()
            or not packages.is_dir() or not binary.is_file() or not os.access(binary, os.X_OK)
            or binary.parent != root / "bin"
            or not re.fullmatch(r"python3(?:\.\d+)?", binary.name)
            or packages.parent.parent != root / "lib"
            or not re.fullmatch(r"python3\.\d+", packages.parent.name)
            or packages.name != "site-packages"):
        raise E._Refused("invalid_runtime_layout")
    return binary, root, packages


def _runtime_fingerprint(python: str, prefix: str, site_packages: str) -> str:
    binary, root, packages = _paths(python, prefix, site_packages)
    digest = hashlib.sha256()
    digest.update(json.dumps(["file_tool_runtime_v1", str(binary), str(root), str(packages),
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        hashlib.sha256(Path(E.__file__).read_bytes()).hexdigest()],
        separators=(",", ":")).encode())
    count = total = 0
    def walk_error(error):
        raise error

    for directory, names, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        names.sort()
        for name in sorted(names + filenames):
            path = Path(directory) / name
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                # Some relocated runtimes retain stale man/pkgconfig links.
                # Bind the link itself without reading its target. Seatbelt
                # grants only this prefix; an external target remains denied.
                target = Path(os.path.realpath(path))
                value = [relative, "link" if target.is_relative_to(root) else "external_link_denied",
                         os.readlink(path)]
            elif stat.S_ISDIR(info.st_mode):
                value = [relative, "directory", stat.S_IMODE(info.st_mode)]
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                count += 1
                total += info.st_size
                if count > MAX_RUNTIME_FILES or total > MAX_RUNTIME_BYTES:
                    raise E._Refused("runtime_limit")
                file_hash = hashlib.sha256()
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                        raise E._Refused("runtime_changed")
                    size = 0
                    while chunk := stream.read(1024 * 1024):
                        size += len(chunk)
                        if size > info.st_size:
                            raise E._Refused("runtime_changed")
                        file_hash.update(chunk)
                    after = os.fstat(stream.fileno())
                current = path.lstat()
                identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size,
                                      s.st_mtime_ns, s.st_ctime_ns)
                if (size != info.st_size or identity(after) != identity(info)
                        or identity(current) != identity(info)):
                    raise E._Refused("runtime_changed")
                value = [relative, "file", stat.S_IMODE(info.st_mode), size, file_hash.hexdigest()]
            else:
                raise E._Refused("runtime_not_regular")
            digest.update(json.dumps(value, separators=(",", ":")).encode() + b"\n")
    if not count:
        raise E._Refused("runtime_empty")
    return digest.hexdigest()


def bind_runtime(*, python: str, prefix: str, site_packages: str) -> FileToolRuntime:
    """Measure explicitly configured files, without launching Python or a tool."""
    binary, root, packages = _paths(python, prefix, site_packages)
    fingerprint = _runtime_fingerprint(str(binary), str(root), str(packages))
    return FileToolRuntime(str(binary), str(root), str(packages), fingerprint)


def _json_object(raw: bytes) -> None:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite_number")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    if type(value) is not dict:
        raise ValueError("object_required")
    remaining = [(value, 0)]
    nodes = 0
    while remaining:
        item, depth = remaining.pop()
        nodes += 1
        if nodes > 100_000 or depth > 64:
            raise ValueError("json_complexity")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("nonfinite_number")
        if isinstance(item, dict):
            remaining.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            remaining.extend((v, depth + 1) for v in item)


def _binding(invocation: FileToolInvocation, payload: bytes) -> dict[str, str]:
    if type(invocation) is not FileToolInvocation or type(payload) is not bytes:
        raise E._Refused("invalid_invocation")
    if (type(invocation.runtime) is not FileToolRuntime
            or not isinstance(invocation.runtime.fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", invocation.runtime.fingerprint)):
        raise E._Refused("invalid_runtime_binding")
    if (type(invocation.timeout_s) not in (int, float)
            or not math.isfinite(invocation.timeout_s)
            or not 0 < invocation.timeout_s <= MAX_TIMEOUT):
        raise E._Refused("invalid_deadline")
    for value, ceiling in ((invocation.max_input_bytes, MAX_INPUT_BYTES),
            (invocation.max_output_bytes, MAX_OUTPUT_BYTES),
            (invocation.scratch_bytes, MAX_SCRATCH_BYTES),
            (invocation.scratch_files, MAX_SCRATCH_FILES)):
        if type(value) is not int or not 0 < value <= ceiling:
            raise E._Refused("invalid_byte_limit")
    if invocation.scratch_bytes < invocation.scratch_files:
        raise E._Refused("invalid_byte_limit")
    if type(invocation.memory_bytes) is not int or not MIN_MEMORY_BYTES <= invocation.memory_bytes <= MAX_MEMORY_BYTES:
        raise E._Refused("invalid_memory_limit")
    if len(payload) > invocation.max_input_bytes:
        raise E._Refused("input_limit")
    try:
        _json_object(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise E._Refused("invalid_input_json") from None
    # Preserve the original source-path/digest/quantity checks and snapshot
    # contract. This additive process has its own JSON and runtime limits.
    source = E.ExtensionInvocation(invocation.artifact_dir, invocation.entrypoint, invocation.files)
    return E._binding(source, b"")


_BOOTSTRAP = """import resource, sys
resource.setrlimit(resource.RLIMIT_FSIZE, ({per_file}, {per_file}))
resource.setrlimit(resource.RLIMIT_CPU, ({cpu}, {cpu}))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
# -I -S: no user site, .pth, sitecustomize, inherited PYTHONPATH or startup code.
# The single measured package directory is inserted explicitly, without site.
sys.path.insert(0, {packages!r})
# The parent first obtains a real RSS sample. No supplied code/import runs
# until its monitor is ready; a missing monitor never releases this barrier.
if sys.stdin.buffer.read(1) != b'\\x00':
    raise SystemExit(74)
entry = sys.argv[1]
sys.argv = [entry]
with open(entry, 'rb') as source:
    code = compile(source.read(), entry, 'exec')
exec(code, {{'__name__': '__main__', '__file__': entry}})
"""


class _ProcTaskInfo(ctypes.Structure):
    # Darwin SDK sys/proc_info.h: PROC_PIDTASKINFO (4). These fixed native
    # fields are read only for the child handle owned by this invocation.
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "virtual_size", "resident_size", "total_user", "total_system", "threads_user", "threads_system"
    )] + [(name, ctypes.c_int32) for name in (
        "policy", "faults", "pageins", "cow_faults", "messages_sent", "messages_received",
        "syscalls_mach", "syscalls_unix", "csw", "threadnum", "numrunning", "priority")]


@lru_cache(maxsize=1)
def _proc_pidinfo():
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


def _memory_rss(pid: int) -> int | None:
    info = _ProcTaskInfo()
    try:
        size = _proc_pidinfo()(pid, 4, 0, ctypes.byref(info), ctypes.sizeof(info))
    except (OSError, AttributeError):
        return None
    return int(info.resident_size) if size == ctypes.sizeof(info) and info.resident_size > 0 else None


async def _communicate_with_memory(process, payload: bytes, output_limit: int, memory_limit: int) -> bytes:
    """Observed RSS threshold, not an instantaneous kernel memory guarantee.

    On the measured Darwin runtime RLIMIT_AS and RLIMIT_DATA reject even a
    finite soft limit (ValueError); neither bounds real allocations. libproc
    therefore samples the owned process before code starts and every 25 ms.
    A very short peak between samples can escape detection. Missing telemetry
    fails closed. Fork remains kernel-denied, so no child tree is omitted.
    The outer finally always reaps the process on refusal or cancellation.
    """
    first = _memory_rss(process.pid)
    if first is None:
        raise E._Refused("memory_measurement_unavailable")
    if first > memory_limit:
        raise E._Refused("memory_limit")
    # The only handshake byte is consumed by the fixed bootstrap; supplied
    # code still sees exactly the original JSON, not a modified protocol.
    communication = asyncio.create_task(E._communicate(process, b"\0" + payload, output_limit))

    async def watch():
        while not communication.done():
            if process.returncode is not None:
                return
            rss = _memory_rss(process.pid)
            if rss is None:
                # The process can disappear just before asyncio receives its
                # exit notification. Reap that exit; a still-live unreadable
                # process must not continue without a monitor.
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=MEMORY_POLL_SECONDS)
                except asyncio.TimeoutError:
                    raise E._Refused("memory_measurement_unavailable") from None
                return
            if rss > memory_limit:
                raise E._Refused("memory_limit")
            await asyncio.sleep(MEMORY_POLL_SECONDS)

    monitor = asyncio.create_task(watch())
    try:
        result, _ = await asyncio.gather(communication, monitor)
        return result
    finally:
        for task in (communication, monitor):
            if not task.done():
                task.cancel()
        await asyncio.gather(communication, monitor, return_exceptions=True)


def _profile(*, artifact: Path, scratch: Path, bootstrap: Path,
             runtime: FileToolRuntime, slots: list[Path]) -> str:
    q = lambda value: json.dumps(os.path.realpath(value))
    writable = " ".join(f"(literal {q(p)})" for p in slots)
    return f"""(version 1)
(deny default)
(deny process-fork)
(allow process-exec (literal {q(runtime.python)}))
(allow signal (target self))
(allow sysctl-read)
(allow file-read-metadata
  (literal "/") (literal "/private") (literal "/private/var")
  (subpath "/usr") (subpath "/System") (subpath "/dev")
  (subpath {q(runtime.prefix)}) (subpath {q(artifact)}) (subpath {q(scratch)})
  (literal {q(bootstrap)}))
(allow file-read*
  (literal "/") (subpath "/usr/lib") (subpath "/System/Library")
  (literal "/dev/null") (literal "/dev/zero")
  (literal "/dev/random") (literal "/dev/urandom")
  (subpath {q(runtime.prefix)}) (subpath {q(artifact)}) (subpath {q(scratch)})
  (literal {q(bootstrap)}))
(allow file-write-data {writable}
  (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))
"""


async def run_file_tool(invocation: FileToolInvocation, payload: bytes) -> E.ExtensionOutcome:
    """Execute once after the caller's grant/cost claim. Cancellation propagates.

    Scratch contains only ``slot-00.tmp`` ... pre-created files. The kernel
    denies creating, replacing or removing files; each slot's hard FSIZE cap is
    floor(scratch_bytes / scratch_files), so total writable storage is bounded.
    Prefer BytesIO for library outputs and return them in the JSON envelope.
    A started timeout/output violation is unknown; it never means not-dispatched.
    """
    started = time.monotonic()
    process = None
    try:
        files = _binding(invocation, payload)
        runtime = invocation.runtime
        if not E.isolation.available():
            raise E._Refused("sandbox_unavailable")
        if await asyncio.to_thread(_runtime_fingerprint, runtime.python,
                runtime.prefix, runtime.site_packages) != runtime.fingerprint:
            raise E._Refused("runtime_changed")
        with tempfile.TemporaryDirectory(prefix="solvio-file-tool-") as directory:
            base = Path(os.path.realpath(directory))
            artifact, scratch = base / "artifact", base / "scratch"
            artifact.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            E._snapshot(invocation, files, artifact)
            slots = [scratch / f"slot-{i:02d}.tmp" for i in range(invocation.scratch_files)]
            for slot in slots:
                slot.touch(mode=0o600)
            bootstrap = base / "bootstrap.py"
            bootstrap.write_text(_BOOTSTRAP.format(
                per_file=invocation.scratch_bytes // invocation.scratch_files,
                cpu=max(1, math.ceil(invocation.timeout_s)), packages=runtime.site_packages))
            profile = base / "file-tool.sb"
            profile.write_text(_profile(artifact=artifact, scratch=scratch,
                bootstrap=bootstrap, runtime=runtime, slots=slots))
            env = {"HOME": str(scratch), "TMPDIR": str(scratch), "LANG": "C.UTF-8", "TZ": "UTC",
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                   "MKL_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"}
            process = await E._spawn_owned(E.isolation.SANDBOX_EXEC, "-f", str(profile),
                runtime.python, "-I", "-S", "-B", str(bootstrap), str(artifact / invocation.entrypoint),
                cwd=str(scratch), env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                output = await asyncio.wait_for(_communicate_with_memory(process, payload,
                    invocation.max_output_bytes, invocation.memory_bytes), timeout=invocation.timeout_s)
            finally:
                await E.launcher._stop(process)
            if await asyncio.to_thread(_runtime_fingerprint, runtime.python,
                    runtime.prefix, runtime.site_packages) != runtime.fingerprint:
                raise E._Refused("runtime_changed")
            reason = "" if process.returncode == 0 else "nonzero_exit"
            if not reason:
                try:
                    _json_object(output)
                except (ValueError, UnicodeError, RecursionError):
                    reason = "invalid_output_json"
            return E.ExtensionOutcome(not reason, reason=reason, stdout=output if not reason else b"",
                process_started=True, execution_status="terminal", exit_code=process.returncode,
                elapsed=time.monotonic() - started)
    except asyncio.TimeoutError:
        reason = "timeout"
    except E._Refused as exc:
        reason = str(exc)
    except (OSError, ValueError, RuntimeError):
        reason = "process_failed" if process else "artifact_or_spawn_failed"
    return E.ExtensionOutcome(False, reason=reason, process_started=process is not None,
        execution_status="unknown" if process else "not_started",
        exit_code=process.returncode if process else None, elapsed=time.monotonic() - started)
