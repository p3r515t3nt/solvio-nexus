"""Das **einzige** Modul, das den echten Anbieterschluessel beruehrt.

Die Bauform ist die der Zahlung (`src/solvio/payment/executor.py`): ein Wert
erreicht genau ein namentlich geprueftes Modul, und eine AST-Zusicherung im Test
haelt das fest — nicht ein Kommentar, der eine Bitte ist.

Was hier steht und nirgends sonst:

* `UPSTREAM_ORIGIN` — im Code gepinnt. **Kein** Wirt, Port, Schema oder
  Pfadpraefix stammt je aus einer Anfrage des Kaefigs. Damit ist SSRF nicht
  gemildert, sondern strukturell abwesend: es gibt keinen Weg, auf dem ein
  Angreifer ein Ziel benennen koennte.
* Der Bau des ausgehenden `Authorization`-Kopfes.

Drei Einstellungen, die jede fuer sich ein Loch waeren:

* `trust_env=False` — sonst uebernaehme der Klient `HTTPS_PROXY` aus der
  Umgebung des Cores und schickte Anfragen mitsamt Anbieterschluessel an einen
  Wirt, den niemand gepinnt hat.
* `allow_redirects=False` — ein `3xx` wird kategorisch `502`. Ein gefolgter
  Umzug traegt den `Authorization`-Kopf mit; das ist bewusst strenger als der
  Zahlungsklient.
* TLS-Pruefung an — die Vorgabe von aiohttp, unangetastet. Sie wird nirgends
  abgeschaltet, auch nicht „nur zum Testen"; eine Zusicherung sucht diese Datei
  woertlich nach dem Abschalter ab, weshalb er hier nicht einmal als Beispiel
  steht.

Kuenftig steht hier der **eine** `SecretBroker.use(...)`-Aufruf fuer
`secret://openai/core` mit einer neuen, engen `ExecutorId.PROVIDER_BROKER`. Er
muss woertlich in dieser Datei stehen: der Aufreferabgleich des Tresors laeuft
ueber `sys._getframe(1)`. In V1 wandert der Wert **nicht** — die Sprachschicht
des Cores braucht ihn beim Start, und ein gesperrter Schluesselbund waere ein
ungeloester fail-closed Fall.
"""
from __future__ import annotations

from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("broker")

#: Im Code gepinnt. Nicht aus Einstellungen, nicht aus der Umgebung, niemals
#: aus der Anfrage.
UPSTREAM_ORIGIN = "https://api.openai.com"

#: Gesamtfrist einer Weiterleitung. Ein Recherchelauf mit Verdichtung darf lang
#: sein; ein haengender Anbieter darf keinen Platz im Kappenzaehler blockieren.
TOTAL_TIMEOUT = 900.0
CONNECT_TIMEOUT = 20.0

#: Was aus der eingehenden Anfrage ueberhaupt weiterreisen darf. Alles andere
#: wird neu gebaut — eine Erlaubnisliste, weil eine Sperrliste in diesem
#: Projekt schon einmal unvollstaendig war.
FORWARDABLE_REQUEST_HEADERS = ("content-type", "accept")

#: Was aus der Antwort des Anbieters NICHT an den Kaefig geht.
STRIPPED_RESPONSE_HEADERS = frozenset({
    "set-cookie", "set-cookie2", "www-authenticate", "proxy-authenticate",
    "transfer-encoding", "content-encoding", "content-length", "connection",
    "keep-alive", "trailer", "upgrade",
})


def target_url(canonical_path: str) -> str:
    """`UPSTREAM_ORIGIN` plus der EINE kanonische Pfad aus dem Pfadtor.

    Der Pfad beginnt bereits mit `/v1` — er wird nicht an eine Basis gehaengt,
    die selbst auf `/v1` endet. Ein `/v1/v1/responses` waere ein `404` beim
    Anbieter und ein sehr schwer zu lesender Fehler im Kaefig.
    """
    if not canonical_path.startswith("/"):
        raise ValueError("canonical path must be absolute")
    return UPSTREAM_ORIGIN + canonical_path


class Upstream:
    """Haelt den echten Schluessel und baut den ausgehenden Kopfsatz.

    Der Wert wird nie protokolliert, nie in eine Ausnahme geschrieben und nie
    an ein anderes Modul weitergereicht. Wer ihn braucht, ruft `open()`.
    """

    def __init__(self, provider_key: str) -> None:
        self._provider_key = (provider_key or "").strip()

    def configured(self) -> bool:
        return bool(self._provider_key)

    def authorization(self) -> str:
        """Der eine Ort, an dem der echte `Authorization`-Kopf entsteht."""
        return f"Bearer {self._provider_key}"

    def outbound_headers(self, inbound: Any) -> dict[str, str]:
        """Ein **frischer** Kopfsatz. Nichts wird durchgereicht, was nicht
        ausdruecklich erlaubt ist.

        Insbesondere faellt der eingehende `Authorization` weg — der Kaefig
        traegt dort seinen Broker-Token, und der hat draussen nichts zu suchen.
        Ebenso `proxy-authorization`, `cookie`, `forwarded`, `x-forwarded-*`,
        `x-real-ip` und `host`: sie stammen alle aus dem Kaefig und wuerden dem
        Anbieter eine Herkunft erzaehlen, die der Broker nicht bestaetigen kann.
        """
        headers = {
            "Authorization": self.authorization(),
            "Host": "api.openai.com",
        }
        for name in FORWARDABLE_REQUEST_HEADERS:
            value = _header(inbound, name)
            if value:
                headers[name.title()] = value
        return headers

    def session(self) -> Any:
        """Ein Klient, der weder der Umgebung noch einem Umzug folgt."""
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT,
                                        sock_connect=CONNECT_TIMEOUT)
        return aiohttp.ClientSession(timeout=timeout, trust_env=False)


def _header(inbound: Any, name: str) -> str:
    try:
        return str(inbound.headers.get(name, "") or "").strip()
    except AttributeError:
        return ""


def safe_response_headers(raw: Any) -> dict[str, str]:
    """Die Antwortkoepfe, die der Kaefig sehen darf.

    `set-cookie` faellt weg; die Laengen- und Kodierungskoepfe ebenfalls, weil
    der Broker den Rumpf als Strom neu rahmt und eine geerbte
    `content-length` dann nicht mehr stimmt.
    """
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key.lower() in STRIPPED_RESPONSE_HEADERS:
            continue
        out[key] = value
    return out
