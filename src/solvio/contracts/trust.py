"""SOLVIO Trust-Boundary - Referenz-Typen (STEP 19B.3, DESIGN-ONLY).

Framework-neutrale Typen zur Herkunfts-/Vertrauensklassifizierung.
Siehe docs/architecture/TRUST_BOUNDARY.md.

STAND M2: SourceType und TrustLevel werden vom produktiven Core IMPORTIERT (M2
schreibt jede Erinnerung mit TrustLevel.USER_DIRECT). Der frueher hier stehende
Vermerk 'DESIGN-ONLY, wird nicht importiert' ist ueberholt. Die Typen dienen weiter als
verbindliche Schnittstelle fuer eine spaetere Implementierung und als Messlatte
fuer den OpenClaw-Benchmark (STEP 19C).

Kernregel: Unvertrauter Inhalt kann Information liefern, aber NIE Autoritaet.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SourceType(str, Enum):
    """Unmittelbare Herkunft eines Inhalts/Fakts."""
    USER_DIRECT = "user_direct"                # Gregor hat es direkt gesagt (Voice)
    HOME_ASSISTANT = "home_assistant"          # Messwert/Zustand aus HA
    CODEX_RESULT = "codex_result"              # Ausgabe des Codex-Agenten
    SOLVIO_INFERENCE = "solvio_inference"      # von SOLVIO selbst abgeleitet
    SYSTEM_OBSERVATION = "system_observation"  # eigene Laufzeitbeobachtung
    GMAIL_MESSAGE = "gmail_message"            # Inhalt einer E-Mail
    WEB_PAGE = "web_page"                      # Inhalt einer Webseite


class TrustLevel(str, Enum):
    """Herkunftsklasse. Entscheidend ist die Autoritaet (s. bears_authority)."""
    SYSTEM_TRUSTED = "system_trusted"
    USER_DIRECT = "user_direct"
    LOCAL_TRUSTED_TOOL = "local_trusted_tool"
    MEMORY_CURATED = "memory_curated"
    EXTERNAL_TOOL_RESULT = "external_tool_result"
    UNTRUSTED_EMAIL = "untrusted_email"
    UNTRUSTED_WEB = "untrusted_web"
    UNTRUSTED_DOCUMENT = "untrusted_document"
    UNTRUSTED_MESSAGE = "untrusted_message"
    AGENT_GENERATED = "agent_generated"


# Nur diese Klassen koennen eine privilegierte Aktion autorisieren.
AUTHORITY_BEARING: frozenset[TrustLevel] = frozenset({
    TrustLevel.SYSTEM_TRUSTED,
    TrustLevel.USER_DIRECT,
})

# Fremdverfasste Inhalte: koennen informieren, nie autorisieren.
UNTRUSTED: frozenset[TrustLevel] = frozenset({
    TrustLevel.UNTRUSTED_EMAIL,
    TrustLevel.UNTRUSTED_WEB,
    TrustLevel.UNTRUSTED_DOCUMENT,
    TrustLevel.UNTRUSTED_MESSAGE,
})

# Default-Mapping Herkunft -> Vertrauensklasse (TRUST_BOUNDARY.md §2).
SOURCE_TRUST_MAP: dict[SourceType, TrustLevel] = {
    SourceType.USER_DIRECT: TrustLevel.USER_DIRECT,
    SourceType.HOME_ASSISTANT: TrustLevel.LOCAL_TRUSTED_TOOL,
    SourceType.CODEX_RESULT: TrustLevel.AGENT_GENERATED,
    SourceType.SOLVIO_INFERENCE: TrustLevel.AGENT_GENERATED,
    SourceType.SYSTEM_OBSERVATION: TrustLevel.SYSTEM_TRUSTED,
    SourceType.GMAIL_MESSAGE: TrustLevel.UNTRUSTED_EMAIL,
    SourceType.WEB_PAGE: TrustLevel.UNTRUSTED_WEB,
}


def trust_for_source(source_type: SourceType) -> TrustLevel:
    """Vertrauensklasse fuer eine Herkunft. Default konservativ: AGENT_GENERATED."""
    return SOURCE_TRUST_MAP.get(source_type, TrustLevel.AGENT_GENERATED)


def bears_authority(level: TrustLevel) -> bool:
    """True nur, wenn diese Klasse eine privilegierte Aktion legitimieren darf."""
    return level in AUTHORITY_BEARING


def is_untrusted(level: TrustLevel) -> bool:
    """True fuer fremdverfasste Inhalte (E-Mail/Web/Dokument/Nachricht)."""
    return level in UNTRUSTED


@dataclass
class TrustContext:
    """Wird an jedem Tool-Call und jedem DeepTask mitgefuehrt.

    origin_trust    - Vertrauensklasse der dominanten Eingabe des Turns.
    user_authorized - True nur, wenn ein echter USER_DIRECT-Akt vorliegt
                      (z. B. eine per Voice bestaetigte Pending Action).
    """
    origin_trust: TrustLevel
    user_authorized: bool = False
    note: str = ""

    def may_authorize(self) -> bool:
        """Darf dieser Kontext eine privilegierte Aktion legitimieren?"""
        return self.user_authorized and bears_authority(self.origin_trust)
