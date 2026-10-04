"""Wann eine Aufgabe faellig ist — in Ortszeit, nicht in Sekunden seit 1970.

Der bequeme Weg waere, „jeden Morgen um 7:30" als 86400 Sekunden Abstand zu
speichern. Er ist zweimal im Jahr falsch: bei der Zeitumstellung verschiebt sich
alles um eine Stunde, und aus „halb acht" wird „halb sieben" oder „halb neun".
Wer das einmal erlebt hat, speichert danach die **Wanduhrzeit** und rechnet die
naechste Gelegenheit jedes Mal neu aus.

Zwei Sonderfaelle, die im Fruehjahr und Herbst wirklich auftreten:

* **Die Stunde, die es nicht gibt.** In der Nacht der Umstellung springt die Uhr
  von 2:00 auf 3:00. „Jeden Tag um 2:30" hat an diesem Tag keine Entsprechung.
  Ein naiver Rechner liefert dann entweder gar nichts oder — schlimmer — still
  einen Zeitpunkt am Vortag. Hier wird auf den ersten gueltigen Augenblick
  danach gelegt.
* **Die Stunde, die es zweimal gibt.** Im Herbst laeuft 2:30 zweimal. Genommen
  wird die erste; sonst liefe die Aufgabe zweimal.

Ausserdem: **verpasste Gelegenheiten werden nicht nachgeholt.** War der Rechner
zwei Wochen aus, will niemand vierzehn Morgenberichte. Hoechstens einer, und nur
wenn er noch etwas bedeutet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

#: Die Zeitzone des Nutzers. Eine Angabe, kein Rechenwert — die Umstellung
#: steckt in der Zone, nicht in einer Sekundenzahl.
DEFAULT_TZ = "Europe/Berlin"

#: Wie oft eine Beobachtung hoechstens nachsieht. Ein Wachposten, der im
#: Sekundentakt fragt, ist kein Wachposten, sondern ein Angriff auf den Anbieter.
MIN_INTERVAL_SECONDS = 300

#: Wie weit eine Gelegenheit in der Vergangenheit liegen darf und trotzdem noch
#: sinnvoll ist. Ein Morgenbericht um 7:30, der um 9:00 nachgeholt wird, taugt
#: noch etwas; einer um 23:00 nicht mehr.
DEFAULT_GRACE_SECONDS = 3 * 3600


class Kind(str, Enum):
    ONE_SHOT = "one_shot"
    DAILY = "daily"
    WEEKLY = "weekly"
    INTERVAL = "interval"


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    """Ein typisierter Zeitplan. Ausdruecklich kein Cron-Ausdruck.

    Cron waere maechtiger und genau deshalb falsch an dieser Stelle: es waere
    eine Zeichenkette, die das Modell frei fuellen darf, und `* * * * *` ist
    davon nur einen Tippfehler entfernt.
    """

    kind: Kind
    #: Wanduhrzeit fuer DAILY/WEEKLY, z. B. (7, 30).
    hour: int = 0
    minute: int = 0
    #: Wochentage fuer WEEKLY, Montag = 0.
    weekdays: tuple[int, ...] = ()
    #: Absoluter Zeitpunkt fuer ONE_SHOT (Sekunden seit 1970, UTC).
    at_epoch: float = 0.0
    #: Abstand fuer INTERVAL.
    every_seconds: int = MIN_INTERVAL_SECONDS
    timezone: str = DEFAULT_TZ
    grace_seconds: int = DEFAULT_GRACE_SECONDS

    def __post_init__(self) -> None:
        if self.kind in (Kind.DAILY, Kind.WEEKLY):
            if not (0 <= self.hour <= 23 and 0 <= self.minute <= 59):
                raise ScheduleError(f"ungueltige Uhrzeit {self.hour}:{self.minute}")
        if self.kind is Kind.WEEKLY and not self.weekdays:
            raise ScheduleError("woechentlich ohne Wochentag")
        if any(not 0 <= d <= 6 for d in self.weekdays):
            raise ScheduleError("Wochentag ausserhalb 0..6")
        if self.kind is Kind.INTERVAL and self.every_seconds < MIN_INTERVAL_SECONDS:
            raise ScheduleError(
                f"Abstand {self.every_seconds}s unter der Untergrenze "
                f"{MIN_INTERVAL_SECONDS}s")
        if self.kind is Kind.ONE_SHOT and self.at_epoch <= 0:
            raise ScheduleError("einmalig ohne Zeitpunkt")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:  # noqa: BLE001
            raise ScheduleError(f"unbekannte Zeitzone {self.timezone!r}") from exc

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def recurring(self) -> bool:
        return self.kind is not Kind.ONE_SHOT

    # -- Die eigentliche Rechnung -------------------------------------------

    def next_after(self, after_epoch: float) -> float | None:
        """Die naechste Gelegenheit STRENG nach diesem Zeitpunkt, oder `None`.

        Streng nach: sonst liefert eine Aufgabe, die gerade gelaufen ist,
        denselben Zeitpunkt noch einmal und laeuft in einer Schleife.
        """
        if self.kind is Kind.ONE_SHOT:
            return self.at_epoch if self.at_epoch > after_epoch else None
        if self.kind is Kind.INTERVAL:
            return after_epoch + self.every_seconds

        zone = self.zone
        local = datetime.fromtimestamp(after_epoch, zone)
        # 400 Tage: deckt jede Wochentagskombination und beide Umstellungen ab.
        for offset in range(0, 400):
            day = (local + timedelta(days=offset)).date()
            if self.kind is Kind.WEEKLY and day.weekday() not in self.weekdays:
                continue
            moment = self._wall_clock(day, zone)
            if moment is not None and moment > after_epoch:
                return moment
        return None

    def _wall_clock(self, day, zone: ZoneInfo) -> float | None:
        """Der Zeitpunkt zur Wanduhrzeit an diesem Tag — mit beiden Sonderfaellen.

        `fold=0` waehlt bei einer doppelten Stunde die erste. Existiert die
        Stunde gar nicht, weicht Python nicht aus, sondern liefert einen
        Zeitpunkt, dessen Rueckrechnung eine ANDERE Uhrzeit ergibt. Genau daran
        wird die Luecke erkannt — und dann auf den ersten gueltigen Augenblick
        danach gelegt, statt den Tag stillschweigend zu ueberspringen.
        """
        naive = datetime(day.year, day.month, day.day, self.hour, self.minute)
        moment = naive.replace(tzinfo=zone, fold=0)
        stamp = moment.timestamp()
        back = datetime.fromtimestamp(stamp, zone)
        if back.hour == self.hour and back.minute == self.minute:
            return stamp
        # Die Wanduhrzeit gibt es an diesem Tag nicht. Minutenweise vorruecken,
        # bis sie wieder existiert — das ist der Sprungmoment selbst.
        for extra in range(1, 180):
            probe = naive + timedelta(minutes=extra)
            candidate = probe.replace(tzinfo=zone, fold=0)
            reread = datetime.fromtimestamp(candidate.timestamp(), zone)
            if (reread.hour, reread.minute) == (probe.hour, probe.minute):
                return candidate.timestamp()
        return None

    def catch_up(self, missed_since: float, now: float) -> tuple[float | None, int]:
        """Was nach einer Auszeit passiert: hoechstens EIN Lauf, plus die Zahl.

        Gibt zurueck, wann jetzt zu laufen ist (oder `None`) und wie viele
        Gelegenheiten uebersprungen wurden. Siebzehn Morgenberichte nachzuholen
        waere kein Dienst, sondern eine Strafe.
        """
        if missed_since <= 0 or missed_since > now:
            return None, 0
        skipped = 0
        moment: float | None = missed_since
        last_due: float | None = None
        while moment is not None and moment <= now:
            last_due = moment
            skipped += 1
            moment = self.next_after(moment)
            if skipped > 1000:      # Reissleine gegen einen kaputten Plan
                break
        if last_due is None:
            return None, 0
        # Nur wenn die letzte verpasste Gelegenheit noch etwas bedeutet.
        if now - last_due <= self.grace_seconds:
            return last_due, max(0, skipped - 1)
        return None, skipped

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"art": self.kind.value, "zeitzone": self.timezone}
        if self.kind in (Kind.DAILY, Kind.WEEKLY):
            entry["uhrzeit"] = f"{self.hour:02d}:{self.minute:02d}"
        if self.kind is Kind.WEEKLY:
            entry["wochentage"] = list(self.weekdays)
        if self.kind is Kind.ONE_SHOT:
            entry["zeitpunkt"] = self.at_epoch
        if self.kind is Kind.INTERVAL:
            entry["abstand_s"] = self.every_seconds
        return entry

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Schedule":
        kind = Kind(str(data.get("art", data.get("kind", ""))))
        clock = str(data.get("uhrzeit", "00:00"))
        hour, _, minute = clock.partition(":")
        return cls(kind=kind, hour=int(hour or 0), minute=int(minute or 0),
                   weekdays=tuple(data.get("wochentage", ()) or ()),
                   at_epoch=float(data.get("zeitpunkt", 0.0) or 0.0),
                   every_seconds=int(data.get("abstand_s", MIN_INTERVAL_SECONDS)
                                     or MIN_INTERVAL_SECONDS),
                   timezone=str(data.get("zeitzone", DEFAULT_TZ)))


def in_seconds(seconds: float, *, now: float) -> Schedule:
    """„In zwei Stunden" — die haeufigste einmalige Form."""
    return Schedule(kind=Kind.ONE_SHOT, at_epoch=now + max(1.0, seconds))


def daily(hour: int, minute: int = 0, *, timezone: str = DEFAULT_TZ) -> Schedule:
    return Schedule(kind=Kind.DAILY, hour=hour, minute=minute, timezone=timezone)


def weekly(weekdays: tuple[int, ...], hour: int, minute: int = 0, *,
           timezone: str = DEFAULT_TZ) -> Schedule:
    return Schedule(kind=Kind.WEEKLY, hour=hour, minute=minute,
                    weekdays=tuple(sorted(set(weekdays))), timezone=timezone)


def every(seconds: int) -> Schedule:
    """Ein Beobachtungsabstand. Zu klein ist ein Fehler, keine Kleinigkeit.

    Die erste Fassung hat still auf die Untergrenze hochgesetzt. Das ist bequem
    und falsch: wer „alle 30 Sekunden" sagt und „alle 5 Minuten" bekommt, erfaehrt
    es nie. Gehoben wird weiterhin — aber an der Oberflaeche, wo der Nutzer den
    Satz dazu hoert (siehe `clamp_interval`).
    """
    return Schedule(kind=Kind.INTERVAL, every_seconds=int(seconds))


def clamp_interval(seconds: int) -> tuple[int, str]:
    """Hebt einen zu kleinen Abstand — und sagt, dass es passiert ist."""
    wanted = int(seconds)
    if wanted >= MIN_INTERVAL_SECONDS:
        return wanted, ""
    return MIN_INTERVAL_SECONDS, (
        f"Ich sehe hoechstens alle {MIN_INTERVAL_SECONDS // 60} Minuten nach — "
        f"oefter waere fuer den Anbieter zu viel.")
