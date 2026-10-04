"""Private IPC adapter for native DynamicToolSpec requests, no service logic.

Create the endpoint before quoting so its identity is in the invocation;
open it only inside the actual claimed native dispatch. The model's shell
profile must exclude this directory. The Core callback rechecks every grant.

The endpoint lives in a Core-owned private root that the native sandbox
cannot list or write. Measured on 2026-09-17 with Codex 0.147.0 and the
solvio-task profile (docs/plan/evidence/n8-native-sandbox-probe-20260917.json):
``:minimal`` shields every private directory outside the workspace — except
``/tmp``/``/private/tmp``, which stay readable and writable from the native
shell despite ``excludeSlashTmp``. A root there is therefore refused.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import tempfile

from solvio.agent_runtime.native_tools import NativeCoreTools
from solvio.specialists import native_tool_wire as W

SOCKET_ROOT_NAME = "native-sockets"
_SHARED_TEMP = (Path("/tmp"), Path("/private/tmp"))


def _private_root(value):
    """A canonical, existing, owner-private directory outside the shared temp tree."""
    if type(value) is not str or not value or len(value) > 2000:
        raise ValueError("native_tool_socket_location")
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or str(path.resolve()) != value or path == Path("/")
            or any(path.is_relative_to(shared) for shared in _SHARED_TEMP)):
        raise ValueError("native_tool_socket_location")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("native_tool_socket_location")
    return path


def socket_root(ledger_path):
    """The Core-owned socket root beside the ledger, created privately on demand."""
    root = Path(ledger_path).resolve().parent / SOCKET_ROOT_NAME
    root.mkdir(mode=0o700, exist_ok=True)
    return str(_private_root(str(root)))


class NativeToolBridge:
    def __init__(self, adapter, *, socket_root):
        if type(adapter) is not NativeCoreTools:
            raise ValueError("native_tool_core_binding_invalid")
        self.adapter = adapter
        self.tools = adapter.manifest()
        self.manifest_digest = W.digest(self.tools)
        root = _private_root(socket_root)
        # A short random leaf keeps the endpoint under the AF_UNIX path limit.
        self.directory = tempfile.TemporaryDirectory(prefix="", dir=str(root))
        self.endpoint = str(Path(self.directory.name).resolve() / "core.sock")
        if len(os.fsencode(self.endpoint)) >= 104 or Path(self.directory.name).is_symlink():
            self.directory.cleanup()
            raise ValueError("native_tool_socket_location")
        self._server = None
        self._connections = set()
        self._ready = asyncio.Event()
        self._claim = ""
        self._closed = False

    def _active(self):
        claim = self.adapter.sessions.active_claim(self.adapter.run_id)
        if (self._closed or not claim or claim["invocation_id"] != self._claim
                or W.digest(self.adapter.manifest()) != self.manifest_digest):
            raise ValueError("native_tool_active_claim_required")

    def started(self):
        self._active()
        turn = self.adapter.sessions.turn(self._claim)
        if not turn or turn.state != "started":
            raise ValueError("native_tool_session_binding_invalid")
        self._ready.set()

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            if len(self._connections) > 8:
                raise ValueError("native_tool_wire_busy")
            async with asyncio.timeout(W.TIMEOUT):
                # read(n) may return a partial frame. EOF is the frame boundary.
                chunks, size = [], 0
                while True:
                    chunk = await reader.read(min(4096, W.MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > W.MAX_BYTES:
                        raise ValueError("native_tool_wire_oversize")
                request = W.decode(b"".join(chunks))
                if (type(request) is not dict or set(request) != {"version", "method", "body"}
                        or type(request["version"]) is not int or request["version"] != W.VERSION):
                    raise ValueError("native_tool_wire_invalid")
                self._active()
                if request["method"] == "manifest" and request["body"] == {}:
                    response = {"tools": self.adapter.manifest()}
                elif request["method"] == "call":
                    # stdout can be buffered briefly behind the native RPC.
                    # No call executes until Core records that turn's start.
                    await asyncio.wait_for(self._ready.wait(), timeout=5)
                    self._active()
                    response = await self.adapter.call(request["body"])
                else:
                    raise ValueError("native_tool_wire_invalid")
                writer.write(W.encode(response))
                await writer.drain()
        except (ValueError, TypeError, OSError, TimeoutError):
            # Never copy exception text or pretend an uncertain delivery failed
            # before dispatch. The native worker receives a closed connection.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass
            self._connections.discard(task)

    @asynccontextmanager
    async def open(self):
        if self._closed or self._server is not None:
            raise ValueError("native_tool_wire_already_open")
        claim = self.adapter.sessions.active_claim(self.adapter.run_id)
        if not claim:
            raise ValueError("native_tool_active_claim_required")
        self._claim = claim["invocation_id"]
        self._active()
        self._server = await asyncio.start_unix_server(self._serve, path=self.endpoint, limit=W.MAX_BYTES + 1)
        os.chmod(self.endpoint, 0o600)
        try:
            yield self
        finally:
            await self.close()

    async def close(self):
        self._closed = True
        if self._server is not None:
            self._server.close()
        pending = tuple(self._connections)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
        self.directory.cleanup()
