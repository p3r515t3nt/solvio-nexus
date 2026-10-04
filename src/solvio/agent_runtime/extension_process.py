"""One bound local adapter in an offline process, never a Core Python import.

The caller supplies verified artifact hashes and owns task authority, cost claims
and activation. This module grants none of them. Source bytes are copied after
hash verification; the child sees only that immutable snapshot, its Python
runtime, OS libraries and one size-limited scratch file. No provider state is
inherited. Results are data, not evidence of a successful user task.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time

from solvio.agent_runtime import isolation
from solvio.specialists import launcher

MAX_FILES = 16
MAX_ARTIFACT_BYTES = 1_048_576
MAX_INPUT_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 1_048_576
MAX_SCRATCH_BYTES = 1_048_576
MAX_TIMEOUT = 30.0
TEXTUTIL = "/usr/bin/textutil"


@dataclass(frozen=True)
class ExtensionInvocation:
    artifact_dir: str
    entrypoint: str
    files: dict[str, str]
    timeout_s: float = 10.0
    max_input_bytes: int = MAX_INPUT_BYTES
    max_output_bytes: int = 65_536
    scratch_bytes: int = MAX_SCRATCH_BYTES


@dataclass(frozen=True)
class ExtensionOutcome:
    ok: bool
    reason: str = ""
    stdout: bytes = b""
    stderr_note: str = ""
    process_started: bool = False
    execution_status: str = "not_started"
    exit_code: int | None = None
    elapsed: float = 0.0


class _Refused(ValueError):
    pass


def _relative(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}", value)
            or any(part in ("", ".", "..") for part in value.split("/"))
            or str(PurePosixPath(value)) != value):
        raise _Refused("invalid_artifact_path")
    return value


def _binding(invocation: ExtensionInvocation, payload: bytes) -> dict[str, str]:
    if type(invocation) is not ExtensionInvocation or type(payload) is not bytes:
        raise _Refused("invalid_invocation")
    if (type(invocation.timeout_s) not in (int, float)
            or not math.isfinite(invocation.timeout_s)
            or not 0 < invocation.timeout_s <= MAX_TIMEOUT):
        raise _Refused("invalid_deadline")
    for value, ceiling in ((invocation.max_input_bytes, MAX_INPUT_BYTES),
                           (invocation.max_output_bytes, MAX_OUTPUT_BYTES),
                           (invocation.scratch_bytes, MAX_SCRATCH_BYTES)):
        if type(value) is not int or not 0 < value <= ceiling:
            raise _Refused("invalid_byte_limit")
    if len(payload) > invocation.max_input_bytes:
        raise _Refused("input_limit")
    if (not isinstance(invocation.files, dict) or not 1 <= len(invocation.files) <= MAX_FILES
            or not isinstance(invocation.artifact_dir, str)
            or not os.path.isabs(invocation.artifact_dir)):
        raise _Refused("invalid_artifact_binding")
    files = dict(invocation.files)
    if _relative(invocation.entrypoint) not in files or not invocation.entrypoint.endswith(".py"):
        raise _Refused("invalid_entrypoint")
    for relative, digest in files.items():
        _relative(relative)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise _Refused("invalid_artifact_digest")
    return files


def _read_bound(root: int, relative: str, remaining: int) -> bytes:
    """Open every component without following links; bound the actual bytes."""
    descriptor = os.dup(root)
    try:
        pieces = relative.split("/")
        for component in pieces[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file = os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=descriptor)
        with os.fdopen(file, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _Refused("artifact_not_regular")
            data = stream.read(remaining + 1)
        if len(data) > remaining:
            raise _Refused("artifact_limit")
        return data
    finally:
        os.close(descriptor)


def _snapshot(invocation: ExtensionInvocation, files: dict[str, str], destination: Path) -> None:
    if os.path.realpath(invocation.artifact_dir) != os.path.abspath(invocation.artifact_dir):
        raise _Refused("artifact_directory_not_canonical")
    root = os.open(invocation.artifact_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        remaining = MAX_ARTIFACT_BYTES
        for relative, expected in files.items():
            data = _read_bound(root, relative, remaining)
            remaining -= len(data)
            if hashlib.sha256(data).hexdigest() != expected:
                raise _Refused("artifact_changed")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o400)
    finally:
        os.close(root)


# This fixed bootstrap runs IN THE CHILD. No untrusted code is compiled or
# imported by Core. Hard limits cannot be raised again by adapter code.
_BOOTSTRAP = """import resource, sys
resource.setrlimit(resource.RLIMIT_FSIZE, ({scratch}, {scratch}))
resource.setrlimit(resource.RLIMIT_CPU, ({cpu}, {cpu}))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
entry = sys.argv[1]
sys.argv = [entry]
with open(entry, 'rb') as source:
    code = compile(source.read(), entry, 'exec')
exec(code, {{'__name__': '__main__', '__file__': entry}})
"""


def _profile(*, artifact: Path, scratch: Path, bootstrap: Path, python: str) -> str:
    """Separate offline profile; the existing Builder policy stays unchanged."""
    q = lambda value: json.dumps(os.path.realpath(value))
    runtime = os.path.realpath(sys.base_prefix)
    # No mach-lookup: in particular no Keychain or application automation.
    return f"""(version 1)
(deny default)
(deny process-fork)
(allow process-exec (literal {q(python)}) (literal {q(TEXTUTIL)}))
(allow signal (target self))
(allow sysctl-read)
(allow file-read-metadata
  (literal "/") (literal "/private") (literal "/private/var")
  (subpath "/usr") (subpath "/System") (subpath "/dev")
  (subpath {q(runtime)}) (subpath {q(artifact)}) (subpath {q(scratch)})
  (literal {q(bootstrap)}))
(allow file-read*
  (literal "/") (subpath "/usr/lib") (subpath "/System/Library")
  (literal {q(TEXTUTIL)}) (literal "/dev/null") (literal "/dev/zero")
  (literal "/dev/random") (literal "/dev/urandom")
  (subpath {q(runtime)}) (subpath {q(artifact)}) (subpath {q(scratch)})
  (literal {q(bootstrap)}))
(allow file-write-data
  (literal {q(scratch / 'work.tmp')})
  (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))
"""


async def _communicate(process, payload: bytes, limit: int) -> bytes:
    """Both output channels share a strict retained byte budget."""
    total = 0
    stdout = bytearray()

    async def read(stream, keep: bool):
        nonlocal total
        while chunk := await stream.read(min(8192, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise _Refused("output_limit")
            if keep:
                stdout.extend(chunk)

    async def send():
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
        await process.wait()

    jobs = [asyncio.create_task(read(process.stdout, True)),
            asyncio.create_task(read(process.stderr, False)), asyncio.create_task(send())]
    try:
        await asyncio.gather(*jobs)
        return bytes(stdout)
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


async def _spawn_owned(*argv, **kwargs):
    """Cancellation cannot lose the child between spawn and handle delivery."""
    pending = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
        try:
            process = pending.result()
        except (Exception, asyncio.CancelledError):
            raise asyncio.CancelledError from None
        await launcher._stop(process)
        raise


async def run_extension(invocation: ExtensionInvocation, payload: bytes) -> ExtensionOutcome:
    """Run only after caller authorization/cost claim; cancellation propagates.

    Forking is denied by the kernel. An adapter can replace its own process
    with the fixed native textutil executable; stdin/stdout and PID survive.
    A timeout/output violation/cancel after spawn is unknown, never proof of
    non-dispatch. Even completed bytes are untrusted until the caller validates
    its result contract. No stderr content is returned or logged.
    """
    started = time.monotonic()
    process = None
    try:
        files = _binding(invocation, payload)
        if not isolation.available():
            raise _Refused("sandbox_unavailable")
        with tempfile.TemporaryDirectory(prefix="solvio-extension-") as directory:
            base = Path(os.path.realpath(directory))
            artifact, scratch = base / "artifact", base / "scratch"
            artifact.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            _snapshot(invocation, files, artifact)
            (scratch / "work.tmp").touch(mode=0o600)
            bootstrap = base / "bootstrap.py"
            bootstrap.write_text(_BOOTSTRAP.format(scratch=invocation.scratch_bytes,
                cpu=max(1, math.ceil(invocation.timeout_s))), encoding="utf-8")
            python = os.path.realpath(sys.executable)
            profile = base / "extension.sb"
            profile.write_text(_profile(artifact=artifact, scratch=scratch,
                                         bootstrap=bootstrap, python=python), encoding="utf-8")
            env = {"HOME": str(scratch), "TMPDIR": str(scratch), "LANG": "C.UTF-8"}
            process = await _spawn_owned(
                isolation.SANDBOX_EXEC, "-f", str(profile), python, "-I", "-S", "-B",
                str(bootstrap), str(artifact / invocation.entrypoint),
                cwd=str(scratch), env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                output = await asyncio.wait_for(_communicate(process, payload,
                    invocation.max_output_bytes), timeout=invocation.timeout_s)
            finally:
                # Existing cancellation-safe group cleanup also works after
                # the group leader exited: start_new_session pins PGID=PID.
                await launcher._stop(process)
            return ExtensionOutcome(process.returncode == 0,
                reason="" if process.returncode == 0 else "nonzero_exit", stdout=output,
                process_started=True, execution_status="terminal", exit_code=process.returncode,
                elapsed=time.monotonic() - started)
    except asyncio.TimeoutError:
        reason = "timeout"
    except _Refused as exc:
        reason = str(exc)
    except (OSError, ValueError):
        reason = "process_failed" if process else "artifact_or_spawn_failed"
    return ExtensionOutcome(False, reason=reason, process_started=process is not None,
        execution_status="unknown" if process else "not_started",
        exit_code=process.returncode if process else None, elapsed=time.monotonic() - started)
