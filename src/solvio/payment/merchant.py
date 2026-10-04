"""Wer das Geld bekommt — und warum ein Anzeigename das nie beantwortet.

„Amazon" ist kein Haendler, sondern ein Wort. `amazon.de` und
`amazon.de.angreifer.example` tragen beide dieses Wort, und ein Mensch, der auf
einem Telefon „Amazon" liest, hat den Unterschied nicht gesehen.

Deshalb hat ein Haendler hier IMMER zwei Teile:

    merchant_id      die Kennung in SOLVIOs eigener Liste (`amazon-de`)
    merchant_origin  die exakte Herkunft (`https://www.amazon.de`)

Gebunden wird die HERKUNFT. Der Anzeigename kommt aus der eigenen Liste und nie
von der Seite — sonst waere er genau das, was er hier verhindern soll: ein von
aussen bestimmter Text unter einer Freigabe.

**Punycode vor jedem Vergleich.** `аmazon.de` mit kyrillischem „а" ist ein
anderer Host und sieht identisch aus. `idna`-Kodierung macht den Unterschied
sichtbar, bevor verglichen wird; was sich nicht kodieren laesst, ist kein
Haendler.

**Kein Praefix-, Suffix- oder Teilstringvergleich.** Nur Gleichheit auf der
ganzen kanonischen Herkunft. Jede Bequemlichkeit an dieser Stelle ist die
Bequemlichkeit eines Angreifers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Kennungen in SOLVIOs Liste. Dieselbe Namensform wie ueberall sonst.
_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

#: Nur https. Eine Zahlung ueber eine unverschluesselte Verbindung ist kein
#: Randfall, den man abfangen muss — sie ist ein Nein.
_SCHEME = "https"

_DEFAULT_PORT = 443


class InvalidMerchant(ValueError):
    """Der Haendler ist keiner. Traegt nie den Eingabetext."""


def normalize_origin(text: str) -> str:
    """Die kanonische Herkunft: `https://host[:port]`, punycodiert, ohne Pfad.

    Absichtlich streng. Ein Pfad, eine Abfrage, ein Fragment, ein Benutzerteil
    (`https://amazon.de@angreifer.example`) oder ein anderes Schema sind keine
    Toleranzfaelle, sondern die bekannten Verwechslungen.
    """
    if not isinstance(text, str):
        raise InvalidMerchant("origin must be a string")
    raw = text.strip()
    if not raw or len(raw) > 255:
        raise InvalidMerchant("origin has an implausible length")
    if "\\" in raw or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise InvalidMerchant("origin contains a control character")
    parts = urlsplit(raw)
    if parts.scheme.lower() != _SCHEME:
        raise InvalidMerchant("origin must be https")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise InvalidMerchant("origin must not carry a path, query or fragment")
    if parts.username or parts.password:
        raise InvalidMerchant("origin must not carry userinfo")
    host = parts.hostname or ""
    if not host:
        raise InvalidMerchant("origin has no host")
    if host.endswith("."):
        # Der absolute Punkt ist derselbe Host und waere ein zweiter Schluessel.
        host = host[:-1]
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError) as exc:
        raise InvalidMerchant("host is not a valid international domain") from exc
    if not ascii_host or ".." in ascii_host:
        raise InvalidMerchant("host is not a valid domain")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidMerchant("port is not a number") from exc
    if port is None or port == _DEFAULT_PORT:
        return f"{_SCHEME}://{ascii_host}"
    if not (0 < port < 65536):
        raise InvalidMerchant("port is out of range")
    return f"{_SCHEME}://{ascii_host}:{port}"


def same_origin(left: str, right: str) -> bool:
    """Gleichheit auf der kanonischen Herkunft. Nichts anderes zaehlt."""
    try:
        return normalize_origin(left) == normalize_origin(right)
    except InvalidMerchant:
        return False


@dataclass(frozen=True)
class Merchant:
    """Ein Haendler aus SOLVIOs eigener Liste. Der Anzeigename ist Kernwahrheit."""

    merchant_id: str
    display_name: str
    origin: str

    def __post_init__(self) -> None:
        if not isinstance(self.merchant_id, str) or not _ID.match(self.merchant_id):
            raise InvalidMerchant("merchant id is not a valid name")
        if not isinstance(self.display_name, str) or not (0 < len(self.display_name) <= 80):
            raise InvalidMerchant("merchant display name has an implausible length")
        object.__setattr__(self, "origin", normalize_origin(self.origin))


#: DIE LISTE. Serverseitig, statisch, vom Modell unerreichbar — dieselbe
#: Haltung wie die Klassenregistry der Freigabepolitik. Ein Haendler, der hier
#: nicht steht, ist kein Haendler; es gibt keinen Weg, zur Laufzeit einen
#: hinzuzufuegen, und das ist der Punkt.
#:
#: `sandbox-shop` ist der Pruefhaendler des Testanbieters. Er steht hier, damit
#: die Abnahme denselben Weg nimmt wie ein echter Kauf — nicht einen kuerzeren.
MERCHANTS: dict[str, Merchant] = {
    m.merchant_id: m for m in (
        Merchant("sandbox-shop", "SOLVIO Testladen", "https://sandbox.solvio.invalid"),
    )
}


def merchant_for_id(merchant_id: str) -> Merchant | None:
    if not isinstance(merchant_id, str):
        return None
    return MERCHANTS.get(merchant_id.strip().lower())


def merchant_for_origin(origin: str) -> Merchant | None:
    """Welcher Haendler ist das? Entscheidet die HERKUNFT, nie die Seite."""
    try:
        canonical = normalize_origin(origin)
    except InvalidMerchant:
        return None
    for merchant in MERCHANTS.values():
        if merchant.origin == canonical:
            return merchant
    return None


def resolve(merchant_id: str, origin: str) -> Merchant:
    """Kennung UND Herkunft muessen zusammen auf denselben Eintrag zeigen.

    Zwei Wege auf dieselbe Zeile, und beide muessen stimmen. Wer nur die Kennung
    prueft, laesst eine fremde Herkunft unter bekanntem Namen durch; wer nur die
    Herkunft prueft, laesst eine fremde Kennung mit passender Herkunft durch.
    """
    by_id = merchant_for_id(merchant_id)
    if by_id is None:
        raise InvalidMerchant("unknown merchant")
    if by_id.origin != normalize_origin(origin):
        raise InvalidMerchant("merchant origin does not match the registered merchant")
    return by_id
