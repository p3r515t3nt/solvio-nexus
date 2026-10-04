"""Wie ein Lauf den Menschen erreicht — ueber den vorhandenen Posteingang.

Es gibt bewusst **keinen zweiten Posteingang**. Ein Lauf schreibt in denselben
`ProactiveStore`, den die Hintergrundlaeufe benutzen, nach denselben zwei
dokumentierten Regeln:

* **`task_id=""`.** Die Eindeutigkeit im Store ist `UNIQUE(task_id,
  fingerprint)`. Wer eine echte Aufgaben-Kennung einsetzt, bekommt je Aufgabe
  einen eigenen Namensraum — und damit dieselbe Meldung so oft, wie es Aufgaben
  gibt. Die leere Kennung ist die Entscheidung, dass Agentenmeldungen sich
  untereinander deduplizieren.
* **Das Ruhefenster steht IM Fingerprint.** Nicht daneben. Ein Fingerprint ohne
  Zeitanteil meldet dieselbe Sache genau einmal und danach nie wieder — auch
  nicht in einer Woche, wenn sie wieder gilt. Einer mit zu feinem Zeitanteil
  meldet sie jede Minute.

Und es gibt kein Push (DEBT-0029). Das wird gesagt, nicht ueberspielt: eine
Meldung liegt da, bis der Mensch das naechste Mal hinsieht.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from solvio.agent_runtime.specialists import redact_specialist_output
from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

#: Wie lange dieselbe Meldung als „schon gesagt" gilt. Sechs Stunden, wie beim
#: Arzt — lang genug gegen Wiederholung, kurz genug, dass eine erneut geltende
#: Lage wieder durchkommt.
QUIET_SECONDS = 6 * 3600.0

#: Der Rang jeder Agentenmeldung. Ein Lauf liefert Arbeit, nie Autoritaet.
CONTENT_TRUST = "untrusted_executor"

MAX_SUMMARY = 1_200


@dataclass(frozen=True)
class Notice:
    run_id: str
    kind: str
    summary: str
    priority: str = "normal"
    findings: tuple[str, ...] = ()


def fingerprint(run_id: str, kind: str, summary: str, *, now: float = 0.0) -> str:
    """Der Fingerabdruck — mit dem Ruhefenster darin.

    Das Fenster ist ein ganzzahliger Bucket: dieselbe Meldung desselben Laufs
    innerhalb von sechs Stunden traegt denselben Abdruck und faellt am
    `UNIQUE`-Index des Stores ab. Danach ist sie wieder sagbar.
    """
    moment = now or time.time()
    window = int(moment // QUIET_SECONDS)
    seed = f"agent|{run_id}|{kind}|{summary.strip()[:200]}|{window}".encode("utf-8")
    return "an-" + hashlib.sha256(seed).hexdigest()[:20]


def build_item(notice: Notice, *, now: float = 0.0) -> dict:
    """Baut die Zeile fuer den Posteingang. Redigiert, gedeckelt, gekennzeichnet.

    Die Redaktion laeuft HIER und nicht erst im Store: der Store verweigert
    Geheimnisgestalt (das ist richtig), aber eine verweigerte Meldung ist eine
    verlorene Meldung. Was ein Spezialist zurueckgab, wird deshalb vorher durch
    dieselbe Redaktion gezogen, die auch ins Ledger fuehrt — und was DANN noch
    nach Geheimnis aussieht, soll auch wirklich scheitern.
    """
    moment = now or time.time()
    # State precedes prose and survives the inbox length cap. A long image
    # prompt is useful partial work, never the completion of an image task.
    prefix = "Auftrag fehlgeschlagen. " if notice.kind == "failed" else ""
    summary = (prefix + redact_specialist_output(notice.summary))[:MAX_SUMMARY]
    findings = [redact_specialist_output(str(f))[:400] for f in notice.findings][:8]
    return {
        "notification_id": fingerprint(notice.run_id, notice.kind, summary, now=moment),
        # Leer, und zwar absichtlich — siehe Modul-Docstring.
        "task_id": "",
        "run_id": notice.run_id,
        "created_at": moment,
        "priority": notice.priority,
        "summary": summary,
        "findings": findings,
        "source_capability": "agent_runtime",
        "content_trust": CONTENT_TRUST,
        "fingerprint": fingerprint(notice.run_id, notice.kind, summary, now=moment),
    }


async def send(store, notice: Notice, *, now: float = 0.0) -> bool:
    """Legt die Meldung ab. `False` heisst „dasselbe lag schon da".

    Scheitert der Store an seiner Geheimnis-Firewall, ist das KEIN Grund, den
    Lauf abzubrechen — aber auch keiner, es zu verschweigen: es wird protokolliert
    und als „nicht gemeldet" zurueckgegeben.
    """
    if store is None:
        return False
    item = build_item(notice, now=now)
    try:
        return bool(await store.add_item(item))
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_runtime.notice_refused", run_id=notice.run_id,
                    kind=type(exc).__name__)
        return False
