"""Client identity for the job control plane, hardened at FINALIZE).

Job ownership is a CA-signed LOGICAL client identity carried in a single URI SAN in
the ``spiffe://`` namespace — a **SPIFFE-form URI identity** (``spiffe://solvio/client/
<name>``). This is a CA-signed logical client identity in a URI SAN; it is NOT a full
SPIFFE/SPIRE deployment and not a verified X.509-SVID. The security property is: a
CA-signed logical identity carried in the URI SAN, decoupled from the certificate.

Contract (fail-closed):
  * a client presents EXACTLY ONE URI SAN total (any extra URI SAN -> reject),
  * scheme ``spiffe``, trust domain ``solvio``, path ``/client/<name>`` with exactly
    one non-empty name segment, no query, no fragment,
  * ownership NEVER falls back to the certificate CN or a fingerprint.

Consequences: a rotated certificate with the same URI identity keeps access; a cert
with the same CN but a different (or missing) URI SAN is a different/unauthenticated
client. mTLS still gates the connection; this only decides *which* logical client
owns a job.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from aiohttp import web

TRUST_DOMAIN = "solvio"
_PATH_PREFIX = "/client/"
SAN_PREFIX = "spiffe://solvio/client/"   # canonical identity prefix


def extract_client_identity(cert: dict | None) -> str | None:
    """Return the SOLVIO client identity (canonical SPIFFE-form URI) or None to reject."""
    if not cert:
        return None
    uris = [value for (kind, value) in (cert.get("subjectAltName") or ())
            if kind == "URI"]
    if len(uris) != 1:                       # exactly one URI SAN total (fail-closed)
        return None
    uri = uris[0]
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme != "spiffe":
        return None
    if parts.netloc != TRUST_DOMAIN:         # trust domain
        return None
    if parts.query or parts.fragment:
        return None
    if not parts.path.startswith(_PATH_PREFIX):
        return None
    name = parts.path[len(_PATH_PREFIX):]
    if not name or "/" in name or name != name.strip():
        return None
    identity = SAN_PREFIX + name
    if identity != uri:                      # reject any normalization/encoding ambiguity
        return None
    return identity


def client_identity(req: web.Request) -> str | None:
    """Extract the calling client's identity from its verified certificate.

    mTLS is enforced by the SSL context, so a request that reaches here already
    presented a valid SOLVIO client certificate. Ownership still requires exactly one
    valid SOLVIO URI SAN (no CN fallback).
    """
    ssl_obj = req.transport.get_extra_info("ssl_object") if req.transport else None
    if ssl_obj is None:
        return None
    return extract_client_identity(ssl_obj.getpeercert())
