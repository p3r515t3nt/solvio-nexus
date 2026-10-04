"""N7 document admission through real authenticated HTTPS and persistent grants.

Temporary stores/files only. App Attest device signing is synthetic, the
public proof/Router/TaskStart/Grant/file boundary is real. No provider or
converter runs; admission is not claimed as completed document processing.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as E
from solvio.agent_runtime import document_contract as DC, store as S
from solvio.agent_runtime.task_start_service import TaskStartService, AuthorizedTaskStart
from solvio.agent_runtime.task_authority import TaskAuthority
from solvio.agent_runtime.costs import CostLedger

RTF = b"{\\rtf1\\ansi SYNTHETIC_PRIVATE_DOCUMENT_739.}"


def body(content=RTF, request_id="document-request-001"):
    return dict(E.BODY, objective="Lies das beigefuegte RTF-Dokument und gib seinen Text aus.",
        client_request_id=request_id, document_request={"operation": "extract_text", "format": "rtf",
            "content_b64": base64.b64encode(content).decode("ascii")})


async def accepted(w, request=None):
    response = await w.start(request or body())
    data = await response.json()
    require_equal(response.status, 201, str(data))
    return data


def input_path(w, run_id):
    rows = [a for a in w.ledger.artifacts_for_run(run_id) if a.kind == "task_input"]
    require_equal(len(rows), 1)
    return Path(rows[0].path)


def fresh_service(w):
    ledger = S.AgentRunLedger(w.ledger.path)
    return TaskStartService(ledger, grants=TaskAuthority(ledger), costs=CostLedger(ledger), router=w.router)


async def t_https_parallel_document_start_binds_one_resource_and_first_exact_grant():
    async with E.world() as w:
        responses = await asyncio.gather(w.start(body()), w.start(body()))
        require_equal([r.status for r in responses], [201, 201])
        results = [await r.json() for r in responses]
        require_equal(results[0]["run_id"], results[1]["run_id"])
        require_equal(len(w.ledger.recent_runs()), 1)
        run_id = results[0]["run_id"]
        resource = DC.for_run(w.ledger, run_id)
        grant = w.orch.task_authority.for_run(run_id)
        require_equal((resource.task_id, resource.run_id, resource.grant_reference),
                      (results[0]["task_id"], run_id, grant.reference))
        require_equal(resource.source_sha256, hashlib.sha256(RTF).hexdigest())
        require_equal(resource.source_bytes, len(RTF))
        require_equal([(c.name, c.version, c.constraints) for c in grant.capabilities],
                      [(DC.CAPABILITY, DC.VERSION, resource.arguments)])
        require(w.orch.task_authority.verify(grant.reference, DC.CAPABILITY, resource.arguments,
            DC.VERSION, task_id=resource.task_id, run_id=run_id).allowed)
        require_equal(DC.read_for_run(S.AgentRunLedger(w.ledger.path), run_id,
                                     arguments=resource.arguments), RTF)
        require_equal(input_path(w, run_id).stat().st_mode & 0o777, 0o400)
        require_equal(await w.store.list_pending(), [])
        require(DC.CAPABILITY not in w.router.names(), "admission silently installed an implementation")
        require_equal(w.ledger.get_run(run_id).planner_calls, 0)
        require_equal(w.ledger.get_run(run_id).state, S.CREATED)
        with w.ledger._open() as connection:
            dump = "\n".join(connection.iterdump())
        require(RTF.decode() not in dump and body()["document_request"]["content_b64"] not in dump,
                "document bytes leaked into task/approval/model metadata")


async def t_request_replay_cannot_replace_remove_or_add_a_document():
    async with E.world() as w:
        original = await accepted(w)
        for changed in (body(b"{\\rtf1\\ansi Different document.}"),
                        {k: v for k, v in body().items() if k != "document_request"}):
            response = await w.start(changed)
            require_equal(response.status, 409, str(await response.json()))
        require_equal(DC.read_for_run(w.ledger, original["run_id"]), RTF)
        require_equal(len(w.ledger.recent_runs()), 1)
        ordinary = dict(E.BODY, client_request_id="ordinary-request-001")
        require_equal((await w.start(ordinary)).status, 201)
        response = await w.start(dict(ordinary, document_request=body()["document_request"]))
        require_equal(response.status, 409)
        require_equal(len(w.ledger.recent_runs()), 2)


async def t_document_contract_retains_portal_read_but_no_generic_artifact_grant():
    from solvio.capabilities.portal import SPECS as PORTAL
    from solvio.agent_runtime import artifact_creation as AR
    async with E.world() as w:
        async def unused_portal(arguments):
            raise AssertionError('admission must not execute a portal read')
        w.router.register(PORTAL['portal_list'], unused_portal)
        w.orch.extension_activation = SimpleNamespace(file_runtime=object())
        result = await accepted(w)
        run_id = result['run_id']
        resource = DC.for_run(w.ledger, run_id)
        grant = w.orch.task_authority.for_run(run_id)
        require_equal([(c.name, c.version, c.constraints) for c in grant.capabilities],
                      [(DC.CAPABILITY, DC.VERSION, resource.arguments), ('portal_list', 1, {})])
        require(not w.orch.task_authority.verify(grant.reference, AR.CAPABILITY, AR.arguments,
            AR.VERSION, task_id=result['task_id'], run_id=run_id).allowed)
        require(AR.PROFILE not in w.orch._allowed_profiles(w.ledger.get_task(result['task_id']), run_id),
                'closed document upload exposed the generic research file producer')
        require_equal(DC.read_for_run(w.ledger, run_id), RTF)
        require_equal(w.ledger.steps_for_run(run_id), [])
        require_equal(await w.store.list_pending(), [])


async def t_document_wire_is_closed_research_only_and_bounded_by_actual_bytes():
    async with E.world() as w:
        invalid = [dict(body(), scope="build"), dict(body(), document_request=None)]
        for fields in ({"operation": "write_file"}, {"format": "html"}, {"content_b64": "%%%"},
                       {"path": "/some/source.rtf"}, {"capability": "payment_execute"},
                       {"sha256": "0" * 64}):
            invalid.append(dict(body(), document_request=dict(body()["document_request"], **fields)))
        invalid += [body(b"not an RTF file"), body(b"{\\rtf1 " + b"x" * DC.MAX_BYTES)]
        for request in invalid:
            response = await w.start(request)
            require_equal(response.status, 400, str(await response.json()))
        require_equal(w.ledger.recent_runs(), [])
        exact = b"{\\rtf1 " + b" " * (DC.MAX_BYTES - 8) + b"}"
        require_equal(len(exact), DC.MAX_BYTES)
        result = await accepted(w, body(exact))
        require_equal(DC.read_for_run(w.ledger, result["run_id"]), exact)


async def t_csrf_or_missing_app_proof_creates_no_document_or_task():
    async with E.world() as w:
        for headers in ({}, {"Origin": w.origin}, dict(w.headers, Origin="https://foreign.example")):
            response = await w.client.post("/v1/agent/tasks", json={"task": body()}, headers=headers)
            require_equal(response.status, 401)
        require_equal(w.ledger.recent_runs(), [])
        require(not Path(S.artifact_root()).exists(), "unauthorized input was persisted")
        require_equal(await w.store.list_pending(), [])


async def t_app_attest_binds_actual_document_bytes_and_cannot_be_replayed():
    async with E.world() as w:
        device = await E.H.enroll_attested(w.cp, transport_cred="temporary-document-transport")
        client = await w.new_client()
        headers = {"X-Device-Id": device.device_id, "X-Transport-Cred": "temporary-document-transport"}
        require_equal((await client.post("/v1/agent/tasks", json={"task": body()}, headers=headers)).status, 401)

        async def signed(request):
            response = await client.post("/v1/agent/tasks/challenge", json={"task": request}, headers=headers)
            require_equal(response.status, 200)
            wire = await response.json()
            assertion = E.AA.fake_assertion(device.aakey,
                E.T.client_data_hash(base64.b64decode(wire["binding_b64"])), 1)
            return {"task": request, "proof": {"nonce": wire["nonce"],
                "assertion_b64": base64.b64encode(assertion).decode()}}

        changed = await signed(body())
        changed["task"] = body(b"{\\rtf1\\ansi Substituted document.}")
        require_equal((await client.post("/v1/agent/tasks", json=changed, headers=headers)).status, 401)
        require_equal(w.ledger.recent_runs(), [])
        payload = await signed(body())
        response = await client.post("/v1/agent/tasks", json=payload, headers=headers)
        require_equal(response.status, 201, str(await response.json()))
        result = await response.json()
        require_equal(w.orch.task_authority.for_run(result["run_id"]).receipt_method, "app_session")
        require_equal(DC.read_for_run(w.ledger, result["run_id"]), RTF)
        require_equal((await client.post("/v1/agent/tasks", json=payload, headers=headers)).status, 401)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_restart_after_acceptance_before_grant_keeps_bytes_and_same_first_grant():
    async with E.world() as w:
        with patch.object(w.orch.task_authority, "issue", side_effect=RuntimeError("simulated process loss")):
            response = await w.start(body())
            require_equal(response.status, 202)
            result = await response.json()
        run_id = result["run_id"]
        require_equal(input_path(w, run_id).read_bytes(), RTF)
        require(not w.orch.task_starts.ready(run_id))
        require(w.orch.task_authority.for_run(run_id) is None)
        fresh = fresh_service(w)
        require(fresh.finish(run_id))
        before = DC.for_run(fresh.ledger, run_id)
        require_equal(DC.read_for_run(fresh.ledger, run_id), RTF)
        again = await accepted(w)
        require_equal(again["run_id"], run_id)
        require_equal(DC.for_run(w.ledger, run_id).grant_reference, before.grant_reference)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_precommit_resource_failure_cannot_accept_a_task_without_its_artifact():
    async with E.world() as w:
        with patch.object(DC, "record_prepared", side_effect=RuntimeError("simulated before commit")):
            response = await w.start(body())
            require(response.status >= 400)
        require_equal(w.ledger.recent_runs(), [])
        with w.ledger._open() as connection:
            for table in ("agent_task_sources", "agent_task_grants", "agent_artifacts"):
                require_equal(connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        result = await accepted(w)
        require_equal(DC.read_for_run(w.ledger, result["run_id"]), RTF)


async def t_missing_or_changed_input_never_completes_preparing_after_restart():
    async with E.world() as w:
        with patch.object(w.orch.task_authority, "issue", side_effect=RuntimeError("simulated process loss")):
            response = await w.start(body())
            require_equal(response.status, 202)
            result = await response.json()
        run_id = result["run_id"]
        input_path(w, run_id).unlink()
        fresh = fresh_service(w)
        try:
            fresh.finish(run_id)
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError("missing resource became ready")
        require(not fresh.ready(run_id))
        require(fresh.grants.for_run(run_id) is None)


async def t_resource_change_during_grant_issue_is_rechecked_before_ready():
    async with E.world() as w:
        issue = w.orch.task_authority.issue
        def lose_input(task_id, run_id, **kwargs):
            grant = issue(task_id, run_id, **kwargs)
            input_path(w, run_id).unlink()
            return grant
        with patch.object(w.orch.task_authority, "issue", side_effect=lose_input):
            response = await w.start(body())
            require_equal(response.status, 202)
            run_id = (await response.json())["run_id"]
        require(not w.orch.task_starts.ready(run_id))


async def t_bound_reader_rejects_changed_bytes_paths_links_and_permissions():
    for change in ("bytes", "path", "symlink", "hardlink", "permissions"):
        async with E.world() as w:
            result = await accepted(w)
            run_id = result["run_id"]
            path = input_path(w, run_id)
            if change == "bytes":
                path.chmod(0o600)
                path.write_bytes(RTF.replace(b"739", b"999"))
                path.chmod(0o400)
            elif change == "path":
                with w.ledger._open() as connection:
                    connection.execute("UPDATE agent_artifacts SET path=? WHERE run_id=?", ("/not/read/source.rtf", run_id))
            elif change == "symlink":
                original = path.with_name("redirect.rtf")
                path.rename(original)
                path.symlink_to(original)
            elif change == "hardlink":
                os.link(path, path.with_name("copy.rtf"))
            else:
                path.chmod(0o644)
            try:
                DC.read_for_run(w.ledger, run_id)
            except (ValueError, OSError):
                pass
            else:
                raise AssertionError("changed resource was read: " + change)


async def t_other_run_arguments_revoked_grant_and_ungranted_input_are_rejected():
    async with E.world() as w:
        first = await accepted(w)
        second = await accepted(w, body(request_id="document-request-002"))
        first_bound = DC.for_run(w.ledger, first["run_id"])
        second_bound = DC.for_run(w.ledger, second["run_id"])
        require(first_bound.resource_id != second_bound.resource_id)
        for run_id, arguments in ((second["run_id"], first_bound.arguments),
                (first["run_id"], dict(first_bound.arguments, source_sha256="0" * 64)),
                (first["run_id"], dict(first_bound.arguments, path="/other"))):
            try:
                DC.read_for_run(w.ledger, run_id, arguments=arguments)
            except ValueError:
                pass
            else:
                raise AssertionError("foreign or extended resource binding accepted")
        w.orch.task_authority.revoke(first_bound.grant_reference, "test:owner-revocation")
        try:
            DC.read_for_run(w.ledger, first["run_id"])
        except ValueError:
            pass
        else:
            raise AssertionError("revoked document grant remained readable")
        ordinary = await accepted(w, dict(E.BODY, client_request_id="ordinary-request-002"))
        require(DC.for_run(w.ledger, ordinary["run_id"]) is None)


async def t_core_receipt_binds_document_without_exposing_it_as_model_arguments():
    async with E.world() as w:
        result = await accepted(w)
        grant = w.orch.task_authority.for_run(result["run_id"])
        receipt = E.VerifiedTaskReceipt(grant.receipt_method, grant.receipt_reference, grant.authorizer)
        arguments = {"objective": body()["objective"]}
        bound = AuthorizedTaskStart.bind(receipt=receipt, request_id="binding-request-001",
            capability="agent_task_research", arguments=arguments, document_request=body()["document_request"])
        require(bound.matches("agent_task_research", arguments, "local-owner", E.OriginClass.TRUSTED_DASHBOARD))
        changed = replace(bound, document_request=DC.DocumentRequest(b"{\\rtf1\\ansi Other.}"))
        require(not changed.matches("agent_task_research", arguments, "local-owner", E.OriginClass.TRUSTED_DASHBOARD))
        require(RTF.decode() not in repr(bound) and body()["document_request"]["content_b64"] not in repr(bound))
        require("document_request" not in E.SPECS["agent_task_research"].input_schema["properties"])
        require_equal((await w.router.execute("agent_task_research", dict(arguments,
            document_request=body()["document_request"]), trust=E.OWNER,
            origin=E.OriginClass.TRUSTED_DASHBOARD, principal="local-owner")).outcome, E.OUT.INVALID_INPUT)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
