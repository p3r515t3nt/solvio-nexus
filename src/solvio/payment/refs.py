"""Der Verweis auf ein Zahlungsmittel — und warum er selbst keine Befugnis ist.

Ein Modell bekommt `payment://shopping/default` zu sehen und nie eine Kartennummer,
nie eine Pruefziffer, nie einen Anbieter-Token. Das ist der Satz, an dem dieser
Milestone haengt, und dieses Modul ist die Stelle, an der er syntaktisch wird.

**Warum nicht `secret://`.** Ein Zahlungsmittel ist kein Geheimnistyp. Der Tresor
schuetzt Zugangsdaten, die ein Executor braucht; die Zahlungsbefugnis ist eine
eigene Autoritaetsdomaene mit eigener Buchhaltung — Absicht, Haendler, Betrag,
Waehrung, Freigabe, Ausfuehrungszustand, Beleg. Zwei Schemata, die sich nicht
ineinander uebersetzen lassen, sind der strukturelle Grund, warum ein
Tresor-Verweis nie zu einer Zahlungsbefugnis werden kann und umgekehrt
(ADR-0026 in `docs/decisions/`).

Drei Eigenschaften, dieselben wie beim Tresor-Verweis und aus denselben Gruenden:

**Er ist undurchsichtig.** Aus `payment://shopping/default` folgt, DASS es ein
Zahlungsmittel gibt, nie WELCHES. Was ihn traegt, ist sagbar, protokollierbar und
anzeigbar — im Gegensatz zu allem, was er bezeichnet.

**Er ist kanonisch.** `payment://Shopping/Default` und `payment://shopping/default`
duerfen nicht zwei Eintraege sein: eine Regel, die an einer Schreibweise haengt,
ist die Regel eines Angreifers, der eine zweite findet.

**Er ist Metadaten, nie Befugnis.** Der Name sagt nicht, wofuer bezahlt werden
darf. Das steht in der Zahlungsmittel-Politik und nur dort
(`solvio.payment.instruments`). Wer Befugnis an einem lesbaren Namen festmacht,
hat eine Zugriffskontrolle gebaut, die man umbenennen kann.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Das Schema. Bewusst weder `https:` noch `secret:` — ein Zahlungsverweis soll
#: auf den ersten Blick als etwas anderes erkennbar sein als eine Adresse, die
#: man abrufen koennte, UND als etwas anderes als ein Tresor-Verweis.
SCHEME = "payment"
PREFIX = SCHEME + "://"

#: Was ein Namensteil sein darf. Wortgleich zur Regel des Tresor-Verweises:
#: Kleinbuchstaben, Ziffern, und `-` `_` `.` in der Mitte. Ein Verweis wandert
#: durch Logzeilen, JSON, SQL und eine iPhone-Oberflaeche.
_PART = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

#: Obergrenze fuer den ganzen Verweis. Kein Sicherheitsmerkmal, sondern eine
#: Zusage an alles, was ihn speichert.
MAX_LENGTH = 160


class InvalidPaymentRef(ValueError):
    """Der Verweis ist keiner. Traegt nie einen Wert und nie den Eingabetext."""


@dataclass(frozen=True, order=True)
class PaymentRef:
    """`payment://<zweck>/<name>` — zwei Namensteile, sonst nichts.

    `zweck` ist die Verwendung („shopping"), `name` das konkrete Mittel
    („default"). Beide sind vom Eigentuemer vergeben und sagen ueber das
    dahinterliegende Material nichts aus.
    """

    purpose: str
    name: str

    def __post_init__(self) -> None:
        if not _PART.match(self.purpose):
            raise InvalidPaymentRef("purpose part is not a valid name")
        if not _PART.match(self.name):
            raise InvalidPaymentRef("name part is not a valid name")
        if len(str(self)) > MAX_LENGTH:
            raise InvalidPaymentRef("reference is too long")

    def __str__(self) -> str:
        return f"{PREFIX}{self.purpose}/{self.name}"

    def __repr__(self) -> str:  # damit ein Debug-Print nie mehr zeigt als der Verweis
        return f"PaymentRef({str(self)!r})"


def parse(text: str) -> PaymentRef:
    """Liest einen Verweis. Normalisiert, bevor er irgendwo verglichen wird.

    Absichtlich streng: `payment://a/b/c` ist kein Verweis mit einem Pfad,
    sondern ein Fehler. Und ein `secret://`-Verweis ist hier KEIN Verweis —
    die beiden Schemata sind nicht ineinander ueberfuehrbar, und genau das ist
    der Punkt.
    """
    if not isinstance(text, str):
        raise InvalidPaymentRef("reference must be a string")
    stripped = text.strip()
    if len(stripped) > MAX_LENGTH:
        raise InvalidPaymentRef("reference is too long")
    lowered = stripped.lower()
    if not lowered.startswith(PREFIX):
        raise InvalidPaymentRef("reference does not start with " + PREFIX)
    body = lowered[len(PREFIX):]
    parts = body.split("/")
    if len(parts) != 2:
        raise InvalidPaymentRef("reference must have exactly one purpose and one name")
    return PaymentRef(purpose=parts[0], name=parts[1])


def is_valid(text: str) -> bool:
    try:
        parse(text)
    except InvalidPaymentRef:
        return False
    return True
