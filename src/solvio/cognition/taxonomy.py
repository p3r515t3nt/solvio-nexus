"""Die Fehlschlaege des Routers — geschlossen, und jeder mit seinem Satz.

**Nichts faellt auf „Ich kann das nicht" zusammen.** Ein erschoepftes
Kontingent ist etwas anderes als ein nicht erreichbarer Anbieter, und beides
ist etwas anderes als eine Rueckfrage. Wer die drei gleich beantwortet, nimmt
dem Menschen die Information, die er braucht, um zu entscheiden, ob er gleich
noch einmal fragt oder morgen.

Was hier NICHT steht: alles, wofuer die freigegebene Maschinerie schon eine
Antwort hat. Ein `EXECUTOR_UNAVAILABLE` der beauftragten Faehigkeit behaelt
seinen Wortlaut und laeuft ueber Umschlag und Gap Resolver — der Router
uebermalt das nicht mit einer eigenen, aermeren Fassung.

**Eine erschoepfte Eskalationskappe ist ausdruecklich KEIN Fehlschlag.** Sie
faellt auf den Weg zurueck, den es ohne Eskalation gegeben haette. „Budget
niedrig" darf nie eine Antwort abziehen, die das System vorher gegeben haette.
"""
from __future__ import annotations

#: Die geschlossene Menge. Ein Wert ausserhalb ist ein Programmierfehler und
#: wird beim Schreiben ins Buch zurueckgewiesen.
FAILURE_KINDS: frozenset[str] = frozenset({
    "",
    "assessment_unavailable",
    "assessment_quota",
    "route_unavailable",
    "clarification_required",
    "not_routable",
    "loop_detected",
    "refused_policy",
})

#: Was der Mensch hoert. Haus-Transliteration (ue/oe/ae), weil es gesprochen
#: wird — dieselbe Schreibweise wie in `capabilities/router.py`.
WORDING: dict[str, str] = {
    "assessment_unavailable":
        "Ich kann das gerade nicht richtig einordnen — der Anbieter ist nicht "
        "erreichbar. Frag mich gleich noch mal oder sag mir direkt, was ich "
        "tun soll.",
    "assessment_quota":
        "Mein Denk-Kontingent ist fuer heute erschoepft; es erneuert sich von "
        "selbst.",
    "route_unavailable":
        "Dafuer ist gerade nichts erreichbar — es ist nichts passiert.",
    "not_routable":
        "Das kann ich nicht als Auftrag fassen.",
    "loop_detected":
        "Das haben wir gerade mehrfach versucht — ich hoere auf, bis sich "
        "etwas aendert.",
    "refused_policy":
        "Das mache ich auf diesem Weg nicht.",
}

#: Die Absagegruende des Brokers, die ein Kontingent bedeuten und keinen
#: Ausfall. Sie stehen so auf dem Draht (`provider_broker/ledger.py`), und sie
#: werden GELESEN statt geraten.
QUOTA_REASONS: frozenset[str] = frozenset({"token_capped", "rate_capped"})


def speak(kind: str) -> str:
    """Der eine ehrliche Satz zu einem Fehlschlag."""
    return WORDING.get(kind, "Das habe ich nicht ausgefuehrt.")


def quota_denied(reason: str) -> bool:
    """War das ein Kontingent — oder ein Ausfall?

    Der Unterschied ist fuer den Menschen der ganze Punkt: ein Kontingent
    erneuert sich von selbst, ein Ausfall nicht. Geraten wird hier nichts; der
    Broker sagt den Grund, und zwar aus einer geschlossenen Menge.
    """
    text = str(reason or "")
    return any(word in text for word in QUOTA_REASONS)
