"""SOLVIO Memory-Contract - Referenz-Typen (STEP 19B.3 / gehaertet 19B.3A, DESIGN-ONLY).

Framework-neutrale Schnittstelle fuer SOLVIOs Gedaechtnis.
Siehe docs/architecture/MEMORY_CONTRACT.md.

STAND M2: Diese Typen werden vom produktiven Core IMPORTIERT — solvio/memory/ und
seit M2 auch die Sprachlaufzeit bauen darauf auf. Der frueher hier stehende Vermerk
'DESIGN-ONLY, wird nicht importiert' ist damit ueberholt. Die Typen selbst sind
unveraendert. Legt die
Messlatte fuer den OpenClaw-Benchmark (STEP 19C) und fuer eine spaetere native
Implementierung in solvio/memory/ fest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from solvio.contracts.trust import SourceType, TrustLevel


class MemoryType(str, Enum):
    """Neun Kategorien (MEMORY_CONTRACT.md §3). Bewusst nicht mehr."""
    WORKING = "working"                    # Kurzzeit-Kontext des Gespraechs
    USER = "user"                          # stabile Fakten ueber den Nutzer
    PEOPLE = "people"                      # Fakten ueber andere Personen
    EPISODIC = "episodic"                  # was wann passiert ist
    SEMANTIC = "semantic"                  # gelerntes Allgemeinwissen
    PREFERENCE = "preference"              # Vorlieben/Gewohnheiten (supersession-typisch)
    RULE = "rule"                          # dauerhafte Nutzerregeln/Policies
    PROJECT = "project"                    # Wissen zu laufenden Vorhaben
    STANDING_INTENT = "standing_intent"    # dauerhafte Absicht mit Trigger (Deklaration)


class Sensitivity(str, Enum):
    """Schutzbedarf des INHALTS - orthogonal zu trust_level (MEMORY_CONTRACT.md §5.1).

    trust_level  = Vertrauen in die HERKUNFT (wer sagt es, darf es autorisieren?)
    sensitivity  = Schutzbedarf des INHALTS (wie geheim, wohin darf es fliessen?)
    Die beiden Achsen duerfen nicht vermischt werden.
    """
    PUBLIC = "public"                        # unkritisch
    PERSONAL = "personal"                    # personenbezogen, alltaeglich
    SENSITIVE = "sensitive"                  # besonders schuetzenswert (Gesundheit/Finanzen/...)
    SECRET_REFERENCE = "secret_reference"    # verweist NUR auf ein extern verwaltetes Secret


@dataclass
class RetentionPolicy:
    """Schlanke Aufbewahrungs-/Loeschregel (MEMORY_CONTRACT.md §5.2)."""
    mode: str = "default"           # default | audit_only | ttl
    ttl_days: int | None = None     # nur bei mode=ttl
    purge_on_expiry: bool = False   # bei Ablauf hart purgen statt nur forgetten


@dataclass
class ProvenanceEntry:
    """Ein Glied der Ableitungskette eines Records."""
    source_type: SourceType
    source: str
    trust_level: TrustLevel
    at: datetime
    note: str = ""


@dataclass
class Relation:
    """Typisierte Kante zu einem anderen Record."""
    kind: str          # about | caused_by | contradicts | refines
    target_id: str


@dataclass
class Tombstone:
    """Inhaltsloser Nachweis eines Purge (MEMORY_CONTRACT.md §7.1).

    Enthaelt KEINE personenbezogenen Daten - nur einen Einweg-Hash von id/subject,
    damit 'wurde gepurged?' geprueft werden kann, ohne die Daten erneut offenzulegen.
    Ein purge-aware Restore MUSS getombstonete Records ueberspringen.
    """
    subject_hash: str
    purged_at: datetime
    reason: str


@dataclass
class MemoryRecord:
    """Neutraler, serialisierbarer Gedaechtnis-Eintrag (MEMORY_CONTRACT.md §5)."""
    id: str
    memory_type: MemoryType
    content: str
    subject: str
    source: str
    source_type: SourceType
    created_at: datetime
    updated_at: datetime
    trust_level: TrustLevel                       # Vertrauen in die HERKUNFT
    sensitivity: Sensitivity = Sensitivity.PERSONAL  # Schutzbedarf des INHALTS
    retention_policy: RetentionPolicy = field(default_factory=RetentionPolicy)
    confidence: float = 1.0
    importance: float = 0.5
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    provenance: list[ProvenanceEntry] = field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    tags: list[str] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_current(self) -> bool:
        """True, wenn der Record nicht supersediert ist.

        Die zeitliche Gueltigkeit (valid_from/valid_until) prueft der Store
        zusaetzlich gegen 'jetzt'; dieses Flag deckt nur die Supersession ab.
        """
        return self.superseded_by is None


@runtime_checkable
class MemoryStore(Protocol):
    """Framework-neutrale Gedaechtnis-Schnittstelle. Alle Operationen async.

    Ein Backend (OpenClaw-Adapter, native Implementierung, Letta-Adapter) gilt
    als konform, wenn es diese Methoden mit der in MEMORY_CONTRACT.md
    beschriebenen Semantik erfuellt (insb. aktuelle Wahrheit != Historie,
    verlustfreie Provenance, forget/purge mit Tombstone- und purge-aware-Restore).
    """

    async def remember(self, record: MemoryRecord) -> str:
        """Neue Erinnerung speichern. Gibt die vergebene id zurueck."""
        ...

    async def get(self, id: str) -> MemoryRecord | None:
        """Exakter Abruf per id, ohne Interpretation."""
        ...

    async def recall(self, query: str, *, memory_types: list[MemoryType] | None = None,
                     subject: str | None = None, limit: int = 10) -> list[MemoryRecord]:
        """Antwort-Pfad: NUR aktuelle, nicht-vergessene Wahrheit. Nichts -> [] (nie raten)."""
        ...

    async def search(self, query: str, *, include_superseded: bool = False,
                     limit: int = 20) -> list[MemoryRecord]:
        """Breite, gerankte Suche. Optional inkl. Historie. NIE gepurgte Records."""
        ...

    async def update(self, id: str, changes: dict[str, Any]) -> MemoryRecord:
        """Denselben Fakt am selben Record aendern (kein neuer Record)."""
        ...

    async def supersede(self, old_id: str, new_record: MemoryRecord) -> MemoryRecord:
        """Fakt aendert sich: neuer Record wird aktuell, alter bleibt historisch."""
        ...

    async def forget(self, id: str, *, reason: str) -> bool:
        """Aus aktivem Recall entfernen/deaktivieren. Audit-/Historie darf gemaess
        retention_policy bestehen bleiben. Reversibel (Soft)."""
        ...

    async def purge(self, id: str, *, reason: str) -> bool:
        """Endgueltige personenbezogene Loeschung aus dem AKTIVEN Backend inkl. der
        KONTROLLIERBAREN semantischen/Vektor-Indizes und abgeleiteten Retrieval-
        Repraesentationen. Verhindert Wiederverwendung durch normalen recall/consolidate.
        Hinterlaesst einen inhaltslosen Tombstone. Irreversibel.

        Managed Restore MUSS Tombstones beachten und darf gepurgte Daten nicht
        reaktivieren. Hinweis: active purge != garantierte physische Loeschung aus jedem
        historischen/Offline-Backup (diese unterliegen ihrer eigenen Retention).
        Siehe MEMORY_CONTRACT.md §7.1."""
        ...

    async def history(self, subject: str) -> list[MemoryRecord]:
        """Vollstaendige Zeitreihe zu einem Subjekt, chronologisch (inkl. supersediert/
        vergessen). Gepurgte erscheinen NICHT (nur als inhaltsloser Tombstone)."""
        ...

    async def consolidate(self, scope: str | None = None) -> dict[str, Any]:
        """'Dreaming': zusammenfuehren ohne Herkunftsverlust; umkehrbar; respektiert Tombstones."""
        ...

    async def list_related(self, id: str) -> list[MemoryRecord]:
        """Verknuepfte Records (folgt relations-Kanten). Keine gepurgten."""
        ...

    async def get_provenance(self, id: str) -> list[ProvenanceEntry]:
        """Vollstaendige Herkunftskette. Basis fuer den Trust-Audit."""
        ...

    async def list_tombstones(self) -> list[Tombstone]:
        """Purge-Registry: inhaltslose Tombstones. Ein Managed Restore MUSS diese
        konsultieren und getombstonete Records NICHT reaktivieren."""
        ...

    async def reinforce(self, id: str, entry: ProvenanceEntry) -> bool:
        """Eine weitere Beobachtung an einen abgeleiteten Record anhaengen.

        APPEND-ONLY. Verlaengert die Provenienzkette und hebt `updated_at`;
        aendert weder Inhalt noch Herkunftsklasse noch Vertrauensstufe.

        Warum es diese Operation gibt: wiederholte Beobachtung desselben
        Sachverhalts darf keinen zweiten Record erzeugen. `update()` laesst die
        Provenienz per Feld-Whitelist bewusst nicht zu, und `supersede()` fuer
        blosse Verstaerkung wuerde die Historie fluten.

        NUR gegen `solvio_inference`. Einen `user_direct`-Record um eine
        Maschinenbeobachtung zu ergaenzen hiesse, seine Herkunft zu
        verwaessern — und an der Herkunft haengt die Autoritaetsachse. Eine
        Implementierung MUSS in diesem Fall werfen, nicht still nichts tun.
        """
        ...
