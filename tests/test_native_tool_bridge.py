"""Private Unix IPC with actual child Python, Core router, vault and cost ledger.

No model, account, portal worker, production state or alternative service handler.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_native_core_tools as C
from solvio.agent_runtime.native_tool_bridge import NativeToolBridge


CHILD = r'''
import json, os, socket, sys
sys.path.insert(0, sys.argv[1])
from solvio.specialists.native_tool_wire import NativeToolClient, encode
request = json.load(sys.stdin)
client = NativeToolClient(request["endpoint"], request["digest"])
replies = []
for action in request["actions"]:
    try:
        if action["method"] == "manifest":
            value = client.manifest()
        elif action["method"] == "call":
            value = client.call(action["body"])
        else:
            raw = action.get("raw", "").encode()
            if action["method"] == "disconnect":
                raw = encode({"version": 1, "method": "call", "body": action["body"]})
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(10)
                connection.connect(request["endpoint"])
                connection.sendall(raw)
                connection.shutdown(socket.SHUT_WR)
                data = b""
                if action["method"] != "disconnect":
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                value = data.decode()
        replies.append({"value": value})
    except (ValueError, OSError) as exc:
        replies.append({"error": type(exc).__name__})
        # A manifest mismatch ends this worker; never attempt the tool call.
        if action["method"] == "manifest":
            break
print(json.dumps({"pid": os.getpid(), "replies": replies}))
'''


async def client(bridge, actions, *, digest=None):
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", CHILD,
        str(Path(__file__).resolve().parents[1] / "src"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(process.communicate(json.dumps({
            "endpoint": bridge.endpoint, "digest": digest or bridge.manifest_digest,
            "actions": actions}).encode()), timeout=15)
        require_equal(process.returncode, 0, error.decode())
        result = json.loads(output)
        require(result["pid"] != os.getpid(), "IPC must cross a real process boundary")
        return result["replies"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


CALL = {"method": "call", "body": C.BODY}


async def t_external_process_manifest_and_replay_preserve_one_real_service_dispatch():
    with C.world() as w:
        bridge = NativeToolBridge(w.tools, socket_root=w.socket_root)
        async def native():
            async with bridge.open():
                bridge.started()
                replies = await client(bridge, [{"method": "manifest"}, CALL, CALL])
                require_equal(replies[0]["value"], w.tools.manifest())
                first = replies[1]["value"]
                require(first["success"], first)
                require_equal(replies[2]["value"], first)
                require(w.accesses, "actual local vault read missing")
                reads = len(w.accesses)
                require_equal((await client(bridge, [CALL]))[0]["value"], first)
                require_equal(len(w.accesses), reads)
                require_equal(len(C.tool_rows(w)), 1)
                local = [v for v in C.D.invocations(w.ledger, w.task)
                         if v["provider"] == "local.portal-catalog"]
                require_equal(len(local), 1)
                require_equal(local[0]["state"], "finished")
        outcome = await C.dispatch(w, native)
        require_equal(outcome.cost_status, "settled")
        require(not Path(bridge.endpoint).parent.exists())


async def t_bad_manifest_native_ids_and_frames_never_reach_local_service():
    with C.world() as w:
        bridge = NativeToolBridge(w.tools, socket_root=w.socket_root)
        async def native():
            async with bridge.open():
                bridge.started()
                replies = await client(bridge, [{"method": "manifest"}, CALL], digest="0" * 64)
                require_equal(replies, [{"error": "ValueError"}])
                malformed = ["", '{"version":1,"version":1,"method":"manifest","body":{}}',
                    '{"version":true,"method":"manifest","body":{}}',
                    '{"version":1,"method":"call","body":NaN}',
                    '{"version":1,"method":"manifest","body":{},"extra":true}', "x" * 65537]
                replies = await client(bridge, [{"method": "raw", "raw": raw} for raw in malformed])
                require(all(v == {"value": ""} or "error" in v for v in replies), replies)
                changes = ({"threadId": "other"}, {"turnId": "other"},
                           {"tool": "portal_open"}, {"arguments": {"approved": True}})
                replies = await client(bridge, [{"method": "call", "body": dict(C.BODY, **v)} for v in changes])
                require(all(v["value"]["success"] is False for v in replies), replies)
                require_equal(w.accesses, [])
                require_equal(C.tool_rows(w), [])
        await C.dispatch(w, native)


async def t_call_waits_for_recorded_start_callback_before_any_service_admission():
    with C.world() as w:
        bridge = NativeToolBridge(w.tools, socket_root=w.socket_root)
        async def native():
            async with bridge.open():
                require_equal((await client(bridge, [{"method": "manifest"}]))[0]["value"], bridge.tools)
                pending = asyncio.create_task(client(bridge, [CALL]))
                await until(lambda: bridge._connections)
                await asyncio.sleep(0.05)
                require(not pending.done())
                require_equal(C.tool_rows(w), [])
                require_equal(w.accesses, [])
                bridge.started()
                require((await pending)[0]["value"]["success"])
        await C.dispatch(w, native)


async def t_revocation_and_cancellation_after_open_deny_service_and_endpoint_reuse():
    for cause in ("revoked", "cancelled"):
        with C.world() as w:
            bridge = NativeToolBridge(w.tools, socket_root=w.socket_root)
            async def native():
                async with bridge.open():
                    bridge.started()
                    if cause == "revoked":
                        w.authority.revoke(w.grant.reference, "test:bridge-revoked")
                    else:
                        w.tools.cancel_token.set()
                    result = (await client(bridge, [CALL]))[0]
                    require("error" in result or result["value"]["success"] is False, result)
                    require_equal(w.accesses, [])
                    require_equal(C.tool_rows(w), [])
                require(not Path(bridge.endpoint).parent.exists())
                require_equal(bridge._connections, set())
                try:
                    async with bridge.open():
                        require(False, "closed bridge was reused")
                except ValueError:
                    pass
            await C.dispatch(w, native)


async def t_bridge_close_during_quote_retains_unknown_and_refuses_new_call_retry():
    with C.world() as w:
        bridge = NativeToolBridge(w.tools, socket_root=w.socket_root)
        async def native():
            entered, release = asyncio.Event(), asyncio.Event()
            async def pause():
                entered.set()
                await release.wait()
            async with bridge.open():
                bridge.started()
                with C.P.quote_gate(pause):
                    pending = asyncio.create_task(client(bridge, [CALL]))
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    await bridge.close()
                    require_equal(await pending, [{"error": "ValueError"}])
                require_equal(C.tool_rows(w)[0]["state"], "unknown")
                require_equal(w.ledger.get_step(C.tool_rows(w)[0]["step_id"]).state, "unknown")
                require_equal(w.sessions.turn(w.invocation).state, "started")
                require_equal(w.accesses, [])
            restored = NativeToolBridge(w.tools, socket_root=w.socket_root)
            async with restored.open():
                restored.started()
                replies = await client(restored, [CALL, {"method": "call", "body": dict(C.BODY, callId="new-call")}])
                require(all(C.payload(v["value"])["error"] == "native_tool_recovery_required" for v in replies))
                require_equal(len(C.tool_rows(w)), 1)
                require_equal(w.accesses, [])
        await C.dispatch(w, native)


async def t_peer_disconnect_after_admission_preserves_receipt_without_reexecuting():
    with C.world() as w:
        bridge = NativeToolBridge(w.tools, socket_root=w.socket_root)
        async def native():
            entered, release = asyncio.Event(), asyncio.Event()
            async def pause():
                entered.set()
                await release.wait()
            async with bridge.open():
                bridge.started()
                with C.P.quote_gate(pause):
                    pending = asyncio.create_task(client(bridge, [{"method": "disconnect", "body": C.BODY}]))
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    require_equal(await pending, [{"value": ""}])
                    require_equal(C.tool_rows(w)[0]["state"], "running")
                    require_equal(w.accesses, [])
                    release.set()
                    await until(lambda: not bridge._connections)
                require_equal(C.tool_rows(w)[0]["state"], "completed")
                reads = len(w.accesses)
                require(reads > 0)
                result = (await client(bridge, [CALL]))[0]["value"]
                require(result["success"], result)
                require_equal(len(w.accesses), reads)
                require_equal(len(C.tool_rows(w)), 1)
                # Delivery of a local result never marks the whole native turn complete.
                require_equal(w.sessions.turn(w.invocation).state, "started")
        await C.dispatch(w, native)


def t_endpoint_root_must_be_private_canonical_and_outside_the_shared_temp_tree():
    """Measured 2026-09-17: /tmp stays reachable from the native sandbox.

    The bridge therefore refuses a root there before any endpoint exists, and
    refuses roots the native shell could reach by other means: non-private
    modes, symlinks, foreign owners. It also refuses an endpoint that would
    exceed the AF_UNIX path limit instead of silently truncating it.
    """
    import stat
    import tempfile
    from _private_temp import private_folder
    from unittest.mock import patch
    from solvio.agent_runtime.native_tool_bridge import socket_root
    with private_folder("sk-ledger-") as ledger_base, \
            patch("tempfile.tempdir", str(ledger_base)), C.world() as w:
        def refused(root):
            try:
                NativeToolBridge(w.tools, socket_root=root)
            except ValueError as exc:
                require_equal(str(exc), "native_tool_socket_location", root)
                return
            require(False, "unsafe socket root accepted: " + str(root))
        for shared in ("/tmp", "/private/tmp"):
            if Path(shared).is_dir():
                with tempfile.TemporaryDirectory(dir=shared) as folder:
                    os.chmod(folder, 0o700)
                    refused(str(Path(folder).resolve()))
                    refused(folder)  # unresolved spelling of the same place
        base = Path(w.socket_root)
        loose = base / "loose"
        loose.mkdir(mode=0o750)
        refused(str(loose))
        link = base / "link"
        link.symlink_to(base)
        refused(str(link))
        refused(str(base / "absent"))
        refused("relative/root")
        deep = base / ("d" * max(1, 104 - len(os.fsencode(str(base))) - 12))
        deep.mkdir(mode=0o700)
        refused(str(deep))
        require(not any(child.is_dir() and child.name not in {"loose", "link"} and not child.name.startswith("d")
                        for child in base.iterdir()), "refused bridge left a leaf behind")
        # The runtime derivation beside the ledger is itself private and canonical.
        derived = Path(socket_root(w.ledger.path))
        require_equal(derived.name, "native-sockets")
        require_equal(derived.parent, Path(w.ledger.path).resolve().parent)
        require_equal(stat.S_IMODE(derived.stat().st_mode), 0o700)
        require(not derived.is_relative_to("/tmp") and not derived.is_relative_to("/private/tmp"))
        bridge = NativeToolBridge(w.tools, socket_root=w.socket_root)
        try:
            endpoint = Path(bridge.endpoint)
            require_equal(endpoint.name, "core.sock")
            require_equal(endpoint.parent.parent, base)
            require(len(os.fsencode(bridge.endpoint)) < 104)
            require_equal(stat.S_IMODE(endpoint.parent.stat().st_mode), 0o700)
        finally:
            asyncio.run(bridge.close())
        require(not endpoint.parent.exists(), "leaf directory survives close")


async def t_two_tool_manifest_crosses_the_wire_and_result_files_list_answers_from_a_jail_style_root():
    """N8/C4 §4: the closed manifest reaches a real child process; a
    result_files_list call is served only after the recorded start; the
    socket root may be a private per-task leaf (a Claude jail's sock/)."""
    import stat
    from solvio.specialists import native_tool_wire as W
    with C.tools_world() as w:
        # Base + "cj/<leaf>/sock" stays under the AF_UNIX limit; a real jail
        # under ~/.solvio-tasks/claude-jails/<task_id>/sock is shorter still.
        jail = Path(w.socket_root) / "cj" / w.task[:6] / "sock"
        jail.mkdir(parents=True, mode=0o700)
        for part in (jail.parent.parent, jail.parent):
            part.chmod(0o700)
        C.deliver(w, w.run, "bericht.txt", b"eigener Bericht\n")
        bridge = NativeToolBridge(w.tools, socket_root=str(jail))
        require_equal([t["name"] for t in bridge.tools], ["portal_list", "result_files_list"])
        require_equal(bridge.manifest_digest, W.digest(w.tools.manifest()))
        require(Path(bridge.endpoint).is_relative_to(jail))
        require_equal(stat.S_IMODE(Path(bridge.endpoint).parent.stat().st_mode), 0o700)
        listing = {"method": "call", "body": C.LIST}
        async def native():
            async with bridge.open():
                manifest = (await client(bridge, [{"method": "manifest"}]))[0]["value"]
                require_equal([t["name"] for t in manifest], ["portal_list", "result_files_list"])
                pending = asyncio.create_task(client(bridge, [listing]))
                await until(lambda: bridge._connections)
                await asyncio.sleep(0.05)
                require(not pending.done(), "a call must wait for the recorded turn start")
                require_equal(C.tool_rows(w), [])
                bridge.started()
                reply = (await pending)[0]["value"]
                require(reply["success"], reply)
                files = C.payload(reply)["data"]["dateien"]
                require_equal([(f["name"], f["run_id"]) for f in files], [("bericht.txt", w.run)])
                foreign = (await client(bridge, [{"method": "call", "body": dict(C.LIST, arguments={"run_id": "x"})},
                                                 {"method": "call", "body": dict(C.LIST, tool="memory_recall")}]))
                require(all(v["value"]["success"] is False for v in foreign), foreign)
                require_equal(len(C.tool_rows(w)), 1)
                require_equal(w.accesses, [])
        outcome = await C.dispatch(w, native)
        require_equal(outcome.cost_status, "settled")
        require(not Path(bridge.endpoint).parent.exists())
        # A manifest with the old single-tool digest is refused by the same wire.
        stale = NativeToolBridge(w.tools, socket_root=str(jail))
        async def stale_native():
            async with stale.open():
                stale.started()
                single = W.digest([t for t in w.tools.manifest() if t["name"] == "portal_list"])
                require_equal(await client(stale, [{"method": "manifest"}, listing], digest=single),
                              [{"error": "ValueError"}])
                require_equal(len(C.tool_rows(w)), 1)
        await C.dispatch(w, stale_native)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
