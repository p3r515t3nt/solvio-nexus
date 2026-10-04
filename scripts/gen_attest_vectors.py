"""Generate shared App Attest binding golden vectors (STEP S2A.1).

Fixed inputs -> canonical binding bytes + domain-separated clientDataHash. The iOS Kit builds
the same bindings and must reproduce these EXACT bytes/hashes, freezing the Mac<->iOS
canonicalisation contract for App Attest (attestKey + generateAssertion clientDataHash).

    PYTHONPATH=src python scripts/gen_attest_vectors.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from solvio.security.mobile_approval import attest_protocol as AP
from solvio.security.mobile_approval import protocol as P

OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "vectors", "app_attest_binding_v1.json")

ENROLL_INPUT = dict(
    core_instance_id="core-vec-0001", principal_id="local-owner", device_id="dev-vec-0001",
    enrollment_id="enr-vec-0001", approval_key_id="1122334455667788",
    approval_public_key_sha256="aa" * 32, attestation_nonce="bb" * 32,
    issued_at=1723900000, expires_at=1723900300)

DECISION_INPUT = dict(
    core_instance_id="core-vec-0001", approval_id="ap-vec-0001", device_id="dev-vec-0001",
    decision_sha256="cc" * 32, challenge_nonce="dd" * 32, approval_public_key_sha256="aa" * 32)


def _entry(binding, cdh):
    raw = P.canonical_bytes(binding)
    return {"binding": binding, "canonical_bytes_hex": raw.hex(),
            "canonical_bytes_utf8": raw.decode("utf-8"), "client_data_hash_hex": cdh(raw).hex()}


def main() -> None:
    enroll = AP.build_enrollment_binding(**ENROLL_INPUT)
    decision = AP.build_decision_binding(**DECISION_INPUT)
    data = {
        "version": 1,
        "domains": {"enrollment": AP.DOMAIN_ENROLLMENT.decode(),
                    "decision": AP.DOMAIN_DECISION.decode()},
        "enrollment": _entry(enroll, AP.enrollment_client_data_hash),
        "decision": _entry(decision, AP.decision_client_data_hash),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("wrote", os.path.relpath(OUT))
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
