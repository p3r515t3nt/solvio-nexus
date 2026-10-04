"""Wie es der Zahlungsschicht geht — ohne dafuer Geld zu bewegen.

Die Regel, die diese Datei klein haelt: **eine Gesundheitspruefung bucht
nichts.** Eine Testbelastung, die alle dreissig Sekunden liefe, waere keine
Pruefung, sondern ein Dauerauftrag. Deshalb steht hier keine einzige
Anbieteranfrage, die Geld bewegen koennte — und auch keine, die einen Zugang
ausleiht.

Was stattdessen geprueft wird, ist billig und trotzdem aussagekraeftig:

| Lage | Wort | warum |
|---|---|---|
| keine Zahlungsablage | `healthy` | nicht eingerichtet ist kein Defekt |
| Datei fuer andere lesbar | `degraded` | Rechte, die offen standen, gelten als gelesen |
| Datei nicht lesbar | `unavailable` | hier hilft nur Wiederherstellung |
| ein Vorgang wartet auf Klaerung | `degraded` | offen ist nicht kaputt, aber auch nicht gut |
| ein Vorgang blieb beim Ausfuehren stehen | `degraded` | die Belastung KANN stattgefunden haben |
| ein Vorgang wartet auf die Bank | `degraded` | nur ein Mensch kann das |
| Zahlungsmittel ohne Anbietereintrag | `degraded` | es sieht benutzbar aus und ist es nicht |
| Zahlungsmittel ohne Tresorzugang | `auth_required` | es fehlt eine menschliche Handlung |
| Zahlungsmittel muss neu geprueft werden | `auth_required` | nach einer Wiederherstellung |
| alle gesperrt, aber vorhanden | `degraded` | ehrlich benannt statt gruen |
| sonst | `healthy` | |

`auth_required` ist dasselbe Wort, das der Tresor und die verschluesselte
Sicherungsplatte benutzen, und es bedeutet dasselbe: es ist nichts kaputt, es
fehlt eine menschliche Handlung.
"""
from __future__ import annotations

import os

from solvio.payment import config as PC
from solvio.payment.instruments import InstrumentStatus
from solvio.payment.intent import PaymentState
from solvio.payment.store import PaymentStore, db_path

HEALTHY = "healthy"
DEGRADED = "degraded"
AUTH_REQUIRED = "auth_required"
UNAVAILABLE = "unavailable"


def assess() -> tuple[str, str]:
    """Ein Wort und ein Satz. Nennt nie einen Betrag, nie einen Token.

    Ausdruecklich synchron und ohne Netz: der Aufrufer legt das in einen Thread,
    weil eine SQLite-Datei blockieren kann — aber es gibt hier nichts, was auf
    einen Anbieter wartet.
    """
    path = db_path()
    if not os.path.exists(path):
        return HEALTHY, "Zahlungen sind noch nicht eingerichtet"

    try:
        store = PaymentStore(path)
    except Exception:  # noqa: BLE001 - eine unlesbare Datei ist eine Aussage
        return UNAVAILABLE, "Zahlungsablage ist nicht lesbar"

    if not store.permissions_ok():
        return DEGRADED, "Zahlungsablage steht fuer andere offen"

    try:
        instruments = store.instruments()
        # `EXECUTING` zaehlt als offen. Ein Vorgang, der zwischen Anspruch und
        # Antwort stehen blieb, ist nicht erledigt — er ist der Fall, bei dem
        # die Belastung stattgefunden haben kann und niemand es weiss.
        offen = store.intents(states=(PaymentState.RECONCILIATION_REQUIRED,
                                      PaymentState.EXECUTING), limit=200)
        bank = store.intents(states=(PaymentState.AWAITING_SCA,), limit=200)
    except Exception:  # noqa: BLE001
        return UNAVAILABLE, "Zahlungsablage ist beschaedigt"

    if not instruments:
        return HEALTHY, "kein Zahlungsmittel hinterlegt"

    # Die unklaren Faelle zuerst: sie sind das Einzige, wo Geld im Spiel ist.
    if offen:
        return DEGRADED, (f"{len(offen)} Zahlung(en) mit unklarem Ausgang — "
                          "bitte nachsehen lassen")
    if bank:
        return DEGRADED, f"{len(bank)} Zahlung(en) warten auf deine Bank"

    providers = PC.load()
    revalidate = [i for i in instruments
                  if i.status is InstrumentStatus.REVALIDATION_REQUIRED]
    if revalidate:
        return AUTH_REQUIRED, (f"{len(revalidate)} Zahlungsmittel muss/muessen "
                               "nach einer Wiederherstellung neu geprueft werden")

    aktiv = [i for i in instruments if i.status is InstrumentStatus.ACTIVE]
    if not aktiv:
        return DEGRADED, f"alle {len(instruments)} Zahlungsmittel sind gesperrt"

    ohne_anbieter = [i for i in aktiv if i.provider not in providers]
    if ohne_anbieter:
        return DEGRADED, (f"{len(ohne_anbieter)} Zahlungsmittel zeigen auf einen "
                          "Anbieter, der nicht eingerichtet ist")
    ohne_zugang = [i for i in aktiv if not i.provider_secret_ref]
    if ohne_zugang:
        return AUTH_REQUIRED, (f"{len(ohne_zugang)} Zahlungsmittel haben keinen "
                               "Zugang im Tresor")
    return HEALTHY, f"{len(aktiv)} Zahlungsmittel bereit"


def reconciliation_open() -> int:
    """Wie viele Vorgaenge auf Klaerung warten. Fuer den proaktiven Eingang."""
    try:
        if not os.path.exists(db_path()):
            return 0
        return len(PaymentStore().intents(
            states=(PaymentState.RECONCILIATION_REQUIRED,
                    PaymentState.EXECUTING), limit=200))
    except Exception:  # noqa: BLE001
        return 0
