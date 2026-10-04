"""Remote-Access-Contract — Transport ist nur Transport (Remote Access V1, PREBUILD).

Framework- und anbieterneutrale Typen fuer die Frage „auf welchem Weg erreicht
das iPhone gerade den Core, und ist dieser Weg ueberhaupt da?".

Siehe `docs/design/remote-access-v1/REMOTE_ACCESS_V1_ARCHITECTURE.md`.

Die eine Regel dieses Moduls
----------------------------
**Ein Transportbefund traegt niemals Autoritaet.** Er sagt, ueber welchen Weg
Bytes ankommen — nicht, wer etwas darf. Deshalb hat `RemoteEndpoint` kein
Herkunfts-, Vertrauens- oder Freigabefeld, und `RemoteAccessReport` ist eine
reine Beobachtung ohne Entscheidungsfunktion.

Das ist dieselbe Haltung, die `solvio.nodes` schon traegt: „Transport-Trust ist
nicht Content-Trust" (`docs/architecture/SOLVIO_NODES.md` §5). Hier ist sie
schaerfer, weil auf der anderen Seite ein Freigabeweg liegt: Ein Vermittler,
der Pakete weiterreicht, darf keine Freigabe ausstellen, Face ID nicht
ersetzen, keinen `TrustContext` veraendern und keine Risikoklasse senken. Die
Freigabeautoritaet ist eine Secure-Enclave-Signatur ueber die exakten
Entscheidungsbytes plus eine frische App-Attest-Aussage
(`src/solvio/security/mobile_approval/gateway.py:7-13`) — beides an
`core_instance_id`, `device_id` und eine Core-Nonce gebunden, an KEINE
TLS-Verbindung und an keinen Netzweg. Ein anderer Transport aendert daran
nichts, und dieses Modul bekommt gar nicht erst die Mittel, es zu versuchen.

Was hier bewusst NICHT steht
----------------------------
Kein VPN, kein Kryptoprotokoll, kein TCP-Tunnel, kein Reconnect-Supervisor und
keine Anbieterbibliothek. Der Tunnel wird betrieben, nicht programmiert — auf
dem Mac von launchd, wie `deploy/macos-wireguard/` es seit STEP 21.2E tut. Der
Core beobachtet ihn hoechstens.

Und keine fest verdrahteten Adressen. Jede Adresse kommt aus Konfiguration; das
Modul kennt weder `10.77.0.x` noch `192.168.178.x`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class PathKind(str, Enum):
    """Wie ein Endpunkt erreicht wird — eine Beschreibung, keine Bewertung.

    `LOCAL` heisst „im selben Heimnetz, ohne Vermittler". `REMOTE` heisst „ueber
    einen Tunnel, an dem ein Vermittler beteiligt ist". Mehr sagt der Wert
    nicht, und insbesondere sagt er nichts ueber Rechte: ein attestiertes
    Geraet ist ueber beide Wege dasselbe Geraet.
    """

    LOCAL = "local"
    REMOTE = "remote"


class ConnectionState(str, Enum):
    """Der beobachtete Zustand des Zugangs.

    Die Werte sind absichtlich grob. Ein feineres Vokabular waere eine zweite
    Wahrheit neben dem, was die Runtime ohnehin weiss — und die aeltere.
    """

    #: Der Core ist im Heimnetz direkt erreichbar. Der Normalfall.
    LOCAL_DIRECT = "local_direct"
    #: Kein lokaler Weg, aber ein Fernweg steht.
    REMOTE_CONNECTED = "remote_connected"
    #: Weder lokal noch fern erreichbar.
    REMOTE_UNAVAILABLE = "remote_unavailable"
    #: Ein Weg antwortet, aber nicht sauber (Teilausfall, langsam, flatternd).
    DEGRADED = "degraded"
    #: Ein Weg besteht, aber das Geraet hat sich nicht ausgewiesen.
    #:
    #: Diese Lage gehoert NICHT diesem Modul: sie entsteht im Freigabe-Gateway
    #: und wird hier nur benannt, damit ein Bericht sie durchreichen kann, ohne
    #: sie zu erfinden. Dieses Modul setzt sie nie selbst.
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True)
class RemoteEndpoint:
    """Ein Weg zum Core — Adresse, Art, Reihenfolge. Sonst nichts.

    `name` ist eine Betriebskennung fuer Log und Bericht, keine Identitaet.
    `base_url` ist die Adresse, die die App ansprechen wuerde.
    `preference` ordnet: kleiner heisst frueher probiert.

    Bewusst ohne Zugangsdaten. Ein Endpunkt beschreibt, WOHIN, nie WOMIT — die
    Kennung des Geraets liegt auf dem Geraet, und der Core haelt sie in der
    eingefrorenen Kontrollebene, nicht hier.
    """

    name: str
    kind: PathKind
    base_url: str
    preference: int = 100

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("RemoteEndpoint.name darf nicht leer sein")
        if not self.base_url.strip():
            raise ValueError("RemoteEndpoint.base_url darf nicht leer sein")


@dataclass(frozen=True)
class ProbeResult:
    """Was eine einzelne Erreichbarkeitsmessung ergeben hat.

    `reachable` ist das Ergebnis, `detail` ein kurzer, geheimnisfreier Grund.
    `latency_ms` ist optional und nur eine Messung — keine Zusicherung.
    """

    endpoint: RemoteEndpoint
    reachable: bool
    detail: str = ""
    latency_ms: float | None = None


@dataclass(frozen=True)
class RemoteAccessReport:
    """Der Zustand des Zugangs zu einem Zeitpunkt. Eine Beobachtung.

    Absichtlich ohne Methode, die etwas entscheidet oder erlaubt. Wer diesen
    Bericht liest, erfaehrt, ob ein Weg steht — nie, ob jemand etwas darf.
    """

    state: ConnectionState
    #: Der gewaehlte Weg, oder None, wenn keiner steht.
    active: RemoteEndpoint | None = None
    #: Alle Messungen dieser Runde, in Probier-Reihenfolge.
    probes: tuple[ProbeResult, ...] = field(default_factory=tuple)

    @property
    def kind(self) -> PathKind | None:
        """Ueber welche Art Weg es gerade laeuft — oder None."""
        return self.active.kind if self.active is not None else None

    def as_health(self) -> dict[str, object]:
        """Knappe, geheimnisfreie Fassung fuer Gesundheit und Kontrollzentrum.

        Es steht bewusst KEINE Adresse darin: eine Gesundheitszeile wandert in
        Logs und auf ein Telefondisplay, und die Adresse des Cores ist eine
        Betriebstatsache, die dort nichts zu suchen hat.
        """
        return {
            "state": self.state.value,
            "path": self.kind.value if self.kind is not None else None,
            "endpoint": self.active.name if self.active is not None else None,
            "checked": len(self.probes),
        }


@runtime_checkable
class RemoteTransport(Protocol):
    """Ein austauschbarer Weg, Erreichbarkeit zu MESSEN — nicht herzustellen.

    Bewusst schmal. Ein Transport dieses Contracts baut keinen Tunnel auf, haelt
    keine Verbindung und kennt keine Zugangsdaten; er beantwortet genau eine
    Frage: „ist dieser Endpunkt gerade erreichbar?".

    Der Grund fuer diese Enge ist der SOLVIO-Grundsatz: WireGuard, Tailscale
    oder ein spaeterer Nachfolger bringen Aufbau, Reconnect und NAT-Traversal
    fertig mit und werden vom Betriebssystem betrieben. SOLVIO nachzubauen, was
    launchd und ein etablierter Dienst zuverlaessig koennen, waere genau der
    Fehler, den der Architekturvertrag verbietet.
    """

    @property
    def name(self) -> str:
        """Kurzname des Transports fuer Bericht und Log (z. B. `wireguard`)."""
        ...

    async def probe(self, endpoint: RemoteEndpoint, *, timeout_s: float) -> ProbeResult:
        """Misst genau einen Endpunkt. Wirft nicht — ein Fehler IST das Ergebnis."""
        ...
