"""Woher der laufende Vorgang kommt — gesetzt vom Core, gelesen vom Tresor.

Ein Executor weiss, WAS er tut, aber nicht, WER es angestossen hat. Der
`CapabilityRouter` weiss beides. Ohne eine Bruecke muesste jede Funktion, die
irgendwann ein Geheimnis brauchen koennte, ihre Herkunft als Parameter
durchreichen — durch Kalender-, Mail-, Haus- und Anbieterschichten, die damit
nichts zu tun haben. Solche Parameter werden irgendwo vergessen, und ein
vergessener Herkunftsparameter ist eine Vermutung.

Also ein `ContextVar`. Er hat drei Eigenschaften, die hier genau passen:

* **Er folgt dem `await`.** Eine asynchrone Faehigkeit behaelt ihre Herkunft
  ueber jede Wartestelle hinweg, ohne dass jemand sie weiterreicht.
* **Er ist nebenlaeufigkeitssicher.** Zwei gleichzeitige Faehigkeiten teilen
  ihn nicht; jede Aufgabe hat ihre eigene Kopie.
* **Sein Vorgabewert ist die strengste Antwort.** Wer nichts gesetzt hat, ist
  `UNSPECIFIED` — und der Tresor verweigert das. Ein Executor, der ausserhalb
  eines Router-Vorgangs nach einem Geheimnis fragt, bekommt keines.

Was ein Modell hier kann: nichts. Gesetzt wird ausschliesslich im Router, aus
Werten, die der Core selbst aus Transportfakten abgeleitet hat.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass

from solvio.capabilities import policy as AP


@dataclass(frozen=True)
class UseContext:
    """Der laufende Vorgang, so weit der Tresor ihn braucht."""

    origin: AP.OriginClass = AP.OriginClass.UNSPECIFIED
    capability: str = ""
    approval_id: str = ""
    execution_id: str = ""
    user_present: bool = False
    automation_id: str = ""
    # Identitaet des verifizierten Eingangs. Nie aus Werkzeugargumenten.
    principal: str = ""
    task_start_receipt: object | None = None


_EMPTY = UseContext()

_CURRENT: contextvars.ContextVar[UseContext] = contextvars.ContextVar(
    "solvio_secret_use_context", default=_EMPTY)


def current() -> UseContext:
    return _CURRENT.get()


def bind(context: UseContext):
    """Setzt den Vorgang. Gibt das Token zurueck, mit dem er zurueckgenommen wird."""
    return _CURRENT.set(context)


def release(token) -> None:
    try:
        _CURRENT.reset(token)
    except ValueError:  # pragma: no cover - anderer Kontext, dann galt er dort nie
        pass


class bound:
    """`with bound(UseContext(...)):` — fuer Aufrufer, die keinen Router haben."""

    def __init__(self, context: UseContext) -> None:
        self.context = context
        self._token = None

    def __enter__(self) -> UseContext:
        self._token = bind(self.context)
        return self.context

    def __exit__(self, kind, value, traceback) -> bool:
        if self._token is not None:
            release(self._token)
        return False
