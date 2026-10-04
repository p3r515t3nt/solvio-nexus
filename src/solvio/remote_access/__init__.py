"""SOLVIO Remote Access — die Naht, hinter der ein Transport austauschbar ist.

Remote Access V1. Eingehaengt ist genau eine Stelle: die Gesundheitssonde
`remote_access` (`solvio.control_center.probes`) misst, ob der Fernweg steht,
damit SOLVIO „ich bin von unterwegs gerade nicht erreichbar" SAGEN kann,
statt es einen Zeitablauf werden zu lassen. Der Entwurf steht in
`docs/design/remote-access-v1/`.

Das Paket enthaelt bewusst KEINEN Tunnel, KEIN VPN und KEINE Kryptographie.
Es beobachtet, welcher Weg zum Core gerade steht, und haelt fest, dass ein Weg
niemals eine Befugnis ist.
"""
from solvio.remote_access.contract import (
    ConnectionState,
    PathKind,
    ProbeResult,
    RemoteAccessReport,
    RemoteEndpoint,
    RemoteTransport,
)
from solvio.remote_access.resolver import LocalFirstResolver, order_endpoints

__all__ = [
    "ConnectionState",
    "LocalFirstResolver",
    "PathKind",
    "ProbeResult",
    "RemoteAccessReport",
    "RemoteEndpoint",
    "RemoteTransport",
    "order_endpoints",
]
