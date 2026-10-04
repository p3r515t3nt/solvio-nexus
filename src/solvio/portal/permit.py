"""Die Erlaubnis, genau einmal zu schreiben.

Browser V1 laesst nur `GET` und `HEAD` hinaus, und das traegt dort die halbe
Sicherheit. Sobald angemeldete Vorgaenge schreiben duerfen, faellt diese Regel —
also braucht sie einen Nachfolger, und der darf nicht „POST ist jetzt erlaubt"
lauten. Ein Formular freizugeben, das einmal etwas abschickt, darf keiner Seite
dauerhaft das Recht geben, Anfragen zu stellen.

Deshalb ist die Erlaubnis so eng wie moeglich geschnitten: **eine** Herkunft,
**eine** Methode, **eine** Aktionskennung, **eine** Verwendung, und eine kurze
Frist. Die erste passende Anfrage verbraucht sie; danach gilt wieder Verweigern.
Was danach noch kommt — ein Doppelklick, ein wiederholender Skriptaufruf, ein
zweiter Callback aus dem Browser — trifft auf nichts mehr.

Der Zaehler steht bewusst hier und nicht im Browser. Der Browser fuehrt aus; ob
etwas ausgefuehrt werden darf, entscheidet SOLVIO.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from solvio.logging_setup import get_logger

log = get_logger("portal")

#: Wie lange eine Erlaubnis hoechstens gilt. Kurz: sie entsteht unmittelbar vor
#: der Ausfuehrung und wird unmittelbar danach verbraucht.
DEFAULT_TTL = 45.0

#: Methoden, die eine Erlaubnis ueberhaupt tragen kann.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def origin_of(url: str) -> str:
    """Schema, Host und Port — mehr macht eine Herkunft nicht aus."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}{port}"


@dataclass
class WritePermit:
    """Eine Erlaubnis fuer genau eine schreibende Anfrage."""

    action_id: str
    origin: str
    method: str
    path_prefix: str = ""
    ttl: float = DEFAULT_TTL
    issued_at: float = field(default_factory=time.monotonic)
    uses: int = 0
    consumed_url: str = ""

    @property
    def spent(self) -> bool:
        return self.uses >= 1

    def expired(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) - self.issued_at > self.ttl

    def matches(self, *, url: str, method: str, now: float | None = None) -> str:
        """Passt diese Anfrage zur Erlaubnis? Leer heisst ja, sonst der Grund.

        Absichtlich wortkarg und vollstaendig: jede Bedingung wird einzeln
        benannt, damit ein abgelehnter Schreibvorgang im Protokoll sagt, woran
        es lag, statt nur „nicht erlaubt".
        """
        if self.spent:
            return "permit_spent"
        if self.expired(now=now):
            return "permit_expired"
        if method.upper() != self.method.upper():
            return "method_mismatch"
        if origin_of(url) != self.origin:
            return "origin_mismatch"
        if self.path_prefix:
            try:
                path = urlsplit(url).path or "/"
            except ValueError:
                return "invalid_url"
            if not path.startswith(self.path_prefix):
                return "path_mismatch"
        return ""

    def consume(self, url: str) -> None:
        self.uses += 1
        self.consumed_url = url


class PermitBook:
    """Alle offenen Erlaubnisse. Hoechstens eine je Aktion, meist gar keine."""

    def __init__(self) -> None:
        self._permits: dict[str, WritePermit] = {}

    def issue(self, *, action_id: str, origin: str, method: str,
              path_prefix: str = "", ttl: float = DEFAULT_TTL) -> WritePermit:
        if method.upper() not in WRITE_METHODS:
            raise ValueError(f"a permit is for writes, not for {method!r}")
        if not origin:
            raise ValueError("a permit needs an exact origin")
        permit = WritePermit(action_id=action_id, origin=origin,
                             method=method.upper(), path_prefix=path_prefix, ttl=ttl)
        self._permits[action_id] = permit
        log.info("portal.permit_issued", action_id=action_id, origin=origin,
                 method=permit.method, ttl=int(ttl))
        return permit

    def allow(self, *, url: str, method: str) -> tuple[bool, str]:
        """Darf diese Anfrage hinaus? Verbraucht die Erlaubnis, wenn ja.

        Es gibt keinen Weg, hier ohne Erlaubnis durchzukommen — die
        Voreinstellung ist Verweigern, und eine Erlaubnis entsteht nur aus einer
        Freigabe.
        """
        if method.upper() not in WRITE_METHODS:
            return True, ""            # Lesen regelt die Netz-Policy, nicht dieses Buch
        reason = "no_permit"
        for permit in list(self._permits.values()):
            problem = permit.matches(url=url, method=method)
            if not problem:
                permit.consume(url)
                self._permits.pop(permit.action_id, None)
                log.info("portal.permit_consumed", action_id=permit.action_id,
                         method=permit.method)
                return True, ""
            reason = problem
        log.warning("portal.write_denied", method=method.upper(), reason=reason)
        return False, reason

    def revoke(self, action_id: str) -> bool:
        return self._permits.pop(action_id, None) is not None

    def clear(self) -> None:
        self._permits.clear()

    @property
    def open_permits(self) -> int:
        return len(self._permits)
