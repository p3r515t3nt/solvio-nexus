"""Geheimnisse aus allem tilgen, was das Modell zu sehen bekommt.

Zwei Schichten, und die erste ist die wichtigere.

**Strukturell.** Der Wert eines Passwortfeldes wird gar nicht erst gelesen. Die
Seitenskripte holen sichtbaren Text und zugaengliche Namen; der Inhalt eines
`input[type=password]` oder `input[type=hidden]` gehoert zu keinem von beidem.
Was nie eingesammelt wird, muss auch nicht entfernt werden.

**Nachtraeglich.** Eine Seite kann ein eingegebenes Geheimnis trotzdem
zurueckspiegeln — in einer Fehlermeldung, in einer Bestaetigungszeile, in einem
`title`. Dagegen haelt dieses Modul die Werte, die in dieser Sitzung tatsaechlich
eingesetzt wurden, und streicht sie aus allem, was hinausgeht.

Ehrlich zur Reichweite: das ist Nachsorge, keine Garantie. Eine Seite, die ein
Geheimnis zeichenweise zerlegt oder umkodiert wieder ausgibt, entkommt jeder
Textersetzung. Die Garantie liegt woanders — das Geheimnis erreicht nie das
Modell, nie einen Prompt, nie ein Werkzeugargument und nie ein Protokoll, weil
es diesen Weg gar nicht erst nimmt.

Ein Detail, das man einmal falsch macht: sehr kurze Werte werden **nicht**
aufgenommen. Ein dreistelliges Geheimnis wuerde die halbe Seite schwaerzen und
das Ergebnis unbrauchbar machen — und ein dreistelliges Geheimnis ist ohnehin
keines.
"""
from __future__ import annotations

import html
import urllib.parse
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("portal")

#: Was an die Stelle eines Geheimnisses tritt.
MASK = "«redigiert»"

#: Kuerzere Werte werden nicht getilgt — sie kaemen zu oft zufaellig vor.
MIN_SECRET = 6

#: Feldarten, deren Inhalt niemals eingesammelt wird.
NEVER_READ_TYPES = ("password", "hidden")


class SecretRedactor:
    """Haelt die Geheimnisse einer Sitzung — und gibt sie nie heraus."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._skipped = 0

    def remember(self, value: str) -> bool:
        """Merkt einen eingesetzten Wert zum spaeteren Tilgen.

        Der Rueckgabewert sagt, ob er aufgenommen wurde — nicht, welcher es war.
        """
        text = value or ""
        if len(text) < MIN_SECRET:
            self._skipped += 1
            log.info("portal.secret_too_short_to_redact", skipped=self._skipped)
            return False
        self._values.add(text)
        return True

    def scrub(self, value: Any) -> Any:
        """Streicht alle bekannten Geheimnisse — auch in ueblichen Kodierungen."""
        if isinstance(value, str):
            return self._scrub_text(value)
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            scrubbed = [self.scrub(v) for v in value]
            return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
        return value

    def _scrub_text(self, text: str) -> str:
        if not self._values or not text:
            return text
        result = text
        for secret in self._values:
            for shape in self._shapes(secret):
                if shape and shape in result:
                    result = result.replace(shape, MASK)
        return result

    @staticmethod
    def _shapes(secret: str) -> tuple[str, ...]:
        """Dieselbe Zeichenkette in den Formen, in denen eine Seite sie ausgibt."""
        return (secret,
                html.escape(secret),
                urllib.parse.quote(secret, safe=""),
                urllib.parse.quote_plus(secret))

    def contains_secret(self, text: str) -> bool:
        """Nur fuer Tests und Selbstpruefung. Sagt ja/nein, nie welchen."""
        return any(shape in (text or "")
                   for secret in self._values for shape in self._shapes(secret))

    def clear(self) -> None:
        self._values.clear()

    @property
    def held(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        # Kein `__repr__`, der irgendetwas verraet. Ein Objekt landet schneller in
        # einem Protokoll oder einer Fehlermeldung, als einem lieb ist.
        return f"<SecretRedactor holding={len(self._values)}>"
