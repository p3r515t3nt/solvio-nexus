"""Validate the shared golden vectors (STEP S2A / S2A.1-P0, Protocol V2). The iOS test suite mirrors this against
the SAME tests/vectors/mobile_approval_v2.json. Direct: python test_mobile_vectors.py."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from solvio.security.mobile_approval import crypto
from solvio.security.mobile_approval import protocol as P

VEC = os.path.join(os.path.dirname(__file__), "vectors", "mobile_approval_v2.json")


def _load():
    with open(VEC, encoding="utf-8") as f:
        return json.load(f)


def test_signature_vectors_verify_as_expected():
    data = _load()
    assert data["signature_vectors"], "no signature vectors"
    for v in data["signature_vectors"]:
        pub = crypto.public_key_from_x963(P.b64d(v["pubkey_x963_b64"]))
        ok = crypto.verify(pub, P.b64d(v["signature_b64"]), P.b64d(v["payload_b64"]))
        assert ok == v["expect_valid"], f"{v['name']}: got {ok}, want {v['expect_valid']}"


def test_parse_vectors_match_strict_parser():
    data = _load()
    for v in data["parse_vectors"]:
        raw = P.b64d(v["payload_b64"])
        try:
            P.strict_parse(raw)
            parsed = True
        except P.ProtocolError:
            parsed = False
        assert parsed == v["expect_parse"], f"{v['name']}: got {parsed}, want {v['expect_parse']}"


def test_header_and_pubkeys():
    data = _load()
    assert data["protocol_version"] == P.PROTOCOL_VERSION
    assert data["curve"] == "P-256" and data["sig_encoding"] == "DER"
    assert len(P.b64d(data["mac_pubkey_x963_b64"])) == 65
    assert len(P.b64d(data["device_pubkey_x963_b64"])) == 65



def test_downgrade_vectors_signature_valid_but_contract_rejected():
    """A correctly-signed V1 challenge must still be refused: a valid signature never
    resurrects the old contract (F1 regression -- no silent downgrade)."""
    data = _load()
    assert data["downgrade_vectors"], "no downgrade vectors"
    for v in data["downgrade_vectors"]:
        pub = crypto.public_key_from_x963(P.b64d(v["pubkey_x963_b64"]))
        raw = P.b64d(v["payload_b64"])
        assert crypto.verify(pub, P.b64d(v["signature_b64"]), raw) == v["expect_signature_valid"]
        try:
            P.validate_challenge_payload(P.strict_parse(raw))
            parsed = True
        except P.ProtocolError:
            parsed = False
        assert parsed == v["expect_challenge_parse"], v["name"]


def test_display_vectors_match_policy():
    """The display-safety policy is a shared contract; Swift mirrors this exact table."""
    data = _load()
    assert data["display_vectors"], "no display vectors"
    for v in data["display_vectors"]:
        try:
            P.validate_display_text(v["text"], field="task", max_len=P.MAX_TASK_LEN)
            safe = True
        except P.ProtocolError:
            safe = False
        assert safe == v["expect_safe"], f"{v['name']}: got {safe}, want {v['expect_safe']}"


def test_golden_challenge_carries_task_distinct_from_summary():
    """V2 core property: the signed challenge carries the EXACT task, and the untrusted
    summary is deliberately NOT a description of it."""
    data = _load()
    vec = next(v for v in data["signature_vectors"] if v["name"] == "valid_challenge")
    ch = P.strict_parse(P.b64d(vec["payload_b64"]))
    P.validate_challenge_payload(ch)
    assert ch["task"] == data["golden_challenge_task"]
    assert ch["human_summary"] == data["golden_challenge_human_summary"]
    assert ch["task"] != ch["human_summary"]
    assert "device_id" in ch and ch["device_id"]
    import hashlib
    assert hashlib.sha256(P.b64d(vec["payload_b64"])).hexdigest() == \
        data["golden_challenge_payload_sha256"]


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
