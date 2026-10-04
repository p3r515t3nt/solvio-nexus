"""Absichtliche Weckversuche messen, ohne die Erkennung anzufassen.

Der Grund, warum es dieses Modul gibt, steht in einer einzigen Beobachtung: im
Journal des Satelliten tauchten Scores von 0,112 und 0,225 auf, weit ueber dem
Raumrauschen und weit unter der Schwelle. War das ein Mensch, der "Hey Solvio"
rief und nicht gehoert wurde? Oder der Fernseher? Aus dem Log ist das nicht zu
beantworten, und **ein niedriger Score allein ist kein Beleg fuer einen
verpassten Weckversuch**. Wer ihn dafuer haelt, baut denselben Fehlschluss
wieder auf, an dem die erste Ursachensuche gescheitert ist.

Der erste Entwurf wollte Versuche akustisch erkennen — laute Stelle im Raum,
also wohl ein Versuch. Das ist verkehrt herum, und die Messung zeigt es:

* Die Torschwelle "+12 dB ueber dem Grundpegel" wird in 35 % der belegten
  Fenster vom lautesten Haeppchen der ganzen zehn Sekunden nicht erreicht. Der
  Erkenner haette gerade die LEISEN Versuche verworfen — und leise Versuche
  sind die wahrscheinlichsten Fehlschlaege.
* Bei Dauerpegel (Fernseher) haette er sich in rund 51 Sekunden selbst
  abgeschaltet, ohne einen einzigen echten Versuch gesehen zu haben.
* Zwei Rufe im Abstand von 400 ms waeren still zu einem Datensatz verschmolzen.

**Absicht ist keine akustische Eigenschaft.** Sie kommt vom Menschen, also muss
sie auch von dort kommen: ein `touch` auf die Flagdatei oeffnet genau ein
Versuchsfenster, und dieses Fenster erzeugt genau einen Datensatz. Eins zu eins
per Konstruktion — kein Verschmelzen, kein Zerlegen, kein Fernseher.

Der akustische Erkenner bleibt trotzdem, aber als das, was er ist: Beifang.
Seine Datensaetze tragen `src="burst"` und beantworten "war zwischen den
Versuchen ueberhaupt etwas los?". Sie sind niemals Fehlerstatistik.

Drei Zusagen, an denen dieses Modul haengt:

1. **Es kann die Erkennung nicht veraendern.** Es haengt am Ende der ohnehin
   vorhandenen Messung (`WakeTelemetry.on_chunk`/`on_score`), auf denselben
   Bytes zur selben Zeit. Die Aufrufkette zum Detektor bleibt Zeile fuer Zeile
   unveraendert, die Schwelle bleibt 0,5, DSP bleibt unberuehrt.
2. **Es kann den Satelliten nicht toeten.** Die erste Ausnahme aus einem Hook
   schaltet den Recorder dauerhaft ab und wird gemeldet. Ein Testmodus, der den
   Dienst umbringen kann, waere keine unveraenderte Produktion.
3. **Es speichert kein Audio.** Nur Skalare — dieselbe Zusage wie in
   `telemetry.py`, aus demselben Grund.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque

from solvio_sat.telemetry import SCORE_EPSILON, dbfs

#: Wo der Mensch seine Absicht hinterlegt. Ein `touch` je Versuch.
TRIAL_FLAG = "/tmp/solvio_wake_trial.json"
#: Wie oft die Flagdatei gelesen wird. Eigener Task, nie aus dem Audiopfad
#: (`os.stat` kostet ~4 us, aber es hat dort trotzdem nichts zu suchen).
TRIAL_POLL_S = 1.0

#: Wie lange ein markiertes Fenster offen bleibt. Gemessen: `session_ready`
#: brauchte ueber 15 Sitzungen 579 bis 1659 ms, die Wake-Erkennung selbst rund
#: 300 ms. Acht Sekunden decken Ansage und Reaktion mit mehr als vierfacher
#: Reserve.
TRIAL_WINDOW_S = 8.0

#: Untergrenze der Torschwelle des Beifang-Erkenners.
#:
#: Diese Schwelle gilt AUSSCHLIESSLICH fuer den Beifang. Ein markiertes Fenster
#: oeffnet der Mensch, nicht der Pegel — es hat mit ihr nichts zu tun, und ein
#: leiser Versuch kann hier deshalb nicht verlorengehen.
#:
#: Der erste Wert (-40 dBFS) war fuer diesen Raum zu tief: im Selbsttest
#: erzeugte gewoehnliches Wohnzimmerrauschen 11 Datensaetze in 58 Sekunden. Das
#: ist nicht falsch, aber es begraebt die markierten Versuche im Journal.
#: Gemessen liegt der verfolgte Boden hier bei -52 bis -54 dBFS, gewoehnliche
#: Spitzen bei -33 bis -41 und laute Ereignisse bei -13 bis -18. -30 dBFS
#: trennt das zweite vom dritten und laesst Beifang wieder bedeuten, dass
#: wirklich etwas los war.
TRIAL_GATE_ABS_DBFS = -30.0
#: ... und wie weit ueber dem verfolgten Boden.
TRIAL_GATE_REL_DB = 10.0
#: Der Boden faellt schnell und steigt langsam. Ein linearer Mittelwert waere
#: kein Bodenverfolger: er wandert mit jeder lauten Stunde nach oben, und die
#: Schwelle mit ihm — genau dann, wenn sie es nicht darf.
TRIAL_FLOOR_DOWN_DB_S = 20.0
TRIAL_FLOOR_UP_DB_S = 0.5

TRIAL_ONSET_CHUNKS = 3        # 60 ms ueber der Schwelle -> Burst auf
TRIAL_HANGOVER_CHUNKS = 25    # 500 ms darunter -> Burst zu
TRIAL_MAX_BURST_S = 8.0       # Zwangsende
#: Nach einem Zwangsende muss es erst wieder ruhig werden, sonst laeuft bei
#: Dauerpegel ein Burst nahtlos in den naechsten und der Testmodus verbraucht
#: sich an einem Fernseher.
TRIAL_REARM_CHUNKS = 50
#: Naeher als das beieinander: der Datensatz sagt es, statt still zu verschmelzen.
TRIAL_MIN_GAP_S = 1.5

#: Die Obergrenze der Frist. Acht Stunden, nicht fuenfzehn Minuten.
#:
#: Die Frist sollte urspruenglich der Begrenzer gegen ausuferndes Aufzeichnen
#: sein. Das ist sie nicht: diese Rolle tragen TRIAL_MAX_MARKS und
#: TRIAL_MAX_BURSTS, und ohne Scharfschaltung wird ohnehin nichts geschrieben.
#: Was die Frist wirklich bestimmt, ist, wie lange ein Mensch Zeit hat, seine
#: zwanzig Versuche zu machen — und das ist seine Entscheidung, nicht die der
#: Uhr. Eine Viertelstunde zwang ihn, sofort zu sprechen oder gar nicht.
TRIAL_DEADLINE_S = 28800.0
TRIAL_MAX_MARKS = 50
TRIAL_MAX_BURSTS = 200        # reine Loghygiene: 200 x ~330 B
#: Aufloesung des Score-Verlaufs: 32 Werte je 8-s-Fenster.
TRIAL_SCORE_BUCKET_S = 0.25
#: Wie viele fertige Datensaetze warten duerfen, bis der Emitter sie abholt.
TRIAL_QUEUE_MAX = 64


class _Window:
    """Ein offenes Beobachtungsfenster. Sammelt nur, entscheidet nichts."""

    __slots__ = ("src", "t_open", "t_close", "seq", "state", "refractory_left",
                 "floor_db", "gate_db", "chunks", "frames", "frames_nonzero",
                 "sum_ms_l", "sum_ms_r", "peak_ms_l", "peak_ms_r", "zero_l", "zero_r",
                 "score_max", "buckets", "gap_max_ms", "wake_score", "saw_non_idle",
                 "truncated", "close_to_prev", "quiet_chunks", "since_session_end",
                 "ms_at_peak", "last_ms_l")

    def __init__(self, src, t_open, t_close, seq, state, refractory_left,
                 floor_db, gate_db, since_session_end=None):
        self.src = src
        self.t_open = t_open
        self.t_close = t_close
        self.seq = seq
        self.state = state
        self.refractory_left = refractory_left
        self.floor_db = floor_db
        self.gate_db = gate_db
        self.chunks = 0
        self.frames = 0
        self.frames_nonzero = 0
        self.sum_ms_l = 0.0
        self.sum_ms_r = 0.0
        self.peak_ms_l = 0.0
        self.peak_ms_r = 0.0
        self.zero_l = 0
        self.zero_r = 0
        self.score_max = 0.0
        self.buckets: dict[int, int] = {}
        self.gap_max_ms = 0.0
        self.wake_score = None
        self.saw_non_idle = state != "IDLE"
        self.truncated = False
        self.close_to_prev = False
        self.quiet_chunks = 0
        #: Beim OEFFNEN festgehalten. Zum Schliesszeitpunkt gemessen ergab das
        #: negative Werte: waehrend eines langen Fensters kann eine neue Sitzung
        #: beginnen UND enden, und dann liegt "das letzte Sessionende" nach dem
        #: Fensteranfang. Die Frage lautet aber "wie lange nach dem Gespraech
        #: hat der Mensch gesprochen" — und die entscheidet sich beim Oeffnen.
        self.since_session_end = since_session_end
        #: Der Mikrofonpegel IM MOMENT des hoechsten Scores — nicht die Spitze
        #: ueber das ganze Fenster.
        #:
        #: Der Unterschied hat eine Auswertung gekostet: ein Fenster enthielt
        #: einen gescheiterten und einen gelungenen Ruf, und die Fensterspitze
        #: gehoerte zum gelungenen. Damit liess sich ueber den gescheiterten
        #: nichts sagen — die naheliegende Frage "war er zu laut?" war mit den
        #: eigenen Daten nicht beantwortbar.
        self.ms_at_peak = 0.0
        self.last_ms_l = 0.0


class TrialRecorder:
    """Zeichnet Weckversuche auf. Beobachter, kein Beteiligter."""

    def __init__(self, *, threshold: float = 0.5, clock=time.monotonic) -> None:
        self._clock = clock
        self.threshold = threshold
        #: Erste Ausnahme aus einem Hook schaltet dauerhaft ab. Der Grund steht
        #: im Modulkopf: dieser Code darf den Satelliten nicht umbringen.
        self.broken: str | None = None

        self.floor_db: float | None = None
        self.t_last_chunk: float | None = None
        self.hot = 0                 # Haeppchen in Folge ueber der Schwelle
        self.cold = 0                # ... darunter
        self.rearm = 0               # Sperre nach einem Zwangsende
        self.window: _Window | None = None
        self.records: deque = deque(maxlen=TRIAL_QUEUE_MAX)
        self.dropped = 0

        self.state = "STARTING"
        self.refractory_left = 0
        self.sessions_total = 0
        self.t_last_session_end: float | None = None
        self.t_start = clock()

        self.window_s = TRIAL_WINDOW_S
        self.armed_until: float | None = None
        self.marks_left = 0
        self.bursts_left = 0
        self.seq = 0
        self.t_last_window_close: float | None = None
        self.counts = {"hit": 0, "miss": 0, "refractory": 0, "not_idle": 0,
                       "no_audio": 0}
        self.marks_seen = 0
        self.records_marked = 0
        self.bursts_emitted = 0
        self.t_armed: float | None = None

    # ---------------------------------------------------------------- Zustand

    @property
    def armed(self) -> bool:
        return self.armed_until is not None and self._clock() < self.armed_until

    def arm(self, *, marks: int, deadline_s: float,
            window_s: float = TRIAL_WINDOW_S) -> None:
        now = self._clock()
        # Die Fensterbreite ist einstellbar, weil die Versuchsreihe getaktet
        # laeuft: der Mensch spricht in seinem eigenen Rhythmus, und ein
        # Fenster, das schmaler ist als sein Takt, wuerde einen echten Versuch
        # als "kein Versuch" verbuchen. Die Voreinstellung bleibt 8 s.
        self.window_s = max(1.0, min(float(window_s), 60.0))
        self.armed_until = now + min(deadline_s, TRIAL_DEADLINE_S)
        self.marks_left = min(marks, TRIAL_MAX_MARKS)
        self.bursts_left = TRIAL_MAX_BURSTS
        self.t_armed = now
        self.seq = 0
        self.marks_seen = 0
        self.records_marked = 0
        self.bursts_emitted = 0
        for key in self.counts:
            self.counts[key] = 0

    def disarm(self) -> dict:
        """Schliesst den Testmodus und gibt die Bilanz zurueck.

        Die Bilanz ist der zweite Teil der Eins-zu-eins-Zusage: weicht
        "markiert" von "aufgezeichnet" ab, steht das laut da, statt still zu
        verschwinden.
        """
        summary = {
            "dauer_s": int(self._clock() - self.t_armed) if self.t_armed else 0,
            "markiert": self.marks_seen,
            "aufgezeichnet": self.records_marked,
            "beifang": self.bursts_emitted,
            **{k: v for k, v in self.counts.items()},
        }
        self.armed_until = None
        self.marks_left = 0
        self.bursts_left = 0
        self.t_armed = None
        if self.window is not None and self.window.src == "mark":
            self._close(self.window, forced=True)
        return summary

    def on_state(self, state: str, *, refractory_left: int = 0) -> None:
        self.state = state
        self.refractory_left = refractory_left
        if self.window is not None and state != "IDLE":
            self.window.saw_non_idle = True

    def on_session_end(self) -> None:
        self.sessions_total += 1
        self.t_last_session_end = self._clock()

    def on_wake(self, score: float) -> None:
        if self.window is not None:
            self.window.wake_score = float(score)

    def mark(self) -> None:
        """Ein Mensch hat einen Versuch angekuendigt."""
        now = self._clock()
        self.marks_seen += 1
        if self.window is not None:
            # Ein laufender Burst ist Hintergrund, nicht der Versuch. Er wird
            # als Beifang geschlossen, damit das markierte Fenster sauber
            # beginnt.
            self._close(self.window)
        self.marks_left = max(0, self.marks_left - 1)
        self.window = _Window("mark", now, now + self.window_s, self._next_seq(),
                              self.state, self.refractory_left,
                              self.floor_db, self._gate_db(),
                              self._since_session_end(now))
        if self.t_last_window_close is not None:
            self.window.close_to_prev = (now - self.t_last_window_close) < TRIAL_MIN_GAP_S

    # ------------------------------------------------------------------ Hooks

    def on_chunk(self, mean_square_l: float, mean_square_r: float) -> None:
        """Am Ende von WakeTelemetry.on_chunk. Darf niemals werfen."""
        if self.broken:
            return
        now = self._clock()
        if self.t_last_chunk is not None:
            gap = (now - self.t_last_chunk) * 1000.0
            if self.window is not None and gap > self.window.gap_max_ms:
                self.window.gap_max_ms = gap
        self.t_last_chunk = now

        level_db = dbfs(mean_square_l)
        self._track_floor(level_db)
        gate = self._gate_db()

        window = self.window
        if window is not None:
            window.chunks += 1
            window.sum_ms_l += mean_square_l
            window.sum_ms_r += mean_square_r
            window.last_ms_l = mean_square_l
            if mean_square_l > window.peak_ms_l:
                window.peak_ms_l = mean_square_l
            if mean_square_r > window.peak_ms_r:
                window.peak_ms_r = mean_square_r
            if mean_square_l == 0.0:
                window.zero_l += 1
            if mean_square_r == 0.0:
                window.zero_r += 1

        above = level_db >= gate
        if window is not None and window.src == "mark":
            if now >= window.t_close:
                self._close(window)
            return

        if self.rearm > 0:
            # Verkehrt herum war das eine Einladung: `= 0 if above` gab den
            # Erkenner frei, SOLANGE es laut blieb — also genau dann, wenn die
            # Sperre gebraucht wird. Bei Dauerpegel lief ein Zwangsende nahtlos
            # ins naechste, ein Datensatz alle acht Sekunden. Richtig ist das
            # Gegenteil: lauter Pegel setzt die Sperre zurueck, erst Ruhe zaehlt
            # sie herunter.
            self.rearm = TRIAL_REARM_CHUNKS if above else self.rearm - 1
            return

        if window is None:
            self.hot = self.hot + 1 if above else 0
            if self.hot >= TRIAL_ONSET_CHUNKS and self.state == "IDLE":
                self.hot = 0
                self.window = _Window("burst", now, now + TRIAL_MAX_BURST_S,
                                      self._next_seq(), self.state,
                                      self.refractory_left, self.floor_db, gate,
                                      self._since_session_end(now))
                if self.t_last_window_close is not None:
                    self.window.close_to_prev = (
                        now - self.t_last_window_close) < TRIAL_MIN_GAP_S
            return

        # offener Burst
        if above:
            self.cold = 0
        else:
            self.cold += 1
            window.quiet_chunks += 1
        if self.cold >= TRIAL_HANGOVER_CHUNKS:
            self._close(window)
        elif now >= window.t_close:
            window.truncated = True
            self.rearm = TRIAL_REARM_CHUNKS
            self._close(window)

    def on_score(self, score: float) -> None:
        """Am Ende von WakeTelemetry.on_score. Darf niemals werfen."""
        if self.broken:
            return
        window = self.window
        if window is None:
            return
        window.frames += 1
        if score > SCORE_EPSILON:
            window.frames_nonzero += 1
        if score > window.score_max:
            window.score_max = float(score)
            window.ms_at_peak = window.last_ms_l
        bucket = int((self._clock() - window.t_open) / TRIAL_SCORE_BUCKET_S)
        value = int(round(score * 100))
        if value > window.buckets.get(bucket, -1):
            window.buckets[bucket] = value

    # ------------------------------------------------------------ Auswertung

    def drain(self) -> list[dict]:
        out = list(self.records)
        self.records.clear()
        return out

    def tick(self) -> None:
        """Vom 1-Hz-Task: schliesst ein abgelaufenes markiertes Fenster auch
        dann, wenn gerade kein Haeppchen mehr kommt — sonst haette ausgerechnet
        ein stockendes Mikrofon keinen Datensatz hinterlassen."""
        if self.broken:
            return
        window = self.window
        if window is not None and self._clock() >= window.t_close:
            self._close(window)

    # ------------------------------------------------------------- Innenleben

    def _since_session_end(self, now: float):
        if self.t_last_session_end is None:
            return None
        return round(now - self.t_last_session_end, 1)

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _track_floor(self, level_db: float) -> None:
        if self.floor_db is None:
            self.floor_db = level_db
            return
        if level_db < self.floor_db:
            step = TRIAL_FLOOR_DOWN_DB_S * 0.02
            self.floor_db = max(level_db, self.floor_db - step)
        else:
            step = TRIAL_FLOOR_UP_DB_S * 0.02
            self.floor_db = min(level_db, self.floor_db + step)

    def _gate_db(self) -> float:
        if self.floor_db is None:
            return TRIAL_GATE_ABS_DBFS
        return max(TRIAL_GATE_ABS_DBFS, self.floor_db + TRIAL_GATE_REL_DB)

    def _outcome(self, window: _Window) -> str:
        """Fuenf Ausgaenge, die sich wirklich unterscheiden lassen.

        Die Reihenfolge ist die Beweisordnung. Ohne `refractory` und `not_idle`
        wuerde spaeter als Taubheit gelesen, was in Wahrheit Konstruktion ist —
        genau der Fehlschluss, den der Auftrag verbietet.
        """
        if window.wake_score is not None:
            return "hit"
        expected = max(1, int((min(self._clock(), window.t_close) - window.t_open) / 0.02))
        if window.chunks * 2 < expected or (window.chunks and window.zero_l == window.chunks):
            return "no_audio"
        if window.refractory_left > 0 and window.score_max >= self.threshold:
            return "refractory"
        if window.saw_non_idle:
            return "not_idle"
        # KEIN pauschales "refractory" mehr, nur weil die Sperre beim Oeffnen
        # noch lief. Sie dauert 16 Frames = 1,28 s; ein 60-s-Fenster, das am
        # Sitzungsende aufgeht, waere sonst vollstaendig als "bauartbedingt
        # unterdrueckt" abgestempelt — und das ist ausgerechnet die Messung, um
        # die es geht. Sie erklaert einen ausgebliebenen Weckvorgang nur, wenn
        # ueberhaupt etwas zu unterdruecken war; dieser Fall steht oben.
        return "miss"

    def _close(self, window: _Window, *, forced: bool = False) -> None:
        self.window = None
        self.cold = 0
        self.hot = 0
        now = self._clock()
        self.t_last_window_close = now
        outcome = self._outcome(window)
        if window.src == "burst":
            if not self.armed or self.bursts_left <= 0:
                return
            self.bursts_left -= 1
            self.bursts_emitted += 1
            # Beifang wird NICHT in die Ausgangsbilanz gezaehlt. Er wird vom
            # Pegel geoeffnet, nicht von einem Menschen; ihn mitzuzaehlen ergaebe
            # eine Zeile wie "hit 0 / miss 51" fuer einen Abend, an dem niemand
            # etwas versucht hat — exakt die irrefuehrende Fehlerstatistik,
            # gegen die dieser Testmodus gebaut ist.
        else:
            self.records_marked += 1
            self.counts[outcome] = self.counts.get(outcome, 0) + 1
        n = max(1, window.chunks)
        record = {
            "seq": window.seq,
            "src": window.src,
            "outcome": outcome,
            "state": window.state,
            "score_max": round(window.score_max, 3),
            "scores": [window.buckets.get(i, 0)
                       for i in range(max(window.buckets, default=-1) + 1)],
            "frames": window.frames,
            "frames_nonzero": window.frames_nonzero,
            "chunks": window.chunks,
            "chunks_expected": int((now - window.t_open) / 0.02),
            "gap_max_ms": int(window.gap_max_ms),
            "rms_l_mean_db": round(dbfs(window.sum_ms_l / n), 1),
            "rms_l_peak_db": round(dbfs(window.peak_ms_l), 1),
            "rms_l_at_peak_db": round(dbfs(window.ms_at_peak), 1),
            "rms_r_mean_db": round(dbfs(window.sum_ms_r / n), 1),
            "rms_r_peak_db": round(dbfs(window.peak_ms_r), 1),
            "zero_l": window.zero_l,
            "zero_r": window.zero_r,
            "floor_db": round(window.floor_db, 1) if window.floor_db is not None else None,
            "gate_db": round(window.gate_db, 1),
            "refractory_left": window.refractory_left,
            "since_session_end_s": window.since_session_end,
            "sessions_total": self.sessions_total,
            "uptime_s": int(window.t_open - self.t_start),
            "close_to_prev": window.close_to_prev,
            "truncated": window.truncated or forced,
            "marks_left": self.marks_left,
        }
        if len(self.records) == self.records.maxlen:
            self.dropped += 1
        self.records.append(record)


def read_flag(path: str = TRIAL_FLAG) -> tuple[float, dict] | None:
    """Die Flagdatei lesen. Rueckgabe: (mtime, inhalt) oder None.

    Bewusst tolerant: eine halb geschriebene oder unsinnige Datei schaltet den
    Testmodus nicht scharf und wirft auch nichts — sie wird ignoriert.
    """
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
        if not isinstance(spec, dict):
            return None
    except (OSError, ValueError):
        spec = {}
    return mtime, spec


def host_stats() -> dict:
    """CPU, Speicher und Temperatur — billig und ausserhalb des Audiopfads.

    Alle drei sind Dateilesungen im Bereich von 50 us. Ausdruecklich NICHT
    dabei: `vcgencmd get_throttled`, das einen Unterprozess startet und
    gemessen 2,1 ms kostet. Es gehoert in die Rahmenzeilen des Testmodus, nicht
    in jeden Datensatz.
    """
    out: dict = {}
    try:
        with open("/proc/self/stat", encoding="ascii") as handle:
            fields = handle.read().split()
        ticks = float(fields[13]) + float(fields[14])
        out["cpu_s"] = round(ticks / os.sysconf("SC_CLK_TCK"), 1)
    except (OSError, IndexError, ValueError):
        pass
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = int(int(line.split()[1]) / 1024)
                    break
    except (OSError, IndexError, ValueError):
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as handle:
            out["temp_c"] = round(int(handle.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    return out
