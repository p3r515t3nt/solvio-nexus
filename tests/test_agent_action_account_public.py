"""Actual owner HTTPS account amendment after a native Vault credential rotation."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from _guard import enforce_assertions, require, require_equal
from test_agent_action_execution import world, admit
from solvio.agent_runtime import action_contract as AC, store as S
from solvio.secret_vault import admin

enforce_assertions()


def rotate_fixture_access(w):
    store = w.native.broker.store
    ref = w.native.calendar.REFRESH_TOKEN_REF
    policy = store.policy(ref)
    admin.add(secret_ref=ref, kind=policy.kind, plaintext=b"synthetic-rotated-native-access",
        allowed_capabilities=policy.allowed_capabilities, allowed_targets=policy.allowed_targets,
        allowed_executors=policy.allowed_executors, allow_background=policy.allow_background,
        requires_user_presence=policy.requires_user_presence, replace=True, store=store)
    w.native.transport.auth_error = ""


async def parked(w):
    run_id = await admit(w)
    w.native.calendar._access_token = ""
    w.native.calendar._expires_at = 0
    w.native.transport.auth_error = "invalid_grant"
    for _ in range(3):
        await w.orch.tick()
    require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)
    require_equal(w.native.transport.mutations, [])
    return run_id


async def decision(w, run_id, request_id="account-choice-001"):
    response = await w.client.get("/v1/agent/runs/" + run_id)
    require_equal(response.status, 200)
    binding = (await response.json())["kontobindung"]
    require(binding["selection_required"])
    require_equal(len(binding["choices"]), 1)
    return {"action_id": binding["action_id"], "new_account": binding["choices"][0]["account"],
        "expected_account": binding["current_account"], "expected_receipt_digest": binding["receipt_digest"],
        "client_request_id": request_id}


def immutable_rows(w, run_id):
    with w.ledger._open() as c:
        return [dict(c.execute("SELECT * FROM " + table + " WHERE run_id=?", (run_id,)).fetchone())
                for table in ("agent_task_grants", "agent_action_contracts", "agent_task_sources")]


async def t_owner_selects_rotated_account_same_task_restarts_and_replays_without_new_effect():
    async with world() as w:
        run_id = await parked(w)
        original = immutable_rows(w, run_id)
        old_receipt = deepcopy(AC.read_receipts(w.ledger, run_id)[0])
        rotate_fixture_access(w)
        data = await decision(w, run_id)
        path = "/v1/agent/runs/" + run_id + "/action-account"
        response = await w.client.post(path, json=data, headers=w.headers)
        require_equal(response.status, 200, str(await response.json()))
        require_equal(w.ledger.get_run(run_id).state, S.RUNNING)
        require_equal(immutable_rows(w, run_id), original)
        require_equal(AC.read_receipts(w.ledger, run_id)[0], old_receipt)
        w.runtime()
        await w.orch.reconcile()
        await w.finish(run_id)
        native = AC.read_receipts(w.ledger, run_id)[-1]["native"]["observed"]
        require_equal(native["actual_account"], data["new_account"])
        require(native["account_rebind_reference"])
        require_equal(len(w.native.transport.mutations), 1)
        require_equal((await w.client.post(path, json=data, headers=w.headers)).status, 200)
        changed = dict(data, new_account=data["expected_account"])
        require_equal((await w.client.post(path, json=changed, headers=w.headers)).status, 409)
        with w.ledger._open() as c:
            require_equal(c.execute("SELECT count(*) FROM agent_action_account_rebindings").fetchone()[0], 1)
        require_equal(immutable_rows(w, run_id), original)
        require_equal(len(w.native.transport.mutations), 1)


async def t_untrusted_or_stale_account_selection_never_amends_or_dispatches():
    async with world() as w:
        run_id = await parked(w)
        rotate_fixture_access(w)
        data = await decision(w, run_id)
        path = "/v1/agent/runs/" + run_id + "/action-account"
        require_equal((await w.client.post(path, json=data, headers={"Origin": w.origin})).status, 401)
        stranger = await w.new_client()
        headers = await w.login(stranger, "another-owner")
        require_equal((await stranger.post(path, json=data, headers=headers)).status, 404)
        for changed in (dict(data, expected_account="calendar-" + "0" * 32),
                        dict(data, expected_receipt_digest="0" * 64),
                        dict(data, new_account="calendar-" + "0" * 32),
                        dict(data, action_id="other")):
            require_equal((await w.client.post(path, json=changed, headers=w.headers)).status, 409)
        with w.ledger._open() as c:
            require_equal(c.execute("SELECT count(*) FROM agent_action_account_rebindings").fetchone()[0], 0)
        require_equal(w.native.transport.mutations, [])
        require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)


async def t_replayed_old_account_post_cannot_resume_a_later_authentication_boundary():
    async with world() as w:
        run_id = await parked(w)
        original = immutable_rows(w, run_id)
        rotate_fixture_access(w)
        data = await decision(w, run_id)
        w.native.transport.auth_error = "invalid_grant"
        path = "/v1/agent/runs/" + run_id + "/action-account"
        require_equal((await w.client.post(path, json=data, headers=w.headers)).status, 200)
        await w.orch.tick()
        before = w.ledger.get_run(run_id)
        receipts = AC.read_receipts(w.ledger, run_id)
        require_equal(before.state, S.WAITING_USER)
        require_equal([r["status"] for r in receipts], ["not_dispatched", "not_dispatched"])
        require(receipts[-1]["receipt_digest"] != data["expected_receipt_digest"])
        response = await w.client.post(path, json=data, headers=w.headers)
        require_equal(response.status, 200)
        require_equal((await response.json())["state"], S.WAITING_USER)
        await w.orch.tick()
        after = w.ledger.get_run(run_id)
        require_equal((after.state, after.plan_revision, after.boundary),
                      (before.state, before.plan_revision, before.boundary))
        require_equal(AC.read_receipts(w.ledger, run_id), receipts)
        require_equal(immutable_rows(w, run_id), original)
        require_equal(w.native.transport.mutations, [])


async def t_replay_after_amendment_before_resume_continues_the_same_original_boundary():
    async with world() as w:
        run_id = await parked(w)
        rotate_fixture_access(w)
        data = await decision(w, run_id)
        path = "/v1/agent/runs/" + run_id + "/action-account"
        # Simulate process loss after the immutable write, before transition.
        with patch.object(w.orch, "resume", AsyncMock(return_value=False)):
            require_equal((await w.client.post(path, json=data, headers=w.headers)).status, 200)
        require_equal(w.ledger.get_run(run_id).state, S.WAITING_USER)
        w.runtime()
        await w.orch.reconcile()
        response = await w.client.post(path, json=data, headers=w.headers)
        require_equal(response.status, 200)
        require_equal((await response.json())["state"], S.RUNNING)
        await w.finish(run_id)
        require_equal(len(w.native.transport.mutations), 1)
        with w.ledger._open() as c:
            require_equal(c.execute("SELECT count(*) FROM agent_action_account_rebindings").fetchone()[0], 1)


async def t_unknown_native_effect_cannot_be_rebound_with_new_credentials():
    async with world() as w:
        run_id = await admit(w)
        w.native.transport.drop_after_write = True
        await w.finish(run_id, S.FAILED)
        receipt = AC.read_receipts(w.ledger, run_id)[0]
        require_equal(receipt["status"], "unknown")
        rotate_fixture_access(w)
        account = w.orch.action_service.accounts()[0]["account"]
        data = {"action_id": "a1", "new_account": account,
            "expected_account": w.body["action_request"]["actions"][0]["account"],
            "expected_receipt_digest": receipt["receipt_digest"], "client_request_id": "unknown-choice-001"}
        response = await w.client.post("/v1/agent/runs/" + run_id + "/action-account", json=data, headers=w.headers)
        require_equal(response.status, 409)
        require_equal(len(w.native.transport.mutations), 1)
        require_equal(AC.read_receipts(w.ledger, run_id)[0], receipt)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
