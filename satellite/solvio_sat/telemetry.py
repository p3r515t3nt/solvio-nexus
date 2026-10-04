"""Was der Satellit ueber sein eigenes Hoeren weiss.

Bis hierher konnte SOLVIO seine eigene Taubheit nicht messen. Der Automat gab
beim Ruecksprung nach IDLE genau eine Zeile aus, und ein NICHT erkanntes
Weckwort gab gar keine. Jede Luecke im Journal begann daher zwangslaeufig mit
einem Sessionende, und ein stundenlanges Schweigen sah exakt so aus wie ein
Abend, an dem niemand gesprochen hat. Aus solchen Daten laesst sich "taub"
nicht von "still" trennen — egal wie lange man sie anstarrt.

Deshalb misst dieses Modul vier Dinge getrennt voneinander, denn genau diese
vier Faelle sehen im Log sonst gleich aus:

  1. Die Schleife laeuft nicht mehr        -> `updated` friert ein
  2. Frames kommen nicht mehr an           -> `chunks` bleibt stehen, `lag` waechst
  3. Frames kommen an, sind aber Stille    -> `rms_l` am Boden
  4. Sprache da, Detektor liefert flach    -> `rms_l` mit Spitzen, `score_max` bei null

Was hier NICHT passiert: es verlaesst kein PCM diese Datei. Gemessen werden
zwei Skalare je 20 ms — Effektivwert links und rechts — und daraus werden
Mittel und Spitze eines Fensters. Aus einem Effektivwert laesst sich kein Wort
rekonstruieren. Die Zusage "im IDLE verlaesst kein Audio den Pi" bleibt
woertlich wahr, und sie wird hier auch nicht durch die Hintertuer aufgeweicht:
die Rohbytes werden gelesen, quadriert und fallen gelassen.

Der Emitter ist ein EIGENER Task, der `read_chunk()` niemals anfasst. Laege er
in derselben await-Kette, wuerde ein haengendes `read_chunk()` Messung und
Gemessenes gleichzeitig stumm schalten — und das Schweigen der Telemetrie waere
wieder nicht von einem stillen Zimmer zu unterscheiden.
"""
from __future__ import annotations

import math
import time

#: Volle Aussteuerung eines 16-Bit-Samples. Bezugsgroesse fuer dBFS.
FULL_SCALE = 32768.0

#: Ab hier gilt ein Kanal als leise. Nur zum Lesen durch Menschen — das Urteil
#: haengt bewusst NICHT daran. Ein Pegel in dBFS braucht eine geeichte Schwelle,
#: und die haette man bei einem stillen Wohnzimmer gegen einen toten Kanal
#: raten muessen. Der eindeutige Test steht weiter unten: ein echtes Mikrofon in
#: einem echten Raum liefert immer ein Rauschen, niemals exakte Null. Wer exakte
#: Null liefert, liefert nichts.
QUIET_DBFS = -70.0


#: Ab wann ein Score als "der Detektor hat etwas getan" zaehlt. openWakeWord
#: liefert nach `reset()` fuenf Frames lang exakte 0.0 (model.py:243); exakte
#: Null traegt hier also keine Aussage.
SCORE_EPSILON = 1e-4


def dbfs(mean_square: float) -> float:
    """Effektivwert in dBFS. Ohne Signal: -inf, hier als -120 gefuehrt."""
    if mean_square <= 0.0:
        return -120.0
    rms = math.sqrt(mean_square)
    if rms <= 0.0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms / FULL_SCALE))


class WakeTelemetry:
    """Zaehlt, was die Aufnahme und der Detektor nebenbei ohnehin wissen.

    Alle Werte sind Fensterwerte: `snapshot()` gibt sie aus und setzt das
    Fenster zurueck. Zwei Groessen ueberdauern das Fenster bewusst — die
    Gesamtzahl der Haeppchen und der Zeitpunkt des letzten Haeppchens —, denn
    sie sind die Antwort auf "laeuft ueberhaupt noch etwas".
    """

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        #: Optionaler Mitleser. Er haengt am ENDE der Erfassung, auf denselben
        #: Bytes zur selben Zeit — die Aufrufkette zum Detektor bleibt dadurch
        #: Zeile fuer Zeile unveraendert. Wer stattdessen zwischen
        #: `prewake.append` und `det.feed` einstiege, verschoebe den
        #: Detektoraufruf und veraenderte damit genau das, was er messen will.
        self.observer = None
        self.t_start = clock()
        #: Ueberdauernd: seit Prozessstart.
        self.chunks_total = 0
        self.t_last_chunk = self.t_start
        self.max_gap_ms = 0.0
        self.reset_window()

    # --- Erfassung: drei Aufrufe, alle im Mikrosekundenbereich ---------------

    def on_chunk(self, mean_square_l: float, mean_square_r: float) -> None:
        """Ein 20-ms-Haeppchen ist gelesen worden. Zwei Zahlen, kein Audio."""
        now = self._clock()
        gap_ms = (now - self.t_last_chunk) * 1000.0
        if gap_ms > self.max_gap_ms:
            self.max_gap_ms = gap_ms
        if gap_ms > self.win_max_gap_ms:
            self.win_max_gap_ms = gap_ms
        self.t_last_chunk = now
        self.chunks_total += 1
        self.win_chunks += 1
        self.win_sum_ms_l += mean_square_l
        self.win_sum_ms_r += mean_square_r
        if mean_square_l == 0.0:
            self.win_zero_l += 1
        if mean_square_r == 0.0:
            self.win_zero_r += 1
        if mean_square_l > self.win_peak_ms_l:
            self.win_peak_ms_l = mean_square_l
        if mean_square_r > self.win_peak_ms_r:
            self.win_peak_ms_r = mean_square_r
        self._observe("on_chunk", mean_square_l, mean_square_r)

    def _observe(self, hook: str, *args) -> None:
        """Den Mitleser rufen — und ihn abschalten, statt zu sterben.

        Dieser Pfad laeuft fuenfzigmal je Sekunde im Audiopfad. Eine Ausnahme
        von hier wuerde durch die Hauptschleife nach oben durchschlagen, wo nur
        `asyncio.TimeoutError` gefangen wird, und den Satelliten beenden. Eine
        Messung darf das Gemessene nicht umbringen: die erste Ausnahme schaltet
        den Mitleser dauerhaft ab und wird sichtbar gemacht.
        """
        obs = self.observer
        if obs is None or getattr(obs, "broken", None):
            return
        try:
            getattr(obs, hook)(*args)
        except BaseException as exc:  # noqa: BLE001 - Absicht, siehe oben
            try:
                obs.broken = type(exc).__name__
            except Exception:  # noqa: BLE001
                self.observer = None

    def on_score(self, score: float) -> None:
        """Ein 80-ms-Frame ist bewertet worden — auch unter der Schwelle.

        Der bisher fehlende Beweis steckt genau hier: bisher wurde ein Score
        nur sichtbar, wenn er ausloeste. Ein Weckversuch, der bei 0,34 stehen
        blieb, hinterliess keine Spur, und hinterher war nicht zu sagen, ob der
        Detektor knapp danebenlag oder gar nichts mehr hoerte.
        """
        self.win_frames += 1
        if score > self.win_score_max:
            self.win_score_max = score
        if score > SCORE_EPSILON:
            self.win_frames_nonzero += 1
            self.t_last_nonzero = self._clock()
        self._observe("on_score", score)

    def on_session(self) -> None:
        self.win_sessions += 1

    def on_feed_drop(self) -> None:
        """Ein Haeppchen hat den Detektor nicht erreicht — eine Luecke in genau
        dem Zustand, dessen Luecke dieser Milestone beseitigt."""
        self.win_feed_drops += 1

    def on_phantom(self) -> None:
        """Ein Score ueber der Schwelle, der aus Alt-Kontext stammte und
        deshalb nicht geweckt hat. Gezaehlt, damit sichtbar bleibt, wie oft die
        Sperre wirklich greift — eine Sperre, die nie zaehlt, waere ein
        Argument, sie wieder zu entfernen."""
        self.win_phantoms += 1

    # --- Auswertung ----------------------------------------------------------

    def reset_window(self) -> None:
        self.t_window = self._clock()
        self.win_chunks = 0
        self.win_frames = 0
        self.win_frames_nonzero = 0
        self.win_sum_ms_l = 0.0
        self.win_sum_ms_r = 0.0
        self.win_peak_ms_l = 0.0
        self.win_peak_ms_r = 0.0
        self.win_zero_l = 0
        self.win_zero_r = 0
        self.win_score_max = 0.0
        self.win_max_gap_ms = 0.0
        self.win_sessions = 0
        self.win_phantoms = 0
        self.win_feed_drops = 0
        if not hasattr(self, "t_last_nonzero"):
            self.t_last_nonzero = self.t_start

    def snapshot(self, *, reset: bool = True) -> dict:
        """Der Fensterbericht. `expected` ist der Massstab fuer `chunks`:
        wie viele Haeppchen in dieser Zeitspanne haetten ankommen muessen."""
        now = self._clock()
        elapsed = max(1e-6, now - self.t_window)
        # Gerundet, nicht abgeschnitten: `chunks=500/499` waere ein Artefakt der
        # Gleitkommaaddition und wuerde beim Lesen wie ein verlorenes Haeppchen
        # aussehen. Der Massstab traegt hier ohnehin nur eine grobe Aussage — das
        # Urteil haengt an "weniger als die Haelfte", nicht an einer Einzelzahl.
        expected = round(elapsed / 0.02)
        mean_l = self.win_sum_ms_l / self.win_chunks if self.win_chunks else 0.0
        mean_r = self.win_sum_ms_r / self.win_chunks if self.win_chunks else 0.0
        snap = {
            "window_s": round(elapsed, 1),
            "chunks": self.win_chunks,
            "chunks_expected": expected,
            "chunks_total": self.chunks_total,
            "frames": self.win_frames,
            "frames_nonzero": self.win_frames_nonzero,
            "rms_l_mean_db": round(dbfs(mean_l), 1),
            "rms_l_peak_db": round(dbfs(self.win_peak_ms_l), 1),
            "rms_r_mean_db": round(dbfs(mean_r), 1),
            "rms_r_peak_db": round(dbfs(self.win_peak_ms_r), 1),
            "zero_l": self.win_zero_l,
            "zero_r": self.win_zero_r,
            "score_max": round(self.win_score_max, 3),
            "gap_max_ms": int(self.win_max_gap_ms),
            "since_last_chunk_ms": int((now - self.t_last_chunk) * 1000),
            "since_last_nonzero_s": int(now - self.t_last_nonzero),
            "sessions": self.win_sessions,
            "phantoms_suppressed": self.win_phantoms,
            "feed_drops": self.win_feed_drops,
            "observer_broken": getattr(self.observer, "broken", None),
        }
        if reset:
            self.reset_window()
        return snap

    def line(self, snap: dict) -> str:
        """Eine Zeile fuers Journal. Kurz genug, um sie taeglich zu ertragen."""
        return (f"[TELEM] chunks={snap['chunks']}/{snap['chunks_expected']} "
                f"frames={snap['frames']} nz={snap['frames_nonzero']} "
                f"rmsL={snap['rms_l_mean_db']}/{snap['rms_l_peak_db']} "
                f"rmsR={snap['rms_r_mean_db']}/{snap['rms_r_peak_db']} "
                f"smax={snap['score_max']} gap={snap['gap_max_ms']}ms")


#: Wie ein Fensterbericht zu lesen ist. Die Reihenfolge ist die Reihenfolge der
#: Pruefung: erst ob ueberhaupt etwas laeuft, dann ob Audio ankommt, dann ob es
#: Inhalt hat, und erst zuletzt, ob der Detektor ihn verarbeitet.
VERDICT_NO_FRAMES = "no_frames"
VERDICT_LEFT_DEAD = "left_dead"
VERDICT_DETECTOR_FLAT = "detector_flat"
VERDICT_QUIET = "quiet"
VERDICT_HEALTHY = "healthy"

#: Ab wann ein Fenster Sprachpegel enthaelt. Nur fuer das Urteil
#: `detector_flat` gebraucht: ohne Sprache im Raum sagt ein flacher Score
#: nichts aus.
SPEECH_PEAK_DBFS = -35.0


def classify(snap: dict) -> str:
    """Aus einem Fensterbericht ein Urteil.

    Die Reihenfolge ist die Reihenfolge der Beweiskraft. Zuerst die beiden
    eindeutigen Faelle — kein Zufluss, und ein linker Kanal, der ueber ein
    ganzes Fenster ausschliesslich exakte Null liefert. Erst danach der
    Detektor, und der nur, wenn ueberhaupt Sprache im Raum war. Alles, was
    mehrdeutig bleibt, bekommt `quiet` und loest nichts aus: ein stiller Abend
    darf keine Reparatur ausloesen.
    """
    if snap["chunks"] == 0:
        return VERDICT_NO_FRAMES
    if snap["chunks_expected"] and snap["chunks"] * 2 < snap["chunks_expected"]:
        return VERDICT_NO_FRAMES
    # Exakte Null ueber das ganze Fenster: kein Raum klingt so. Der rechte Kanal
    # (AEC-Referenz) DARF im IDLE still sein — er wird deshalb nicht geprueft.
    if snap["zero_l"] == snap["chunks"]:
        return VERDICT_LEFT_DEAD
    if snap["rms_l_peak_db"] >= SPEECH_PEAK_DBFS:
        if snap["frames_nonzero"] == 0:
            return VERDICT_DETECTOR_FLAT
        return VERDICT_HEALTHY
    return VERDICT_QUIET


#: Welche Urteile ueberhaupt eine Reparatur rechtfertigen. `quiet` steht
#: ausdruecklich nicht dabei.
ACTIONABLE = frozenset({VERDICT_NO_FRAMES, VERDICT_LEFT_DEAD, VERDICT_DETECTOR_FLAT})
