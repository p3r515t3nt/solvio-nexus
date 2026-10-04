"""Ein kurzes, fluechtiges Fenster Audio — damit der Satzanfang nicht verschwindet.

Gemessen an 44 echten Sitzungen vergingen zwischen „der Satellit ist da" und
„der Anbieter kann hoeren" **530 bis 1675 ms** (Median 1111). In genau diesem
Fenster warf `feed_audio` jedes Frame weg, weil die Sitzung noch nicht `active`
war. Elf von 42 Sitzungen endeten mit `turns=0` — der Mensch sprach, und es kam
nie etwas an.

Dieses Modul ist die Gegenmassnahme, und es ist absichtlich sehr klein. Es haelt
rohes PCM im Arbeitsspeicher, in Reihenfolge, mit einer harten Obergrenze — und
sonst nichts. Was es NICHT kann, ist die eigentliche Zusage:

* Es schreibt nichts auf die Platte. Es gibt keinen Pfad, keine Datei, keine
  Serialisierung.
* Es hat kein `__repr__` und keine `__str__`, die Nutzdaten zeigen wuerden —
  ein Puffer, der in einem Stacktrace auftaucht, verraet sonst genau das, was
  er schuetzen soll.
* Es kennt weder Hermes noch das Gedaechtnis noch das Kontrollzentrum. Niemand
  kann es von dort erreichen, weil es niemandem gehoert ausser der einen
  Sitzung.
* Es wird geleert, wenn die Sitzung schliesst, wenn das Oeffnen scheitert und
  wenn der Prozess endet — der letzte Fall von selbst, weil es nichts gibt, das
  ihn ueberdauert.

Die Obergrenze ist in Sekunden gedacht, nicht in Frames: eine Framezahl haengt
an der Framegroesse des Satelliten und ist damit eine Zusage, die jemand anders
einhalten muesste.

Laeuft der Puffer ueber, fallen die AELTESTEN Bytes heraus. Das ist die
unangenehmere der beiden Moeglichkeiten — sie kostet ausgerechnet den
Satzanfang — aber die Alternative waere, den gerade gesprochenen Satz zu
verlieren und einen alten zu behalten. Ein Ueberlauf ist ohnehin ein Defekt und
wird als solcher gemeldet: bei der gemessenen Obergrenze von 1,7 s ist ein
Fenster von mehreren Sekunden nicht knapp, sondern grosszuegig.
"""
from __future__ import annotations

from collections import deque

#: Wie lange das Fenster hoechstens reicht.
#:
#: Gemessen war das schlimmste Oeffnen 1675 ms. Vier Sekunden geben dem das
#: Doppelte an Luft und bleiben trotzdem so kurz, dass der Puffer nie zu einer
#: Aufzeichnung des Raumes wird — worauf es hier mehr ankommt als auf Reserve.
DEFAULT_SECONDS = 4.0

#: Rohes PCM, 16 bit, ein Kanal.
SAMPLE_WIDTH = 2


class Preroll:
    """Rohes PCM in Reihenfolge, im Arbeitsspeicher, mit harter Obergrenze."""

    __slots__ = ("_frames", "_bytes", "_budget", "_rate", "_dropped", "_seconds")

    def __init__(self, *, rate: int, seconds: float = DEFAULT_SECONDS) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        self._rate = rate
        self._seconds = seconds
        self._budget = int(rate * seconds) * SAMPLE_WIDTH
        self._frames: deque[bytes] = deque()
        self._bytes = 0
        #: Wie viele Bytes wegen der Obergrenze verloren gingen. Ein Zaehler,
        #: keine Nutzdaten — er darf ins Log.
        self._dropped = 0

    # -- Fuellen und leeren ----------------------------------------------------

    def add(self, pcm: bytes) -> None:
        """Ein Frame anhaengen. Ueber der Grenze fallen die aeltesten heraus.

        Verworfen werden immer GANZE Frames, nie einzelne Bytes. Auf die Grenze
        genau zuzuschneiden waere sparsamer und wuerde die 16-Bit-Ausrichtung
        zerstoeren: ein um ein Byte verschobener Strom ist kein leiserer Ton,
        sondern Rauschen. Der Puffer haelt dadurch etwas weniger als das Budget,
        nie mehr.
        """
        if not pcm:
            return
        self._frames.append(pcm)
        self._bytes += len(pcm)
        while self._bytes > self._budget and self._frames:
            oldest = self._frames.popleft()
            self._bytes -= len(oldest)
            self._dropped += len(oldest)

    def drain(self) -> bytes:
        """Alles in Reihenfolge herausgeben — und dabei leeren.

        Das Leeren gehoert ausdruecklich hierher und nicht in einen zweiten
        Aufruf. Ein Puffer, der nach dem Auslesen noch voll ist, wird beim
        naechsten Mal ein zweites Mal gesendet; doppeltes Audio ist fuer den
        Anbieter nicht von einem wiederholten Satz zu unterscheiden.
        """
        if not self._frames:
            return b""
        joined = b"".join(self._frames)
        self.clear()
        return joined

    def clear(self) -> None:
        """Vergessen. Der einzige Weg, wie Audio dieses Modul verlaesst, ist
        `drain` — alles andere endet hier."""
        self._frames.clear()
        self._bytes = 0

    # -- Auskunft, ohne Nutzdaten ---------------------------------------------

    @property
    def empty(self) -> bool:
        return not self._frames

    @property
    def held_bytes(self) -> int:
        return self._bytes

    @property
    def held_seconds(self) -> float:
        return self._bytes / (self._rate * SAMPLE_WIDTH)

    @property
    def dropped_bytes(self) -> int:
        return self._dropped

    @property
    def overflowed(self) -> bool:
        """Ob je etwas wegen der Obergrenze verloren ging.

        Ueberlebt `clear()` bewusst: das ist eine Aussage ueber die Sitzung,
        nicht ueber den aktuellen Inhalt, und sie soll am Ende noch berichtbar
        sein.
        """
        return self._dropped > 0

    @property
    def budget_seconds(self) -> float:
        return self._seconds

    def stats(self) -> dict[str, float | int | bool]:
        """Was ins Log darf: Mengen, nie Inhalt."""
        return {"preroll_ms": int(self.held_seconds * 1000),
                "preroll_dropped_ms": int(self._dropped
                                          / (self._rate * SAMPLE_WIDTH) * 1000),
                "preroll_overflowed": self.overflowed}
