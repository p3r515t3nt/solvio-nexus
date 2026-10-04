"""Local approval gateway (STEP S2A + S2A.1). LOCAL NETWORK ONLY, HTTPS, minimal endpoints.

This is NOT a general Core API — no shell, no exec, no generic tool, no arbitrary action.
Only: two-step device enrollment (begin + App Attest complete), listing pending approvals,
issuing a Mac-signed challenge, and submitting an iPhone-signed decision.

Two separate credentials:
- TRANSPORT AUTH (`X-Device-Id` + `X-Transport-Cred`) may READ pending approvals / request a
  challenge / read status — and ONLY for an ATTESTED device. It can NEVER approve.
- APPROVAL AUTHORITY is the Face-ID Secure-Enclave signature over the exact decision bytes,
  AND (S2A.1) a fresh Apple App Attest assertion over the decision binding. Both are verified
  by the control-plane. The decision endpoint needs no transport auth because the two proofs
  are the authority (and they bind the device).

Runtime (scripts/run_approval_gateway.py): bind the LAN address on a dedicated port, serve
HTTPS with a local certificate whose fingerprint the iPhone pins from the pairing QR. Never
bind the public internet / Hetzner.
"""
from __future__ import annotations

from aiohttp import web

API = "/v1"


def _err(status, code):
    return web.json_response({"error": code}, status=status)


async def _authed_device(request):
    cp = request.app["control_plane"]
    device_id = request.headers.get("X-Device-Id", "")
    cred = request.headers.get("X-Transport-Cred", "")
    if not device_id or not await cp.verify_transport_cred(device_id, cred):
        return None
    return device_id


async def h_enroll_begin(request):
    cp = request.app["control_plane"]
    try:
        body = await request.json()
        args = dict(enrollment_token=body["enrollment_token"], device_id=body["device_id"],
                    approval_public_key_x963_b64=body["approval_public_key_x963_b64"],
                    app_attest_key_id=body["app_attest_key_id"])
    except Exception:  # noqa: BLE001
        return _err(400, "bad_request")
    if "transport_cred" in body:
        args["transport_cred"] = body["transport_cred"]
    res, st = await cp.begin_enrollment(**args)
    if res is None:
        return _err(403, st)
    return web.json_response(res)


async def h_enroll_complete(request):
    cp = request.app["control_plane"]
    try:
        body = await request.json()
        args = dict(enrollment_id=body["enrollment_id"], attestation_b64=body["attestation_b64"])
    except Exception:  # noqa: BLE001
        return _err(400, "bad_request")
    res, st = await cp.complete_attestation(**args)
    if res is None:
        return _err(403, st)
    return web.json_response(res)


async def h_list(request):
    cp = request.app["control_plane"]
    device_id = await _authed_device(request)
    if device_id is None:
        return _err(401, "unauthorized")
    dev = await cp.store.get_device(device_id)
    pend = await cp.store.list_pending(principal=dev["principal"])
    out = [{"approval_id": r["approval_id"], "tool": r["tool"], "mode": r["mode"],
            "task": r["task"], "workspace": r["workspace"],
            "human_summary": r["human_summary"], "action_digest": r["action_digest"],
            "expires_at": r["expires_at"]} for r in pend]
    return web.json_response({"approvals": out})


async def h_challenge(request):
    cp = request.app["control_plane"]
    device_id = await _authed_device(request)
    if device_id is None:
        return _err(401, "unauthorized")
    wire, st = await cp.issue_challenge(approval_id=request.match_info["id"],
                                        device_id=device_id)
    if wire is None:
        return _err(409, st)
    return web.json_response(wire)


async def h_decision(request):
    coord = request.app["coordinator"]
    try:
        body = await request.json()
        args = dict(payload_b64=body["payload_b64"], signature_b64=body["signature_b64"],
                    key_id=body["key_id"])
    except Exception:  # noqa: BLE001
        return _err(400, "bad_request")
    if "assertion_b64" in body:
        args["assertion_b64"] = body["assertion_b64"]
    # approval authority = the Secure-Enclave signature + App Attest assertion, both verified
    # inside the control-plane.
    res, st = await coord.apply_mobile_decision(**args)
    if res is None:
        return _err(403, st)
    return web.json_response(res)


async def h_status(request):
    cp = request.app["control_plane"]
    device_id = await _authed_device(request)
    if device_id is None:
        return _err(401, "unauthorized")
    req = await cp.store.get_request(request.match_info["id"])
    if req is None:
        return _err(404, "unknown")
    return web.json_response({"approval_id": req["approval_id"], "state": req["state"],
                             "expires_at": req["expires_at"]})


def build_app(*, control_plane, coordinator) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app["control_plane"] = control_plane
    app["coordinator"] = coordinator
    app.add_routes([
        web.post(f"{API}/enroll/begin", h_enroll_begin),
        web.post(f"{API}/enroll/complete", h_enroll_complete),
        web.get(f"{API}/approvals", h_list),
        web.post(f"{API}/approvals/{{id}}/challenge", h_challenge),
        web.post(f"{API}/approvals/{{id}}/decision", h_decision),
        web.get(f"{API}/approvals/{{id}}/status", h_status),
    ])
    return app
