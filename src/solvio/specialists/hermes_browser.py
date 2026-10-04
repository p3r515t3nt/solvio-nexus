"""Small stdio MCP bridge to installed Hermes browser tools, never an agent loop.

Only this callback process imports browser dependencies. Task and runtime paths
are Core-owned argv; page/tool text cannot select a profile, process or account.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import secrets
import shutil
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

SERVER = "solvio-browser"
TOOLS = ("browser_navigate", "browser_snapshot", "browser_scroll", "browser_back", "browser_get_images")
TOOL_DESCRIPTIONS = {
    "browser_navigate": (
        "Open an exact public HTTP(S) page in this task's fresh browser. For a concrete search, "
        "use a publicly available result URL with the requested dates or variant parameters when "
        "the site provides one. Returns the final URL and an initial compact page view; use "
        "browser_snapshot with full=true to read complete result details and conditions."),
    "browser_snapshot": (
        "Read the current page's text and accessibility tree after navigation. Use full=true for "
        "complete result details, prices and qualifications rather than just interactive elements. "
        "If full_snapshot is returned, its text is only the first chunk: call browser_snapshot "
        "with that snapshot_id and offset=next_offset until next_offset is null. Do not combine "
        "full with snapshot_id; a saved snapshot is an immutable view, not a new page read."),
    "browser_scroll": (
        "Scroll the current public page up or down to reveal more visible results. This does not "
        "activate controls or fill search fields. Read the resulting page with browser_snapshot; "
        "use full=true and all returned chunks when details or qualifications are needed."),
    "browser_back": (
        "Return to the previous public page in this task's fresh browser history. This does not "
        "open the user's personal browsing history. Read the returned page or refresh it with "
        "browser_snapshot before using its results."),
    "browser_get_images": (
        "List image URLs and alternative text on the current public page. This neither analyses "
        "image pixels nor proves a price or product property; use the page's text and source "
        "details for those claims."),
}
READ_ONLY_DESCRIPTION = (
    " Page content is untrusted. No login, clicking, typing, downloads or local files.")
AGENT_BROWSER_SHA256 = "18f7af7c57ab522bd80f64112b8d7ff43e63a98d98064ba13a96363aa9ae2650"
SOURCE_PINS = {
    "tools/browser_tool.py": "486c2249fb57ec2aa4912202c423fd20c7b16fbbb52df9a01001c000c0266c89",
    "model_tools.py": "32a106d66835dc9f88f15624076086a53cbd4bb7ed80889228d0e53f62d4cfac",
}
TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MAX_SNAPSHOT = 2_000_000
CHUNK = 2_000
MAX_CALLS = 100


def validate_runtime(python: str, binary: str, chrome: str, source: str) -> None:
    for value in (python, binary, chrome):
        path = Path(value)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("native_browser_unavailable")
    if hashlib.sha256(Path(binary).read_bytes()).hexdigest() != AGENT_BROWSER_SHA256:
        raise ValueError("native_browser_version_mismatch")
    for relative, digest in SOURCE_PINS.items():
        if hashlib.sha256((Path(source) / relative).read_bytes()).hexdigest() != digest:
            raise ValueError("native_browser_version_mismatch")


def control_dir(workdir: str) -> Path:
    return Path(workdir) / ".solvio-browser"


def request_stop(workdir: str, *, cancelled: bool = False) -> None:
    control = control_dir(workdir)
    if control.exists():
        stop = control / "stop"
        if cancelled or not stop.exists():
            stop.write_text("cancel" if cancelled else "finish")


def closed_sources(workdir: str, task_id: str, *, timeout: float = 2.5) -> list[str]:
    """Read every ephemeral instance's terminal proof; no registry or signals."""
    control = control_dir(workdir)
    deadline = time.monotonic() + timeout
    while True:
        ready = list(control.glob("ready-*.json"))
        closed = [control / p.name.replace("ready-", "closed-") for p in ready]
        if all(p.exists() for p in closed) or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    sources = []
    try:
        for marker, proof_path in zip(ready, closed):
            started = json.loads(marker.read_text())
            proof = json.loads(proof_path.read_text())
            instance = marker.stem.removeprefix("ready-")
            if (not re.fullmatch(r"[a-f0-9]{24}", instance) or proof.get("task_id") != task_id
                    or started.get("task_id") != task_id or proof.get("closed") is not True
                    or proof.get("instance") != instance or started.get("instance") != instance
                    or type(proof.get("llm_calls")) is not int or proof["llm_calls"] != 0):
                raise ValueError("native_browser_cleanup_failed")
            for source in proof.get("sources", []):
                if isinstance(source, str) and source not in sources and len(sources) < 12:
                    sources.append(source)
    except (OSError, ValueError, TypeError):
        raise ValueError("native_browser_cleanup_failed") from None
    return sources


def mcp_entry(*, python: str, binary: str, chrome: str, hermes_python: str,
              source: str, task_id: str, workdir: str, timeout: float) -> dict:
    if not TASK.fullmatch(task_id):
        raise ValueError("native_browser_binding_invalid")
    return {
        "command": python,
        "args": ["-I", "-B", str(Path(__file__).resolve()), "--hermes-source", source,
                 "--hermes-python", hermes_python, "--browser-bin", binary,
                 "--chrome", chrome, "--task-id", task_id, "--workdir", workdir,
                 "--timeout", str(timeout)],
        "cwd": workdir, "env_vars": [], "enabled": True, "required": True,
        # Native config/read represents durations as floats even in its raw
        # CLI layer. Emit that measured type so exact comparison stays strict.
        "enabled_tools": list(TOOLS), "startup_timeout_sec": 15.0,
        "tool_timeout_sec": 35.0,
    }


def codex_mcp_args(entry: dict) -> list[str]:
    """Codex -c values are TOML; nested JSON objects would become strings."""
    args = []
    for key, value in entry.items():
        args += ["-c", f"mcp_servers.{SERVER}.{key}=" + json.dumps(value)]
    return args


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


class Browser:
    """One process, one task, upstream dispatch and process cleanup."""
    def __init__(self, args):
        self.args = args
        self.task_id = args.task_id
        if not TASK.fullmatch(self.task_id):
            raise ValueError("native_browser_binding_invalid")
        validate_runtime(sys.executable, args.browser_bin, args.chrome, args.hermes_source)
        self.control = control_dir(args.workdir)
        self.control.mkdir(mode=0o700, exist_ok=True)
        if self.control.is_symlink() or self.control.stat().st_mode & 0o077:
            raise ValueError("native_browser_workspace_invalid")
        # macOS Unix socket names have a short fixed limit. Never let upstream
        # orphan cleanup inspect another task's shared /tmp browser directory.
        self.root = Path(tempfile.mkdtemp(prefix="snxb-", dir="/tmp")).resolve()
        self.instance = secrets.token_hex(12)
        self.finished = self.control / ("closed-" + self.instance + ".json")
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.closed = False
        self.closing = False
        self.stop_requested = False
        self.cancelled = False
        self.lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.worker_id = None
        self.done = threading.Event()
        self.done.set()
        self.calls = 0
        self.saved = {}
        self.current = None
        self.bound = {}
        self.processes = {}
        self.llm_calls = 0
        self.sources = []
        self.successful_tools = []
        self.started = time.time()
        self.deadline = time.monotonic() + args.timeout
        # No CI (even CI=0 disables upstream Chrome sandbox), inherited CDP,
        # cookies, cloud settings, proxies, keychain, API keys or user profile.
        os.environ.clear()
        os.environ.update({"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8", "TMPDIR": str(self.root), "HERMES_HOME": str(self.home),
            "AGENT_BROWSER_CONFIG": str(self.home / "agent-browser.json"),
            "AGENT_BROWSER_EXECUTABLE_PATH": args.chrome,
            "AGENT_BROWSER_IDLE_TIMEOUT_MS": "60000",
            "AGENT_BROWSER_ARGS": "--disable-extensions"})
        (self.home / "agent-browser.json").write_text("{}\n")
        (self.home / "config.yaml").write_text(
            "browser:\n  cloud_provider: local\n  engine: chrome\n  headed: false\n"
            "  command_timeout: 25\n  auto_local_for_private_urls: false\n"
            "  allow_private_urls: false\nsecurity:\n  allow_lazy_installs: false\n")
        sys.path.insert(0, args.hermes_source)
        packages = Path(args.hermes_python).parent.parent / "lib" / (
            "python" + str(sys.version_info.major) + "." + str(sys.version_info.minor)) / "site-packages"
        if not packages.is_dir():
            raise ValueError("native_browser_unavailable")
        sys.path.append(str(packages))
        with contextlib.redirect_stdout(sys.stderr):
            from model_tools import handle_function_call
            from tools import browser_tool, process_registry, interrupt, url_safety
        import psutil
        self.B = browser_tool
        self.PR = process_registry.ProcessRegistry
        self.interrupt = interrupt
        self.psutil = psutil
        self.public_url = url_safety.is_safe_url
        self.dispatch_upstream = handle_function_call
        if self.B._needs_chromium_sandbox_bypass():
            raise ValueError("native_browser_sandbox_unavailable")
        self.B._find_agent_browser = lambda: args.browser_bin
        self.B._socket_safe_tmpdir = lambda: str(self.root)
        self.B._lazy_call_llm = self.forbidden_llm
        self.B._extract_relevant_content = self.forbidden_llm
        self.original_store = self.B._store_full_snapshot
        self.B._store_full_snapshot = self.store
        self.original_terminate = self.PR._terminate_host_pid
        owner = self

        def terminate(cls, pid, expected_start=None):
            owner.terminate_bound(pid, expected_start)

        self.PR._terminate_host_pid = classmethod(terminate)
        self.original_command = self.B._run_browser_command

        def command(task_id, name, *a, **kw):
            if self.closed:
                if name == "close":
                    return {"success": True}
                return {"success": False, "error": "Browser task ended"}
            return self.original_command(task_id, name, *a, **kw)

        self.B._run_browser_command = command
        self.schemas = {s["name"]: dict(s["parameters"]) for s in self.B.BROWSER_TOOL_SCHEMAS if s["name"] in TOOLS}
        self.schemas["browser_snapshot"] = {"type": "object", "properties": {
            "full": {"type": "boolean"}, "snapshot_id": {"type": "string", "maxLength": 200},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": CHUNK}}}
        for schema in self.schemas.values():
            schema["additionalProperties"] = False
        _atomic_json(self.control / ("ready-" + self.instance + ".json"), {
            "task_id": self.task_id, "instance": self.instance, "pid": os.getpid(), "root": str(self.root)})

    def forbidden_llm(self, *a, **kw):
        self.llm_calls += 1
        raise ValueError("native_browser_model_forbidden")

    def request_instance_stop(self, *_):
        # Native clients may terminate a temporary catalogue instance while a
        # second instance serves the same task. Only Core writes the shared
        # task stop file; an OS signal ends this process's own browser.
        self.stop_requested = True

    def store(self, text):
        if len(text) > MAX_SNAPSHOT:
            raise ValueError("native_browser_snapshot_too_large")
        path = self.original_store(text)
        if path is None:
            raise ValueError("native_browser_snapshot_missing")
        path = Path(path).resolve()
        if path.parent != self.home / "cache/web":
            raise ValueError("native_browser_snapshot_invalid")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        sid = self.task_id + ":" + digest
        self.saved[sid] = (path, digest)
        self.current = sid
        return str(path)

    def read_snapshot(self, args):
        saved = self.saved.get(args["snapshot_id"])
        if not saved or "full" in args:
            raise ValueError("native_browser_snapshot_invalid")
        path, digest = saved
        data = path.read_bytes()
        if path.is_symlink() or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("native_browser_snapshot_changed")
        text = data.decode("utf-8")
        offset = args.get("offset", 0)
        if offset > len(text):
            raise ValueError("native_browser_snapshot_invalid")
        end = min(len(text), offset + args.get("limit", CHUNK))
        return {"success": True, "snapshot_id": args["snapshot_id"], "sha256": digest,
                "characters": len(text), "offset": offset, "next_offset": end if end < len(text) else None,
                "text": text[offset:end]}

    def check_url(self, url):
        p = urlsplit(url)
        if (p.scheme not in {"http", "https"} or p.username or p.password
                or self.B._sensitive_query_param_name(url) or not self.public_url(url)):
            raise ValueError("native_browser_public_url_required")

    def observe(self):
        if set(self.B._active_sessions) - {self.task_id}:
            raise ValueError("native_browser_binding_invalid")
        for session in self.B._active_sessions.copy().values():
            name = session["session_name"]
            directory = self.root / ("agent-browser-" + name)
            pidfile = directory / (name + ".pid")
            if not pidfile.exists():
                continue
            pid = int(pidfile.read_text())
            if not self.psutil.pid_exists(pid):
                continue
            if not self.B._verify_reapable_browser_daemon(pid, str(directory), name):
                raise ValueError("native_browser_process_unbound")
            start = self.PR._safe_host_start_time(pid)
            if type(start) is not int or start < int(self.started * 100) - 100:
                raise ValueError("native_browser_process_unbound")
            binding = (start, str(directory), name)
            if pid in self.bound and self.bound[pid] != binding:
                raise ValueError("native_browser_process_unbound")
            self.bound[pid] = binding
            parent = self.psutil.Process(pid)
            for process in [parent, *parent.children(recursive=True)]:
                try:
                    self.processes[process.pid] = process.create_time()
                except self.psutil.NoSuchProcess:
                    pass

    def terminate_bound(self, pid, expected_start=None):
        binding = self.bound.get(pid)
        if not binding:
            raise ValueError("native_browser_process_unbound")
        start, directory, name = binding
        if expected_start is not None and expected_start != start:
            raise ValueError("native_browser_process_unbound")
        if not self.psutil.pid_exists(pid):
            return
        if (not self.PR._host_pid_is_ours(pid, start)
                or not self.B._verify_reapable_browser_daemon(pid, directory, name)):
            raise ValueError("native_browser_process_unbound")
        self.original_terminate(pid, expected_start=start)

    def dispatch(self, name, args):
        import jsonschema
        with self.lock:
            if self.closed or self.closing or time.monotonic() >= self.deadline or self.calls >= MAX_CALLS:
                raise ValueError("native_browser_closed")
            if name not in self.schemas:
                raise ValueError("native_browser_tool_forbidden")
            jsonschema.validate(args, self.schemas[name])
            self.calls += 1
            if name == "browser_snapshot" and "snapshot_id" in args:
                result = self.read_snapshot(args)
                if name not in self.successful_tools:
                    self.successful_tools.append(name)
                return result
            if name == "browser_snapshot" and set(args) - {"full"}:
                raise ValueError("native_browser_snapshot_invalid")
            if name == "browser_navigate":
                self.check_url(args["url"])
            else:
                if not self.B._active_sessions:
                    raise ValueError("native_browser_navigation_required")
                # Check current URL before emitting page text after redirects or
                # script navigation. This is not a subrequest network firewall.
                current = self.original_command(self.task_id, "get", ["url"])
                url = (current.get("data") or {}).get("url")
                if url:
                    self.check_url(url)
            self.worker_id = threading.get_ident()
            self.done.clear()
            self.current = None
            try:
                raw = self.dispatch_upstream(name, args, task_id=self.task_id, user_task=None,
                                              enabled_tools=list(TOOLS), enabled_toolsets=["browser"])
                result = json.loads(raw)
                if self.closed:
                    raise ValueError("native_browser_closed")
                if name == "browser_navigate" and result.get("success"):
                    self.check_url(result.get("url", ""))
                    if result["url"] not in self.sources:
                        self.sources.append(result["url"])
                elif result.get("success"):
                    current = self.original_command(self.task_id, "get", ["url"])
                    url = (current.get("data") or {}).get("url")
                    if not url:
                        raise ValueError("native_browser_public_url_required")
                    self.check_url(url)
                if not self.current and isinstance(result.get("snapshot"), str) and len(result["snapshot"]) > CHUNK:
                    self.store(result["snapshot"])
                if self.current:
                    # Always return a bounded first block and an opaque complete
                    # readback id, never a path the model could use as file access.
                    result.pop("snapshot", None)
                    result["full_snapshot"] = self.read_snapshot({"snapshot_id": self.current})
                result.pop("stealth_warning", None)
                if len(json.dumps(result, ensure_ascii=False)) > 20_000:
                    raise ValueError("native_browser_output_too_large")
                self.observe()
                if result.get("success") is True and name not in self.successful_tools:
                    self.successful_tools.append(name)
                return result
            finally:
                self.done.set()

    def close(self, *, cancelled=False):
        with self.close_lock:
            if self.finished.exists():
                return
            self.closing = True
            # Signal upstream's existing reaper before waiting for processes.
            # Its one-second sleep must overlap the TERM grace, not add to it;
            # the existing stop helper still joins the thread below.
            self.B._cleanup_running = False
            self.cancelled = self.cancelled or cancelled
            try:
                # A normal CLI "close" can start a fresh daemon after the
                # browser's idle exit, and may block for upstream's ten-second
                # command timeout. Teardown must never create a browser. Use
                # the existing identity-bound process reaper for both exits;
                # cleanup_browser still owns its session/socket bookkeeping.
                self.closed = True
                if cancelled and self.worker_id is not None:
                    self.interrupt.set_interrupt(True, self.worker_id)
                self.observe()
                for pid in self.bound:
                    self.terminate_bound(pid)
                self.B.cleanup_browser(self.task_id)
                self.B._stop_browser_cleanup_thread()
                self.done.wait(0.5)
                survivors = []
                for pid, created in self.processes.items():
                    try:
                        p = self.psutil.Process(pid)
                        if p.create_time() == created and p.status() != self.psutil.STATUS_ZOMBIE:
                            survivors.append(pid)
                    except self.psutil.NoSuchProcess:
                        pass
                if survivors or self.B._active_sessions or not self.done.is_set():
                    raise ValueError("native_browser_cleanup_failed")
                shutil.rmtree(self.root)
                _atomic_json(self.finished, {"task_id": self.task_id, "instance": self.instance, "closed": True,
                    "cancelled": self.cancelled, "calls": self.calls, "llm_calls": self.llm_calls,
                    "sources": self.sources[:12], "successful_tools": self.successful_tools})
            except Exception:
                _atomic_json(self.finished, {"task_id": self.task_id, "instance": self.instance, "closed": False})
                raise


async def serve(browser):
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    import anyio

    async def list_tools(context, params):
        return types.ListToolsResult(tools=[types.Tool(name=name, inputSchema=schema,
            description=TOOL_DESCRIPTIONS[name] + READ_ONLY_DESCRIPTION,
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False))
            for name, schema in browser.schemas.items()])

    async def call_tool(context, params):
        try:
            value = await asyncio.to_thread(browser.dispatch, params.name, params.arguments or {})
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value))],
                                        isError=not value.get("success", False))
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(lambda: browser.close(cancelled=True))
            raise
        except Exception as exc:
            reason = str(exc) if str(exc).startswith("native_browser_") else "native_browser_tool_failed"
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"error": reason}))], isError=True)

    server = Server(SERVER, on_list_tools=list_tools, on_call_tool=call_tool)
    async def watch(task):
        while not task.done():
            await asyncio.sleep(0.05)
            # The existing stdio transport owns disconnect/EOF. A native MCP
            # launcher may reparent a process; its transient parent PID is not
            # the lifetime of the still-open MCP connection.
            if (browser.stop_requested or (browser.control / "stop").exists()
                    or time.monotonic() >= browser.deadline):
                orderly = (not browser.stop_requested and (browser.control / "stop").exists()
                           and (browser.control / "stop").read_text() == "finish"
                           and browser.done.is_set())
                await asyncio.to_thread(browser.close, cancelled=not orderly)
                task.cancel()
                return

    async with stdio_server() as streams:
        task = asyncio.create_task(server.run(*streams, server.create_initialization_options()))
        watcher = asyncio.create_task(watch(task))
        try:
            await task
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


def main():
    parser = argparse.ArgumentParser()
    for name in ("hermes-source", "hermes-python", "browser-bin", "chrome", "task-id", "workdir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    if not 0.05 <= args.timeout <= 3600:
        raise ValueError("native_browser_timeout_invalid")
    browser = Browser.__new__(Browser)
    try:
        Browser.__init__(browser, args)
        signal.signal(signal.SIGTERM, browser.request_instance_stop)
        signal.signal(signal.SIGINT, browser.request_instance_stop)
        asyncio.run(serve(browser))
    finally:
        if hasattr(browser, "original_command"):
            browser.close(cancelled=not browser.done.is_set())
        elif hasattr(browser, "root"):
            shutil.rmtree(browser.root)


if __name__ == "__main__":
    main()
