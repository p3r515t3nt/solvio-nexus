"""Die AUSFUEHRUNGSIDENTITAET des laufenden Vorgangs — gesetzt vom Freigabepfad.

Der eingefrorene Freigabepfad kennt zwei Werte, die genau eine Faehigkeit
wirklich braucht und die deshalb sonst nirgends hingehoeren:

    execution_id      die stabile Identitaet EINER freigegebenen Handlung
    idempotency_key   worauf ein fremder Dienst entdoppeln soll

Beide sind reine Funktionen aus Core-Instanz, Freigabekennung und Faehigkeit
(`security/mobile_approval/execution.py`) — sie ueberleben einen Neustart, eine
neue Datenbankverbindung und jeden Wiederholungsversuch, ohne neu gewuerfelt zu
werden. Genau das macht sie zum Zaun gegen die Doppelbuchung.

**Warum ein ContextVar und kein Parameter.** `CapabilityApprovals.resume()`
uebergibt einem Handler bewusst nur seine Argumente — „Autoritaet gehoert nicht
in einen Handler" steht dort woertlich, und das bleibt richtig. Eine
Ausfuehrungsidentitaet ist aber keine Autoritaet: sie autorisiert nichts, sie
BENENNT nur, welcher Vorgang gerade laeuft. Sie durch Kalender-, Mail- und
Anbieterschichten zu reichen, die nichts damit zu tun haben, waere derselbe
Fehler, den `secret_vault.context` schon einmal vermieden hat.

**Ein Nebeneffekt, und ein laengst faelliger.** `UseContext.execution_id` steht
seit dem Tresor-Milestone im Vorgangskontext und wurde von NIEMANDEM gefuellt —
die Spalte `execution_id` der Zugriffsspur blieb deshalb leer. Der Freigabepfad
fuellt sie ab jetzt fuer JEDE Faehigkeit mit, nicht nur fuer Zahlungen.

**Der Vorgabewert ist die strengste Antwort.** Wer nichts gesetzt hat, hat eine
leere Identitaet — und der Zahlungs-Executor belastet dann NICHT. Eine Zahlung
ausserhalb eines freigegebenen Vorgangs ist strukturell unmoeglich, nicht bloss
verboten.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionIdentity:
    """Welcher freigegebene Vorgang laeuft gerade. Benennt, autorisiert nie."""

    execution_id: str = ""
    idempotency_key: str = ""
    approval_id: str = ""
    capability: str = ""
    #: Die Ausfuehrungssemantik aus dem eingefrorenen Satz. Fuer eine Zahlung
    #: immer `NON_IDEMPOTENT_WRITE` — steht hier, damit ein Test es festnageln
    #: kann, statt es zu glauben.
    semantics: str = ""
    #: Der Autorisierungs-Digest, den der Mensch bestaetigt hat.
    action_digest: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.execution_id and self.idempotency_key)


_EMPTY = ExecutionIdentity()

_CURRENT: contextvars.ContextVar[ExecutionIdentity] = contextvars.ContextVar(
    "solvio_payment_execution_identity", default=_EMPTY)


def current() -> ExecutionIdentity:
    return _CURRENT.get()


def bind(identity: ExecutionIdentity):
    return _CURRENT.set(identity)


def release(token) -> None:
    try:
        _CURRENT.reset(token)
    except ValueError:  # pragma: no cover - anderer Kontext, dann galt er dort nie
        pass


class bound:
    """`with bound(ExecutionIdentity(...)):` — der Freigabepfad setzt, sonst niemand."""

    def __init__(self, identity: ExecutionIdentity) -> None:
        self.identity = identity
        self._token = None

    def __enter__(self) -> ExecutionIdentity:
        self._token = bind(self.identity)
        return self.identity

    def __exit__(self, kind, value, traceback) -> bool:
        if self._token is not None:
            release(self._token)
        return False
