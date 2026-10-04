"""Wohin der Browser darf — und wohin unter keinen Umstaenden.

Ein Browser ist das erste Werkzeug in SOLVIO, das fremden Code ausfuehrt. Eine
Webseite bestimmt, was er als Naechstes anfordert: per Weiterleitung, per
`<img src>`, per `fetch()`. Damit ist die Frage nicht mehr „welche URL hat der
Nutzer genannt", sondern „welche Ziele koennen ueberhaupt erreicht werden".

Im selben Netz stehen: der Core (8766), der Freigabeweg (8770), der tiefe
Executor (8791), Home Assistant, der Router, ein VPN. Nichts davon verlangt eine
Anmeldung, die eine Webseite nicht auch mitschicken koennte — also darf die
Anfrage gar nicht erst hinausgehen.

Drei Schichten, weil keine allein reicht:

1. **Vor dem Aufruf** — Schema, Form, und die Namensaufloesung. Hier faellt auch
   `http://2130706433/` durch: als Zahl ist das keine erkennbare Adresse, aber
   der Resolver macht `127.0.0.1` daraus. Deshalb wird geprueft, was
   herauskommt, nicht was dasteht.
2. **Im Browser** — jede einzelne Anfrage wird abgefangen. Nur so ist eine
   Weiterleitung zu fassen: die kennt niemand vorher, und sie ist der bequemste
   Weg, einen Browser ins lokale Netz zu schicken.
3. **Nach der Navigation** — die tatsaechlich erreichte Adresse wird noch einmal
   angesehen.

Eine Anmerkung zur Namensaufloesung: geprueft werden **alle** zurueckgegebenen
Adressen, nicht die erste. Ein Name, der auf eine oeffentliche und eine private
Adresse zeigt, ist der Standardtrick gegen Pruefungen, die nach dem ersten
Treffer aufhoeren.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Nur diese beiden. `file:` liest die Platte, `data:`/`javascript:` fuehren
#: Inhalt aus, den jemand anderes gewaehlt hat, `chrome:`/`devtools:` oeffnen die
#: Innereien des Browsers.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Namen, die per Definition nach Hause zeigen. Die Adresspruefung faengt sie
#: ohnehin — hier stehen sie, damit die Ablehnung ohne Netzverkehr passiert.
BLOCKED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata.google.internal", "metadata", "instance-data",
})

#: Endungen, die zu lokalen Netzen gehoeren. `.local` ist mDNS, `.internal` und
#: `.home` sind Heimnetz-Konventionen, `.arpa` ist Infrastruktur.
BLOCKED_SUFFIXES = (".local", ".localdomain", ".internal", ".home", ".lan", ".arpa")

MAX_URL = 2048


@dataclass(frozen=True)
class UrlVerdict:
    """Das Ergebnis einer Pruefung. `reason` ist leer, wenn die URL passt."""

    url: str
    allowed: bool
    reason: str = ""
    host: str = ""
    addresses: tuple[str, ...] = ()

    @property
    def blocked_locally(self) -> bool:
        return self.reason == "private_network_blocked"


def _unmap(address: ipaddress._BaseAddress):
    """IPv4-in-IPv6 entpacken. `::ffff:127.0.0.1` ist 127.0.0.1."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address.sixtofour is not None:
            return address.sixtofour
    return address


def address_is_local(text: str) -> bool:
    """Zeigt diese Adresse ins eigene oder ein privates Netz?

    Bewusst grosszuegig abgelehnt: privat, Loopback, link-local (und damit die
    Metadaten-Adresse 169.254.169.254), Multicast, reserviert, unspezifiziert und
    Carrier-NAT. Eine oeffentliche Seite, die aus Versehen unter eine dieser
    Adressen faellt, ist ein verkraftbarer Verlust; das Gegenteil nicht.
    """
    try:
        address = _unmap(ipaddress.ip_address(text))
    except ValueError:
        return False
    if address.is_private or address.is_loopback or address.is_link_local:
        return True
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        # 100.64.0.0/10 — Carrier-NAT. In manchen Python-Versionen nicht in
        # `is_private`, und ein Heimrouter kann darin stehen.
        return address in ipaddress.ip_network("100.64.0.0/10")
    return False


def resolve(host: str) -> list[str]:
    """Alle Adressen zu einem Namen. Leer, wenn er nicht aufloest."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return []
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def check(url: str, *, resolver=resolve) -> UrlVerdict:
    """Darf der Browser dorthin? Fail-closed bei allem Unklaren.

    `resolver` ist austauschbar, damit Tests die Namensaufloesung bestimmen
    koennen, ohne ein Netz zu brauchen — die Rebinding-Faelle sind sonst nicht
    zuverlaessig herstellbar.
    """
    raw = (url or "").strip()
    if not raw:
        return UrlVerdict(raw, False, "invalid_url")
    if len(raw) > MAX_URL:
        return UrlVerdict(raw[:80], False, "invalid_url")
    if any(ch in raw for ch in ("\n", "\r", "\t", " ")):
        return UrlVerdict(raw[:80], False, "invalid_url")

    try:
        parts = urlsplit(raw)
    except ValueError:
        return UrlVerdict(raw[:80], False, "invalid_url")

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        # `file:`, `chrome:`, `data:`, `javascript:`, `about:`, `blob:`,
        # `view-source:` und alles Unbekannte landen hier. Eine Erlaubnisliste,
        # damit ein neues Schema nicht automatisch erlaubt ist.
        return UrlVerdict(raw[:120], False, "invalid_url")

    try:
        host = (parts.hostname or "").lower().strip(".")
    except ValueError:
        return UrlVerdict(raw[:120], False, "invalid_url")
    if not host:
        return UrlVerdict(raw[:120], False, "invalid_url")

    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        return UrlVerdict(raw[:120], False, "private_network_blocked", host)

    if address_is_local(host.strip("[]")):
        return UrlVerdict(raw[:120], False, "private_network_blocked", host)

    addresses = resolver(host)
    if not addresses:
        return UrlVerdict(raw[:120], False, "navigation_failed", host)
    # ALLE, nicht die erste: ein Name, der auf eine oeffentliche und eine private
    # Adresse zeigt, ist genau der Trick, den eine Pruefung mit `any()` verpasst.
    for address in addresses:
        if address_is_local(address):
            return UrlVerdict(raw[:120], False, "private_network_blocked", host,
                              tuple(addresses))
    return UrlVerdict(raw, True, "", host, tuple(addresses))
