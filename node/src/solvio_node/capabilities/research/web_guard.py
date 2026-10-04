"""SSRF / egress guard for research.fetch_public_url.

Security strategy (see docs/PUBLIC_WEB_FETCH.md), aligned with the OWASP SSRF
Prevention Cheat Sheet:

  * URL is parsed/normalised with yarl (never a hand-rolled regex). Only http/https,
    only ports 80/443, no embedded credentials, no zone ids, host required.
  * EVERY resolved address is validated as globally-routable public
    (ipaddress.is_global + explicit attribute checks + an explicit CIDR denylist,
    because a denylist alone is insufficient per OWASP). Fail-closed on ANY
    disallowed address (defeats mixed public/private DNS answers).
  * A custom aiohttp resolver returns ONLY validated addresses and records them, and
    DNS caching is disabled, so the IP actually connected to is an IP that was just
    validated — this is the DNS-rebinding / TOCTOU defense.

No message ever contains the URL, host, or IP (payload-free): the node runtime
surfaces only the exception TYPE name.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from yarl import URL

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
_DEFAULT_PORT = {"http": 80, "https": 443}


class WebFetchError(Exception):
    """Base for all fetch/guard errors (surfaced to the core as a type name)."""


class InvalidUrl(WebFetchError):
    pass


class SchemeNotAllowed(WebFetchError):
    pass


class PortNotAllowed(WebFetchError):
    pass


class SsrfBlocked(WebFetchError):
    pass


class DnsResolutionError(WebFetchError):
    pass


# Explicit denylist (belt-and-suspenders on top of is_global). Non-global,
# special-purpose, and metadata-adjacent ranges for IPv4 and IPv6.
_DENY_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
        "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
        "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
        "::1/128", "::/128", "::ffff:0:0/96", "64:ff9b:1::/48", "100::/64",
        "2001:db8::/32", "2001::/23", "fc00::/7", "fe80::/10", "ff00::/8",
    )
)


def ip_is_public(ip_str: str) -> bool:
    """True only for a globally-routable public unicast address. Fail-closed."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # Unwrap IPv4-embedded IPv6 forms and validate the underlying IPv4.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    if not ip.is_global:
        return False
    for net in _DENY_NETS:
        if ip.version == net.version and ip in net:
            return False
    return True


@dataclass
class ValidatedTarget:
    url: URL
    scheme: str
    host: str
    port: int


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def validate_url(raw: str) -> ValidatedTarget:
    """Validate + normalise a URL. Raises a WebFetchError subclass on any problem."""
    try:
        url = URL(str(raw))
    except (ValueError, TypeError):
        raise InvalidUrl("unparseable url") from None
    scheme = (url.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SchemeNotAllowed("scheme not allowed")
    if url.user is not None or url.password is not None:
        raise InvalidUrl("credentials not allowed in url")
    host = url.host
    if not host:
        raise InvalidUrl("missing host")
    if "%" in host:  # IPv6 zone identifier
        raise InvalidUrl("zone id not allowed")
    # Explicit port only; anything other than 80/443 (incl. an explicit 0) is out.
    explicit = url.explicit_port
    port = explicit if explicit is not None else _DEFAULT_PORT[scheme]
    if port not in ALLOWED_PORTS:
        raise PortNotAllowed("port not allowed")
    # IP literal -> validate now (fail fast). Hostname -> IDNA-normalise to a single
    # canonical ASCII/punycode form used for both resolution and TLS.
    if _is_ip_literal(host):
        if not ip_is_public(host):
            raise SsrfBlocked("non-public ip literal")
    else:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, UnicodeDecodeError):
            raise InvalidUrl("invalid international domain name") from None
        url = url.with_host(host)
    return ValidatedTarget(url=url, scheme=scheme, host=host, port=port)


class PublicOnlyResolver(AbstractResolver):
    """aiohttp resolver that only ever returns validated public addresses.

    Fail-closed: if ANY address a host resolves to is non-public, the whole
    resolution raises SsrfBlocked (so a mixed public/private answer is rejected).
    Validated addresses are recorded so the connected peer can be re-checked.
    """

    def __init__(self, *, policy=ip_is_public, base: AbstractResolver | None = None,
                 dns_timeout: float = 5.0) -> None:
        self._policy = policy
        self._base = base if base is not None else ThreadedResolver()
        self._dns_timeout = dns_timeout
        self.validated: set[str] = set()

    async def resolve(self, host: str, port: int = 0,
                      family: int = socket.AF_UNSPEC):
        try:
            infos = await asyncio.wait_for(
                self._base.resolve(host, port, family), self._dns_timeout)
        except asyncio.TimeoutError:
            raise DnsResolutionError("dns timeout") from None
        except (OSError, socket.gaierror):
            raise DnsResolutionError("dns resolution failed") from None
        if not infos:
            raise DnsResolutionError("no dns result")
        for info in infos:
            if not self._policy(info["host"]):
                raise SsrfBlocked("host resolved to non-public address")
        for info in infos:
            self.validated.add(info["host"])
        return infos

    async def close(self) -> None:
        await self._base.close()
