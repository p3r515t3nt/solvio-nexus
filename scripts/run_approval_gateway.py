"""Run the local SOLVIO approval gateway (STEP S2A). LOCAL NETWORK ONLY, HTTPS.

    PYTHONPATH=src .venv/bin/python3 scripts/run_approval_gateway.py \
        --host 192.168.178.123 --port 8770 --pair

`--pair` mints a one-time enrollment token and prints the pairing QR payload for the
iPhone. Bind ONLY to a LAN address; never the public internet / Hetzner. The gateway is
NOT wired into the running realtime Core here — it is a standalone local control plane for
S2A bring-up. Codex execution still flows through the S1 broker + an injected executor.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import ssl
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aiohttp import web

from solvio.security.approval import ApprovalBroker
from solvio.security.mobile_approval import bridge as B
from solvio.security.mobile_approval import control as C
from solvio.security.mobile_approval import gateway as G
from solvio.security.mobile_approval import app_attest as AA
from solvio.security.mobile_approval import identity
from solvio.security.mobile_approval import pairing
from solvio.security.mobile_approval import protocol as P
from solvio.security.mobile_approval import store as S


async def main(host: str, port: int, do_pair: bool) -> None:
    state = identity.DEFAULT_STATE_DIR
    core_id = identity.load_or_create_core_instance_id(state)
    mac_key = identity.MacSigningKey.load_or_create(state)
    st = S.ApprovalControlStore(os.path.join(state, "approval_control.sqlite3"))
    await st.open()
    team_id = os.environ.get("SOLVIO_APP_ATTEST_TEAM", "WQ8CG7R53R")
    bundle_id = os.environ.get("SOLVIO_APP_ATTEST_BUNDLE", "de.solvio.approvals")
    # P1A/F6: the runtime mode must be stated EXPLICITLY. A production runtime accepts only
    # production App Attest; there is no implicit mixed policy and no silent downgrade.
    # Unknown/missing mode raises RuntimeModeError -> startup fails closed.
    mode = os.environ.get("SOLVIO_RUNTIME_MODE", "").strip().lower()
    if not mode:
        raise AA.RuntimeModeError(
            "SOLVIO_RUNTIME_MODE must be set explicitly to "
            f"one of {AA.RUNTIME_MODES} (no implicit default)")
    allowed_envs = AA.allowed_environments_for_mode(mode)
    verifier = AA.AppleAppAttestVerifier(team_id=team_id, bundle_id=bundle_id,
                                         allowed_environments=allowed_envs)
    # P1A.5/M3: a security policy must not be enforced by `assert` — `python -O` strips it.
    if getattr(verifier, "is_fake", False):
        raise SystemExit("FATAL: refusing to start with a fake App Attest verifier.")
    cp = C.MobileApprovalControlPlane(st, mac_key, core_id, attest_verifier=verifier,
                                      app_id=AA.app_id_for(team_id, bundle_id),
                                      allowed_environments=allowed_envs)
    approver = B.MobileApprover()
    coord = B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver), approver)
    # FREEZE/F1C: look at the journal ONCE, before serving. This performs no external
    # write: it closes only attempts still `CLAIMED` (provably never handed to an adapter),
    # under a recovery lease and an expected-status compare, and reports everything else.
    # Every shipped capability is NON_IDEMPOTENT_WRITE, so an ambiguous attempt is surfaced
    # for a human and never retried — restarting this Mac must not act on the world.
    recovery = await coord.startup_recovery_scan()
    print(f"startup recovery: open={recovery['open']} "
          f"closed_no_effect={recovery['closed_no_effect']} "
          f"needs_attention={len(recovery['needs_attention'])} "
          f"retryable={len(recovery['retryable'])} "
          f"reconcilable={len(recovery['reconcilable'])} "
          f"contended={len(recovery['contended'])}")
    for entry in recovery["needs_attention"]:
        print(f"  MANUAL RECOVERY: attempt={entry['attempt_id']} "
              f"approval={entry['approval_id']} capability={entry['capability']} "
              f"status={entry['status']} -> {entry['decision']}")
        print("    the external effect MAY have happened; check the far side before "
              "anything is repeated.")
    for entry in recovery["retryable"] + recovery["reconcilable"]:
        print(f"  RECOVERABLE:     attempt={entry['attempt_id']} "
              f"approval={entry['approval_id']} -> {entry['decision']} "
              f"(run: solvio-approval-admin recover-execution {entry['approval_id']})")

    app = G.build_app(control_plane=cp, coordinator=coord)

    cert, key, tls_fp = pairing.load_or_create_gateway_cert(state, host=host)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    if do_pair:
        token, exp = await cp.create_enrollment_token("local-owner")
        payload = pairing.build_pairing_payload(
            core_instance_id=core_id, endpoint=f"https://{host}:{port}",
            tls_fingerprint=tls_fp, mac_pubkey_fingerprint=mac_key.fingerprint(),
            mac_pubkey_x963_b64=P.b64e(mac_key.public_key_x963()),
            enrollment_token=token, expires_at=exp)
        print("=== PAIRING (scan within the token TTL) ===")
        print(pairing.pairing_qr_text(payload))
        print("=== end pairing payload ===")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port, ssl_context=ctx)
    await site.start()
    print(f"approval gateway listening on https://{host}:{port}  (core={core_id}, "
          f"tls={tls_fp[:16]}…, runtime={mode}, app_attest_env={'+'.join(allowed_envs)})")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        await st.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="LAN address to bind (never 0.0.0.0/public)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--pair", action="store_true", help="mint a one-time pairing token + print QR payload")
    a = ap.parse_args()
    asyncio.run(main(a.host, a.port, a.pair))
