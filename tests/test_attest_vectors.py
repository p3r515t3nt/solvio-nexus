"""App Attest binding golden vectors stay in sync with attest_protocol (STEP S2A.1).

Guards the shared tests/vectors/app_attest_binding_v1.json that the iOS Kit also asserts
against, so a canonicalisation change on either side is caught. Direct: python <file>."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from solvio.security.mobile_approval import attest_protocol as AP
from solvio.security.mobile_approval import protocol as P

VEC = os.path.join(os.path.dirname(__file__), "vectors", "app_attest_binding_v1.json")


def _load():
    with open(VEC, encoding="utf-8") as f:
        return json.load(f)


def test_enrollment_vector_matches_code():
    v = _load()["enrollment"]
    keys = ("core_instance_id", "principal_id", "device_id", "enrollment_id", "approval_key_id",
            "approval_public_key_sha256", "attestation_nonce", "issued_at", "expires_at")
    b = AP.build_enrollment_binding(**{k: v["binding"][k] for k in keys})
    raw = P.canonical_bytes(b)
    assert raw.hex() == v["canonical_bytes_hex"]
    assert AP.enrollment_client_data_hash(raw).hex() == v["client_data_hash_hex"]


def test_decision_vector_matches_code():
    v = _load()["decision"]
    keys = ("core_instance_id", "approval_id", "device_id", "decision_sha256", "challenge_nonce",
            "approval_public_key_sha256")
    b = AP.build_decision_binding(**{k: v["binding"][k] for k in keys})
    raw = P.canonical_bytes(b)
    assert raw.hex() == v["canonical_bytes_hex"]
    assert AP.decision_client_data_hash(raw).hex() == v["client_data_hash_hex"]


def test_domains_match():
    v = _load()["domains"]
    assert v["enrollment"] == AP.DOMAIN_ENROLLMENT.decode()
    assert v["decision"] == AP.DOMAIN_DECISION.decode()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
