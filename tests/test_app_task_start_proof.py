"""Fresh app task authority: real approval DB, existing verifier, synthetic device keys.

No HTTP/server edits here; endpoint integration has its own public-route tests.
All identity, enrollment and challenge state is temporary. No real iPhone or provider.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal, require_raises
enforce_assertions()
import mobile_attest_helper as H
from solvio import voice_session_proof as V
from solvio.agent_runtime import task_start_proof as T
from solvio.security.mobile_approval import app_attest as AA, attest_protocol as AP
from solvio.security.mobile_approval import control as C, crypto, identity, protocol as P, store as S

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
TRANSPORT = "temporary-test-transport-credential"
BODY = {"scope": "research", "objective": "Vergleiche drei Hotels in Hamburg mit Quellen.",
        "target_repo": "", "client_request_id": "request-001"}


async def _open(path):
    store = S.ApprovalControlStore(os.path.join(path, "approval_control.sqlite3"))
    await store.open()
    cp = C.MobileApprovalControlPlane(store, identity.MacSigningKey.load_or_create(path),
        identity.load_or_create_core_instance_id(path), attest_verifier=H.fake_verifier(), app_id=APP_ID)
    return store, cp, T.TaskStartProofService(cp)


@asynccontextmanager
async def _world():
    with tempfile.TemporaryDirectory(prefix="solvio-app-task-proof-") as path:
        store, cp, service = await _open(path)
        device = await H.enroll_attested(cp, transport_cred=TRANSPORT)
        try:
            yield path, store, cp, service, device
        finally:
            await store.close()


async def _issue(service, ctx, body=BODY):
    challenge = await service.issue(device_id=ctx.device_id, transport_cred=TRANSPORT, task_body=body)
    require(challenge is not None, "valid enrolled app could not request a task nonce")
    return challenge


def _sign(ctx, challenge, counter=1):
    return AA.fake_assertion(ctx.aakey, T.client_data_hash(challenge.binding_raw), counter)


async def _verify(service, ctx, challenge, body=BODY, assertion=None):
    return await service.verify(device_id=ctx.device_id, nonce=challenge.nonce, task_body=body,
        assertion=assertion if assertion is not None else _sign(ctx, challenge))


async def t_exact_task_start_returns_a_bound_app_receipt_without_biometric_decision():
    async with _world() as (_, store, cp, service, device):
        challenge = await _issue(service, device)
        binding = P.strict_parse(P.b64d(challenge.as_dict()["binding_b64"]))
        require_equal(binding["core_instance_id"], cp.core_instance_id)
        require_equal(binding["principal_id"], "local-owner")
        require_equal(binding["device_id"], device.device_id)
        require_equal(binding["type"], T.TYPE_TASK_START_BINDING)
        require_equal(binding["request_digest"], T.request_digest(BODY))
        require(binding["enrollment_id"])
        proof = await _verify(service, device, challenge)
        require(proof is not None)
        require_equal(proof.principal, "local-owner")
        require_equal(proof.device_id, device.device_id)
        require_equal(proof.nonce, challenge.nonce)
        require_equal(proof.request_digest, T.request_digest(BODY))
        require_equal(proof.core_instance_id, cp.core_instance_id)
        require_equal((await store.get_device(device.device_id))["app_attest_counter"], 0,
                      "task proof wrote the existing decision counter")
        counts = await store._run(lambda: [store._conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("approval_requests", "execution_attempts", "devices")])
        require_equal(counts, [0, 0, 1], "task proof created a biometric approval or a fake device")
        require_equal(sum(e["event"] == "app_task_start_proved" for e in await store.audit_events()), 1)


async def t_static_transport_can_fetch_but_cannot_prove_a_task():
    async with _world() as (_, _, _, service, device):
        require(await service.issue(device_id=device.device_id, transport_cred="wrong", task_body=BODY) is None)
        require(await service.issue(device_id="unknown-device", transport_cred=TRANSPORT, task_body=BODY) is None)
        challenge = await _issue(service, device)
        for assertion in (b"", TRANSPORT.encode(), b"arbitrary assertion", None, {}, b"x" * 16385):
            result = await service.verify(device_id=device.device_id, nonce=challenge.nonce,
                                          task_body=BODY, assertion=assertion)
            require(result is None)
        require(await _verify(service, device, challenge) is not None,
                "invalid assertions consumed an honest device's nonce")


async def t_each_task_body_field_is_bound_without_postproof_normalization():
    async with _world() as (_, _, _, service, device):
        challenge = await _issue(service, device)
        changed = [dict(BODY, scope="build"), dict(BODY, objective=BODY["objective"] + " "),
                   dict(BODY, target_repo="other-repository"), dict(BODY, client_request_id="request-002")]
        for body in changed:
            require(await _verify(service, device, challenge, body=body) is None)
        require(await _verify(service, device, challenge) is not None)


async def t_build_repository_is_part_of_the_signed_request():
    async with _world() as (_, _, _, service, device):
        body = dict(BODY, scope="build", target_repo="repository-original")
        challenge = await _issue(service, device, body)
        require(await _verify(service, device, challenge,
                              body=dict(body, target_repo="repository-other")) is None)
        proof = await _verify(service, device, challenge, body=body)
        require(proof is not None)
        require_equal(proof.request_digest, T.request_digest(body))


async def t_nonce_is_single_use_across_connections_and_after_restart():
    async with _world() as (path, store, cp, service, device):
        challenge = await _issue(service, device)
        second, cp2, service2 = await _open(path)
        try:
            require_equal(cp2.core_instance_id, cp.core_instance_id)
            results = await asyncio.gather(_verify(service, device, challenge), _verify(service2, device, challenge))
            require_equal(sum(item is not None for item in results), 1)
        finally:
            await second.close()
        reopened, _, again = await _open(path)
        try:
            require(await _verify(again, device, challenge) is None, "replayed proof survived restart")
            require_equal(sum(e["event"] == "app_task_start_proved" for e in await store.audit_events()), 1)
        finally:
            await reopened.close()


async def t_unconsumed_challenge_survives_restart_bound_to_the_persisted_core():
    with tempfile.TemporaryDirectory(prefix="solvio-app-task-restart-") as path:
        store, cp, service = await _open(path)
        device = await H.enroll_attested(cp, transport_cred=TRANSPORT)
        challenge = await _issue(service, device)
        old_core = cp.core_instance_id
        await store.close()
        store, cp, service = await _open(path)
        try:
            require_equal(cp.core_instance_id, old_core)
            require(await _verify(service, device, challenge) is not None)
        finally:
            await store.close()


async def t_assertion_cannot_move_to_another_device_core_or_principal():
    async with _world() as (_, store, cp, service, device):
        second = await H.enroll_attested(cp, device_id="dev-2", transport_cred=TRANSPORT)
        challenge = await _issue(service, device)
        require(await service.verify(device_id=second.device_id, nonce=challenge.nonce, task_body=BODY,
                                     assertion=_sign(device, challenge)) is None)
        other = C.MobileApprovalControlPlane(store, cp.mac_key, "core-other",
                                            attest_verifier=H.fake_verifier(), app_id=APP_ID)
        require(await _verify(T.TaskStartProofService(other), device, challenge) is None)
        await store._run(lambda: store._conn.execute("UPDATE devices SET principal=? WHERE device_id=?",
                                                    ("other-owner", device.device_id)))
        require(await _verify(service, device, challenge) is None)


async def t_wrong_key_signature_and_voice_or_decision_domains_are_rejected():
    async with _world() as (_, _, cp, service, device):
        challenge = await _issue(service, device)
        other = H.new_device("attacker")
        voice_raw = V.canonical_bytes(V.build_binding(core_instance_id=cp.core_instance_id,
            device_id=device.device_id, session_nonce=challenge.nonce))
        wrong_hashes = [V.client_data_hash(voice_raw), V.client_data_hash(challenge.binding_raw),
                        AP.decision_client_data_hash(challenge.binding_raw),
                        AP.enrollment_client_data_hash(challenge.binding_raw)]
        assertions = [_sign(other, challenge)] + [AA.fake_assertion(device.aakey, digest, 1) for digest in wrong_hashes]
        for assertion in assertions:
            require(await _verify(service, device, challenge, assertion=assertion) is None)
        require(await _verify(service, device, challenge) is not None)


async def t_real_mobile_decision_nonce_is_not_consumable_as_a_task_start():
    async with _world() as (_, store, cp, service, device):
        request = await cp.create_request(principal="local-owner", tool="note_write", mode="execute",
            task="temporary test", workspace="", human_summary="Temporary test")
        challenge, status = await cp.issue_challenge(approval_id=request, device_id=device.device_id)
        require_equal(status, "ok")
        raw = P.b64d(challenge["payload_b64"])
        nonce = P.strict_parse(raw)["challenge_nonce"]
        signed = H.sign_decision(device, raw)
        # A valid, fully signed decision must keep its own unconsumed challenge.
        require(await service.verify(device_id=device.device_id, nonce=nonce, task_body=BODY,
            assertion=P.b64d(signed["assertion_b64"])) is None)
        require_equal(await store._run(lambda: store._conn.execute(
            "SELECT consumed FROM challenges WHERE challenge_nonce=?", (nonce,)).fetchone()[0]), 0)
        result, status = await cp.submit_decision(**signed)
        require_equal(status, "ok")
        require(result is not None)
        require_equal((await store.get_request(request))["state"], S.APPROVED)


async def t_task_nonce_cannot_authorize_even_a_correctly_signed_mobile_decision():
    async with _world() as (_, store, cp, service, device):
        task_challenge = await _issue(service, device)
        request = await cp.create_request(principal="local-owner", tool="note_write", mode="execute",
            task="temporary test", workspace="", human_summary="Temporary test")
        req = await store.get_request(request)
        claimed_challenge = {"core_instance_id": cp.core_instance_id, "approval_id": request,
            "action_digest": req["action_digest"], "principal_id": req["principal"],
            "challenge_nonce": task_challenge.nonce, "expires_at": task_challenge.expires_at}
        # Both signatures are valid for the claimed decision. The DB namespace must
        # still refuse this task nonce; failing only a signature check would be weaker.
        signed = H.sign_decision(device, P.canonical_bytes(claimed_challenge),
            payload_sha256=hashlib.sha256(task_challenge.binding_raw).hexdigest())
        result, status = await cp.submit_decision(**signed)
        require_equal(result, None)
        require_equal(status, "nonce_mismatch")
        require_equal((await store.get_request(request))["state"], S.PENDING)
        require(await _verify(service, device, task_challenge) is not None,
                "decision path consumed a task nonce")


async def t_voice_nonce_never_becomes_a_task_start_nonce():
    async with _world() as (_, _, cp, service, device):
        voices = V.SessionNonces()
        nonce = voices.issue(device.device_id)
        raw = V.canonical_bytes(V.build_binding(core_instance_id=cp.core_instance_id,
            device_id=device.device_id, session_nonce=nonce))
        assertion = AA.fake_assertion(device.aakey, V.client_data_hash(raw), 1)
        require(await service.verify(device_id=device.device_id, nonce=nonce, task_body=BODY,
                                     assertion=assertion) is None)
        require(voices.consume(nonce, device.device_id), "task path consumed a voice nonce")


async def t_expiry_is_exact_and_unknown_or_unissued_nonces_never_authorize():
    async with _world() as (_, _, _, service, device):
        with patch("time.time", return_value=10000.0):
            challenge = await _issue(service, device)
        require_equal(challenge.expires_at, 10030.0)
        with patch("time.time", return_value=10030.0):
            require(await _verify(service, device, challenge) is None)
        for nonce in ("0" * 64, "", "x" * 64, challenge.nonce + "00"):
            require(await service.verify(device_id=device.device_id, nonce=nonce, task_body=BODY,
                                         assertion=_sign(device, challenge)) is None)


async def t_nonce_must_still_be_unexpired_when_verification_finishes():
    async with _world() as (_, _, cp, service, device):
        with patch("time.time", return_value=10000.0):
            challenge = await _issue(service, device)
        verify = cp.attest_verifier.verify_assertion
        clock = [10029.0]

        def delayed_verifier(**kwargs):
            result = verify(**kwargs)
            clock[0] = challenge.expires_at
            return result

        with patch("time.time", side_effect=lambda: clock[0]), \
                patch.object(cp.attest_verifier, "verify_assertion", delayed_verifier):
            require(await _verify(service, device, challenge) is None)


async def t_revocation_of_every_bound_identity_blocks_a_previously_issued_proof():
    for kind in ("device", "approval_key", "app_attest_key"):
        async with _world() as (_, _, cp, service, device):
            challenge = await _issue(service, device)
            if kind == "device":
                await cp.revoke_device(device.device_id)
            elif kind == "approval_key":
                await cp.revoke_approval_key(crypto.fingerprint(device.appr_x963))
            else:
                await cp.revoke_app_attest_key(device.aakid)
            require(await _verify(service, device, challenge) is None, kind)
            require(await service.issue(device_id=device.device_id, transport_cred=TRANSPORT, task_body=BODY) is None)


async def t_reenrollment_and_environment_changes_invalidate_the_old_binding():
    async with _world() as (_, _, cp, service, device):
        challenge = await _issue(service, device)
        # Re-enroll the SAME keys under a new generation, fully attested again.
        token, _ = await cp.create_enrollment_token("local-owner")
        result, status = await cp.begin_enrollment(enrollment_token=token, device_id=device.device_id,
            approval_public_key_x963_b64=P.b64e(device.appr_x963), app_attest_key_id=device.aakid,
            transport_cred=TRANSPORT)
        require_equal(status, "ok")
        require(await _verify(service, device, challenge) is None, "half-enrolled device retained task authority")
        _, status = await cp.complete_attestation(enrollment_id=result["enrollment_id"],
            attestation_b64=P.b64e(AA.fake_attestation(device.aakey_x963)))
        require_equal(status, "ok")
        require(await _verify(service, device, challenge) is None, "old proof crossed enrollment generation")
        new_challenge = await _issue(service, device)
        cp.allowed_environments = {AA.ENV_PRODUCTION}
        require(await _verify(service, device, new_challenge) is None)
        cp.allowed_environments = None
        require(await _verify(service, device, new_challenge) is None)


async def t_existing_counter_floor_is_enforced_without_manufacturing_face_id():
    async with _world() as (_, store, cp, service, device):
        # Advance the floor through the existing real two-proof approval path.
        request = await cp.create_request(principal="local-owner", tool="note_write", mode="execute",
                                         task="temporary test", workspace="", human_summary="Temporary test")
        _, status = await H.decide(cp, device, request, counter=5)
        require_equal(status, "ok")
        challenge = await _issue(service, device)
        require(await _verify(service, device, challenge, assertion=_sign(device, challenge, counter=5)) is None)
        require(await _verify(service, device, challenge, assertion=_sign(device, challenge, counter=6)) is not None)
        require_equal((await store.get_device(device.device_id))["app_attest_counter"], 5)


async def t_failed_authority_transaction_does_not_consume_nonce_or_leave_a_proof_event():
    async with _world() as (_, store, _, service, device):
        challenge = await _issue(service, device)
        await store._run(lambda: store._conn.execute("CREATE TRIGGER test_proof_failure "
            "BEFORE INSERT ON audit WHEN NEW.event='app_task_start_proved' "
            "BEGIN SELECT RAISE(ABORT, 'simulated proof transaction failure'); END"))
        require(await _verify(service, device, challenge) is None)
        require_equal(await store._run(lambda: store._conn.execute(
            "SELECT consumed FROM challenges WHERE challenge_nonce=?", (challenge.nonce,)).fetchone()[0]), 0)
        await store._run(lambda: store._conn.execute("DROP TRIGGER test_proof_failure"))
        require(await _verify(service, device, challenge) is not None)


async def t_forged_fields_and_malformed_task_bodies_cannot_issue_a_nonce():
    async with _world() as (_, store, _, service, device):
        invalid = [dict(BODY, origin="iphone_app"), dict(BODY, principal="local-owner"),
                   dict(BODY, scope="everything"), dict(BODY, objective=" "),
                   dict(BODY, objective="Too short"), dict(BODY, objective=" " + BODY["objective"]),
                   dict(BODY, objective=BODY["objective"] + " "), dict(BODY, target_repo="ignored-repo"),
                   dict(BODY, scope="build", target_repo=" trailing-space "),
                   dict(BODY, objective="a" * (T.MAX_OBJECTIVE + 1)),
                   dict(BODY, objective="bad\ud800"), dict(BODY, target_repo=None),
                   dict(BODY, client_request_id=""), dict(BODY, client_request_id="short"),
                   dict(BODY, client_request_id="x" * 129),
                   dict(BODY, client_request_id="same\nrequest"), [], None,
                   {key: value for key, value in BODY.items() if key != "target_repo"}]
        for body in invalid:
            require(await service.issue(device_id=device.device_id, transport_cred=TRANSPORT, task_body=body) is None)
        require_equal(await store._run(lambda: store._conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0]), 0)
        require_equal(T.canonical_task_body(BODY), BODY)


# ---------------------------------------------------------------- N8/C3: optionales conversation_ref

#: Gemessen mit `task_start_proof.py` von c91a745: der Digest von BODY ohne Feld.
GOLDEN_REQUEST_DIGEST = "82a90d7a03a16a90311ab3a769ffac91d9186d996e02bd475b6f2334b3d40f63"
CHAT = "c-0123456789abcdef"


async def t_golden_digest_without_conversation_ref_is_byte_identical_and_a_bound_body_proves():
    require_equal(T.request_digest(BODY), GOLDEN_REQUEST_DIGEST, "an unbound body's digest moved")
    require_equal(T.canonical_task_body(BODY), BODY)
    bound = dict(BODY, conversation_ref=CHAT)
    require_equal(T.canonical_task_body(bound), bound)
    require(T.request_digest(bound) != GOLDEN_REQUEST_DIGEST, "the chat is not part of the signed request")
    async with _world() as (_, store, _, service, device):
        challenge = await _issue(service, device, bound)
        require_equal(challenge.request_digest, T.request_digest(bound))
        # Der Beweis bindet den Chat: derselbe Body ohne Verweis oder mit anderem Chat geht nicht durch.
        for other in (BODY, dict(BODY, conversation_ref="c-fedcba9876543210")):
            require(await _verify(service, device, challenge, body=other) is None)
        proof = await _verify(service, device, challenge, body=bound)
        require(proof is not None, "a chat-bound body could not be proven")
        require_equal(proof.request_digest, T.request_digest(bound))
        # Kombinierbar mit genau einem Zusatz; Form ist Pflicht.
        for body in (dict(bound, conversation_ref=""), dict(bound, conversation_ref="c-0123"),
                     dict(bound, conversation_ref="at-0123456789abcdef"), dict(bound, conversation_ref=None),
                     dict(bound, document_request={}, file_request={})):
            require(await service.issue(device_id=device.device_id, transport_cred=TRANSPORT, task_body=body) is None,
                    f"a malformed chat-bound body issued a nonce: {body.get('conversation_ref')!r}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
