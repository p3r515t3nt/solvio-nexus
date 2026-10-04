"""Fixed public-source pre-read through the existing browser stdio MCP server.

Runs only in the configured browser Python. No model, source discovery,
interaction, account, task store or native turn belongs to this process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time

# -I excludes the script directory. Only the fixed reviewed sibling adapter
# and worker helpers belong on this standalone callback's import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_INPUT = 8_000
MAX_MATERIAL = 64_000
MAX_CALLS = 100
TEARDOWN_SECONDS = 10.0


class InvalidRead(ValueError):
    pass


class MaterialLimit(ValueError):
    pass


def _integer(value, low=0, high=2_000_000):
    return type(value) is int and low <= value <= high


def _snapshot_part(value, *, task_id, sid=None, offset=0):
    if (not isinstance(value, dict) or value.get("success") is not True
            or not isinstance(value.get("snapshot_id"), str)
            or not isinstance(value.get("sha256"), str)
            or len(value["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["sha256"])
            or value["snapshot_id"] != task_id + ":" + value["sha256"]
            or sid is not None and value["snapshot_id"] != sid
            or not _integer(value.get("characters"))
            or not _integer(value.get("offset")) or value["offset"] != offset
            or not isinstance(value.get("text"), str) or len(value["text"]) > 2_000):
        raise InvalidRead
    end = offset + len(value["text"])
    total = value["characters"]
    next_offset = value.get("next_offset")
    if (end > total or next_offset is not None and
            (not _integer(next_offset, 1) or next_offset != end or end >= total)
            or next_offset is None and end != total):
        raise InvalidRead
    return value


async def read_sources(args, urls, material_limit, run_id):
    import anyio
    from mcp import ClientSession, MCPError, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp_types import REQUEST_TIMEOUT
    from hermes_browser import (SERVER, TOOLS, CHUNK, closed_sources, control_dir,
                                mcp_entry, request_stop, validate_runtime)

    validate_runtime(args.browser_python, args.browser_bin, args.chrome, args.hermes_source)
    receipt = {"schema": 1, "task_id": args.task_id, "run_id": run_id, "phase": "direct_sources",
               "requested_sources": len(urls), "started_calls": 0,
               "completed_calls": 0, "complete_snapshots": 0, "tool_errors": 0,
               "hash_verified_snapshots": 0, "forwarded_bytes": 0, "redacted_snapshots": 0,
               "material_limits": 0, "cleanup": "unknown", "status": "invalid"}
    materials = []
    io_deadline = args.deadline - TEARDOWN_SECONDS
    interrupted = False
    catalogue_valid = False
    server_started = False
    scope = None

    def stop(*_):
        nonlocal interrupted
        interrupted = True
        request_stop(args.workdir, cancelled=True)
        if scope is not None:
            scope.cancel()

    old_term = signal.signal(signal.SIGTERM, stop)
    old_int = signal.signal(signal.SIGINT, stop)

    def remaining():
        return max(0.001, io_deadline - time.monotonic())

    async def call(session, name, arguments):
        if receipt["started_calls"] >= MAX_CALLS or time.monotonic() >= io_deadline:
            raise MaterialLimit
        receipt["started_calls"] += 1
        result = await session.call_tool(name, arguments,
                                         read_timeout_seconds=min(15.0, remaining()))
        receipt["completed_calls"] += 1
        if result.is_error:
            receipt["tool_errors"] += 1
            return None
        # The reviewed server returns exactly one JSON text block. Do not
        # forward arbitrary MCP payloads or error strings into the model.
        if len(result.content) != 1 or getattr(result.content[0], "type", None) != "text":
            raise InvalidRead
        value = json.loads(result.content[0].text)
        if not isinstance(value, dict) or value.get("success") is not True:
            raise InvalidRead
        return value

    try:
        if interrupted or io_deadline <= time.monotonic():
            receipt["status"] = "cancelled" if interrupted else "timeout"
            return {"receipt": receipt, "materials": []}
        entry = mcp_entry(python=args.browser_python, binary=args.browser_bin,
            chrome=args.chrome, hermes_python=args.hermes_python, source=args.hermes_source,
            task_id=args.task_id, workdir=args.workdir,
            timeout=max(0.05, args.deadline - time.monotonic()))
        params = StdioServerParameters(command=entry["command"], args=entry["args"],
            cwd=args.workdir, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        with anyio.CancelScope() as scope:
            if interrupted:
                scope.cancel()
            with anyio.fail_after(remaining()):
                # Raw native stderr and page errors are never returned.
                with open(os.devnull, "w") as errlog:
                    async with stdio_client(params, errlog=errlog) as streams:
                        server_started = True
                        async with ClientSession(*streams, read_timeout_seconds=min(15.0, remaining())) as session:
                            initialized = await session.initialize()
                            catalogue = await session.list_tools()
                            tools = catalogue.tools
                            if (initialized.server_info.name != SERVER
                                    or len(tools) != len(TOOLS)
                                    or {tool.name for tool in tools} != set(TOOLS)
                                    or getattr(catalogue, "next_cursor", None)
                                    or any(tool.annotations is None
                                        or tool.annotations.read_only_hint is not True
                                        or tool.annotations.destructive_hint is not False
                                        for tool in tools)):
                                raise InvalidRead
                            catalogue_valid = True
                            receipt["status"] = "completed"
                            try:
                                for url in urls:
                                    navigated = await call(session, "browser_navigate", {"url": url})
                                    if navigated is None:
                                        receipt["status"] = "tool_error"
                                        continue
                                    value = await call(session, "browser_snapshot", {"full": True})
                                    if value is None:
                                        receipt["status"] = "tool_error"
                                        continue
                                    if "full_snapshot" in value:
                                        part = _snapshot_part(value["full_snapshot"], task_id=args.task_id)
                                        if part["characters"] > material_limit:
                                            receipt["material_limits"] += 1
                                            receipt["status"] = "partial"
                                            continue
                                        sid, digest, total = part["snapshot_id"], part["sha256"], part["characters"]
                                        chunks = [part["text"]]
                                        while part["next_offset"] is not None:
                                            offset = part["next_offset"]
                                            value = await call(session, "browser_snapshot", {
                                                "snapshot_id": sid, "offset": offset, "limit": CHUNK})
                                            if value is None:
                                                receipt["status"] = "tool_error"
                                                break
                                            part = _snapshot_part(value, task_id=args.task_id, sid=sid, offset=offset)
                                            if part["sha256"] != digest or part["characters"] != total:
                                                raise InvalidRead
                                            chunks.append(part["text"])
                                        if part["next_offset"] is not None:
                                            continue
                                        text = "".join(chunks)
                                        if len(text) != total or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
                                            raise InvalidRead
                                        hashed = True
                                    elif isinstance(value.get("snapshot"), str):
                                        # Small full snapshots are returned directly by
                                        # the existing adapter, without a saved id.
                                        text = value["snapshot"]
                                        hashed = False
                                    else:
                                        raise InvalidRead
                                    candidate = materials + [{"source": url, "text": text}]
                                    if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > material_limit:
                                        receipt["material_limits"] += 1
                                        receipt["status"] = "partial"
                                        continue
                                    materials = candidate
                                    receipt["complete_snapshots"] += 1
                                    receipt["hash_verified_snapshots"] += int(hashed)
                            except MCPError as exc:
                                if exc.code != REQUEST_TIMEOUT:
                                    raise
                                # Handle before SDK task-group unwind wraps it.
                                # Only already complete checked snapshots survive.
                                receipt["status"] = "timeout"
                            request_stop(args.workdir, cancelled=receipt["status"] == "timeout")
        if interrupted or scope.cancel_called:
            receipt["status"] = "cancelled"
    except TimeoutError:
        receipt["status"] = "timeout"
    except MaterialLimit:
        receipt["material_limits"] += 1
        receipt["status"] = "partial"
    except Exception:
        # Structural/catalogue/hash failures cannot authorize a model turn.
        receipt["status"] = "invalid"
        receipt["complete_snapshots"] = 0
        receipt["hash_verified_snapshots"] = 0
        materials = []
    finally:
        if receipt["status"] in {"cancelled", "timeout", "invalid"}:
            request_stop(args.workdir, cancelled=True)
        else:
            request_stop(args.workdir)
        try:
            ready = list(control_dir(args.workdir).glob("ready-*.json"))
            # closed_sources([]) is intentionally not a proof that a spawned
            # server existed or shut down; require its own ready marker.
            if not server_started or not ready:
                raise InvalidRead
            closed_sources(args.workdir, args.task_id,
                           timeout=min(2.5, max(0, args.deadline - time.monotonic())))
            receipt["cleanup"] = "closed"
        except Exception:
            receipt["cleanup"] = "unknown"
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    if not catalogue_valid:
        receipt["status"] = "invalid" if not interrupted else "cancelled"
    return {"receipt": receipt, "materials": materials}


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    for name in ("browser-python", "hermes-source", "hermes-python", "browser-bin",
                 "chrome", "task-id", "workdir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    args = parser.parse_args()
    from hermes_browser import TASK
    from hermes_native_worker import _source_url
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    body = json.loads(raw.decode("utf-8"))
    urls, limit, run_id = body.get("urls"), body.get("material_limit"), body.get("run_id")
    work = Path(args.workdir)
    if (len(raw) > MAX_INPUT or not math.isfinite(args.deadline)
            or not TASK.fullmatch(args.task_id) or not work.is_absolute()
            or work.is_symlink() or not work.is_dir() or work.stat().st_mode & 0o077
            or not isinstance(urls, list) or not 1 <= len(urls) <= 3
            or any(not isinstance(url, str) or _source_url(url) != url for url in urls)
            or len(set(urls)) != len(urls)
            or not isinstance(run_id, str) or not re.fullmatch(r"ar-[a-f0-9]{16}", run_id)
            or not _integer(limit, 1, MAX_MATERIAL) or set(body) != {"urls", "material_limit", "run_id"}):
        raise InvalidRead
    import anyio
    result = anyio.run(read_sources, args, urls, limit, run_id)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
