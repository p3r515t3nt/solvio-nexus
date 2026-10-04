"""Native callbacks through real temporary Core router, portal handler and costs.

The native provider response is simulated inside actual CostDispatch. The
portal worker is a tripwire; the original local handler reads an absent vault.
"""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from _private_temp import socket_root
import test_agent_portal_cost_dispatch as P
from solvio.agent_runtime import native_sessions as N, native_tools as T, cost_dispatch as D
from solvio.specialists import launcher as L

BODY = {"threadId": "native-thread", "turnId": "native-turn", "callId": "call-one",
        "tool": "portal_list", "arguments": {}}
FREE = D.CostQuote(0, P.C.CostEvidence("free_local", "test:native-tool-parent"))


@contextmanager
def world(**kwargs):
    with P.world(**kwargs) as w, socket_root() as root:
        w.socket_root = root
        w.sessions = N.NativeSessions(w.ledger, authority=w.authority)
        workspace = Path(w.ledger.path).parent.resolve() / "native-workspace"
        workspace.mkdir(mode=0o700)
        w.session = w.sessions.bind(task_id=w.task, run_id=w.run, provider="codex",
            profile="worker/codex", policy_digest="a" * 64, workspace=str(workspace))
        w.tools = T.NativeCoreTools(w.ledger, w.sessions, w.session.session_id, w.run, w.router)
        yield w


async def dispatch(w, work):
    with D.task_cost_scope(w.ledger, task_id=w.task, run_id=w.run,
            phase="specialist", operation_id="native-one", quote_adapter=lambda *_: FREE):
        async def native(*_):
            claim = w.sessions.active_claim(w.run)
            require(claim is not None)
            w.invocation = claim["invocation_id"]
            turn, fresh = w.sessions.request_turn(session_id=w.session.session_id, run_id=w.run,
                revision=1, invocation_id=w.invocation)
            require(fresh)
            w.sessions.bind_thread(w.invocation, BODY["threadId"])
            w.sessions.started(w.invocation, native_thread_id=BODY["threadId"], native_turn_id=BODY["turnId"])
            await work()
            return L.Outcome(True, exit_code=0, process_started=True)
        return await D.dispatch("codex", L.Invocation("/never-executed", (), cwd="/", timeout=1), "local protocol", native)


def payload(response):
    return json.loads(response["contentItems"][0]["text"])


def tool_rows(w):
    with w.ledger._open() as db:
        return [dict(r) for r in db.execute("SELECT * FROM agent_native_tool_calls")]


async def t_native_callback_executes_real_local_router_once_and_replays_from_new_adapter():
    with world() as w:
        require_equal([v["name"] for v in w.tools.manifest()], ["portal_list"])
        async def native():
            first = await w.tools.call(BODY)
            require(first["success"], payload(first))
            require_equal(len(payload(first)["data"]["portale"]), len(P.P.BINDINGS))
            require(w.accesses, "actual vault existence read missing")
            reads = len(w.accesses)
            reopened = T.NativeCoreTools(w.ledger, N.NativeSessions(w.ledger, authority=w.authority),
                w.session.session_id, w.run, w.router)
            require_equal(await reopened.call(dict(BODY, namespace=None)), first)
            require_equal(len(w.accesses), reads)
            require_equal(len(tool_rows(w)), 1)
            require_equal(tool_rows(w)[0]["state"], "completed")
            step = w.ledger.get_step(tool_rows(w)[0]["step_id"])
            require_equal(step.state, "succeeded")
            require(step.dispatch_claimed_at is not None)
            require(step.call_id)
            claims = D.invocations(w.ledger, w.task)
            require_equal(len(claims), 2)
            local = next(c for c in claims if c["provider"] == "local.portal-catalog")
            require_equal((local["task_id"], local["run_id"], local["operation_id"], local["state"]),
                          (w.task, w.run, step.step_id, "finished"))
        result = await dispatch(w, native)
        require_equal(result.cost_status, "settled")
        require_equal(w.costs.view(w.task)["counts"], {"settled": 2})


async def t_foreign_thread_turn_tool_namespace_or_arguments_never_reach_local_handler():
    for change in ({"threadId":"foreign"}, {"turnId":"foreign"}, {"tool":"portal_open"},
                   {"namespace":"foreign"}, {"arguments":{"approved":True}}, {"callId":""}):
        with world() as w:
            async def native():
                response = await w.tools.call(dict(BODY, **change))
                require(response["success"] is False, change)
                require_equal(w.accesses, [], change)
                require_equal(tool_rows(w), [], change)
            await dispatch(w, native)


async def t_absent_grant_or_ended_native_turn_blocks_callback():
    for change in ("no_grant", "revoked", "unknown", "terminal", "cancelled"):
        with world(granted=change != "no_grant") as w:
            if change == "no_grant":
                require_equal(w.tools.manifest(), [])
            async def native():
                if change == "revoked":
                    w.authority.revoke(w.grant.reference, "test:revoked")
                elif change == "unknown":
                    w.sessions.unknown(w.invocation)
                elif change == "terminal":
                    w.sessions.terminal(w.invocation, native_thread_id=BODY["threadId"], native_turn_id=BODY["turnId"], status="completed")
                elif change == "cancelled":
                    w.tools.cancel_token.set()
                response = await w.tools.call(BODY)
                require(response["success"] is False, (change, response))
                require_equal(w.accesses, [])
                require_equal(tool_rows(w), [])
            await dispatch(w, native)


async def t_outside_live_provider_scope_cannot_execute_or_read_back_old_result():
    with world() as w:
        require((await w.tools.call(BODY))["success"] is False)
        require_equal(tool_rows(w), [])
        async def native():
            require((await w.tools.call(BODY))["success"])
        await dispatch(w, native)
        reads = len(w.accesses)
        require((await w.tools.call(BODY))["success"] is False)
        require_equal(len(w.accesses), reads)


async def t_same_call_changed_payload_never_reexecutes():
    with world() as w:
        async def native():
            require((await w.tools.call(BODY))["success"])
            reads = len(w.accesses)
            require((await w.tools.call(dict(BODY, arguments={"extra": "changed"})))["success"] is False)
            require((await w.tools.call(dict(BODY, threadId="other-thread")))["success"] is False)
            require_equal(len(w.accesses), reads)
            require_equal(len(tool_rows(w)), 1)
        await dispatch(w, native)


async def t_native_turn_loss_during_quote_prevents_physical_service_dispatch():
    with world() as w:
        async def native():
            async def lose_turn():
                w.sessions.unknown(w.invocation)
            with P.quote_gate(lose_turn):
                result = await w.tools.call(BODY)
            require(result["success"] is False)
            require_equal(w.accesses, [])
            step = w.ledger.get_step(tool_rows(w)[0]["step_id"])
            require_equal(step.dispatch_claimed_at, None)
        await dispatch(w, native)


async def t_unknown_delivery_survives_new_adapter_and_blocks_new_call_ids():
    with world() as w:
        async def native():
            async def interrupted():
                raise asyncio.CancelledError()
            try:
                with P.quote_gate(interrupted):
                    await w.tools.call(BODY)
                require(False, "cancellation swallowed")
            except asyncio.CancelledError:
                pass
            require_equal(tool_rows(w)[0]["state"], "unknown")
            restored = T.NativeCoreTools(w.ledger, w.sessions, w.session.session_id, w.run, w.router)
            for request in (BODY, dict(BODY, callId="call-two")):
                result = await restored.call(request)
                require_equal(payload(result)["error"], "native_tool_recovery_required")
            require_equal(w.accesses, [])
            require_equal(len(tool_rows(w)), 1)
        await dispatch(w, native)


async def t_simultaneous_duplicate_has_one_admission_and_one_actual_local_read():
    with world() as w:
        async def native():
            entered, release = asyncio.Event(), asyncio.Event()
            async def pause():
                entered.set()
                await release.wait()
            with P.quote_gate(pause):
                first = asyncio.create_task(w.tools.call(BODY))
                await entered.wait()
                second = await w.tools.call(BODY)
                require_equal(payload(second)["error"], "native_tool_recovery_required")
                release.set()
                result = await first
            require(result["success"], result)
            require_equal(len(tool_rows(w)), 1)
            require_equal(sum(c["provider"] == "local.portal-catalog" for c in D.invocations(w.ledger, w.task)), 1)
        await dispatch(w, native)


async def t_real_handler_result_without_cost_settlement_is_unknown_and_not_replayed():
    from unittest.mock import patch
    with world() as w:
        async def native():
            with patch.object(type(w.costs), "settle", side_effect=ValueError("test:unsettled")):
                result = await w.tools.call(BODY)
            require_equal(payload(result)["error"], "native_tool_recovery_required")
            require(w.accesses, "expected actual local read before settlement failure")
            reads = len(w.accesses)
            require_equal(tool_rows(w)[0]["state"], "unknown")
            require_equal(w.ledger.get_step(tool_rows(w)[0]["step_id"]).state, "unknown")
            require((await w.tools.call(BODY))["success"] is False)
            require_equal(len(w.accesses), reads)
        await dispatch(w, native)


async def t_replay_requires_unchanged_receipt_and_existing_step_cost_evidence():
    for damage in ("receipt", "step", "cost"):
        with world() as w:
            async def native():
                require((await w.tools.call(BODY))["success"])
                reads = len(w.accesses)
                row = tool_rows(w)[0]
                with w.ledger._open() as db:
                    if damage == "receipt":
                        db.execute("UPDATE agent_native_tool_calls SET response_digest='changed'")
                    elif damage == "step":
                        db.execute("UPDATE agent_steps SET state='unknown' WHERE step_id=?", (row["step_id"],))
                    else:
                        db.execute("UPDATE agent_cost_reservations SET state='unknown' WHERE reservation_id IN "
                                   "(SELECT reservation_id FROM agent_provider_invocations WHERE operation_id=?)", (row["step_id"],))
                require((await w.tools.call(BODY))["success"] is False, damage)
                require_equal(len(w.accesses), reads)
                require_equal(tool_rows(w)[0]["state"], "completed")
            await dispatch(w, native)


# -- N8/C4 §4: closed TOOLS table, result_files_list v1 ----------------------

LIST = dict(BODY, tool="result_files_list", callId="call-list")


def granted_run(w, names, suffix):
    """A further run of the same task with its own immutable grant."""
    import time
    from solvio.agent_runtime import store as S
    from solvio.agent_runtime.task_authority import CapabilityGrant, VerifiedTaskReceipt
    run = w.ledger.create_run(task_id=w.task)
    w.ledger.transition(run.run_id, S.PLANNING)
    w.ledger.transition(run.run_id, S.RUNNING)
    grant = w.authority.issue(w.task, run.run_id,
        receipt=VerifiedTaskReceipt("app_session", "test:verified-tools-" + suffix, "owner:device"),
        capabilities=tuple(CapabilityGrant(name, 1) for name in names), expires_at=time.time() + 3600)
    return run.run_id, grant


def deliver(w, run_id, name, content, *, requirement=""):
    """A confirmed result file of a run, as the file specialist would leave it."""
    from solvio.agent_runtime import result_files as RF
    step = w.ledger.create_step(run_id=run_id, seq=90 + len(name), kind="capability", capability="artifact_create")
    w.ledger.update_step(step.step_id, state="running")
    descriptor = RF.publish_file(w.ledger, run_id, step.step_id, content, name, "text/plain",
                                 requirement=requirement, provider="codex")
    w.ledger.update_step(step.step_id, state="succeeded", finished=True)
    return descriptor


@contextmanager
def tools_world(names=("portal_list", "result_files_list"), *, register=True):
    from solvio.agent_runtime import result_files as RF
    with P.world() as w, socket_root() as root:
        w.socket_root = root
        w.run, w.grant = granted_run(w, names, "-".join(names) or "none")
        if register:
            RF.register(w.router, w.ledger)
        w.sessions = N.NativeSessions(w.ledger, authority=w.authority)
        workspace = Path(w.ledger.path).parent.resolve() / "native-workspace"
        workspace.mkdir(mode=0o700)
        w.session = w.sessions.bind(task_id=w.task, run_id=w.run, provider="codex",
            profile="worker/codex", policy_digest="a" * 64, workspace=str(workspace))
        w.tools = T.NativeCoreTools(w.ledger, w.sessions, w.session.session_id, w.run, w.router)
        yield w


async def t_manifest_lists_every_granted_tool_and_result_files_list_reads_only_this_task():
    from solvio.agent_runtime import store as S
    with tools_world() as w:
        require_equal([v["name"] for v in w.tools.manifest()], ["portal_list", "result_files_list"])
        require(all(v["inputSchema"] == {"type": "object", "properties": {}, "additionalProperties": False}
                    for v in w.tools.manifest()))
        own = deliver(w, w.run, "bericht.txt", b"eigener Bericht\n", requirement="")
        earlier = w.ledger.create_run(task_id=w.task)
        w.ledger.transition(earlier.run_id, S.PLANNING)
        w.ledger.transition(earlier.run_id, S.RUNNING)
        previous = deliver(w, earlier.run_id, "alt.txt", b"aelterer Lauf desselben Auftrags\n")
        foreign_task = w.ledger.create_task(objective="Fremder Auftrag.", scope="research",
            created_origin="trusted_interactive_app", created_principal="owner:device")
        foreign = w.ledger.create_run(task_id=foreign_task.task_id)
        w.ledger.transition(foreign.run_id, S.PLANNING)
        w.ledger.transition(foreign.run_id, S.RUNNING)
        deliver(w, foreign.run_id, "geheim.txt", b"nicht dieser Auftrag\n")
        async def native():
            reply = await w.tools.call(LIST)
            require(reply["success"], reply)
            data = payload(reply)["data"]["dateien"]
            require_equal(sorted((d["name"], d["run_id"]) for d in data),
                          sorted([("bericht.txt", w.run), ("alt.txt", earlier.run_id)]))
            for item in data:
                require_equal(set(item), {"name", "mime_type", "size", "sha256", "run_id", "requirement"})
                require_equal(item["mime_type"], "text/plain")
            mine = next(d for d in data if d["name"] == "bericht.txt")
            require_equal((mine["size"], mine["sha256"]), (own["size"], own["sha256"]))
            require_equal(next(d for d in data if d["name"] == "alt.txt")["sha256"], previous["sha256"])
            require("geheim" not in json.dumps(data))
            require_equal(w.accesses, [], "the portal vault is not part of a result-files read")
            rows = tool_rows(w)
            require_equal([(r["call_id"], r["state"]) for r in rows], [("call-list", "completed")])
            step = w.ledger.get_step(rows[0]["step_id"])
            require_equal((step.capability, step.state), ("result_files_list", "succeeded"))
            local = [c for c in D.invocations(w.ledger, w.task) if c["provider"] == "local.result-files"]
            require_equal([(c["operation_id"], c["state"]) for c in local], [(step.step_id, "finished")])
            # Replay from a fresh adapter returns the receipt without a second dispatch.
            reopened = T.NativeCoreTools(w.ledger, N.NativeSessions(w.ledger, authority=w.authority),
                                         w.session.session_id, w.run, w.router)
            require_equal(await reopened.call(LIST), reply)
            require_equal(len(local), 1)
            # Both tools coexist inside one turn with separate steps and cost rows.
            portal = await w.tools.call(BODY)
            require(portal["success"], portal)
            require_equal(len(tool_rows(w)), 2)
        result = await dispatch(w, native)
        require_equal(result.cost_status, "settled")
        require_equal(w.costs.view(w.task)["counts"], {"settled": 3})
        require_equal(w.costs.view(w.task)["ai_tool"]["spent_cents"], 0)


async def t_each_tool_pins_its_own_argument_schema_and_unknown_tools_never_dispatch():
    with tools_world() as w:
        for body in (dict(LIST, arguments={"run_id": w.run}), dict(LIST, arguments={"task_id": "other"}),
                     dict(BODY, arguments={"limit": 1}), dict(LIST, tool="memory_recall"),
                     dict(LIST, tool="secret_list"), dict(LIST, tool="portal_open")):
            try:
                T._request(body)
            except ValueError as exc:
                require_equal(str(exc), "native_tool_request_invalid", body)
            else:
                raise AssertionError("foreign request accepted: " + json.dumps(body))
        require_equal(T._request(LIST), {**{k: LIST[k] for k in ("threadId", "turnId", "callId", "tool")},
                                         "arguments": {}})
        async def native():
            for body in (dict(LIST, arguments={"run_id": w.run}), dict(LIST, tool="memory_recall")):
                require((await w.tools.call(body))["success"] is False, body)
            require_equal(tool_rows(w), [])
        await dispatch(w, native)


async def t_a_result_named_after_a_credential_word_still_lists_and_a_value_never_does():
    """Review round 14, R14-W1: the tool response is a JSON record with worker-chosen
    file names; the statement heuristic (term + any colon — JSON always has one) refused
    `keys.csv`/`token.json`/`passwort.txt`, the step ended `unknown` and every further
    tool call of the session was refused (`native_tool_recovery_required`). Now the
    JSON-record fence applies: a word lists, a key shape or credential line never."""
    from solvio.agent_runtime import result_files as RF
    with tools_world() as w:
        for index, name in enumerate(("keys.csv", "token.json", "passwort.txt", "api-key.md")):
            step = w.ledger.create_step(run_id=w.run, seq=70 + index, kind="capability", capability="artifact_create")
            w.ledger.update_step(step.step_id, state="running")
            RF.publish_file(w.ledger, w.run, step.step_id, b"element,zweck\n", name, "text/plain", requirement="", provider="codex")
            w.ledger.update_step(step.step_id, state="succeeded", finished=True)
        async def native():
            reply = await w.tools.call(LIST)
            require(reply["success"], reply)
            names = sorted(d["name"] for d in payload(reply)["data"]["dateien"])
            require_equal(names, ["api-key.md", "keys.csv", "passwort.txt", "token.json"])
            rows = tool_rows(w)
            require_equal([(r["call_id"], r["state"]) for r in rows], [("call-list", "completed")])
        await dispatch(w, native)


async def t_result_files_list_without_its_grant_is_absent_from_the_manifest_and_refused():
    with tools_world(("portal_list",)) as w:
        require_equal([v["name"] for v in w.tools.manifest()], ["portal_list"])
        async def native():
            reply = await w.tools.call(LIST)
            require(reply["success"] is False, reply)
            require_equal(tool_rows(w), [])
            require_equal([c["provider"] for c in D.invocations(w.ledger, w.task) if c["phase"] == "capability"], [])
        await dispatch(w, native)
    with tools_world(("result_files_list",)) as w:
        require_equal([v["name"] for v in w.tools.manifest()], ["result_files_list"])
        async def native():
            require((await w.tools.call(BODY))["success"] is False)
            require((await w.tools.call(LIST))["success"], "granted tool must work alone")
            require_equal(w.accesses, [])
        await dispatch(w, native)


async def t_a_same_name_result_files_handler_never_inherits_the_local_zero_cost_contract():
    from solvio.agent_runtime import result_files as RF
    with tools_world(register=False) as w:
        reached = []
        async def impostor(arguments):
            reached.append(arguments)
            return {"dateien": [{"name": "erfunden.txt"}]}
        w.router.register(RF.LIST_SPEC, impostor)
        require_equal([v["name"] for v in w.tools.manifest()], ["portal_list", "result_files_list"])
        async def native():
            reply = await w.tools.call(LIST)
            require(reply["success"] is False, reply)
            require_equal(reached, [])
            rows = tool_rows(w)
            require_equal(len(rows), 1)
            require(w.ledger.get_step(rows[0]["step_id"]).state != "succeeded")
            require_equal([c for c in D.invocations(w.ledger, w.task) if c["provider"] == "local.result-files"], [])
        await dispatch(w, native)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
