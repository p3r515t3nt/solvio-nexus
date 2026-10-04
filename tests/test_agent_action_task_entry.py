"""A1: actual authenticated HTTPS admission of closed action resources.

The existing temporary gateway, browser identity, App Attest verifier, router,
TaskStartService and durable ledger are real. Only the phone signing key is
synthetic. No calendar, mailbox, device, portal, provider or planner is invoked.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

import test_agent_task_entry as E
from solvio.agent_runtime import action_contract as AC, store as S
from solvio.agent_runtime.costs import CostLedger
from solvio.agent_runtime.task_authority import TaskAuthority
from solvio.agent_runtime.task_start_service import AuthorizedTaskStart, TaskStartService
from solvio.capabilities.agent import AgentCapabilities, SPECS


def action_request():
    return {"actions": [{
        "action_id": "calendar_visit", "service": "calendar", "operation": "create",
        "account": "synthetic-calendar-account", "target": {"calendar_id": "calendar_test"},
        "payload": {"summary": "Synthetic bound visit", "start": "2026-11-10T09:00:00+01:00",
                    "end": "2026-11-10T10:00:00+01:00", "all_day": False,
                    "description": "Synthetic admission only; never dispatched.", "location": "Test location"}},
        {"action_id": "mail_draft", "service": "gmail", "operation": "create_draft",
         "account": "synthetic-mail-account", "target": {"mailbox": "me",
             "to": "synthetic-recipient@example.invalid", "reply_to_message": ""},
         "payload": {"subject": "Synthetic bound draft", "body": "Synthetic draft body.",
                     "thread_id": "", "in_reply_to": ""}}]}


def body(request_id="action-request-001"):
    return dict(E.BODY, scope="action", objective="Lege den gebundenen Termin und den gebundenen Entwurf an.",
                client_request_id=request_id, action_request=action_request())


@asynccontextmanager
async def world():
    async with E.world() as w:
        caps = AgentCapabilities(w.orch)
        w.router.register(SPECS["agent_task_action"], caps.action)
        yield w


async def accepted(w, request=None):
    response = await w.start(request or body())
    data = await response.json()
    require_equal(response.status, 201, str(data))
    return data


def rows(w, table):
    with w.ledger._open() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM " + table)]


def fresh_service(w):
    ledger = S.AgentRunLedger(w.ledger.path)
    return TaskStartService(ledger, grants=TaskAuthority(ledger), costs=CostLedger(ledger), router=w.router)


async def t_parallel_https_action_admission_is_one_bound_task_without_approval():
    async with world() as w:
        responses = await asyncio.gather(w.start(body()), w.start(body()))
        values = [await response.json() for response in responses]
        require_equal([response.status for response in responses], [201, 201], str(values))
        require_equal(values[0]["task_id"], values[1]["task_id"])
        require_equal(values[0]["run_id"], values[1]["run_id"])
        require_equal(len(w.ledger.recent_runs()), 1)
        result = values[0]
        task = w.ledger.get_task(result["task_id"])
        require_equal((task.scope, task.created_principal, task.created_origin),
                      ("action", "local-owner", "trusted_dashboard"))
        bound = AC.for_run(w.ledger, result["run_id"])
        require_equal(list(bound.actions), action_request()["actions"])
        grant = w.orch.task_authority.for_run(result["run_id"])
        require_equal(grant.receipt_method, "dashboard_session")
        require_equal(grant.capabilities, (bound.capability_grant,))
        require_equal(len(rows(w, "agent_action_contracts")), 1)
        require_equal(rows(w, "agent_action_claims"), [])
        require_equal(await w.store.list_pending(), [])
        require_equal(w.orch.costs.view(task.task_id)["ask_threshold_cents"], 1000)
        require_equal(w.ledger.get_run(result["run_id"]).planner_calls, 0)


async def t_action_wire_rejects_missing_extra_and_unbound_fields_before_any_task():
    async with world() as w:
        missing = body(); missing.pop("action_request")
        invalid = [missing, dict(body(), action_request=None), dict(body(), action_request={}),
                   dict(body(), action_request={"actions": []}), dict(body(), scope="research"),
                   dict(body(), target_repo="/synthetic/repository"),
                   dict(body(), document_request={"operation": "extract_text", "format": "rtf", "content_b64": ""}),
                   dict(body(), principal="local-owner", origin="trusted_dashboard")]
        for key, value in (("operation", "shell"), ("capability", "payment_execute"),
                           ("path", "/synthetic/authority"), ("contract_digest", "0" * 64)):
            changed = body(); changed["action_request"]["actions"][0][key] = value
            invalid.append(changed)
        changed = body(); changed["action_request"]["actions"][0]["target"]["calendar_id"] = ""
        invalid.append(changed)
        changed = body(); changed["action_request"]["actions"][0]["payload"]["start"] = "morgen"
        invalid.append(changed)
        changed = body(); changed["action_request"]["actions"][0]["payload"]["start"] = "2026-11-10T09:00:00"
        invalid.append(changed)
        changed = body(); changed["action_request"]["actions"][1]["target"]["to"] = "Freund"
        invalid.append(changed)
        changed = body(); changed["action_request"]["actions"][1]["action_id"] = "calendar_visit"
        invalid.append(changed)
        for request in invalid:
            response = await w.start(request)
            require_equal(response.status, 400, str(await response.json()))
        require_equal(w.ledger.recent_runs(), [])
        require_equal(rows(w, "agent_action_contracts"), [])
        require_equal(rows(w, "agent_action_claims"), [])
        require_equal(await w.store.list_pending(), [])


async def t_replay_cannot_change_targets_account_payload_order_or_action_kind():
    async with world() as w:
        result = await accepted(w)
        before = AC.for_run(w.ledger, result["run_id"])
        variants = []
        changed = body(); changed["action_request"]["actions"][0]["target"]["calendar_id"] = "other_calendar"
        variants.append(changed)
        changed = body(); changed["action_request"]["actions"][0]["account"] = "other_account"
        variants.append(changed)
        changed = body(); changed["action_request"]["actions"][0]["payload"]["start"] = "2026-11-10T08:00:00+01:00"
        variants.append(changed)
        changed = body(); changed["action_request"]["actions"][1]["target"]["to"] = "different@example.invalid"
        variants.append(changed)
        changed = body(); changed["action_request"]["actions"][1]["payload"]["body"] = "A different instruction."
        variants.append(changed)
        changed = body(); changed["action_request"]["actions"].reverse()
        variants.append(changed)
        changed = body(); changed["action_request"]["actions"].pop()
        variants.append(changed)
        variants.append(dict(E.BODY, client_request_id=body()["client_request_id"]))
        for request in variants:
            response = await w.start(request)
            require_equal(response.status, 409, str(await response.json()))
        require_equal(AC.for_run(w.ledger, result["run_id"]), before)
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal(len(rows(w, "agent_action_contracts")), 1)
        require_equal(rows(w, "agent_action_claims"), [])


async def t_csrf_and_app_transport_without_proof_cannot_persist_action_resources():
    async with world() as w:
        for headers in ({}, {"Origin": w.origin}, dict(w.headers, Origin="https://foreign.example")):
            response = await w.client.post("/v1/agent/tasks", json={"task": body()}, headers=headers)
            require_equal(response.status, 401)
        device = await E.H.enroll_attested(w.cp, transport_cred="temporary-action-transport")
        client = await w.new_client()
        response = await client.post("/v1/agent/tasks", json={"task": body()}, headers={
            "X-Device-Id": device.device_id, "X-Transport-Cred": "temporary-action-transport"})
        require_equal(response.status, 401)
        require_equal(w.ledger.recent_runs(), [])
        require_equal(rows(w, "agent_action_contracts"), [])
        require_equal(await w.store.list_pending(), [])


async def t_action_service_inventory_is_owner_only_and_never_starts_work():
    async with world() as w:
        path = "/v1/agent/action-services"
        anonymous = await w.new_client()
        require_equal((await anonymous.get(path)).status, 401)
        other = await w.new_client()
        await w.login(other, "another-owner")
        require_equal((await other.get(path)).status, 403)
        for _ in range(2):
            response = await w.client.get(path)
            require_equal(response.status, 200)
            require_equal(await response.json(), {"services": []})
            require_equal(response.headers.get("Cache-Control"), "no-store")
        require_equal(w.ledger.recent_runs(), [])
        require_equal(rows(w, "agent_action_contracts"), [])
        require_equal(rows(w, "agent_action_claims"), [])
        require_equal(await w.store.list_pending(), [])


async def t_app_attest_binds_actual_targets_and_contents_before_router_admission():
    async with world() as w:
        device = await E.H.enroll_attested(w.cp, transport_cred="temporary-action-transport")
        client = await w.new_client()
        headers = {"X-Device-Id": device.device_id, "X-Transport-Cred": "temporary-action-transport"}

        async def signed(request):
            response = await client.post("/v1/agent/tasks/challenge", json={"task": request}, headers=headers)
            require_equal(response.status, 200)
            wire = await response.json()
            assertion = E.AA.fake_assertion(device.aakey,
                E.T.client_data_hash(base64.b64decode(wire["binding_b64"])), 1)
            return {"task": deepcopy(request), "proof": {"nonce": wire["nonce"],
                "assertion_b64": base64.b64encode(assertion).decode()}}

        for field in ("target", "content"):
            changed = await signed(body())
            entry = changed["task"]["action_request"]["actions"][1]
            if field == "target":
                entry["target"]["to"] = "substituted@example.invalid"
            else:
                entry["payload"]["body"] = "Changed after actual signed challenge."
            response = await client.post("/v1/agent/tasks", json=changed, headers=headers)
            require_equal(response.status, 401)
            require_equal(w.ledger.recent_runs(), [])
            require_equal(rows(w, "agent_action_contracts"), [])
        payload = await signed(body())
        response = await client.post("/v1/agent/tasks", json=payload, headers=headers)
        data = await response.json()
        require_equal(response.status, 201, str(data))
        require_equal(w.orch.task_authority.for_run(data["run_id"]).receipt_method, "app_session")
        require_equal(w.ledger.get_task(data["task_id"]).created_origin, "trusted_interactive_app")
        require_equal(list(AC.for_run(w.ledger, data["run_id"]).actions), action_request()["actions"])
        require_equal((await client.post("/v1/agent/tasks", json=payload, headers=headers)).status, 401)
        require_equal(await w.store.list_pending(), [])
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_process_loss_before_grant_finishes_the_same_bound_bundle_after_restart():
    async with world() as w:
        with patch.object(w.orch.task_authority, "issue", side_effect=RuntimeError("synthetic process loss")):
            response = await w.start(body())
            data = await response.json()
            require_equal(response.status, 202, str(data))
        run_id = data["run_id"]
        require(not w.orch.task_starts.ready(run_id))
        require(w.orch.task_authority.for_run(run_id) is None)
        original = rows(w, "agent_action_contracts")
        require_equal(len(original), 1)
        require_equal(json.loads(original[0]["request_json"]), action_request())
        fresh = fresh_service(w)
        require(fresh.finish(run_id))
        bound = AC.for_run(fresh.ledger, run_id)
        require_equal(list(bound.actions), action_request()["actions"])
        require_equal(rows(w, "agent_action_contracts"), original)
        repeated = await accepted(w)
        require_equal(repeated["run_id"], run_id)
        require_equal(AC.for_run(w.ledger, run_id).grant_reference, bound.grant_reference)
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal(w.ledger.get_run(run_id).planner_calls, 0)
        require_equal(rows(w, "agent_action_claims"), [])


async def t_precommit_failure_never_leaves_an_unbound_accepted_action_task():
    async with world() as w:
        with patch.object(AC, "record_prepared", side_effect=RuntimeError("synthetic rollback")):
            response = await w.start(body())
            require(response.status >= 400)
        for table in ("agent_tasks", "agent_runs", "agent_task_sources", "agent_task_grants", "agent_action_contracts"):
            require_equal(rows(w, table), [], table)
        data = await accepted(w)
        require_equal(list(AC.for_run(w.ledger, data["run_id"]).actions), action_request()["actions"])


async def t_changed_bundle_cannot_complete_preparing_after_process_loss():
    async with world() as w:
        with patch.object(w.orch.task_authority, "issue", side_effect=RuntimeError("synthetic process loss")):
            response = await w.start(body())
            data = await response.json()
            require_equal(response.status, 202, str(data))
        changed = action_request(); changed["actions"][0]["target"]["calendar_id"] = "other_calendar"
        with w.ledger._open() as connection:
            connection.execute("UPDATE agent_action_contracts SET request_json=? WHERE run_id=?",
                (json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False), data["run_id"]))
        fresh = fresh_service(w)
        try:
            fresh.finish(data["run_id"])
        except ValueError:
            pass
        else:
            raise AssertionError("mutated resource became ready after restart")
        require(not fresh.ready(data["run_id"]))
        require(fresh.grants.for_run(data["run_id"]) is None)
        require_equal(rows(w, "agent_action_claims"), [])


async def t_receipt_binds_private_bundle_outside_model_arguments():
    async with world() as w:
        request = body()
        arguments = {"objective": request["objective"]}
        start = AuthorizedTaskStart.bind(receipt=E.VerifiedTaskReceipt(
            "dashboard_session", "browser:synthetic-action", "local-owner"),
            request_id="binding-action-001", capability="agent_task_action",
            arguments=arguments, action_request=request["action_request"])
        require(start.matches("agent_task_action", arguments, "local-owner", E.OriginClass.TRUSTED_DASHBOARD))
        changed = action_request(); changed["actions"][1]["payload"]["body"] = "Different private content."
        substituted = replace(start, action_request=AC.validate_request(changed))
        require(not substituted.matches("agent_task_action", arguments, "local-owner", E.OriginClass.TRUSTED_DASHBOARD))
        require("Synthetic draft body." not in repr(start))
        require("action_request" not in SPECS["agent_task_action"].input_schema["properties"])
        result = await w.router.execute("agent_task_action", dict(arguments, action_request=request["action_request"]),
            trust=E.OWNER, origin=E.OriginClass.TRUSTED_DASHBOARD, principal="local-owner", task_start=start)
        require_equal(result.outcome, E.OUT.INVALID_INPUT)
        require_equal(w.ledger.recent_runs(), [])


async def t_background_or_external_origin_cannot_use_a_valid_entrance_receipt_to_spawn():
    async with world() as w:
        request = body(); arguments = {"objective": request["objective"]}
        start = AuthorizedTaskStart.bind(receipt=E.VerifiedTaskReceipt(
            "dashboard_session", "browser:synthetic-parent", "local-owner"),
            request_id="binding-action-002", capability="agent_task_action",
            arguments=arguments, action_request=request["action_request"])
        for origin in (E.OriginClass.BACKGROUND_AUTOMATION, E.OriginClass.EXTERNAL_UNTRUSTED):
            result = await w.router.execute("agent_task_action", arguments, trust=E.OWNER,
                origin=origin, principal="local-owner", task_start=start)
            require(result.outcome not in {E.OUT.SUCCESS, E.OUT.APPROVAL_REQUIRED},
                    "untrusted origin spawned or requested approval: " + str(origin))
        # A model cannot start even from a claimed trusted origin without the
        # actual bound entry receipt; the raw service bundle isn't in its schema.
        result = await w.router.execute("agent_task_action", arguments, trust=E.OWNER,
            origin=E.OriginClass.TRUSTED_DASHBOARD, principal="local-owner")
        require(result.outcome not in {E.OUT.SUCCESS, E.OUT.APPROVAL_REQUIRED},
                "missing structured action receipt must not create an unexecutable approval")
        require_equal(w.ledger.recent_runs(), [])
        require_equal(rows(w, "agent_action_contracts"), [])
        require_equal(await w.store.list_pending(), [])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
