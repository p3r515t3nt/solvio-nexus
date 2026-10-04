"""Endpunkte aus der Umgebung — oder gar keine (Remote Access V1, PREBUILD).

Dieselbe Haltung wie `capabilities/approver_runtime.from_environment()`: **kein
Halbzustand.** Fehlt die Angabe, gibt es keinen Fernweg — und dann sagt der
Bericht das, statt eine Adresse zu raten.

Keine Magic IPs
---------------
In diesem Modul steht keine einzige Adresse. Weder `10.77.0.x` noch
`192.168.178.x` noch ein Wirtsname. Das ist eine ausdrueckliche Anforderung an
Remote Access V1 und zugleich die Lehre aus `solvio.nodes`: „**IP ist nicht
Identitaet**" (`docs/architecture/SOLVIO_NODES.md` §2). Wer eine Adresse fest
verdrahtet, bindet die Architektur an eine Betriebstatsache, die sich aendert.

Format
------
`SOLVIO_REMOTE_ACCESS_ENDPOINTS` traegt eine kommagetrennte Liste aus
`name:kind:base_url` — zum Beispiel::

    lan:local:https://<lan-adresse>:8770,tunnel:remote:https://<tunnel-adresse>:8770

Die Reihenfolge in der Variablen ist unerheblich: `local` kommt immer zuerst
(`resolver.order_endpoints`). Wer es andersherum will, muss den Contract
aendern und begruenden — nicht eine Umgebungsvariable umsortieren.

Produktiv steht hier **ein** Eintrag: der Vermittler des Fernwegs. Der lokale
Weg braucht keinen, weil der Core ihn nicht messen muss — er IST der lokale
Weg. Was gemessen werden muss, ist das Stueck, das ausfallen kann, ohne dass
der Mac etwas davon merkt.
"""
from __future__ import annotations

import os

from solvio.logging_setup import get_logger
from solvio.remote_access.contract import PathKind, RemoteEndpoint

log = get_logger("remote_access")

ENV_ENDPOINTS = "SOLVIO_REMOTE_ACCESS_ENDPOINTS"


class EndpointConfigError(ValueError):
    """Die Endpunktangabe ist unbrauchbar. Fail-closed, nie geraten."""


def parse_endpoints(raw: str) -> list[RemoteEndpoint]:
    """Zerlegt die Umgebungsangabe. Wirft bei allem Unklaren.

    Die Reihenfolge in der Zeichenkette bestimmt `preference` innerhalb
    derselben Art — damit zwei Fernwege eine definierte Ordnung haben, ohne
    dass jemand Zahlen pflegen muss.
    """
    endpoints: list[RemoteEndpoint] = []
    seen: set[str] = set()
    for index, chunk in enumerate(part.strip() for part in raw.split(",")):
        if not chunk:
            continue
        # `rsplit` mit 2 Feldern von links: der Name darf keinen Doppelpunkt
        # tragen, die URL dagegen sehr wohl (Schema und Port).
        parts = chunk.split(":", 2)
        if len(parts) != 3:
            raise EndpointConfigError(
                f"Eintrag {index + 1} ist nicht `name:kind:base_url`: {chunk!r}")
        name, kind_raw, base_url = (p.strip() for p in parts)
        try:
            kind = PathKind(kind_raw.lower())
        except ValueError:
            allowed = "/".join(k.value for k in PathKind)
            raise EndpointConfigError(
                f"Eintrag {index + 1}: `{kind_raw}` ist keine Wegart ({allowed})"
            ) from None
        if name in seen:
            raise EndpointConfigError(f"doppelter Endpunktname: {name!r}")
        seen.add(name)
        try:
            endpoints.append(RemoteEndpoint(name=name, kind=kind, base_url=base_url,
                                            preference=index))
        except ValueError as exc:
            raise EndpointConfigError(f"Eintrag {index + 1}: {exc}") from None
    if not endpoints:
        raise EndpointConfigError("kein einziger Endpunkt angegeben")
    return endpoints


def endpoints_from_environment() -> list[RemoteEndpoint]:
    """Liest die Endpunkte — oder gibt eine leere Liste zurueck.

    Eine leere Liste heisst „nicht eingerichtet" und ist ein gueltiger,
    ruhiger Zustand: SOLVIO lief bisher ohne Fernzugriff und laeuft ohne ihn
    weiter. Eine FEHLERHAFTE Angabe ist dagegen laut — sie wirft.
    """
    raw = (os.environ.get(ENV_ENDPOINTS, "") or "").strip()
    if not raw:
        log.info("remote_access.not_configured")
        return []
    endpoints = parse_endpoints(raw)
    log.info("remote_access.configured", count=len(endpoints),
             kinds=sorted({e.kind.value for e in endpoints}))
    return endpoints
