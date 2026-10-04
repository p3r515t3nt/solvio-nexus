"""Fehlertypen des Core-Node-Layers (STEP 21.2E).

Framework-neutral. Alle Meldungen sind payload-frei: sie beschreiben die
Fehlerklasse, niemals den Inhalt eines Requests oder Results.

Trennlinie:
  * Transport-/Verfuegbarkeitsfehler  -> NodeUnavailableError-Familie
    (Netz weg, WireGuard down, TLS/Cert kaputt, Circuit offen). Der Router
    darf solche Knoten ueberspringen; der Core laeuft weiter (PHASE 16).
  * Protokoll-/Identitaetsfehler       -> NodeProtocolError-Familie
    (falsche protocol_version, fremde node_id, verletzte request_id,
    unlesbare Antwort). Das ist ein Vertrauensbruch, kein blosser Ausfall.
  * Capability-Fehler                  -> NodeCapabilityError-Familie
    (der Knoten hat sauber mit status=error geantwortet).
"""
from __future__ import annotations


class NodeError(Exception):
    """Basisklasse aller Node-Layer-Fehler."""


class NodeConfigError(NodeError):
    """Fehlkonfiguration (fehlende Cert-Pfade, ungueltiger Endpoint)."""


class NodeUnavailableError(NodeError):
    """Knoten ist nicht erreichbar (Netz, WireGuard, Verbindungsaufbau)."""


class NodeTLSError(NodeUnavailableError):
    """mTLS-/Zertifikatsproblem beim Verbindungsaufbau. Als Ausfall behandelt."""


class NodeTimeoutError(NodeUnavailableError):
    """Zeitueberschreitung. Wird wie ein Ausfall behandelt (Router darf skippen)."""


class CircuitOpenError(NodeUnavailableError):
    """Der Circuit Breaker fuer diesen Knoten ist offen (PHASE 14)."""


class NodeProtocolError(NodeError):
    """Antwort verletzt das Node-Protokoll (Version/Struktur)."""


class NodeIdentityError(NodeProtocolError):
    """Antwort-node_id != erwartete node_id. IP ist NICHT Identitaet (PHASE 27)."""


class NodeRequestMismatchError(NodeProtocolError):
    """request_id der Antwort passt nicht zum gesendeten Request."""


class NodeCapabilityError(NodeError):
    """Der Knoten hat die Capability sauber, aber mit status=error beantwortet."""

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class UnknownCapabilityError(NodeCapabilityError):
    """Der Knoten kennt die angeforderte Capability nicht."""


class NodeBusyError(NodeCapabilityError):
    """Der Knoten ist ausgelastet (RESOURCE_BUSY / Rate-Limit / Queue voll)."""


class PrivacyRoutingError(NodeError):
    """Das Routing wurde aus Datenschutzgruenden verweigert (PHASE 11).

    Ein Knoten wird NICHT allein deshalb gewaehlt, weil er online ist.
    """


class NoSuitableNodeError(NodeError):
    """Kein Knoten erfuellt Capability + Verfuegbarkeit + Privacy + Health."""
