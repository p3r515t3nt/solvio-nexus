"""Der EINZIGE Ort, an dem der Telefonie-Schluessel eingesetzt wird.

Dieses Modul ist die Kredentialgrenze der Telefonie. `EXECUTOR_MODULES` nennt
genau `solvio.telephony.upstream`, und der Tresor nimmt den Modulnamen per
Stack-Inspektion — deshalb steht `broker.use(...)` **woertlich in dieser Datei**
und in keinem Helfer. In einem Helfer stuende dort der Helfer, und die Grenze
waere eine Behauptung statt einer Pruefung.

Wer hier NICHT hereinkommt: die Faehigkeit, die den Anruf anstoesst, der
Ledger, der ihn nachhaelt, das Sprachmodell, der Cognitive Router, ein
Hermes-Bot. Sie alle reden ueber Kennungen — `conversation_id`, `call_id` —
und nie ueber den Schluessel.

Zwei Schranken liegen vor dem Netz, und sie sind absichtlich verschieden:

1. **Das Pfadtor** (`elevenlabs.path_allowed`). Es entscheidet aus dem
   Bereich, was ueberhaupt hinausgehen darf. Ein Aufrufer, der anrufen darf,
   darf damit noch lange nicht Agenten anlegen.
2. **Der Tresor** (`broker.use`). Er entscheidet, ob DIESER Vorgang diesen
   Wert leihen darf — aus Herkunft, Faehigkeit und Freigabe, die der Router
   gesetzt hat und die kein Aufrufer sich aussuchen kann.

Die erste Schranke ohne die zweite waere ein offener Proxy mit hoeflicher
Wegbeschreibung. Die zweite ohne die erste waere ein Generalschluessel fuer die
gesamte Anbieter-API.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Any

from solvio.logging_setup import get_logger
from solvio.secret_vault import broker as B
from solvio.secret_vault import policy as VP
from solvio.telephony import elevenlabs as EL

log = get_logger("telephony")

#: Wie lange ein einzelner Anbieteraufruf hoechstens dauern darf. Ein Anruf
#: dauert Minuten, aber das TELEFONAT laeuft beim Anbieter — unsere Aufrufe
#: sind kurz: einen Anruf anstossen, einen Ausgang nachlesen.
DEFAULT_TIMEOUT = 20.0


class TelephonyUpstreamError(RuntimeError):
    """Der Aufruf ging nicht hinaus oder kam schlecht zurueck. Genau ein Grund."""

    def __init__(self, reason: str, detail: str = "", *, status: int = 0) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.status = status


class TelephonyPathRefused(TelephonyUpstreamError):
    """Der Pfad steht nicht in der Liste dieses Bereichs. Kein Netzverkehr."""


@dataclass(frozen=True)
class UpstreamResponse:
    """Was der Anbieter gesagt hat. Rohdaten, keine Deutung.

    `body` ist geparst, wenn es JSON war, sonst der Text. Diese Klasse trifft
    ausdruecklich KEINE Aussage darueber, ob ein Anruf zustande kam — das ist
    Sache der Ergebniswahrheit, nicht des Transports. Eine 200 heisst hier
    ausschliesslich: der Anbieter hat die Anfrage angenommen.
    """

    status: int
    body: Any


#: Welche Bereiche ein laufender Vorgang zusaetzlich benutzen darf.
#:
#: Bewusst eine geschlossene Tabelle und keine Regel: „ein Anruf darf auch
#: lesen" ist eine Aussage ueber genau diesen einen Fall. Wer einen weiteren
#: Bereich braucht, traegt ihn hier sichtbar ein — und muss dabei begruenden,
#: warum der laufende Vorgang mehr darf, als er heisst.
_SUBSCOPES: dict[str, tuple[str, ...]] = {
    EL.CAPABILITY_CALL: (EL.CAPABILITY_CALL, EL.CAPABILITY_RESULT),
}


class ElevenLabsUpstream:
    """Der Telefonie-Ausgang. Haelt keinen Wert, leiht ihn je Aufruf."""

    def __init__(self, vault: B.SecretBroker | None = None,
                 origin: str = EL.UPSTREAM_ORIGIN) -> None:
        self._vault = vault
        self._origin = origin.rstrip("/")

    def _broker(self) -> B.SecretBroker:
        if self._vault is None:
            self._vault = B.SecretBroker()
        return self._vault

    async def call(self, method: str, path: str, *, scope: str,
                   body: dict[str, Any] | None = None,
                   timeout: float = DEFAULT_TIMEOUT) -> UpstreamResponse:
        """Ein Aufruf, ein Bereich, ein geliehener Wert.

        Der Wert lebt nur innerhalb des `with`-Blocks und wird nie
        zurueckgegeben, protokolliert oder in eine Ausnahme gelegt.
        """
        method = str(method or "").upper()
        if not EL.path_allowed(method, path, scope=scope):
            # Bewusst VOR dem Tresor: ein Pfad, der nicht hinausgehen darf,
            # soll den Wert nicht einmal anfassen.
            log.warning("telephony.path_refused", method=method,
                        path=path[:80], scope=scope[:40])
            raise TelephonyPathRefused("path_not_allowed", f"{method} {path}")

        # Der Bereich darf nicht mehr erlauben als der laufende Vorgang.
        #
        # Ohne diese Pruefung waehlte der AUFRUFER den Bereich — und damit auch
        # die Faehigkeit, die der Tresor prueft. Unter einem freigegebenen
        # `telephony_call` liesse sich dann `CAPABILITY_SETUP` uebergeben und die
        # Einrichtungsflaeche erreichen: Agenten anlegen, Nummern importieren.
        # Der Tresor haette zugestimmt, denn er bekam genau die Faehigkeit
        # genannt, die zum Bereich passt.
        #
        # Die erste Fassung verlangte GLEICHHEIT — und wuergte damit den
        # Ergebnisabruf ab. Waehrend eines Anrufs steht `telephony_call` im
        # Vorgang, das Nachlesen des Ausgangs benutzt aber `telephony_call_result`.
        # Jeder echte Anruf waere so nach dem Wartebudget als „weiss nicht"
        # geendet, obwohl das Telefon geklingelt hat: das Transkript, der
        # Zustellbeleg und die Kostenwahrheit waeren auf dem Live-Pfad tot
        # gewesen. Keiner der Tests sah es, weil alle an Attrappen messen und
        # nie durch dieses Modul laufen.
        #
        # Richtig ist eine Teilmenge, keine Gleichheit: ein laufender Anruf darf
        # zusaetzlich NUR den lesenden Ergebnisbereich benutzen. Dessen Pfadliste
        # enthaelt genau ein GET. Die Einrichtung bleibt unerreichbar.
        from solvio.secret_vault import context as SC
        laufend = SC.current().capability
        if laufend:
            erlaubte = _SUBSCOPES.get(laufend, (laufend,))
            if scope not in erlaubte:
                log.warning("telephony.scope_mismatch", scope=scope[:40],
                            running=laufend[:40])
                raise TelephonyPathRefused(
                    "scope_not_current_capability", f"{scope} != {laufend}")

        import aiohttp

        url = self._origin + path
        session_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            # `broker.use(...)` steht hier woertlich: der Tresor nimmt den
            # Modulnamen des Aufrufers per Stack-Inspektion, und in einem
            # Helfer stuende dort der Helfer.
            # `target` ist der TATSAECHLICHE Ursprung, nicht die Konstante.
            #
            # Vorher stand hier `EL.UPSTREAM_ORIGIN` — der Tresor bekam also
            # immer dasselbe Ziel gesagt, egal wohin die Anfrage wirklich ging.
            # Ein abweichender `origin` im Konstruktor haette den Wert an einen
            # fremden Server getragen, waehrend die Tresorspur brav
            # `api.elevenlabs.io` protokollierte. Jetzt prueft `evaluate` gegen
            # das, was gleich gesendet wird, und `allowed_targets` verweigert
            # jeden fremden Ursprung.
            with self._broker().use(EL.SECRET_REF,
                                    executor=VP.ExecutorId.TELEPHONY,
                                    target=self._origin,
                                    capability=scope) as material:
                headers = {
                    EL.AUTH_HEADER: material.plaintext(),
                    "content-type": "application/json",
                    "accept": "application/json",
                }
                async with aiohttp.ClientSession(timeout=session_timeout,
                                                 trust_env=False) as session:
                    async with session.request(
                            method, url, headers=headers,
                            # KEINE Umleitungen. aiohttp entfernt bei einem
                            # Ursprungswechsel zwar `Authorization`, nicht aber
                            # einen herstellereigenen Kopfsatz wie `xi-api-key` —
                            # eine 307 des Anbieters truege den Schluessel samt
                            # Rufnummer und Nachricht auf einen fremden Server.
                            # Fuer diese zwei Pfade gibt es keinen legitimen
                            # Umleitungsgrund.
                            allow_redirects=False,
                            data=(_json.dumps(body) if body is not None else None)
                    ) as response:
                        text = await response.text()
                        status = response.status
                        if 300 <= status < 400:
                            raise TelephonyUpstreamError(
                                "upstream_redirect", f"http {status}",
                                status=status)
        except B.SecretDenied as exc:
            raise TelephonyUpstreamError(
                "credential_denied", getattr(exc, "reason", "")) from None
        except B.SecretUnavailable:
            raise TelephonyUpstreamError("credential_unavailable") from None
        except TelephonyUpstreamError:
            raise
        except Exception as exc:                       # Netz, DNS, TLS, Zeit
            # Der Wortlaut einer Netzausnahme kann eine URL enthalten, nie aber
            # den Kopfsatz — trotzdem nur der Ausnahmetyp, nicht ihr Text.
            raise TelephonyUpstreamError(
                "upstream_unreachable", type(exc).__name__) from None
        finally:
            headers = None

        try:
            parsed: Any = _json.loads(text) if text else None
        except ValueError:
            parsed = text

        log.info("telephony.upstream", method=method, path=path[:80],
                 scope=scope[:40], status=status)
        return UpstreamResponse(status=status, body=parsed)
