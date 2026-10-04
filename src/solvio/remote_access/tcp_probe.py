"""Anbieterneutrale Erreichbarkeitsmessung per TCP (Remote Access V1, PREBUILD).

Die kleinstmoegliche Implementierung von `RemoteTransport`: eine TCP-Verbindung
oeffnen, sofort schliessen, das Ergebnis melden.

Warum nur TCP und kein TLS
--------------------------
Ein TLS-Handshake haette den Vorteil, mehr zu pruefen — und drei Nachteile, die
schwerer wiegen:

1. Er beruehrt das gepinnte Zertifikat. Die Pinning-Wahrheit liegt auf dem
   Telefon (der Fingerabdruck stammt aus dem Kopplungs-QR); eine zweite,
   core-seitige Meinung darueber waere eine zweite Wahrheit.
2. Er kostet auf dem Fernweg mehrere Umlaeufe fuer eine Frage, die schon nach
   dem ersten beantwortet ist.
3. Er wuerde gegen ein Zertifikat laufen, dessen einziger SAN die LAN-Adresse
   ist, waehrend diese Messung die Overlay-Adresse anspricht — und dann eine
   Warnung erzeugen, die nichts bedeutet.

Gemessen wird deshalb genau das, was diese Naht wissen muss: **kommt ein
TCP-SYN durch?** Ob der Dienst dahinter gesund ist, weiss die Gesundheit des
Cores besser, und ob das Geraet sich ausweisen darf, weiss allein die
eingefrorene Kontrollebene.

Es werden keine Daten gesendet. Nicht ein Byte, und schon gar keine Kennung:
eine Erreichbarkeitsmessung, die ein Geheimnis mitschickt, ist keine Messung
mehr, sondern eine Anmeldung.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from solvio.remote_access.contract import ProbeResult, RemoteEndpoint

#: Voreingestellte Ports, wenn eine Basisadresse keinen nennt. Kein SOLVIO-Port
#: steht hier: welchen Port das Gateway benutzt, sagt die Konfiguration.
_DEFAULT_PORTS = {"https": 443, "http": 80}


def split_target(base_url: str) -> tuple[str, int]:
    """Zerlegt eine Basisadresse in Wirt und Port.

    Wirft `ValueError` bei allem, was kein brauchbares Ziel ergibt — lieber ein
    lauter Konfigurationsfehler beim Start als eine Messung, die stumm immer
    `False` sagt und wie ein Netzproblem aussieht.
    """
    parts = urlsplit(base_url if "//" in base_url else f"//{base_url}")
    host = parts.hostname
    if not host:
        raise ValueError(f"keine Wirtsangabe in {base_url!r}")
    port = parts.port
    if port is None:
        port = _DEFAULT_PORTS.get((parts.scheme or "https").lower())
    if port is None:
        raise ValueError(f"kein Port bestimmbar fuer {base_url!r}")
    return host, port


class TcpProbeTransport:
    """`RemoteTransport` per TCP-Connect. Plattformneutral, ohne Abhaengigkeit.

    `name` ist frei waehlbar, damit ein Bericht sagen kann, WORUEBER gemessen
    wurde (`wireguard`, `tailscale`, `lan`) — ohne dass dieses Modul den
    jeweiligen Dienst kennen muesste. Der Name ist eine Beschriftung, keine
    Fallunterscheidung: es gibt hier keinen Zweig, der auf ihn schaut.
    """

    def __init__(self, name: str = "tcp") -> None:
        if not name.strip():
            raise ValueError("name darf nicht leer sein")
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def probe(self, endpoint: RemoteEndpoint, *, timeout_s: float) -> ProbeResult:
        """Oeffnet eine TCP-Verbindung und schliesst sie sofort wieder."""
        try:
            host, port = split_target(endpoint.base_url)
        except ValueError as exc:
            return ProbeResult(endpoint=endpoint, reachable=False,
                               detail=f"bad_endpoint:{exc}")

        started = time.monotonic()
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout_s)
        except asyncio.TimeoutError:
            return ProbeResult(endpoint=endpoint, reachable=False, detail="timeout")
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            # Ein geschlossener Port, eine fehlende Route, ein unbekannter Name:
            # alles derselbe Befund („nicht erreichbar"), und keiner davon ist
            # ein Programmfehler.
            return ProbeResult(endpoint=endpoint, reachable=False,
                               detail=f"unreachable:{type(exc).__name__}")
        else:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return ProbeResult(endpoint=endpoint, reachable=True, detail="open",
                               latency_ms=round(elapsed_ms, 1))
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, asyncio.CancelledError):  # pragma: no cover
                    pass
