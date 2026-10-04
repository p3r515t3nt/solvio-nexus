#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Was an der Wake-Zuverlaessigkeit belegt sein muss, damit man sich darauf verlaesst.

Ausfuehren auf dem Pi:  ./venv/bin/python test_wake_reliability.py

Der erste Block ist der wichtigste. Er behauptet nichts ueber openWakeWord,
sondern misst es: dass `Model.reset()` das Fenster des Klassifikators NICHT
anfasst, ist die Ursache des Phantom-Wake, und sie wird hier an der echten
installierten Bibliothek festgenagelt. Sollte ein spaeteres Update das
reparieren, faellt dieser Test auf — und dann darf die Sperre wieder weg.
"""
import sys
import time

import numpy as np

sys.path.insert(0, "/home/gregor/solvio-satellite")

from solvio_sat.audio import channel_power, left_channel      # noqa: E402
from solvio_sat.telemetry import (ACTIONABLE, VERDICT_DETECTOR_FLAT,     # noqa: E402
                                  VERDICT_LEFT_DEAD, VERDICT_NO_FRAMES,
                                  VERDICT_QUIET, WakeTelemetry, classify, dbfs)
from solvio_sat.wakeword import CONTEXT_FRAMES, WakeWordDetector, find_model  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def stereo(left_vals, right_vals):
    """Ein Stereopuffer aus zwei Kanaelen, interleaved wie arecord ihn liefert."""
    a = np.empty(len(left_vals) * 2, dtype="<i2")
    a[0::2] = np.asarray(left_vals, dtype="<i2")
    a[1::2] = np.asarray(right_vals, dtype="<i2")
    return a.tobytes()


# ---------------------------------------------------------------- Mechanismus
print("\n[1] openWakeWord: was reset() wirklich zuruecksetzt")
det = WakeWordDetector(find_model("hey_solvio_final_v2"), 0.5)
rng = np.random.default_rng(20260823)
for _ in range(40):
    det.feed(rng.integers(-8000, 8000, 640, dtype=np.int16).tobytes())

pre = det.model.preprocessor.get_features(CONTEXT_FRAMES).copy()
det.reset()
post_reset = det.model.preprocessor.get_features(CONTEXT_FRAMES).copy()
check("reset() laesst das 16-Frame-Fenster des Klassifikators unveraendert",
      np.array_equal(pre, post_reset),
      "-> waere es hier gleich geworden, braeuchte es die Sperre nicht mehr")
check("reset() leert den Vorhersagepuffer",
      len(det.model.prediction_buffer.get(det.name, [])) == 0)

det.reinitialize()
post_reinit = det.model.preprocessor.get_features(CONTEXT_FRAMES).copy()
check("reinitialize() erzeugt wirklich einen unvoreingenommenen Detektor",
      not np.array_equal(pre, post_reinit))
check("reinitialize() liefert danach weiter Scores",
      det.feed(rng.integers(-8000, 8000, 1280 * 2, dtype=np.int16).tobytes()) is not None)

print("\n[2] Wie lange altes Audio nachwirkt")
det2 = WakeWordDetector(find_model("hey_solvio_final_v2"), 0.5)
loud = (np.sin(np.arange(1280 * 20) / 3.0) * 12000).astype(np.int16).tobytes()
det2.feed(loud)
marked = det2.model.preprocessor.get_features(CONTEXT_FRAMES).copy()
silence = np.zeros(1280, dtype=np.int16).tobytes()
for i in range(CONTEXT_FRAMES - 1):
    det2.feed(silence)
check("nach 15 frischen Frames steckt noch Altes im Fenster",
      not np.array_equal(det2.model.preprocessor.get_features(CONTEXT_FRAMES), marked)
      and np.any(det2.model.preprocessor.get_features(CONTEXT_FRAMES)[0, 0] == marked[0, -1]))
det2.feed(silence)
after = det2.model.preprocessor.get_features(CONTEXT_FRAMES)
check(f"nach {CONTEXT_FRAMES} frischen Frames ist das Fenster frei von der Marke",
      not np.any(np.all(after[0] == marked[0, 0], axis=-1)))

# ------------------------------------------------------------------- Audiopfad
print("\n[3] Audiopfad: Messung darf den Sprachpfad nicht veraendern")
import array as _array


def left_channel_original(pcm):
    a = _array.array("h")
    a.frombytes(pcm)
    return a[0::2].tobytes()


# Bewusst UNsymmetrisch: Quadrate von -320..-1 und 1..320 sind identisch, damit
# haette der Vergleich beider Kanaele nichts geprueft.
buf = stereo(list(range(-320, 0)), [200] * 320)
check("left_channel() liefert byteidentisch dasselbe wie vorher",
      left_channel(buf) == left_channel_original(buf))
ms_l, ms_r = channel_power(buf)
exp_l = float(np.mean(np.arange(-320, 0, dtype=np.float64) ** 2))
exp_r = 200.0 ** 2
check("channel_power misst links richtig", abs(ms_l - exp_l) < 1e-6, f"{ms_l} != {exp_l}")
check("channel_power misst rechts richtig", abs(ms_r - exp_r) < 1e-6, f"{ms_r} != {exp_r}")
check("channel_power verwechselt die Kanaele nicht", ms_l != ms_r)
check("ein toter linker Kanal bleibt bei lebendem rechten sichtbar",
      channel_power(stereo([0] * 320, [9000] * 320)) == (0.0, 9000.0 ** 2))
check("digitale Stille ist exakt null", channel_power(stereo([0] * 320, [0] * 320)) == (0.0, 0.0))
check("dbfs(Vollaussteuerung) ist 0 dB", abs(dbfs(32768.0 ** 2)) < 0.01)

# ------------------------------------------------------------------ Urteilstabelle
print("\n[4] Urteil: jeder Zweig an einer gemessenen Groesse")


def snap(**kw):
    base = dict(chunks=500, chunks_expected=500, chunks_total=500, frames=125,
                frames_nonzero=5, rms_l_mean_db=-55.0, rms_l_peak_db=-30.0,
                rms_r_mean_db=-120.0, rms_r_peak_db=-120.0, zero_l=0, zero_r=500,
                score_max=0.02, gap_max_ms=25, since_last_chunk_ms=20,
                since_last_nonzero_s=1, sessions=0, phantoms_suppressed=0, feed_drops=0,
                window_s=10.0)
    base.update(kw)
    return base


check("gesund", classify(snap()) == "healthy")
check("kein Zufluss", classify(snap(chunks=0)) == VERDICT_NO_FRAMES)
check("stockender Zufluss", classify(snap(chunks=100)) == VERDICT_NO_FRAMES)
check("linker Kanal exakt tot", classify(snap(zero_l=500)) == VERDICT_LEFT_DEAD)
check("stiller Raum ist KEIN Befund",
      classify(snap(rms_l_peak_db=-58.0, frames_nonzero=0)) == VERDICT_QUIET)
check("stiller Raum loest nichts aus", VERDICT_QUIET not in ACTIONABLE)
check("Sprache da, Detektor flach",
      classify(snap(rms_l_peak_db=-20.0, frames_nonzero=0)) == VERDICT_DETECTOR_FLAT)
check("Sprache da, Detektor streut -> gesund",
      classify(snap(rms_l_peak_db=-20.0, frames_nonzero=3)) == "healthy")
check("stiller Raum mit totem rechten Kanal ist normal (AEC-Referenz)",
      classify(snap(zero_r=500)) == "healthy")

# --------------------------------------------------------------------- Bremse
print("\n[5] Reparatur-Bremse")
sys.path.insert(0, "/home/gregor/solvio-satellite")
import importlib.util                                                  # noqa: E402
spec = importlib.util.spec_from_file_location(
    "sat_mod", "/home/gregor/solvio-satellite/satellite.py")
sat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sat)

now = [1000.0]
r = sat.Repair(clock=lambda: now[0])
check("ein einzelner Befund reicht nicht", r.observe(VERDICT_NO_FRAMES) is None)
check("zwei reichen auch nicht", r.observe(VERDICT_NO_FRAMES) is None)
check("drei reichen", r.observe(VERDICT_NO_FRAMES) == VERDICT_NO_FRAMES)
r.started()
check("direkt danach nicht nochmal (Abkuehlzeit)",
      all(r.observe(VERDICT_NO_FRAMES) is None for _ in range(5)))
now[0] += sat.REPAIR_COOLDOWN_S + 1
check("nach der Abkuehlzeit ein zweiter Versuch",
      any(r.observe(VERDICT_NO_FRAMES) == VERDICT_NO_FRAMES for _ in range(3)))
r.started()
now[0] += sat.REPAIR_COOLDOWN_S + 1
check("aber kein dritter — es wird gemeldet, nicht weiterprobiert",
      all(r.observe(VERDICT_NO_FRAMES) is None for _ in range(10)))

now[0] += sat.REPAIR_HOLD_S + 1
r.observe("healthy")
check("nach der Haltefrist zaehlt ein Ausfall als neuer Vorfall",
      r.attempts == 0)
check("und bekommt wieder ein Versuchskonto",
      [r.observe(VERDICT_NO_FRAMES) for _ in range(3)][-1] == VERDICT_NO_FRAMES)

r2 = sat.Repair(clock=lambda: now[0])
for _ in range(3):
    r2.observe(VERDICT_NO_FRAMES)
check("ein Wechsel des Befunds setzt die Serie zurueck",
      r2.observe(VERDICT_DETECTOR_FLAT) is None)
check("ein gesundes Fenster setzt die Serie zurueck",
      (r2.observe("healthy"), r2.observe(VERDICT_DETECTOR_FLAT))[1] is None)

# ---------------------------------------------------------------- Telemetrie
print("\n[6] Telemetrie zaehlt, was sie behauptet")
t = [0.0]
w = WakeTelemetry(clock=lambda: t[0])
for i in range(500):
    t[0] += 0.02
    w.on_chunk(0.0, 0.0)
s1 = w.snapshot()
check("500 Haeppchen in 10 s ergeben 500 erwartete",
      s1["chunks_expected"] == 500, f"-> {s1['chunks_expected']}")
check("exakte Stille wird als solche gezaehlt", s1["zero_l"] == 500)
check("exakte Stille links -> Befund", classify(s1) == VERDICT_LEFT_DEAD)

w2 = WakeTelemetry(clock=lambda: t[0])
w2.on_score(0.0)
w2.on_score(1e-5)
w2.on_score(0.4)
s2 = w2.snapshot()
check("exakte Null zaehlt nicht als Detektorleben", s2["frames_nonzero"] == 1)
check("aber der Hoechstwert wird gefuehrt", s2["score_max"] == 0.4)
check("Fenster wird nach snapshot() geleert", w2.snapshot()["frames"] == 0)
check("Gesamtzahl ueberdauert das Fenster", w.snapshot()["chunks_total"] == 500)

print("\n" + ("ALLE TESTS GRUEN" if not FAILED else f"FEHLGESCHLAGEN: {FAILED}"))
sys.exit(1 if FAILED else 0)
