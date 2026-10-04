"""Wann SOLVIO von sich aus etwas ueber Geld sagt — und wie selten.

Kein zweiter Posteingang: die Zahlung BERICHTET in den bestehenden proaktiven
Eingang, sie plant nichts. Dieselbe Trennung wie bei der Sicherung (ADR-0023).

Gemeldet wird genau das, was ein Mensch WISSEN MUSS und selbst nicht sieht:

* eine Zahlung mit unklarem Ausgang, die nachgeschlagen werden muss;
* eine Bank, die auf eine Bestaetigung wartet;
* ein Zahlungsmittel, das nicht mehr benutzbar ist.

Nicht gemeldet wird: eine gelungene Zahlung. Der Mensch hat sie gerade selbst
mit Face ID bestaetigt — ihm hinterher zu sagen, dass sie stattgefunden hat, ist
Laerm, kein Dienst.

**Zwei Fallen, beide gemessen, beide vom Arzt und von der Sicherung uebernommen:**
`task_id` ist der LEERSTRING und nicht `None` (SQLite haelt NULL in einer
UNIQUE-Bedingung fuer verschieden, die Entdopplung greift sonst nie), und das
Ruhefenster steckt IM Fingerabdruck, damit dieselbe Lage morgen wieder gemeldet
werden kann, heute aber nicht viermal.

**Was in einer Meldung NIE steht:** ein Betrag, ein Haendler, ein Verweis auf
ein Zahlungsmittel, eine Anbieterkennung. Nicht aus Vorsicht vor dem Eingang,
sondern weil er vorgelesen wird — und weil sein Zaun bei einem
zugangsdatenfoermigen Text VERWEIGERT statt zu schwaerzen. Eine Meldung, die den
Eingang zum Werfen bringt, ist keine Meldung. Es steht die Vorgangskennung da
und sonst nichts.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("payment")

#: Wie lange dieselbe Lage still bleibt. Sechs Stunden: oft genug, dass eine
#: unklare Zahlung nicht ueber Nacht liegen bleibt, selten genug, dass niemand
#: sie wegklickt.
QUIET_SECONDS = 6 * 3600


async def notify(summary: str, findings: list[str], *, kind: str,
                 now: float | None = None, store: Any = None) -> bool:
    """Legt eine Meldung in den bestehenden proaktiven Eingang."""
    now = now if now is not None else time.time()
    if store is None:
        from solvio.proactive.store import ProactiveStore
        store = ProactiveStore()
    window = int(now // QUIET_SECONDS)
    item = {
        "notification_id": "pm-" + hashlib.sha256(
            f"{kind}|{window}".encode("utf-8")).hexdigest()[:20],
        "task_id": "", "run_id": None, "created_at": now,
        "priority": "wichtig", "summary": summary[:1200], "findings": findings,
        "source_capability": "payment", "content_trust": "",
        "fingerprint": f"payment:{kind}:{window}",
    }
    return bool(await store.add_item(item))


#: Welcher Zustand welche Meldung ausloest. `healthy` fehlt mit Absicht.
_MESSAGES: dict[str, tuple[str, str]] = {
    "reconcile": (
        "Bei einer Zahlung ist offen, ob sie durchgegangen ist. Ich sehe nach, "
        "sobald du willst — noch einmal versuchen tue ich es nicht.",
        "unklarer Ausgang"),
    "sca": (
        "Eine Zahlung wartet auf deine Bank. Bitte bestaetige sie in deiner "
        "Bank-App; ich kann das nicht fuer dich.",
        "Bestaetigung der Bank fehlt"),
    "unusable": (
        "Ein Zahlungsmittel ist gerade nicht benutzbar. Bis dahin bezahle ich "
        "nichts.",
        "Zahlungsmittel nicht benutzbar"),
}


async def notify_from_state(*, kind: str, intent_ids: list[str],
                            now: float | None = None, store: Any = None) -> bool:
    """Eine Meldung aus einer bekannten Lage. Nennt Kennungen, nie Betraege."""
    message = _MESSAGES.get(kind)
    if message is None or not intent_ids:
        return False
    summary, label = message
    findings = [f"{label}: {pid}" for pid in sorted(intent_ids)[:5]]
    if len(intent_ids) > 5:
        findings.append(f"und {len(intent_ids) - 5} weitere")
    return await notify(summary, findings, kind=kind, now=now, store=store)


async def sweep(*, now: float | None = None, store: Any = None,
                payment_store: Any = None) -> list[str]:
    """Sieht nach, ob es etwas zu melden gibt. Aendert nichts.

    Ausdruecklich lesend: dieser Lauf klaert keine Zahlung auf und stoesst
    keine an. Er sagt einem Menschen, dass etwas auf ihn wartet.
    """
    from solvio.payment.intent import PaymentState
    if payment_store is None:
        import os

        from solvio.payment.store import PaymentStore, db_path
        if not os.path.exists(db_path()):
            return []
        payment_store = PaymentStore()
    sent: list[str] = []
    for kind, states in (("reconcile", (PaymentState.RECONCILIATION_REQUIRED,
                                       PaymentState.EXECUTING)),
                         ("sca", (PaymentState.AWAITING_SCA,))):
        try:
            open_intents = payment_store.intents(states=states, limit=50)
        except Exception as exc:  # noqa: BLE001 - eine Beobachtung stoert nie
            log.error("payment.sweep_failed", kind=type(exc).__name__)
            continue
        if not open_intents:
            continue
        if await notify_from_state(
                kind=kind, intent_ids=[i.payment_intent_id for i in open_intents],
                now=now, store=store):
            sent.append(kind)
    return sent
