"""mTLS contexts (defense-in-depth on top of WireGuard).

Server: requires a client certificate signed by the SOLVIO CA (CERT_REQUIRED).
Client: presents its cert and verifies the server against the same CA.

The TLS layer is the authentication boundary — a peer without a valid SOLVIO client
certificate cannot complete the handshake and never reaches any request handler.
Private keys are referenced by path and must have restrictive permissions; they are
never inlined in config or committed to git.
"""
from __future__ import annotations

import ssl

from solvio_node.config import TLSPaths


def server_ssl_context(tls: TLSPaths) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=tls.server_cert, keyfile=tls.server_key)
    ctx.load_verify_locations(cafile=tls.ca_cert)
    ctx.verify_mode = ssl.CERT_REQUIRED          # mTLS: client cert mandatory
    return ctx


def client_ssl_context(ca_cert: str, client_cert: str, client_key: str,
                       *, check_hostname: bool = False) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=ca_cert)    # verify server against SOLVIO CA
    ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)  # present client cert
    ctx.check_hostname = check_hostname
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
