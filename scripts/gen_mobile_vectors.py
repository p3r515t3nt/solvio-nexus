"""Generate shared golden test vectors for the SOLVIO Mobile Approval Protocol V2.

Deterministic TEST keys only (never production keys). The vectors are self-contained:
each carries a public key (X9.63), the exact payload bytes, a signature, and an
expected result. Both the Core (Python) and the iOS app (Swift/CryptoKit) load the SAME
file so Python <-> Swift interop stays reproducible. ECDSA signatures are randomised, so
consumers VERIFY (not reproduce) them.

V2 (S2A.1-P0): the challenge carries the EXACT `task` + `device_id`; the decision binds
`challenge_payload_sha256`. `downgrade_vectors` carries a correctly-signed V1 challenge
that BOTH sides must reject — a valid signature must never resurrect the old contract.
`display_vectors` pins the shared display-safety policy.

Run:  PYTHONPATH=src .venv/bin/python3 scripts/gen_mobile_vectors.py
Writes: tests/vectors/mobile_approval_v2.json  (committed; regenerate to refresh).
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import protocol as P

OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "vectors", "mobile_approval_v2.json")

# The F1 lesson, encoded in the fixtures: the summary is deliberately NOT a description of
# the task, so any consumer that shows the summary instead of the task is provably wrong.
GOLDEN_TASK = "Refactor auth middleware and delete legacy/session_v1.py"
GOLDEN_SUMMARY = "Kleine Aufraeumarbeit im Projekt."


def _sig_vector(name, signer_key, payload, *, tamper=None, note=""):
    raw = P.canonical_bytes(payload)
    sig = crypto.sign(signer_key, raw)
    verify_bytes = raw if tamper is None else tamper
    return {
        "name": name, "note": note,
        "pubkey_x963_b64": P.b64e(crypto.public_key_x963(signer_key)),
        "key_id": crypto.key_id(signer_key),
        "payload_b64": P.b64e(verify_bytes),
        "signature_b64": P.b64e(sig),
        "expect_valid": tamper is None,
    }


def build():
    mac = crypto.generate_private_key()
    dev = crypto.generate_private_key()

    # The synthetic workspace is bound to the retained cross-platform vectors.
    # It is fixture data, not an installation path or permission grant.
    challenge = P.build_challenge_payload(
        core_instance_id="core-golden", approval_id="ap-golden",
        action_digest="a" * 64, principal_id="local-owner", device_id="dev-golden",
        tool_id="codex_task", mode="modify", workspace="/Users/solvio/solvio-core",
        task=GOLDEN_TASK, human_summary=GOLDEN_SUMMARY, challenge_nonce="n" * 64,
        issued_at=1000, expires_at=1120)
    unicode_challenge = P.build_challenge_payload(
        core_instance_id="core-golden", approval_id="ap-uni",
        action_digest="b" * 64, principal_id="local-owner", device_id="dev-golden",
        tool_id="codex_task", mode="modify",
        workspace="/tmp/projekt \"quotes\"\tTab|pipe:colon",
        task="Ändere \"README\" — über\nZeilen, 日本語, emoji 👍, backslash \\ end",
        human_summary="Änderung: \"README\" — über\nZeilen", challenge_nonce="u" * 64,
        issued_at=2000, expires_at=2120)

    challenge_raw = P.canonical_bytes(challenge)
    decision = P.build_decision_payload(
        core_instance_id="core-golden", approval_id="ap-golden", action_digest="a" * 64,
        principal_id="local-owner", device_id="dev-golden", key_id=crypto.key_id(dev),
        challenge_nonce="n" * 64,
        challenge_payload_sha256=hashlib.sha256(challenge_raw).hexdigest(),
        decision=P.DECISION_APPROVE, issued_at=1010, challenge_expires_at=1120)

    tampered_payload = challenge_raw.replace(b"legacy/session_v1.py", b"legacy/session_v9.py")

    sig_vectors = [
        _sig_vector("valid_challenge", mac, challenge, note="Mac-signed V2 challenge, must verify"),
        _sig_vector("valid_challenge_unicode", mac, unicode_challenge,
                    note="unicode/quotes/tab/newline/pipe/colon/backslash in fields"),
        _sig_vector("tampered_challenge_task", mac, challenge, tamper=tampered_payload,
                    note="TASK flipped after signing -> must NOT verify (F1 regression)"),
        _sig_vector("valid_decision", dev, decision, note="device-signed V2 decision, must verify"),
        _sig_vector("tampered_decision", dev, decision,
                    tamper=P.canonical_bytes(decision).replace(b"APPROVE", b"DENYxxx"),
                    note="decision flipped -> must NOT verify"),
    ]

    # A CORRECTLY SIGNED V1 challenge: the signature verifies, but the typed parser must
    # still reject it (unsupported protocol_version). Signature validity != contract validity.
    v1_challenge = {
        "protocol_version": 1, "type": P.TYPE_CHALLENGE,
        "core_instance_id": "core-golden", "approval_id": "ap-v1",
        "action_digest": "c" * 64, "principal_id": "local-owner",
        "tool_id": "codex_task", "mode": "modify", "workspace": "/tmp/ws",
        "human_summary": GOLDEN_SUMMARY, "challenge_nonce": "v" * 64,
        "issued_at": 3000, "expires_at": 3120,
    }
    v1_raw = P.canonical_bytes(v1_challenge)
    downgrade_vectors = [{
        "name": "v1_challenge_signature_valid_but_contract_rejected",
        "note": "signature verifies; typed parse MUST fail (no silent downgrade to V1)",
        "pubkey_x963_b64": P.b64e(crypto.public_key_x963(mac)),
        "payload_b64": P.b64e(v1_raw),
        "signature_b64": P.b64e(crypto.sign(mac, v1_raw)),
        "expect_signature_valid": True,
        "expect_challenge_parse": False,
    }]

    # Shared display-safety policy (protocol.py validate_display_text / Swift isDisplaySafe).
    display_vectors = [
        {"name": "plain_ascii", "text": "Edit README.md", "expect_safe": True},
        {"name": "international", "text": "\u00c4nderung \u65e5\u672c\u8a9e \u0639\u0631\u0628\u0649 \U0001f44d",
         "expect_safe": True},
        {"name": "tab_and_newline", "text": "line1\nline2\tend", "expect_safe": True},
        {"name": "zwj_emoji_sequence", "text": "\U0001f468\u200d\U0001f469\u200d\U0001f467",
         "expect_safe": True, "note": "ZWJ stays allowed (legitimate sequences)"},
        {"name": "nul", "text": "a\u0000b", "expect_safe": False},
        {"name": "carriage_return", "text": "a\u000db", "expect_safe": False},
        {"name": "bell", "text": "a\u0007b", "expect_safe": False},
        {"name": "escape", "text": "a\u001bb", "expect_safe": False},
        {"name": "del", "text": "a\u007fb", "expect_safe": False},
        {"name": "c1_control", "text": "a\u0085b", "expect_safe": False},
        {"name": "bidi_rlo", "text": "a\u202eb", "expect_safe": False},
        {"name": "bidi_lro", "text": "a\u202db", "expect_safe": False},
        {"name": "bidi_lri", "text": "a\u2066b", "expect_safe": False},
        {"name": "bidi_rlm", "text": "a\u200fb", "expect_safe": False},
        {"name": "bidi_alm", "text": "a\u061cb", "expect_safe": False},
        {"name": "zwnbsp", "text": "a\ufeffb", "expect_safe": False},
    ]

    parse_vectors = [
        {"name": "valid_object", "payload_b64": P.b64e(b'{"a":1,"b":2}'), "expect_parse": True},
        {"name": "duplicate_keys", "payload_b64": P.b64e(b'{"a":1,"a":2}'), "expect_parse": False},
        {"name": "array_root", "payload_b64": P.b64e(b'[1,2]'), "expect_parse": False},
        {"name": "scalar_root", "payload_b64": P.b64e(b'42'), "expect_parse": False},
    ]

    return {
        "protocol_version": P.PROTOCOL_VERSION,
        "description": "SOLVIO Mobile Approval Protocol V2 golden vectors (TEST keys only).",
        "curve": "P-256", "hash": "SHA-256", "sig_encoding": "DER",
        "pubkey_encoding": "x963-uncompressed",
        "mac_pubkey_x963_b64": P.b64e(crypto.public_key_x963(mac)),
        "device_pubkey_x963_b64": P.b64e(crypto.public_key_x963(dev)),
        "golden_challenge_task": GOLDEN_TASK,
        "golden_challenge_human_summary": GOLDEN_SUMMARY,
        "golden_challenge_payload_sha256": hashlib.sha256(challenge_raw).hexdigest(),
        "signature_vectors": sig_vectors, "downgrade_vectors": downgrade_vectors,
        "display_vectors": display_vectors, "parse_vectors": parse_vectors,
    }


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(build(), f, ensure_ascii=False, indent=2)
    print("wrote", os.path.relpath(OUT))
