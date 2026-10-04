"""S2A.1-P0 human display integrity / Approval Protocol V2 (closes audit finding F1).

F1 (reproduced on real hardware, docs/S2A_1_F1_live_reproduction.md): the approver was
shown a model-authored `human_summary` while the execution-determining `task` — the field
`action_digest` actually binds — was never signed and never displayed. Approve A /
execute B with fully valid signatures.

These tests pin the fix:
  * the Mac-SIGNED challenge carries the EXACT stored task, tool_id, mode, workspace;
  * tampering with any of them after signing breaks the Mac signature;
  * the decision binds `challenge_payload_sha256`, so consent is tied to the exact
    displayed bytes — a decision from a different challenge is refused;
  * a V1 challenge is refused outright (no silent downgrade);
  * `human_summary` carries NO execution authority;
  * display-spoofing control/bidi characters are refused at request creation;
  * replay and the S1 gate remain intact.

Direct: python test_p0_display_integrity.py
"""
import asyncio
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import mobile_attest_helper as H
from solvio.security.approval import ApprovalBroker, action_digest
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S

TASK = "Refactor auth middleware and delete legacy/session_v1.py"
SUMMARY = "Nur eine harmlose Aufraeumarbeit."   # deliberately NOT a description of TASK
WS = "/tmp/p0-ws"


async def _wire(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, "c.sqlite3"))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id="TEAM.bundle")
    return st, cp


async def _request_and_challenge(cp, ctx, *, task=TASK, summary=SUMMARY):
    ap = await cp.create_request(principal="local-owner", tool="codex_task", mode="modify",
                                 task=task, workspace=WS, human_summary=summary)
    wire, s = await cp.issue_challenge(approval_id=ap, device_id=ctx.device_id)
    assert s == "ok", s
    raw = P.b64d(wire["payload_b64"])
    return ap, wire, raw, P.strict_parse(raw)


def _mac_verifies(cp, wire, raw):
    return crypto.verify(crypto.public_key_from_x963(cp.mac_key.public_key_x963()),
                         P.b64d(wire["signature_b64"]), raw)


# ---- 1-4: the signed challenge IS the authoritative action ----------------
async def t_signed_challenge_carries_exact_task_and_action_fields():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        ap, wire, raw, ch = await _request_and_challenge(cp, ctx)
        P.validate_challenge_payload(ch)
        req = await st.get_request(ap)
        # (1)(3) exact task, straight from the stored action — not re-summarised
        assert ch["task"] == TASK == req["task"]
        # (2) tool_id + the rest of the execution-determining fields are signed
        assert ch["tool_id"] == "codex_task" == req["tool"]
        assert ch["mode"] == "modify" and ch["workspace"] == WS
        assert ch["device_id"] == ctx.device_id
        # (4) the digest in the challenge recomputes over exactly those fields
        assert ch["action_digest"] == action_digest(
            tool_id=ch["tool_id"], mode=ch["mode"], task=ch["task"], workspace=ch["workspace"])
        assert ch["action_digest"] == req["action_digest"]
        assert _mac_verifies(cp, wire, raw)
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 5-8: tampering any displayed authority field breaks the Mac signature ----
async def t_tampering_any_signed_action_field_breaks_signature():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        _, wire, raw, _ = await _request_and_challenge(cp, ctx)
        assert _mac_verifies(cp, wire, raw)
        for name, before, after in (
                ("task", b"session_v1.py", b"session_v9.py"),
                ("tool_id", b'"codex_task"', b'"codex_evil"'),
                ("workspace", b"/tmp/p0-ws", b"/tmp/p0-XX"),
                ("mode", b'"modify"', b'"delete"')):
            assert before in raw, name
            assert not _mac_verifies(cp, wire, raw.replace(before, after)), \
                f"{name} tamper must break the Mac signature"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 9: no silent downgrade to V1 ----------------------------------------
async def t_v1_challenge_and_decision_fail_closed():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        _, _, _, ch = await _request_and_challenge(cp, ctx)
        v1 = dict(ch)
        v1["protocol_version"] = 1
        v1.pop("task"); v1.pop("device_id")          # the genuine V1 shape
        try:
            P.validate_challenge_payload(v1); assert False, "V1 challenge accepted"
        except P.ProtocolError:
            pass
        # A V1-shaped decision (no display binding) is refused too.
        try:
            P.build_decision_payload(
                core_instance_id="c", approval_id="a", action_digest="d", principal_id="p",
                device_id="dv", key_id="k", challenge_nonce="n",
                decision=P.DECISION_APPROVE, issued_at=1, challenge_expires_at=2)
            assert False, "V1 decision built"
        except TypeError:
            pass
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 10-11: the decision is bound to the exact DISPLAYED challenge --------
async def t_display_binding_mismatch_and_cross_challenge_rejected():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        # (10) right challenge, wrong challenge_payload_sha256 -> refused
        ap, _, raw, _ = await _request_and_challenge(cp, ctx)
        res, s = await cp.submit_decision(
            **H.sign_decision(ctx, raw, payload_sha256="f" * 64))
        assert res is None and s == "nonce_display_mismatch", s
        assert (await st.get_request(ap))["state"] == S.PENDING   # nothing consumed
        # the untouched decision for the SAME challenge still works
        res, s = await cp.submit_decision(**H.sign_decision(ctx, raw))
        assert s == "ok" and res["decision"] == "APPROVE", s

        # (11) a decision minted for challenge A replayed onto approval B -> refused
        apB, _, rawB, _ = await _request_and_challenge(cp, ctx, task="other task")
        wireA_on_B = H.sign_decision(ctx, raw)            # A's nonce + A's display hash
        res, s = await cp.submit_decision(**wireA_on_B)
        assert res is None, "cross-challenge decision accepted"
        assert (await st.get_request(apB))["state"] == S.PENDING
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 12-13: the unsigned list is not authority; summary has none ----------
async def t_summary_has_no_execution_authority():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        # (13) same task, different summary -> identical digest: the summary is not
        # an authorization input and can never change what executes.
        d1 = action_digest(tool_id="codex_task", mode="modify", task=TASK, workspace=WS)
        d2 = action_digest(tool_id="codex_task", mode="modify", task=TASK, workspace=WS)
        assert d1 == d2
        # ... and a different task DOES change it (so the digest tracks the real action)
        assert action_digest(tool_id="codex_task", mode="modify", task="other",
                             workspace=WS) != d1

        # (12) whatever a list/transport view might claim, the SIGNED challenge is the
        # authority and it carries the stored task verbatim.
        ap, _, _, ch = await _request_and_challenge(cp, ctx, summary="voellig anderer Text")
        req = await st.get_request(ap)
        assert ch["task"] == req["task"] == TASK
        assert ch["human_summary"] == "voellig anderer Text" != ch["task"]
        assert ch["action_digest"] == d1
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 14: display-spoofing text is refused at request creation -------------
async def t_display_spoofing_text_rejected_at_creation():
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _wire(tmp)
        spoofs = {
            "nul": "rm \u0000 harmless",
            "cr_overwrite": "harmless\u000drm -rf /",
            "bidi_rlo": "harmless \u202erm -rf /",
            "bidi_isolate": "harmless \u2066rm -rf /\u2069",
            "c1": "harmless \u0085 rm",
            "ansi_escape": "harmless \u001b[2Krm -rf /",
        }
        for name, bad in spoofs.items():
            for field in ("task", "human_summary"):
                kw = dict(principal="local-owner", tool="codex_task", mode="modify",
                          task=TASK, workspace=WS, human_summary=SUMMARY)
                kw[field] = bad
                try:
                    await cp.create_request(**kw)
                    assert False, f"{name} accepted in {field}"
                except P.ProtocolError:
                    pass
        # legitimate international text and tab/newline still work
        ap = await cp.create_request(
            principal="local-owner", tool="codex_task", mode="modify",
            task="Ändere README\n\tZeile 2 — 日本語 👍", workspace=WS,
            human_summary="Änderung am README")
        assert ap
        # over-long task is refused (bounded display)
        try:
            await cp.create_request(principal="local-owner", tool="codex_task", mode="modify",
                                    task="x" * (P.MAX_TASK_LEN + 1), workspace=WS,
                                    human_summary=SUMMARY)
            assert False, "over-long task accepted"
        except P.ProtocolError:
            pass
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 15-16: replay still blocked; S1 remains the last gate ----------------
async def t_replay_blocked_and_s1_still_last_gate():
    tmp = tempfile.mkdtemp()
    ws = tempfile.mkdtemp()
    try:
        st = S.ApprovalControlStore(os.path.join(tmp, "c.sqlite3"))
        await st.open()
        cp = C.MobileApprovalControlPlane(
            st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
            attest_verifier=H.fake_verifier(), app_id="TEAM.bundle")
        approver = B.MobileApprover()
        coord = B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver), approver)
        ctx = await H.enroll_attested(cp)

        ap = await coord.request_codex_modify(principal="local-owner", task=TASK,
                                              workspace=ws, human_summary=SUMMARY)
        wire, s = await cp.issue_challenge(approval_id=ap, device_id=ctx.device_id)
        assert s == "ok"
        packet = H.sign_decision(ctx, P.b64d(wire["payload_b64"]))
        res, s = await coord.apply_mobile_decision(**packet)
        assert s == "ok" and res["decision"] == "APPROVE"

        # (15) exact replay of the accepted packet -> refused
        r2, s2 = await coord.apply_mobile_decision(**packet)
        assert r2 is None and s2 == "not_pending", s2

        # (16) S1 gate is still the last boundary and hands over the EXACT stored action
        calls = []

        async def ex(action):
            calls.append(action)
            return True, {"ok": True}

        out, es = await coord.execute_approved(ap, ex)
        assert es == "ok" and len(calls) == 1
        assert calls[0]["task"] == TASK and calls[0]["workspace"] == ws
        assert calls[0]["action_digest"] == action_digest(
            tool_id="codex_task", mode="modify", task=TASK, workspace=ws)
        # fail-closed without a trusted confirmation
        ap2 = await coord.request_codex_modify(principal="local-owner", task=TASK,
                                               workspace=ws, human_summary=SUMMARY)
        await st.transition(ap2, S.APPROVED)          # durable state WITHOUT iPhone proof
        out2, es2 = await coord.execute_approved(ap2, ex)
        assert out2 is None and es2 == "s1_no_trusted_approver", es2
        assert len(calls) == 1
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(ws, ignore_errors=True)



# ---- 18: tamper in flight + the unsigned list can never be authority --------
async def t_tampered_signed_task_is_rejected_by_the_client_check():
    """If `task` is changed between the Mac signature and the iPhone, the client-side
    verification (verify-over-exact-received-bytes) fails -> the phone must refuse."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        _, wire, raw, _ = await _request_and_challenge(cp, ctx)
        mac_pub = crypto.public_key_from_x963(cp.mac_key.public_key_x963())
        sig = P.b64d(wire["signature_b64"])
        assert crypto.verify(mac_pub, sig, raw)                     # untouched: verifies
        for before, after in ((b"session_v1.py", b"session_v9.py"),
                              (b'"modify"', b'"delete"'),
                              (b"/tmp/p0-ws", b"/tmp/evil--")):
            assert before in raw
            assert not crypto.verify(mac_pub, sig, raw.replace(before, after)), \
                "in-flight tamper must fail the client signature check"
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def t_unsigned_list_can_never_become_authority():
    """A decision assembled from list-shaped data (right approval, right digest) but not
    tied to the ISSUED challenge is refused: authority lives only in the signed challenge."""
    tmp = tempfile.mkdtemp()
    try:
        st, cp = await _wire(tmp)
        ctx = await H.enroll_attested(cp)
        ap, _, raw, ch = await _request_and_challenge(cp, ctx)
        req = await st.get_request(ap)
        # everything the unsigned list exposes really is present ...
        for field in ("task", "tool", "mode", "workspace", "human_summary", "action_digest"):
            assert req[field] is not None
        # ... but a decision that invents its own nonce/display hash from that data fails.
        forged = P.build_decision_payload(
            core_instance_id=cp.core_instance_id, approval_id=ap,
            action_digest=req["action_digest"], principal_id=req["principal"],
            device_id=ctx.device_id, key_id=ctx.key_id,
            challenge_nonce="f" * 64,
            challenge_payload_sha256=hashlib.sha256(b"list-derived").hexdigest(),
            decision=P.DECISION_APPROVE, issued_at=1, challenge_expires_at=ch["expires_at"])
        fraw = P.canonical_bytes(forged)
        # Give it a CORRECT App Attest assertion so proof B passes and the rejection is
        # provably caused by the missing challenge binding, not by a broken second proof.
        from solvio.security.mobile_approval import attest_protocol as AP
        db = AP.build_decision_binding(
            core_instance_id=cp.core_instance_id, approval_id=ap, device_id=ctx.device_id,
            decision_sha256=AP.sha256_hex(fraw), challenge_nonce="f" * 64,
            approval_public_key_sha256=crypto.fingerprint(ctx.appr_x963))
        cdh = AP.decision_client_data_hash(P.canonical_bytes(db))
        res, s = await cp.submit_decision(
            payload_b64=P.b64e(fraw), signature_b64=P.b64e(crypto.sign(ctx.appr, fraw)),
            key_id=ctx.key_id,
            assertion_b64=P.b64e(H.AA.fake_assertion(ctx.aakey, cdh, 1)))
        assert res is None and s == "nonce_unknown", s
        assert (await st.get_request(ap))["state"] == S.PENDING
        # the genuine, challenge-bound decision still works
        res, s = await cp.submit_decision(**H.sign_decision(ctx, raw))
        assert s == "ok" and res["decision"] == "APPROVE", s
        await st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
