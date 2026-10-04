"""Local-First-Aufloesung: welcher Weg gilt gerade? (Remote Access V1, PREBUILD)

Eine Regel, und sie steht in einem Satz:

> **Steht das Heimnetz, wird das Heimnetz benutzt. Der Fernweg ist ein
> zusaetzlicher Pfad, kein Ersatz.**

Warum das mehr ist als eine Vorliebe: Der Fernweg fuehrt ueber einen Vermittler,
der Verkehrsmuster sieht (wann und wie lange gesprochen wird — bei einem
16-kHz-PCM16-Strom ist das am Rahmenmuster ablesbar). Der lokale Weg fuehrt an
niemandem vorbei. Wer im selben WLAN steht und trotzdem fern verbindet,
verschenkt Vertraulichkeit ohne Gegenwert.

Warum das der Owner nicht umschalten muss: Ein Schalter, der falsch stehen
kann, steht irgendwann falsch. Die Reihenfolge ergibt sich aus der Messung.

Wo diese Auswahl WIRKLICH stattfindet
-------------------------------------
Nicht hier. Das Telefon erreicht ueber beide Wege DIESELBE Adresse — zuhause
direkt im WLAN, unterwegs durch den Tunnel, weil der Vermittler das Ziel auf
die Overlay-Adresse des Macs umschreibt. Welcher der beiden Wege benutzt wird,
entscheidet die Routing-Tabelle des Telefons und die On-Demand-Regel des
WireGuard-Clients. Es gibt nichts umzuschalten, und genau deshalb kann hier
nichts falsch stehen.

Dieses Modul waehlt deshalb nichts aus, es BEOBACHTET: steht der Fernweg
gerade? Der Core benutzt es fuer die Gesundheitssonde `remote_access` — die
ehrliche Auskunft „unterwegs waerst du mich gerade nicht erreichbar" ist der
ganze Zweck.

Die Auswahl bleibt trotzdem im Contract, weil ein spaeterer Entwurf zwei
verschiedene Adressen haben kann und die Reihenfolge dann eine Entscheidung
ist, die aufgeschrieben gehoert statt verstreut.
"""
from __future__ import annotations

import asyncio

from solvio.logging_setup import get_logger
from solvio.remote_access.contract import (
    ConnectionState,
    PathKind,
    ProbeResult,
    RemoteAccessReport,
    RemoteEndpoint,
    RemoteTransport,
)

log = get_logger("remote_access")

#: Voreinstellung fuer eine einzelne Messung. Kurz, weil die Messung im
#: schlechten Fall NEBEN dem laufenden Betrieb passiert und ihn nicht aufhalten
#: darf: ein nicht erreichbarer Fernweg ist ein normaler Zustand, kein Notfall.
DEFAULT_PROBE_TIMEOUT_S = 2.0


def order_endpoints(endpoints: list[RemoteEndpoint]) -> list[RemoteEndpoint]:
    """Sortiert local-first, dann nach `preference`, dann nach Name.

    Der Name als letztes Kriterium ist kein Schmuck: ohne ihn haengt die
    Reihenfolge zweier gleichwertiger Endpunkte an der Einfuegereihenfolge, und
    dann misst dieselbe Lage an zwei Tagen zwei verschiedene Wege.
    """
    return sorted(endpoints, key=lambda e: (e.kind is not PathKind.LOCAL,
                                            e.preference, e.name))


class LocalFirstResolver:
    """Waehlt den ersten erreichbaren Weg, lokal zuerst.

    Der Transport ist injiziert, nicht importiert — das ist der Punkt der
    ganzen Naht: `wireguard`, `tailscale` oder ein Nachfolger tauschen sich
    aus, ohne dass hier eine Zeile faellt.
    """

    def __init__(self, transport: RemoteTransport, endpoints: list[RemoteEndpoint],
                 *, timeout_s: float = DEFAULT_PROBE_TIMEOUT_S) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s muss positiv sein")
        self._transport = transport
        self._endpoints = order_endpoints(list(endpoints))
        self._timeout_s = timeout_s

    @property
    def endpoints(self) -> tuple[RemoteEndpoint, ...]:
        """Die geordnete Liste — zum Anschauen, nicht zum Aendern."""
        return tuple(self._endpoints)

    async def resolve(self) -> RemoteAccessReport:
        """Misst der Reihe nach und nimmt den ersten Weg, der antwortet.

        Der Reihe nach und nicht gleichzeitig, und das ist Absicht: Ein
        paralleles Messen wuerde den Fernweg auch dann anfassen, wenn der
        lokale steht — also einen Vermittler ueber die Anwesenheit des
        Telefons informieren, ohne dass es einen Grund gab.
        """
        probes: list[ProbeResult] = []
        for endpoint in self._endpoints:
            result = await self._probe_one(endpoint)
            probes.append(result)
            if result.reachable:
                state = (ConnectionState.LOCAL_DIRECT
                         if endpoint.kind is PathKind.LOCAL
                         else ConnectionState.REMOTE_CONNECTED)
                log.info("remote_access.selected", path=endpoint.kind.value,
                         endpoint=endpoint.name, transport=self._transport.name)
                return RemoteAccessReport(state=state, active=endpoint,
                                          probes=tuple(probes))

        log.info("remote_access.unavailable", checked=len(probes),
                 transport=self._transport.name)
        return RemoteAccessReport(state=ConnectionState.REMOTE_UNAVAILABLE,
                                  active=None, probes=tuple(probes))

    async def _probe_one(self, endpoint: RemoteEndpoint) -> ProbeResult:
        """Eine Messung, die nie wirft.

        Ein Transport, der doch wirft, ist ein Fehler in dieser Naht und nicht
        im Betrieb: er wuerde sonst die Aufloesung abbrechen und damit einen
        FUNKTIONIERENDEN spaeteren Weg verdecken. Deshalb wird hier gefangen —
        genau das ist die Zusicherung „ein Ausfall des Fernwegs beschaedigt die
        LAN-Nutzung nicht", und sie muss auch fuer einen kaputten Transport
        gelten, nicht nur fuer ein unerreichbares Ziel.
        """
        try:
            return await asyncio.wait_for(
                self._transport.probe(endpoint, timeout_s=self._timeout_s),
                timeout=self._timeout_s + 1.0)
        except asyncio.TimeoutError:
            return ProbeResult(endpoint=endpoint, reachable=False, detail="timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — ein Transportfehler ist ein Ergebnis
            log.warning("remote_access.probe_failed", endpoint=endpoint.name,
                        error=type(exc).__name__)
            return ProbeResult(endpoint=endpoint, reachable=False,
                               detail=f"probe_error:{type(exc).__name__}")
