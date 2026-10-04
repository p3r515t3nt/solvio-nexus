"""Ob das Ohr noch hoert — und woher der Core das ueberhaupt wissen soll.

Bis hierher wusste der Core vom Satelliten nur, dass es ihn gibt: er meldete
sich beim Wecken, bewies per HMAC seine Kennung und verschwand danach wieder.
Zwischen zwei Gespraechen war er fuer den Core nicht existent. Ein Satellit, der
das Weckwort nicht mehr erkennt, meldet sich aber genau NIE — und weil "kein
Wecken" von "niemand hat gesprochen" nicht zu unterscheiden war, stand im
Kontrollzentrum alles auf gruen, waehrend im Wohnzimmer jemand gegen eine Wand
sprach.

Deshalb schickt der Satellit jetzt regelmaessig einen kurzen Bericht ueber sein
eigenes Hoeren. Drei Dinge daran sind Absicht:

* **Er geht ueber denselben authentifizierten Weg.** Kein zweiter Port, kein
  zweites Geheimnis, keine neue Vertrauenskante — dieselbe HMAC-Pruefung wie
  eine Sitzung, nur auf einem eigenen Pfad, damit die Sitzungslogik unberuehrt
  bleibt.
* **Der Satellit misst, der Core urteilt.** Der Bericht enthaelt Messwerte und
  die Einschaetzung des Geraets; was daraus im Kontrollzentrum wird, entscheidet
  der Core. Ein Geraet, das seinen eigenen Gesundheitszustand im Vokabular des
  Cores festlegen koennte, waere eine Auskunft, die sich selbst beglaubigt.
* **Kein Bericht ist nicht gesund.** Wer sich nie gemeldet hat, ist `unknown` —
  der Core kann frueher gestartet sein als der Satellit, und das ist wirklich
  keine Auskunft. Wer sich gemeldet hat und dann verstummt ist, ist
  `unavailable` und zaehlt damit als Stoerung. Der ganze Defekt bestand darin,
  dass Schweigen wie Gesundheit aussah; das darf hier nicht wieder entstehen —
  auch nicht als `unknown`, das in der Uebersicht nicht als Stoerung zaehlt.

Diese Berichte sind INFORMATION. Sie tragen keine Autoritaet, loesen keine
Faehigkeit aus und veraendern keinen TrustContext.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: Der eigene Pfad des Gesundheitswegs. Der Sitzungsweg bleibt auf "/", damit
#: sich an ihm nichts aendert.
HEALTH_PATH = "/health"

#: Wie oft der Satellit berichtet (er bestimmt das, hier steht die Erwartung).
HEARTBEAT_INTERVAL_S = 60.0

#: Ab wann ein Bericht als veraltet gilt. Drei ausgefallene Meldungen in Folge
#: sind kein Zufall mehr — zwei koennten ein Netzhaenger sein.
STALE_AFTER_S = HEARTBEAT_INTERVAL_S * 3.5

#: Was der Satellit ueber sich selbst sagen darf. Alles andere wird zu
#: `unknown`: ein unbekanntes Wort ist keine Diagnose.
KNOWN_VERDICTS = frozenset({"healthy", "quiet", "no_frames", "left_dead",
                            "detector_flat", "unknown"})

#: Welche Messwerte uebernommen werden. Was nicht hier steht, wird verworfen —
#: der Bericht ist eine Schnittstelle, kein Ablageplatz.
HEARING_FIELDS = ("window_s", "chunks", "chunks_expected", "chunks_total",
                  "frames", "frames_nonzero", "rms_l_mean_db", "rms_l_peak_db",
                  "rms_r_mean_db", "rms_r_peak_db", "zero_l", "zero_r",
                  "score_max", "gap_max_ms", "since_last_chunk_ms",
                  "since_last_nonzero_s", "sessions", "phantoms_suppressed",
                  "feed_drops", "verdict_age_s")

#: Ab wann das URTEIL selbst veraltet ist, auch wenn der Bericht frisch ankommt.
#: Der Satellit misst alle 10 s und berichtet alle 60 s; ein Urteil, das beim
#: Absenden aelter als das ist, kann nur bedeuten, dass die Messung steht,
#: waehrend der Berichtsweg weiterlaeuft. Genau dann wuerde der Core dauerhaft
#: den letzten guten Zustand anzeigen — Schweigen, das wie Gesundheit aussieht,
#: eine Ebene tiefer.
VERDICT_STALE_S = 75.0


def _clean_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return value


@dataclass
class SatelliteReport:
    """Was ein Satellit zuletzt ueber sein Hoeren gesagt hat."""

    satellite_id: str
    received_at: float
    state: str = "unknown"
    verdict: str = "unknown"
    hearing: dict[str, Any] = field(default_factory=dict)
    repairs: int = 0
    last_repair: str | None = None

    def age(self, now: float) -> float:
        return max(0.0, now - self.received_at)

    def is_stale(self, now: float) -> bool:
        return self.age(now) > STALE_AFTER_S


def parse_report(satellite_id: str, message: dict[str, Any], *,
                 now: float | None = None) -> SatelliteReport:
    """Aus einer Nachricht einen Bericht — defensiv, Feld fuer Feld.

    Der Absender ist per HMAC bewiesen, aber bewiesene Herkunft ist nicht
    dasselbe wie vertrauenswuerdiger Inhalt. Zahlen werden als Zahlen geprueft,
    unbekannte Urteile werden `unknown`, und Text wird gekuerzt.
    """
    verdict = str(message.get("verdict", "unknown"))[:32]
    if verdict not in KNOWN_VERDICTS:
        verdict = "unknown"
    raw = message.get("hearing")
    hearing: dict[str, Any] = {}
    if isinstance(raw, dict):
        for key in HEARING_FIELDS:
            value = _clean_number(raw.get(key))
            if value is not None:
                hearing[key] = value
    repairs = _clean_number(message.get("repairs")) or 0
    last_repair = message.get("last_repair")
    return SatelliteReport(
        satellite_id=satellite_id[:64],
        received_at=time.time() if now is None else now,
        state=str(message.get("state", "unknown"))[:32],
        verdict=verdict,
        hearing=hearing,
        repairs=int(repairs),
        last_repair=str(last_repair)[:120] if last_repair else None,
    )


class SatelliteHealthRegistry:
    """Der letzte Bericht je Satellit. Mehr wird nicht aufgehoben.

    Kein Verlauf: eine zweite Zeitreihe neben dem Journal des Satelliten waere
    eine zweite Wahrheit, und die waere irgendwann die falsche.
    """

    def __init__(self, *, clock=time.time) -> None:
        self.clock = clock
        self._latest: dict[str, SatelliteReport] = {}

    def record(self, report: SatelliteReport) -> None:
        self._latest[report.satellite_id] = report

    def latest(self) -> SatelliteReport | None:
        """Der juengste Bericht ueberhaupt — es gibt genau einen Satelliten."""
        if not self._latest:
            return None
        return max(self._latest.values(), key=lambda r: r.received_at)

    def known(self) -> list[SatelliteReport]:
        return sorted(self._latest.values(), key=lambda r: r.satellite_id)


#: Wie ein Urteil des Satelliten im Vokabular des Kontrollzentrums heisst, und
#: was der Nutzer davon liest. Bewusst hier und nicht auf dem Pi: die Bedeutung
#: gehoert dem Core.
VERDICT_MEANING: dict[str, tuple[str, str]] = {
    "healthy": ("healthy", "hoert zu"),
    "quiet": ("healthy", "hoert zu, es ist still"),
    "no_frames": ("unavailable", "kein Mikrofonsignal mehr"),
    "left_dead": ("unavailable", "Mikrofonkanal liefert nur noch Stille"),
    "detector_flat": ("degraded", "Sprache im Raum, aber die Erkennung reagiert nicht"),
    "unknown": ("unknown", "meldet nichts Verwertbares"),
}


def state_for(report: SatelliteReport | None, *, now: float | None = None
              ) -> tuple[str, str]:
    """Zustand und Begruendung fuer die Gesundheitstafel.

    Reihenfolge mit Bedacht: erst "gibt es ueberhaupt einen Bericht", dann "ist
    er frisch", und erst danach, was drinsteht. Ein alter Bericht mit dem Wort
    `healthy` ist keine Auskunft ueber jetzt.
    """
    now = time.time() if now is None else now
    if report is None:
        # "Noch nie gemeldet" ist wirklich keine Auskunft: der Core kann frueher
        # gestartet sein als der Satellit. Das bleibt `unknown`.
        return "unknown", "hat sich noch nie gemeldet"
    age = int(report.age(now))
    if report.is_stale(now):
        # Verstummt ist etwas anderes als unbekannt. Dieses Geraet HAT sich
        # gemeldet und tut es nicht mehr — das ist ein Ausfall und muss als
        # solcher in der Uebersicht erscheinen. Vorher stand hier `unknown`,
        # und weil `unknown` nicht als Stoerung zaehlt, sagte die Zusammen-
        # fassung "Alles laeuft", waehrend das Ohr seit Stunden weg war. Genau
        # der Fehler, gegen den dieser Milestone gebaut wurde.
        return "unavailable", f"seit {age} s keine Meldung"
    verdict_age = report.hearing.get("verdict_age_s")
    if isinstance(verdict_age, (int, float)) and verdict_age > VERDICT_STALE_S:
        # Der Bericht kommt, aber er traegt eine stehengebliebene Messung.
        return "degraded", f"Messung steht seit {int(verdict_age)} s"
    state, reason = VERDICT_MEANING.get(report.verdict, VERDICT_MEANING["unknown"])
    # Das Alter der MESSUNG gehoert in die Begruendung, nicht nur in die
    # Schwellenpruefung. Sonst steht dort "hoert zu" und niemand kann sehen,
    # worauf sich das stuetzt — und genau die Frage "wie alt ist das, was hier
    # gruen leuchtet?" war der Ausgangspunkt dieses Milestones.
    if isinstance(verdict_age, (int, float)):
        reason = f"{reason} (Messung {int(verdict_age)} s alt)"
    if report.repairs:
        reason = f"{reason}, {report.repairs} Reparatur(en)"
    return state, reason
