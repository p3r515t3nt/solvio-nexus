"""Real local sandbox probes; every document and canary is synthetic."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from solvio.agent_runtime import extension_process as E


def _invocation(root: Path, source: str, **options) -> E.ExtensionInvocation:
    artifact = root / "source"
    artifact.mkdir(exist_ok=True)
    raw = source.encode()
    (artifact / "worker.py").write_bytes(raw)
    return E.ExtensionInvocation(str(artifact.resolve()), "worker.py",
        {"worker.py": hashlib.sha256(raw).hexdigest()}, **options)


async def _run(source: str, payload: bytes = b"", **options):
    if not E.isolation.available():
        raise unittest.SkipTest("real macOS sandbox unavailable")
    with tempfile.TemporaryDirectory(prefix="extension-test-") as directory:
        return await E.run_extension(_invocation(Path(directory), source, **options), payload)


async def t_bound_adapter_runs_with_exact_stdin_and_stdout():
    result = await _run("import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())", b"local input")
    require(result.ok, result.reason)
    require_equal(result.stdout, b"local input")
    require_equal(result.execution_status, "terminal")
    require(result.process_started)


async def t_real_native_textutil_extracts_synthetic_rtf():
    source = """import os
os.execv('/usr/bin/textutil', ['/usr/bin/textutil', '-format', 'rtf', '-convert',
    'txt', '-stdin', '-stdout', '-encoding', 'UTF-8'])
"""
    result = await _run(source, b'{\\rtf1\\ansi SOLVIO test document.}')
    require(result.ok, result.reason)
    require_equal(result.stdout.strip(), b"SOLVIO test document.")


async def t_changed_or_unbound_artifact_never_starts():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = _invocation(root, "print('original')")
        (root / "source" / "worker.py").write_text("print('changed')")
        changed = await E.run_extension(invocation, b"")
        require_equal(changed.reason, "artifact_changed")
        require(not changed.process_started)
        require_equal(changed.execution_status, "not_started")
        bad = await E.run_extension(replace(invocation, entrypoint="../worker.py"), b"")
        require_equal(bad.reason, "invalid_artifact_path")
        require(not bad.process_started)


async def t_symlink_artifact_is_refused_before_spawn():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = _invocation(root, "print('original')")
        original = root / "source" / "worker.py"
        original.rename(root / "outside.py")
        original.symlink_to(root / "outside.py")
        result = await E.run_extension(invocation, b"")
        require(not result.process_started)
        require_equal(result.execution_status, "not_started")


async def t_input_limit_is_checked_before_spawn():
    with tempfile.TemporaryDirectory() as directory:
        invocation = _invocation(Path(directory), "print('should not run')", max_input_bytes=4)
        result = await E.run_extension(invocation, b"12345")
        require_equal(result.reason, "input_limit")
        require(not result.process_started)


async def t_stdout_and_stderr_are_jointly_bounded_and_failure_keeps_no_partial_result():
    result = await _run("import os\nos.write(1,b'x'*33)\nos.write(2,b'y'*32)",
                        max_output_bytes=64)
    require_equal(result.reason, "output_limit")
    require_equal(result.stdout, b"")
    require_equal(result.stderr_note, "")
    require_equal(result.execution_status, "unknown")


async def t_synthetic_foreign_files_artifact_writes_and_shell_are_denied():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        foreign = root / "synthetic-vault"
        foreign.write_text("synthetic private canary")
        require_equal(foreign.read_text(), "synthetic private canary")
        source = """import json, os, pathlib, subprocess, sys
foreign = sys.stdin.read()
answers = []
for action in [lambda: open(foreign).read(),
               lambda: pathlib.Path(__file__).write_text('changed'),
               lambda: subprocess.run(['/bin/sh', '-c', 'true'], check=True),
               lambda: pathlib.Path('new-file').write_text('new')]:
    try:
        action()
        answers.append(False)
    except (OSError, subprocess.SubprocessError):
        answers.append(True)
print(json.dumps(answers))
"""
        result = await E.run_extension(_invocation(root, source), str(foreign).encode())
        require(result.ok, result.reason)
        require_equal(json.loads(result.stdout), [True] * 4)
        require_equal(foreign.read_text(), "synthetic private canary")
        require_equal((root / "source" / "worker.py").read_text(), source)


async def t_child_has_no_inherited_provider_environment_and_only_private_home():
    source = """import json, os
print(json.dumps({'keys': sorted(os.environ), 'home': os.environ['HOME'],
    'cwd': os.getcwd(), 'scratch': os.environ['TMPDIR']}))
"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic", "SOLVIO_VAULT_DIR": "/synthetic"}):
        result = await _run(source)
    require(result.ok, result.reason)
    answer = json.loads(result.stdout)
    require_equal(answer["keys"], ["HOME", "LANG", "TMPDIR"])
    require_equal(answer["cwd"], answer["home"])
    require_equal(answer["scratch"], answer["home"])
    require(not Path(answer["home"]).exists(), "scratch survived process cleanup")


async def t_real_loopback_and_outbound_socket_use_are_denied():
    # Our own temporary listener proves that refusal is the sandbox, not a
    # nonexistent service. No production port or external service is touched.
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        source = """import json, socket, sys
port = int(sys.stdin.read())
answers = []
for address in [('127.0.0.1', port), ('192.0.2.1', 443)]:
    try:
        with socket.socket() as sock:
            sock.settimeout(.2)
            sock.connect(address)
        answers.append('connected')
    except PermissionError:
        answers.append('denied')
    except OSError as error:
        answers.append(type(error).__name__)
print(json.dumps(answers))
"""
        result = await _run(source, str(port).encode())
        require(result.ok, result.reason)
        require_equal(json.loads(result.stdout), ["denied", "denied"])
    finally:
        server.close()
        await server.wait_closed()


async def t_scratch_is_one_file_with_a_hard_byte_limit():
    source = """import json, os
with open('work.tmp', 'wb') as file:
    file.write(b'a'*64)
    file.flush()
try:
    with open('work.tmp', 'ab') as file:
        file.write(b'b')
        file.flush()
    exceeded = True
except OSError:
    exceeded = False
print(json.dumps([os.stat('work.tmp').st_size, exceeded]))
"""
    result = await _run(source, scratch_bytes=64)
    require(result.ok, result.reason)
    require_equal(json.loads(result.stdout), [64, False])


async def t_child_cannot_escape_the_owned_process_group():
    source = """import json, os
try:
    pid = os.fork()
except PermissionError:
    print('denied', flush=True)
else:
    if pid == 0:
        os.setsid()
        print('escaped', flush=True)
        os._exit(0)
    os.waitpid(pid, 0)
"""
    result = await _run(source)
    require(result.ok, result.reason)
    require_equal(result.stdout.strip(), b"denied")


async def t_deadline_is_unknown_and_not_a_not_dispatched_result():
    result = await _run("import time\ntime.sleep(30)", timeout_s=.15)
    require_equal(result.reason, "timeout")
    require_equal(result.execution_status, "unknown")
    require(result.process_started)
    require(result.elapsed < 3)


async def t_unlisted_native_exec_is_denied_without_relying_on_fork_denial():
    result = await _run("""import os
try:
    os.execv('/bin/sh', ['/bin/sh', '-c', 'echo escaped'])
except PermissionError:
    print('denied')
""")
    require(result.ok, result.reason)
    require_equal(result.stdout.strip(), b"denied")


async def t_native_posix_spawn_cannot_bypass_the_fork_boundary():
    result = await _run("""import os, sys
try:
    pid = os.posix_spawn(sys.executable,
        [sys.executable, '-I', '-S', '-c', "print('escaped')"], dict(os.environ),
        setsid=True)
except PermissionError:
    print('denied')
else:
    os.waitpid(pid, 0)
""")
    require(result.ok, result.reason)
    require_equal(result.stdout.strip(), b"denied")


async def t_snapshot_exposes_only_bound_files_and_does_not_follow_source_after_start():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = _invocation(root, """import pathlib, sys, time
time.sleep(.1)
print(pathlib.Path(__file__).read_text().startswith('import pathlib'))
try:
    print(pathlib.Path(__file__).with_name('unbound.txt').read_text())
except FileNotFoundError:
    print('unbound absent')
""")
        (root / "source" / "unbound.txt").write_text("synthetic unbound canary")
        original = E.asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            process = await original(*args, **kwargs)
            (root / "source" / "worker.py").write_text("print('mutated')")
            return process

        with patch.object(E.asyncio, "create_subprocess_exec", spawn):
            result = await E.run_extension(invocation, b"")
        require(result.ok, result.reason)
        require_equal(result.stdout.splitlines(), [b"True", b"unbound absent"])


async def t_cancel_reaps_own_process_cleans_scratch_and_leaves_foreign_process_alive():
    foreign = await asyncio.create_subprocess_exec(sys.executable, "-I", "-S", "-c",
        "import time; time.sleep(30)", stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        with tempfile.TemporaryDirectory() as directory:
            invocation = _invocation(Path(directory), "import time\ntime.sleep(30)")
            original = E.asyncio.create_subprocess_exec
            spawned = asyncio.get_running_loop().create_future()
            scratch = []

            async def spawn(*args, **kwargs):
                process = await original(*args, **kwargs)
                scratch.append(kwargs["cwd"])
                spawned.set_result(process)
                return process

            with patch.object(E.asyncio, "create_subprocess_exec", spawn):
                task = asyncio.create_task(E.run_extension(invocation, b""))
                process = await asyncio.wait_for(spawned, 2)
                task.cancel()
                try:
                    await task
                    require(False, "cancellation was swallowed")
                except asyncio.CancelledError:
                    pass
            require(process.returncode is not None, "own process still running")
            require(not Path(scratch[0]).exists(), "scratch survived cancellation")
            require(foreign.returncode is None, "unrelated process was killed")
    finally:
        if foreign.returncode is None:
            foreign.terminate()
        await foreign.wait()


async def t_missing_sandbox_or_invalid_limits_never_starts():
    with tempfile.TemporaryDirectory() as directory:
        invocation = _invocation(Path(directory), "print('should not run')")
        with patch.object(E.isolation, "available", return_value=False):
            result = await E.run_extension(invocation, b"")
        require_equal(result.reason, "sandbox_unavailable")
        require(not result.process_started)
        for field, value in [("timeout_s", float("nan")), ("timeout_s", 31),
                             ("max_output_bytes", True), ("scratch_bytes", 0)]:
            result = await E.run_extension(replace(invocation, **{field: value}), b"")
            require(not result.process_started)
            require(result.reason.startswith("invalid_"))


async def t_nonzero_completion_is_terminal_failure_and_does_not_expose_stderr():
    result = await _run("import sys\nprint('synthetic diagnostic', file=sys.stderr)\nsys.exit(3)")
    require(not result.ok)
    require_equal(result.exit_code, 3)
    require_equal(result.reason, "nonzero_exit")
    require_equal(result.execution_status, "terminal")
    require_equal(result.stderr_note, "")


async def t_cancellation_during_handle_delivery_never_loses_the_real_child():
    with tempfile.TemporaryDirectory() as directory:
        invocation = _invocation(Path(directory), "import time\ntime.sleep(30)")
        original = E.asyncio.create_subprocess_exec
        spawned = asyncio.get_running_loop().create_future()
        release = asyncio.Event()
        scratch = []

        async def spawn(*args, **kwargs):
            process = await original(*args, **kwargs)
            scratch.append(kwargs["cwd"])
            spawned.set_result(process)
            await release.wait()
            return process

        with patch.object(E.asyncio, "create_subprocess_exec", spawn):
            task = asyncio.create_task(E.run_extension(invocation, b""))
            process = await asyncio.wait_for(spawned, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # repeated Core shutdown must not cut cleanup short
            release.set()
            try:
                await asyncio.wait_for(task, 3)
                require(False, "cancellation was swallowed")
            except asyncio.CancelledError:
                pass
        require(process.returncode is not None, "spawned process was abandoned")
        require(not Path(scratch[0]).exists())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
