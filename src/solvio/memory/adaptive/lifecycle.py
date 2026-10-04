"""Der Lebenszyklus eines Records — ABGELEITET, nie gespeichert.

Es gibt keine Statusspalte. Das ist die tragende Entscheidung des Entwurfs, und
dieses Modul ist ihr Beweis: der ganze diskutierte Lebenszyklus faellt aus
Feldern, die der eingefrorene Contract laengst hat.

    EXPLICIT   source_type=user_direct + metadata.explicit_intent
    CONFIRMED  source_type=user_direct + `confirmed:`-Eintrag in der Provenienz
    LEARNED    source_type=solvio_inference

Warum nicht als Spalte: ein Feld, in dem `confirmed` steht, ist ein Feld, in das
ein fehlerhafter oder angegriffener Pfad `confirmed` schreiben kann. Hier ist
„bestaetigt" kein Wert, sondern die Spur eines Ereignisses — man kann sie nicht
setzen, man kann sie nur herbeifuehren.

Die Ableitung liegt im CORE, nicht im Client. Das ist die Lehre aus der ersten
Obsidian-Ausbaustufe: Sichtbarkeitslogik, die ein Client nachbaut, baut er
falsch nach.
"""
from __future__ import annotations

from typing import Any

from solvio.contracts.memory import MemoryRecord
from solvio.contracts.trust import SourceType

EXPLICIT = "explicit"
CONFIRMED = "confirmed"
LEARNED = "learned"
RECORDED = "recorded"      # user_direct ohne Merk-Mandat (Altbestand, Import)

#: Praefixe der Evidenzarten in `ProvenanceEntry.note`.
STATED = "stated:"
OBSERVED = "observed:"
CONFIRMED_NOTE = "confirmed:"
CORRECTED_NOTE = "corrected:"
CONTRADICTED_NOTE = "contradicted:"


def lifecycle_of(record: MemoryRecord) -> str:
    """Welchen Rang diese Erinnerung hat. Ohne Rateanteil."""
    if record.source_type is SourceType.SOLVIO_INFERENCE:
        return LEARNED
    if record.source_type is SourceType.USER_DIRECT:
        metadata = record.metadata or {}
        if metadata.get("explicit_intent"):
            return EXPLICIT
        for entry in record.provenance or []:
            if (entry.note or "").startswith(CONFIRMED_NOTE):
                return CONFIRMED
        return RECORDED
    return RECORDED


def is_machine_learned(record: MemoryRecord) -> bool:
    """Darf die Automatik diesen Record anfassen?

    Die harte Invariante der Zustandsmaschine, als eine Funktion: Automatik
    erreicht ausschliesslich `solvio_inference` — in beide Richtungen, anlegen
    wie abloesen. Jeder automatische Schreibweg fragt hier, bevor er etwas tut.
    """
    return record.source_type is SourceType.SOLVIO_INFERENCE


def evidence_summary(record: MemoryRecord) -> dict[str, Any]:
    """Wie viele Beobachtungen, ueber welchen Zeitraum — ohne Inhalt.

    Genau das, was „Warum weisst du das?" beantwortet, und nicht mehr: Zahlen,
    Zeitpunkte und Arten. Der Wortlaut des Gesagten liegt nirgends.
    """
    chain = list(record.provenance or [])
    kinds: dict[str, int] = {}
    for entry in chain:
        note = entry.note or ""
        for prefix, name in ((STATED, "stated"), (OBSERVED, "observed"),
                             (CONFIRMED_NOTE, "confirmed"),
                             (CORRECTED_NOTE, "corrected"),
                             (CONTRADICTED_NOTE, "contradicted")):
            if note.startswith(prefix):
                kinds[name] = kinds.get(name, 0) + 1
                break
    times = sorted(e.at for e in chain if e.at is not None)
    return {"observations": len(chain), "kinds": kinds,
            "first_at": times[0].isoformat() if times else None,
            "last_at": times[-1].isoformat() if times else None}


#: Was SOLVIO sagen darf, wenn jemand fragt „woher weisst du das?".
#: Die Saetze kommen aus der Ableitung, nicht aus einer Modellformulierung —
#: eine erfundene Herkunftsauskunft waere ein Contract-Bruch.
SPOKEN = {
    EXPLICIT: "Das hast du mir ausdruecklich gesagt.",
    CONFIRMED: "Das hast du mir bestaetigt.",
    LEARNED: ("Das habe ich aus unseren Gespraechen abgeleitet — sag mir, "
              "wenn es nicht stimmt."),
    RECORDED: "Das stammt aus einem frueheren Gespraech mit dir.",
}


def explain(record: MemoryRecord) -> str:
    """Ein vorlesbarer Satz ueber die Herkunft. Nie ein erfundener."""
    stage = lifecycle_of(record)
    sentence = SPOKEN[stage]
    if stage == LEARNED:
        summary = evidence_summary(record)
        count = summary["observations"]
        if count > 1:
            sentence = (f"Das habe ich aus {count} Beobachtungen in unseren "
                        f"Gespraechen abgeleitet — sag mir, wenn es nicht stimmt.")
    return sentence
