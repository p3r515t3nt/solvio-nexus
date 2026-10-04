"""Der Verweis auf ein Geheimnis — und warum er selbst keines ist.

Ein Agent bekommt `secret://amazon/gregor` zu sehen und nie einen Wert. Das ist
die ganze Idee dieses Milestones, und dieses Modul ist die Stelle, an der sie
syntaktisch wird.

Drei Eigenschaften, die ein Verweis haben muss, damit er trägt:

**Er ist undurchsichtig.** Aus `secret://amazon/gregor` folgt, DASS es einen
Zugang gibt, nie WELCHER. Ein Verweis ist damit sagbar, protokollierbar und
anzeigbar — im Gegensatz zu allem, was er bezeichnet.

**Er ist kanonisch.** `secret://Amazon/Gregor` und `secret://amazon/gregor`
dürfen nicht zwei Einträge sein: eine Regel, die an einer Schreibweise hängt,
ist die Regel eines Angreifers, der eine zweite findet. Deshalb wird
normalisiert, bevor irgendetwas verglichen wird.

**Er ist Metadaten, nie Befugnis.** Der Name sagt nicht, wofür das Geheimnis
benutzt werden darf. Das steht in der Policy und nur dort — siehe
`solvio.secret_vault.policy`. Wer Zugriff an einem lesbaren Namen festmacht, hat eine
Zugriffskontrolle gebaut, die man umbenennen kann.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Das Schema. Bewusst kein `https:` und kein Pfad, der wie eine URL aussieht —
#: ein Verweis soll auf den ersten Blick als etwas anderes erkennbar sein als
#: eine Adresse, die man abrufen könnte.
SCHEME = "secret"
PREFIX = SCHEME + "://"

#: Was ein Namensteil sein darf. Kleinbuchstaben, Ziffern, und `-` `_` `.` in
#: der Mitte. Kein Schrägstrich (er trennt), kein Leerzeichen, kein `@`, keine
#: Umlaute: ein Verweis wandert durch Logzeilen, JSON, SQL und eine iPhone-
#: Oberfläche, und jede dieser Stationen hat ihre eigenen Sonderzeichen.
_PART = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

#: Obergrenze für den ganzen Verweis. Kein Sicherheitsmerkmal, sondern eine
#: Zusage an alles, was ihn speichert.
MAX_LENGTH = 160


class InvalidSecretRef(ValueError):
    """Der Verweis ist keiner. Trägt nie einen Wert und nie den Eingabetext."""


@dataclass(frozen=True, order=True)
class SecretRef:
    """`secret://<dienst>/<konto>` — zwei Namensteile, sonst nichts."""

    service: str
    account: str

    def __post_init__(self) -> None:
        if not _PART.match(self.service):
            raise InvalidSecretRef("service part is not a valid name")
        if not _PART.match(self.account):
            raise InvalidSecretRef("account part is not a valid name")
        if len(str(self)) > MAX_LENGTH:
            raise InvalidSecretRef("reference is too long")

    def __str__(self) -> str:
        return f"{PREFIX}{self.service}/{self.account}"

    def __repr__(self) -> str:  # damit ein Debug-Print nie mehr zeigt als der Verweis
        return f"SecretRef({str(self)!r})"


def parse(text: str) -> SecretRef:
    """Liest einen Verweis. Normalisiert, bevor er irgendwo verglichen wird.

    Absichtlich streng: `secret://a/b/c` ist kein Verweis mit einem Pfad, sondern
    ein Fehler. Wer hier tolerant ist, erlaubt zwei Schreibweisen für dieselbe
    Sache — und eine davon wird irgendwann an einer Policy vorbeirutschen.
    """
    if not isinstance(text, str):
        raise InvalidSecretRef("reference must be a string")
    stripped = text.strip()
    if len(stripped) > MAX_LENGTH:
        raise InvalidSecretRef("reference is too long")
    lowered = stripped.lower()
    if not lowered.startswith(PREFIX):
        raise InvalidSecretRef("reference does not start with " + PREFIX)
    body = lowered[len(PREFIX):]
    parts = body.split("/")
    if len(parts) != 2:
        raise InvalidSecretRef("reference must have exactly one service and one account")
    return SecretRef(service=parts[0], account=parts[1])


def is_valid(text: str) -> bool:
    try:
        parse(text)
    except InvalidSecretRef:
        return False
    return True
