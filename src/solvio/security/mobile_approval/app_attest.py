"""Apple App Attest server-side verification (STEP S2A.1). MAC ONLY, fail-closed.

Implements Apple's documented attestation + assertion validation
(developer.apple.com/documentation/devicecheck/validating-apps-that-connect-to-your-server):

ATTESTATION (enrollment):
  1. CBOR-decode the attestation object; fmt == "apple-appattest".
  2. Verify the x5c chain (leaf, intermediate) up to the pinned Apple App Attestation Root CA
     (signatures + validity windows).
  3. nonce = SHA256(authenticatorData || clientDataHash).
  4. Read the credCert extension OID 1.2.840.113635.100.8.2 (DER SEQUENCE -> single OCTET
     STRING) and require it == nonce.
  5. SHA256(credCert public key, X9.62 uncompressed) == the app-supplied key identifier.
  6. authData.rpIdHash == SHA256(appID), appID = "<TeamID>.<BundleID>".
  7. authData.counter == 0.
  8. authData.aaguid == "appattestdevelop" (development) or "appattest"+0x00*7 (production).
  9. authData.credentialId == key identifier.

ASSERTION (each APPROVE):
  nonce = SHA256(authenticatorData || clientDataHash); verify the ECDSA-P256/SHA-256
  signature over THAT NONCE (Apple signs the nonce, not the raw concatenation — this
  differs from the WebAuthn convention) with the stored app-attest public key;
  authData.rpIdHash == SHA256(appID); authData.counter strictly greater than the last
  stored counter (Apple anti-replay). Our own challenge nonce / one-time decision / approval
  state remain in force as additional layers.

TWO KEYS, never conflated: the APPROVAL key (Face-ID Secure Enclave) is the only approval
authority; the APP ATTEST key verified here proves a legitimate SOLVIO app instance +
app/device integrity. This module NEVER produces an approval.
"""
from __future__ import annotations

import datetime as _dt
import base64
import hashlib
import hmac
from dataclasses import dataclass

import cbor2
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

NONCE_OID = "1.2.840.113635.100.8.2"
AAGUID_DEVELOPMENT = b"appattestdevelop"
AAGUID_PRODUCTION = b"appattest" + b"\x00" * 7
ENV_DEVELOPMENT = "development"
ENV_PRODUCTION = "production"

# ---- P1A/F6: explicit runtime modes --------------------------------------
# A production runtime accepts ONLY production App Attest. A development runtime accepts
# ONLY development. There is no implicit mixed policy and no silent downgrade: the mode
# must be stated explicitly and the environment set is derived from it.
RUNTIME_DEVELOPMENT = "development"
RUNTIME_PRODUCTION = "production"
RUNTIME_MODES = (RUNTIME_DEVELOPMENT, RUNTIME_PRODUCTION)

_MODE_ENVS = {
    RUNTIME_PRODUCTION: (ENV_PRODUCTION,),
    RUNTIME_DEVELOPMENT: (ENV_DEVELOPMENT,),
}


class RuntimeModeError(ValueError):
    """Ambiguous or missing runtime mode -> fail closed at startup."""


def allowed_environments_for_mode(mode: str) -> tuple[str, ...]:
    """Map an explicit runtime mode to the App Attest environments it may accept.
    Unknown/empty mode raises — startup must fail closed rather than guess."""
    if mode not in _MODE_ENVS:
        raise RuntimeModeError(
            f"runtime mode must be one of {RUNTIME_MODES}, got {mode!r}")
    return _MODE_ENVS[mode]
_AAGUID_ENV = {AAGUID_DEVELOPMENT: ENV_DEVELOPMENT, AAGUID_PRODUCTION: ENV_PRODUCTION}

_FLAG_AT = 0x40  # attested-credential-data present

# Apple App Attestation Root CA (apple.com/certificateauthority/private, self-signed P-384,
# SHA256 1CB9823BA28BA6AD2D33A006941DE2AE4F513EF1D4E831B9F7E0FA7B6242C932, valid to 2045).
APPLE_APP_ATTEST_ROOT_CA_PEM = b"""-----BEGIN CERTIFICATE-----
MIICITCCAaegAwIBAgIQC/O+DvHN0uD7jG5yH2IXmDAKBggqhkjOPQQDAzBSMSYw
JAYDVQQDDB1BcHBsZSBBcHAgQXR0ZXN0YXRpb24gUm9vdCBDQTETMBEGA1UECgwK
QXBwbGUgSW5jLjETMBEGA1UECAwKQ2FsaWZvcm5pYTAeFw0yMDAzMTgxODMyNTNa
Fw00NTAzMTUwMDAwMDBaMFIxJjAkBgNVBAMMHUFwcGxlIEFwcCBBdHRlc3RhdGlv
biBSb290IENBMRMwEQYDVQQKDApBcHBsZSBJbmMuMRMwEQYDVQQIDApDYWxpZm9y
bmlhMHYwEAYHKoZIzj0CAQYFK4EEACIDYgAERTHhmLW07ATaFQIEVwTtT4dyctdh
NbJhFs/Ii2FdCgAHGbpphY3+d8qjuDngIN3WVhQUBHAoMeQ/cLiP1sOUtgjqK9au
Yen1mMEvRq9Sk3Jm5X8U62H+xTD3FE9TgS41o0IwQDAPBgNVHRMBAf8EBTADAQH/
MB0GA1UdDgQWBBSskRBTM72+aEH/pwyp5frq5eWKoTAOBgNVHQ8BAf8EBAMCAQYw
CgYIKoZIzj0EAwMDaAAwZQIwQgFGnByvsiVbpTKwSga0kP0e8EeDS4+sQmTvb7vn
53O5+FRXgeLhpJ06ysC5PrOyAjEAp5U4xDgEgllF7En3VcE3iexZZtKeYnpqtijV
oyFraWVIyd/dganmrduC1bmTBGwD
-----END CERTIFICATE-----
"""


class AppAttestError(Exception):
    """Any deviation from the Apple spec / SOLVIO policy -> fail closed."""


@dataclass(frozen=True)
class AttestedKey:
    key_id_b64: str            # Apple App Attest key identifier (base64), == SHA256(pubkey)
    public_key_x963: bytes     # app-attest public key, X9.62 uncompressed point
    counter: int               # authData counter at attestation (must be 0)
    environment: str           # "development" | "production" (from AAGUID)
    receipt: bytes             # App Attest receipt (for later fraud assessment)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def app_id_for(team_id: str, bundle_id: str) -> str:
    return f"{team_id}.{bundle_id}"


# ---- minimal DER walker (nonce extension) ---------------------------------
def _read_tlv(data: bytes, i: int):
    if i + 2 > len(data):
        raise AppAttestError("truncated DER")
    tag = data[i]; i += 1
    ln = data[i]; i += 1
    if ln & 0x80:
        n = ln & 0x7F
        if n == 0 or i + n > len(data):
            raise AppAttestError("bad DER length")
        ln = int.from_bytes(data[i:i + n], "big"); i += n
    if i + ln > len(data):
        raise AppAttestError("DER content overrun")
    return tag, data[i:i + ln], i + ln


def _nonce_octet_string(ext_der: bytes) -> bytes:
    """credCert ext value is DER SEQUENCE { [1] { OCTET STRING nonce } }."""
    tag, seq, _ = _read_tlv(ext_der, 0)
    if tag != 0x30:
        raise AppAttestError("nonce ext: expected SEQUENCE")
    tag, ctx, _ = _read_tlv(seq, 0)
    if tag != 0xA1:
        raise AppAttestError("nonce ext: expected [1]")
    tag, octets, _ = _read_tlv(ctx, 0)
    if tag != 0x04:
        raise AppAttestError("nonce ext: expected OCTET STRING")
    return octets


# ---- authenticator data ---------------------------------------------------
def _parse_attested_authdata(ad: bytes):
    if len(ad) < 55:
        raise AppAttestError("authData too short")
    rp_id_hash = ad[0:32]
    flags = ad[32]
    counter = int.from_bytes(ad[33:37], "big")
    if not (flags & _FLAG_AT):
        raise AppAttestError("attested credential data flag not set")
    aaguid = ad[37:53]
    cred_len = int.from_bytes(ad[53:55], "big")
    cred_id = ad[55:55 + cred_len]
    if len(cred_id) != cred_len:
        raise AppAttestError("credentialId truncated")
    return rp_id_hash, flags, counter, aaguid, cred_id


def _parse_assertion_authdata(ad: bytes):
    if len(ad) < 37:
        raise AppAttestError("assertion authData too short")
    return ad[0:32], ad[32], int.from_bytes(ad[33:37], "big")


class AppAttestVerifier:
    """Interface the control-plane depends on. Production = AppleAppAttestVerifier."""

    def verify_attestation(self, *, key_id_b64: str, attestation: bytes,
                           client_data_hash: bytes) -> AttestedKey:
        raise NotImplementedError

    def verify_assertion(self, *, assertion: bytes, client_data_hash: bytes,
                         public_key_x963: bytes, prev_counter: int) -> int:
        raise NotImplementedError


class AppleAppAttestVerifier(AppAttestVerifier):
    def __init__(self, *, team_id: str, bundle_id: str, allowed_environments,
                 root_ca_pem: bytes = APPLE_APP_ATTEST_ROOT_CA_PEM) -> None:
        """P1A/F6: `allowed_environments` is REQUIRED — there is deliberately no default.
        The previous permissive default `(development, production)` meant a production
        gateway silently accepted development attestations. Callers must state the policy;
        use `allowed_environments_for_mode()` to derive it from an explicit runtime mode."""
        self.app_id = app_id_for(team_id, bundle_id)
        self.rp_id_hash = _sha256(self.app_id.encode("utf-8"))
        envs = frozenset(allowed_environments)
        if not envs or not envs <= {ENV_DEVELOPMENT, ENV_PRODUCTION}:
            raise ValueError(f"invalid allowed_environments: {sorted(envs)}")
        self.allowed_environments = set(envs)
        self.root = x509.load_pem_x509_certificate(root_ca_pem)

    # -- chain --
    def _verify_chain(self, certs) -> None:
        if len(certs) < 2:
            raise AppAttestError("x5c must contain leaf + intermediate")
        chain = list(certs) + [self.root]
        now = _now()
        for cert, issuer in zip(chain, chain[1:]):
            if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
                raise AppAttestError("certificate outside validity window")
            issuer_pub = issuer.public_key()
            if not isinstance(issuer_pub, ec.EllipticCurvePublicKey):
                raise AppAttestError("non-EC issuer key")
            try:
                issuer_pub.verify(cert.signature, cert.tbs_certificate_bytes,
                                  ec.ECDSA(cert.signature_hash_algorithm))
            except InvalidSignature:
                raise AppAttestError("broken certificate chain") from None
        # anchor identity: the last issuer must be exactly our pinned root
        if chain[-1].fingerprint(chain[-1].signature_hash_algorithm) != \
                self.root.fingerprint(self.root.signature_hash_algorithm):
            raise AppAttestError("chain does not anchor to Apple root")

    def verify_attestation(self, *, key_id_b64: str, attestation: bytes,
                           client_data_hash: bytes) -> AttestedKey:
        try:
            obj = cbor2.loads(attestation)
        except Exception as exc:  # noqa: BLE001
            raise AppAttestError(f"attestation not CBOR: {exc}") from None
        if not isinstance(obj, dict) or obj.get("fmt") != "apple-appattest":
            raise AppAttestError("bad attestation fmt")
        att = obj.get("attStmt")
        auth_data = obj.get("authData")
        if not isinstance(att, dict) or not isinstance(auth_data, (bytes, bytearray)):
            raise AppAttestError("bad attestation structure")
        x5c = att.get("x5c")
        if not isinstance(x5c, list) or not x5c:
            raise AppAttestError("missing x5c")
        try:
            certs = [x509.load_der_x509_certificate(bytes(c)) for c in x5c]
        except Exception as exc:  # noqa: BLE001
            raise AppAttestError(f"bad x5c cert: {exc}") from None
        self._verify_chain(certs)
        leaf = certs[0]

        # (3-4) nonce in credCert extension
        nonce = _sha256(bytes(auth_data), client_data_hash)
        try:
            ext = leaf.extensions.get_extension_for_oid(x509.ObjectIdentifier(NONCE_OID))
        except x509.ExtensionNotFound:
            raise AppAttestError("credCert missing nonce extension") from None
        ext_bytes = ext.value.value if isinstance(ext.value, x509.UnrecognizedExtension) \
            else ext.value.public_bytes()
        if not hmac.compare_digest(_nonce_octet_string(ext_bytes), nonce):
            raise AppAttestError("attestation nonce mismatch")

        # (5) public key hash == key id
        leaf_pub = leaf.public_key()
        if not isinstance(leaf_pub, ec.EllipticCurvePublicKey):
            raise AppAttestError("leaf key not EC")
        x963 = leaf_pub.public_bytes(serialization.Encoding.X962,
                                     serialization.PublicFormat.UncompressedPoint)
        try:
            key_id = _b64d(key_id_b64)
        except Exception:  # noqa: BLE001
            raise AppAttestError("bad key_id encoding") from None
        if not hmac.compare_digest(_sha256(x963), key_id):
            raise AppAttestError("public key hash != key_id")

        # (6-9) authenticator data
        rp_id_hash, _flags, counter, aaguid, cred_id = _parse_attested_authdata(bytes(auth_data))
        if not hmac.compare_digest(rp_id_hash, self.rp_id_hash):
            raise AppAttestError("rpIdHash mismatch")
        if counter != 0:
            raise AppAttestError("attestation counter != 0")
        env = _AAGUID_ENV.get(aaguid)
        if env is None:
            raise AppAttestError("unknown AAGUID")
        if env not in self.allowed_environments:
            raise AppAttestError(f"environment {env} not allowed")
        if not hmac.compare_digest(cred_id, key_id):
            raise AppAttestError("credentialId != key_id")

        receipt = att.get("receipt") or b""
        return AttestedKey(key_id_b64=key_id_b64, public_key_x963=x963, counter=counter,
                           environment=env, receipt=bytes(receipt))

    def verify_assertion(self, *, assertion: bytes, client_data_hash: bytes,
                         public_key_x963: bytes, prev_counter: int) -> int:
        try:
            obj = cbor2.loads(assertion)
        except Exception as exc:  # noqa: BLE001
            raise AppAttestError(f"assertion not CBOR: {exc}") from None
        if not isinstance(obj, dict):
            raise AppAttestError("bad assertion structure")
        sig = obj.get("signature")
        auth_data = obj.get("authenticatorData")
        if not isinstance(sig, (bytes, bytearray)) or not isinstance(auth_data, (bytes, bytearray)):
            raise AppAttestError("assertion missing fields")
        rp_id_hash, _flags, counter = _parse_assertion_authdata(bytes(auth_data))
        if not hmac.compare_digest(rp_id_hash, self.rp_id_hash):
            raise AppAttestError("assertion rpIdHash mismatch")
        try:
            pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key_x963)
        except Exception:  # noqa: BLE001
            raise AppAttestError("bad stored app-attest public key") from None
        # Apple signs the NONCE, i.e. SHA256(authenticatorData || clientDataHash) — not the
        # raw concatenation (that is the WebAuthn convention and does NOT verify against a
        # real DCAppAttestService assertion).
        nonce = _sha256(bytes(auth_data), client_data_hash)
        try:
            pub.verify(bytes(sig), nonce, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise AppAttestError("assertion signature invalid") from None
        if not counter > prev_counter:
            raise AppAttestError("assertion counter not strictly increasing")
        return counter


class FakeAppAttestVerifier(AppAttestVerifier):
    """TEST-ONLY. Skips the Apple certificate chain (which the real verifier's own tests
    cover) but STILL binds to client_data_hash: the attestation carries the app-attest
    public key; assertions are real ECDSA-P256/SHA-256 signatures over client_data_hash
    with a strictly-increasing counter. NEVER wired into production — the runner pins
    AppleAppAttestVerifier, and a startup assert forbids this class in production."""

    is_fake = True

    def __init__(self, *, environment: str = ENV_DEVELOPMENT) -> None:
        self.environment = environment
        # P1A.1/F2: same policy semantics as the real verifier, so a control plane built
        # on the fake inherits a REAL policy instead of "no policy" (which is now deny-all).
        self.allowed_environments = {environment}

    def verify_attestation(self, *, key_id_b64: str, attestation: bytes,
                           client_data_hash: bytes) -> AttestedKey:
        obj = cbor2.loads(attestation)
        if obj.get("fail"):
            raise AppAttestError("fake: forced attestation failure")
        if len(client_data_hash) != 32:
            raise AppAttestError("fake: bad clientDataHash")
        return AttestedKey(key_id_b64=key_id_b64, public_key_x963=bytes(obj["app_attest_x963"]),
                           counter=0, environment=self.environment, receipt=b"fake")

    def verify_assertion(self, *, assertion: bytes, client_data_hash: bytes,
                         public_key_x963: bytes, prev_counter: int) -> int:
        obj = cbor2.loads(assertion)
        sig = bytes(obj["signature"])
        counter = int(obj["counter"])
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key_x963)
        try:
            pub.verify(sig, client_data_hash, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise AppAttestError("fake: assertion signature invalid") from None
        if not counter > prev_counter:
            raise AppAttestError("fake: counter not strictly increasing")
        return counter


def fake_attestation(app_attest_x963: bytes, *, fail: bool = False) -> bytes:
    """Build a TEST attestation blob for FakeAppAttestVerifier."""
    return cbor2.dumps({"app_attest_x963": app_attest_x963, "fail": fail})


def fake_assertion(app_attest_key, client_data_hash: bytes, counter: int) -> bytes:
    """Sign a TEST assertion (ECDSA-P256/SHA-256 over client_data_hash) for the fake."""
    sig = app_attest_key.sign(client_data_hash, ec.ECDSA(hashes.SHA256()))
    return cbor2.dumps({"signature": sig, "counter": counter})


APP_ATTEST_KEY_ID_BYTES = 32   # Apple key id == SHA256(public key)


class AppAttestIdentityError(ValueError):
    """The supplied App Attest key identifier is not a usable security identity."""


def canonical_app_attest_key_id(value) -> str:
    """P1A.4/C1: the security identity of an App Attest key, derived from its BYTES.

    Base64 is not injective over the unused trailing bits: a 32-byte key id has FOUR
    spellings that all decode to the same bytes. The revocation store compared the TEXT
    while the verifier compared the decoded bytes, so a revoked key re-enrolled under an
    alternate spelling and regained authority — a real revocation bypass, reproduced.

    Identity is therefore: strict decode -> exact length check -> canonical re-encode.
    Equivalent encodings collapse to one identity; anything malformed fails closed.
    """
    if not isinstance(value, str):
        raise AppAttestIdentityError("app_attest_key_id must be a string")
    text = value.strip()
    if not text or text != value.strip("\x00"):
        raise AppAttestIdentityError("empty app_attest_key_id")
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except Exception:  # noqa: BLE001
        raise AppAttestIdentityError("app_attest_key_id is not valid base64") from None
    if len(raw) != APP_ATTEST_KEY_ID_BYTES:
        raise AppAttestIdentityError(
            f"app_attest_key_id must decode to {APP_ATTEST_KEY_ID_BYTES} bytes, "
            f"got {len(raw)}")
    return base64.b64encode(raw).decode("ascii")


def app_attest_identity_from_public_key(public_key_x963: bytes) -> str:
    """P1A.7/§3: the App Attest security identity DERIVED from the key that was verified.

    Apple defines the key id as base64(SHA256(public key)). Deriving it here means the
    identity a device is recorded under can never be a value someone merely claimed — it is
    a function of the key the attestation actually proved.
    """
    return canonical_app_attest_key_id(
        base64.b64encode(hashlib.sha256(bytes(public_key_x963)).digest()).decode("ascii"))


def _b64d(s: str) -> bytes:
    import base64
    return base64.b64decode(s.encode("ascii"), validate=True)
