"""Abbildung Node-Capability -> Content-Trust-Klasse (PHASE 12).

SEHR WICHTIG: Transport-Trust != Content-Trust.

Ein gueltig per mTLS authentifizierter Knoten beweist nur die HERKUNFT
("das Ergebnis kam von diesem kontrollierten Knoten"), niemals die
VERTRAUENSWUERDIGKEIT DES INHALTS. Der Core bleibt alleinige Autoritaet fuer
die Trust-Klassifikation.

Dieses Modul importiert die bestehenden Trust-Contracts NUR LESEND
(solvio.contracts.trust) und aendert sie nicht. Es liefert die konservative
Default-Content-Klasse fuer das RESULT einer Capability.

Harte Invariante: Ein Node-Result kann NIEMALS autoritaetstragend sein
(nie in AUTHORITY_BEARING). Ein Knoten kann seine eigene Autorisierung nicht
erhoehen.
"""
from __future__ import annotations

from solvio.contracts.trust import AUTHORITY_BEARING, TrustLevel

# Exakte Capability-IDs -> Content-Trust des Ergebnisses.
_EXACT: dict[str, TrustLevel] = {
    # Kontrollierte Infrastruktur-Telemetrie (kein Fremdinhalt).
    "system.health": TrustLevel.EXTERNAL_TOOL_RESULT,
    # Deterministisches, lokal nachrechenbares Rechenergebnis.
    "compute.sha256": TrustLevel.EXTERNAL_TOOL_RESULT,
}

# Praefix-Regeln fuer zukuenftige Capability-Familien (nur Doku/Architektur
# in diesem Step — hier bereits als Messlatte hinterlegt).
_PREFIX: tuple[tuple[str, TrustLevel], ...] = (
    # Web-Inhalt bleibt UNTRUSTED, auch wenn der Transport ueber unseren
    # eigenen Hetzner-Knoten laeuft.
    ("research.", TrustLevel.UNTRUSTED_WEB),
    ("web.", TrustLevel.UNTRUSTED_WEB),
    # Agenten-Ausgaben sind maschinell erzeugt.
    ("agent.", TrustLevel.AGENT_GENERATED),
    # Abgeleitete Rechenergebnisse (z. B. Embeddings).
    ("embedding.", TrustLevel.EXTERNAL_TOOL_RESULT),
    ("compute.", TrustLevel.EXTERNAL_TOOL_RESULT),
)

# Unbekannte Capability -> konservativ als maschinell erzeugt behandeln.
_DEFAULT: TrustLevel = TrustLevel.AGENT_GENERATED


def node_result_trust(capability_id: str) -> TrustLevel:
    """Konservative Content-Trust-Klasse fuer das Ergebnis einer Capability.

    Das Ergebnis ist eine EMPFEHLUNG an den Core; die endgueltige
    Klassifikation trifft der Core. Garantiert nie autoritaetstragend.
    """
    level = _EXACT.get(capability_id)
    if level is None:
        level = _DEFAULT
        for prefix, lvl in _PREFIX:
            if capability_id.startswith(prefix):
                level = lvl
                break
    # Harte Sicherheitsgrenze (PHASE 12/22).
    if level in AUTHORITY_BEARING:  # pragma: no cover - darf nie eintreten
        raise AssertionError("node result trust must never be authority-bearing")
    return level


def result_bears_authority(capability_id: str) -> bool:
    """Immer False: Node-Results legitimieren niemals privilegierte Aktionen."""
    return False
