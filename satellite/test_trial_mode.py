#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Der Testmodus: was er zusagt, und was er nicht darf.

Ausfuehren auf dem Pi:  ./venv/bin/python test_trial_mode.py

Die wichtigste Zusage steht in Block 1: **ein markierter Versuch ergibt genau
einen Datensatz.** Der erste Entwurf wollte Versuche akustisch erkennen; die
Messung hat gezeigt, dass das leise Versuche verwirft, bei Dauerpegel durchdreht
und zwei dichte Rufe still verschmilzt. Absicht kommt vom Menschen — also wird
sie hier auch von dort genommen, und diese Tests halten das fest.

Die zweitwichtigste steht in Block 5: **der Beobachter darf den Satelliten nicht
umbringen.** Er laeuft fuenfzigmal je Sekunde im Audiopfad, und die Hauptschleife
faengt dort nur `asyncio.TimeoutError`.
"""
import sys

sys.path.insert(0, "/home/gregor/solvio-satellite")

from solvio_sat import trial as T                                      # noqa: E402
from solvio_sat.telemetry import WakeTelemetry                         # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    """Ein fehlender Schluessel ist ein Fehlschlag, kein Abbruch.

    Der Vorgaengerstand kennt `rms_l_at_peak_db` nicht, und ein KeyError hat den
    ganzen Lauf beendet — samt aller Bloecke danach. Ein Testfile, das beim
    ersten fehlenden Feld abbricht, verdeckt genau das, was es zeigen soll."""
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def ms(dbfs_value):
    """Mittlere Leistung zu einem Pegel in dBFS."""
    return (10 ** (dbfs_value / 20.0) * 32768.0) ** 2


LOUD = ms(-20.0)
ROOM = ms(-57.0)
QUIET = ms(-67.0)


def feed(rec, clock, seconds, power, score=0.0):
    """Haeppchen einspeisen, wie es die echte Messung tut."""
    for i in range(int(seconds / 0.02)):
        clock.advance(0.02)
        rec.on_chunk(power, 0.0)
        if i % 4 == 3:
            rec.on_score(score)


# ------------------------------------------------------------- 1:1-Zusage
print("\n[1] Ein markierter Versuch ergibt genau einen Datensatz")
c = Clock()
r = T.TrialRecorder(threshold=0.5, clock=c)
r.arm(marks=20, deadline_s=900)
feed(r, c, 20.0, ROOM)                      # Boden einschwingen lassen
r.mark()
feed(r, c, T.TRIAL_WINDOW_S + 0.5, LOUD, score=0.2)
r.tick()
recs = r.drain()
marked = [x for x in recs if x["src"] == "mark"]
check("genau ein markierter Datensatz", len(marked) == 1, f"-> {len(recs)} gesamt")
check("er traegt den Hoechstscore", marked and marked[0]["score_max"] == 0.2)
check("er traegt einen Score-Verlauf", marked and len(marked[0]["scores"]) > 1)
check("er traegt Frame-Kontinuitaet",
      marked and marked[0]["chunks"] > 300 and marked[0]["frames"] > 80)
check("er traegt Laufzeit und Sessionzahl",
      marked and "uptime_s" in marked[0] and "sessions_total" in marked[0])

print("\n[2] Zwei dichte Versuche verschmelzen nicht")
c = Clock(); r = T.TrialRecorder(threshold=0.5, clock=c); r.arm(marks=20, deadline_s=900)
feed(r, c, 20.0, ROOM)
r.mark()
feed(r, c, 0.4, LOUD, score=0.3)
r.mark()                                     # zweiter Versuch nach 400 ms
feed(r, c, T.TRIAL_WINDOW_S + 0.5, LOUD, score=0.4)
r.tick()
marked = [x for x in r.drain() if x["src"] == "mark"]
check("zwei Markierungen -> zwei Datensaetze", len(marked) == 2, f"-> {len(marked)}")
check("der zweite ist als dicht gekennzeichnet",
      len(marked) == 2 and marked[1]["close_to_prev"] is True)
check("die Bilanz zaehlt beide", r.marks_seen == 2 and r.records_marked == 2)

print("\n[3] Dauerpegel verbraucht den Testmodus nicht")
c = Clock(); r = T.TrialRecorder(threshold=0.5, clock=c); r.arm(marks=20, deadline_s=900)
# OHNE diese Zeile oeffnet gar kein Burst (der Erkenner laeuft nur im IDLE) —
# und der Test waere leer, statt zu pruefen.
r.on_state("IDLE")
feed(r, c, 10.0, ROOM)
feed(r, c, 120.0, LOUD, score=0.05)          # zwei Minuten Fernseher
bursts = [x for x in r.drain() if x["src"] == "burst"]
check("ueberhaupt ein Burst erkannt", len(bursts) >= 1, "-> 0, der Test pruefte nichts")
# Ohne Wiederscharf-Sperre waeren das rund 15 Datensaetze (alle 8 s einer).
check("Beifang bleibt selten", len(bursts) <= 3, f"-> {len(bursts)} in 120 s")
check("Markierungen sind unverbraucht", r.marks_left == 20)
check("Zwangsende ist gekennzeichnet", any(b["truncated"] for b in bursts) or not bursts)

print("\n[4] Die fuenf Ausgaenge trennen, was sich wirklich unterscheidet")
c = Clock(); r = T.TrialRecorder(threshold=0.5, clock=c); r.arm(marks=20, deadline_s=900)
feed(r, c, 20.0, ROOM)
r.on_state("IDLE", refractory_left=0)
r.mark(); feed(r, c, 1.0, LOUD, score=0.9); r.on_wake(0.9)
feed(r, c, T.TRIAL_WINDOW_S, LOUD); r.tick()
check("hit, wenn geweckt wurde", [x for x in r.drain() if x["src"] == "mark"][0]["outcome"] == "hit")

r.on_state("IDLE", refractory_left=0)
r.mark(); feed(r, c, T.TRIAL_WINDOW_S + 0.2, LOUD, score=0.2); r.tick()
check("miss, wenn IDLE und nichts kam",
      [x for x in r.drain() if x["src"] == "mark"][0]["outcome"] == "miss")

r.on_state("IDLE", refractory_left=12)
r.mark(); feed(r, c, T.TRIAL_WINDOW_S + 0.2, LOUD, score=0.8); r.tick()
rec = [x for x in r.drain() if x["src"] == "mark"][0]
check("refractory, wenn die Sperre lief", rec["outcome"] == "refractory")
check("und die Sperre steht im Datensatz", rec["refractory_left"] == 12)

r.on_state("ACTIVE", refractory_left=0)
r.mark(); feed(r, c, T.TRIAL_WINDOW_S + 0.2, LOUD, score=0.2); r.tick()
check("not_idle, wenn gar nicht geweckt werden konnte",
      [x for x in r.drain() if x["src"] == "mark"][0]["outcome"] == "not_idle")

c2 = Clock(); r2 = T.TrialRecorder(threshold=0.5, clock=c2); r2.arm(marks=5, deadline_s=900)
r2.on_state("IDLE", refractory_left=0)
r2.mark()
for _ in range(int(T.TRIAL_WINDOW_S / 0.02)):
    c2.advance(0.02); r2.on_chunk(0.0, 0.0)          # exakt digitale Stille
c2.advance(0.1); r2.tick()
check("no_audio, wenn der Kanal exakt null liefert",
      [x for x in r2.drain() if x["src"] == "mark"][0]["outcome"] == "no_audio")

print("\n[5] Der Beobachter kann den Satelliten nicht toeten")
class Exploding:
    broken = None

    def on_chunk(self, *a):
        raise RuntimeError("kaputt")

    def on_score(self, *a):
        raise RuntimeError("kaputt")

t = WakeTelemetry()
t.observer = Exploding()
t.on_chunk(1.0, 1.0)          # darf NICHT werfen
t.on_score(0.3)
check("eine Ausnahme aus dem Hook schlaegt nicht durch", True)
check("der Beobachter ist danach dauerhaft abgeschaltet",
      t.observer.broken == "RuntimeError")
check("und das steht im Fensterbericht", t.snapshot()["observer_broken"] == "RuntimeError")
t2 = WakeTelemetry(); t2.observer = Exploding()
before = t2.chunks_total
t2.on_chunk(1.0, 1.0); t2.on_chunk(1.0, 1.0)
check("die Messung selbst zaehlt unbeirrt weiter", t2.chunks_total == before + 2)

print("\n[6] Der Bodenverfolger faellt schnell und steigt langsam")
c = Clock(); r = T.TrialRecorder(clock=c)
feed(r, c, 5.0, LOUD)
high = r.floor_db
feed(r, c, 5.0, QUIET)
low = r.floor_db
check("er faellt in 5 s auf die Stille", low < -60.0, f"-> {low}")
feed(r, c, 5.0, LOUD)
check("er steigt in 5 s hoechstens 2,5 dB", r.floor_db - low <= 2.6,
      f"-> {r.floor_db - low:.1f} dB")
check("die Torschwelle faellt nie unter die Untergrenze",
      r._gate_db() >= T.TRIAL_GATE_ABS_DBFS)
check("Sprache zieht die Torschwelle nicht hoch", high > low)

print("\n[7] Kein Audio, keine Fremdfelder, kein Griff in die Messung")
c = Clock(); r = T.TrialRecorder(clock=c); r.arm(marks=3, deadline_s=900)
feed(r, c, 20.0, ROOM)
r.mark(); feed(r, c, T.TRIAL_WINDOW_S + 0.2, LOUD, score=0.3); r.tick()
rec = [x for x in r.drain() if x["src"] == "mark"][0]
for key, value in rec.items():
    check(f"  Feld {key} ist ein Skalar",
          isinstance(value, (int, float, str, bool, type(None)))
          or (key == "scores" and all(isinstance(v, int) for v in value)),
          f"-> {type(value)}")
src = open("/home/gregor/solvio-satellite/solvio_sat/trial.py").read()
check("der Recorder fasst snapshot() nicht an", "snapshot(" not in src)
check("er schreibt nichts ins Log", "print(" not in src)
check("die Flagdatei liegt in /tmp, nicht auf der Karte", T.TRIAL_FLAG.startswith("/tmp/"))

print("\n[8] Die Bilanz zaehlt nur Markierungen")
c = Clock(); r = T.TrialRecorder(threshold=0.5, clock=c); r.arm(marks=5, deadline_s=900)
r.on_state("IDLE")
feed(r, c, 10.0, ROOM)
feed(r, c, 90.0, LOUD, score=0.05)          # nur Beifang, kein Mensch
b = r.disarm()
check("kein Versuch markiert", b["markiert"] == 0 and b["aufgezeichnet"] == 0)
check("und deshalb auch kein Fehlschlag", b["miss"] == 0, f"-> miss {b['miss']}")
check("der Beifang steht getrennt da", b["beifang"] > 0)

print("\n[9] Der Pegel wird zum Score gemessen, nicht zum Fenster")
c = Clock(); r = T.TrialRecorder(threshold=0.5, clock=c); r.arm(marks=3, deadline_s=900)
r.on_state("IDLE")
feed(r, c, 20.0, ROOM)
r.mark()
feed(r, c, 1.0, ms(-8.0), score=0.30)     # laut, schlechter Score
feed(r, c, 1.0, ms(-25.0), score=0.90)    # leise, guter Score
feed(r, c, T.TRIAL_WINDOW_S, ROOM)
r.tick()
rec = [x for x in r.drain() if x["src"] == "mark"][0]
check("Fensterspitze ist der lauteste Moment", abs(rec["rms_l_peak_db"] - (-8.0)) < 0.5,
      f"-> {rec['rms_l_peak_db']}")
_at = rec.get("rms_l_at_peak_db")
check("Pegel zum Hoechstscore ist der LEISE Moment",
      _at is not None and abs(_at - (-25.0)) < 0.5, f"-> {_at}")
check("die beiden sind unterscheidbar", rec["rms_l_peak_db"] != _at)

print("\n[11] Die Korrekturen aus a92dfa4 — jede einzeln festgenagelt")
# Diese drei Faelle waren bis a92dfa4 falsch und von keinem Test gedeckt. Gegen
# den Vorgaengerstand geprueft: alle drei werden dort rot. Ein Test, der eine
# Korrektur nicht von ihrem Vorgaenger unterscheidet, nagelt nichts fest.

# (a) Die Sperre erklaert nur, was sie wirklich unterdrueckt hat.
c = Clock(); r = T.TrialRecorder(threshold=0.5, clock=c); r.arm(marks=3, deadline_s=900)
feed(r, c, 20.0, ROOM)
r.on_state("IDLE", refractory_left=14)
r.mark(); feed(r, c, T.TRIAL_WINDOW_S + 0.2, LOUD, score=0.02); r.tick()
rec = [x for x in r.drain() if x["src"] == "mark"][0]
check("kleiner Score bei laufender Sperre ist ein miss, kein refractory",
      rec["outcome"] == "miss", f"-> {rec['outcome']}")
c = Clock(); r = T.TrialRecorder(threshold=0.5, clock=c); r.arm(marks=3, deadline_s=900)
feed(r, c, 20.0, ROOM)
r.on_state("IDLE", refractory_left=14)
r.mark(); feed(r, c, T.TRIAL_WINDOW_S + 0.2, LOUD, score=0.8); r.tick()
rec = [x for x in r.drain() if x["src"] == "mark"][0]
check("grosser Score bei laufender Sperre bleibt refractory",
      rec["outcome"] == "refractory", f"-> {rec['outcome']}")

# (b) Der Abstand zum Sessionende gehoert zum Fensteranfang.
c = Clock(); r = T.TrialRecorder(threshold=0.5, clock=c); r.arm(marks=3, deadline_s=900)
feed(r, c, 20.0, ROOM)
r.on_session_end()          # Sitzung endet
feed(r, c, 5.0, ROOM)       # fuenf Sekunden vergehen
r.on_state("IDLE", refractory_left=0)
r.mark()                    # Fenster oeffnet 5 s nach dem Sessionende
feed(r, c, 2.0, ROOM)
r.on_session_end()          # waehrend des Fensters endet eine WEITERE Sitzung
feed(r, c, T.TRIAL_WINDOW_S, ROOM); r.tick()
rec = [x for x in r.drain() if x["src"] == "mark"][0]
check("Abstand zum Sessionende ist positiv und vom Fensteranfang gemessen",
      rec["since_session_end_s"] is not None and 4.9 <= rec["since_session_end_s"] <= 5.1,
      f"-> {rec['since_session_end_s']}")

# (c) Der Pegel zum Hoechstscore ist eigenstaendig gefuehrt.
check("rms_l_at_peak_db existiert als eigenes Feld", "rms_l_at_peak_db" in rec)
check("Abstand zum Sessionende ist nicht negativ",
      (rec.get("since_session_end_s") or 0) >= 0, f"-> {rec.get('since_session_end_s')}")

# (d) Flagdatei entfernen ruestet ab. Das lebt in satellite.py:trial_loop und
#     nicht in trial.py — hier bleibt nur eine Quelltextpruefung, und die ist
#     schwaecher als die drei Verhaltenstests darueber. Sie steht trotzdem, weil
#     genau diese Luecke mich einmal acht Stunden lang eine falsche Aussage
#     machen liess.
_src = open("/home/gregor/solvio-satellite/satellite.py").read()
check("Flagdatei entfernen loest disarm() aus",
      "elif trial.armed:" in _src and "Flagdatei entfernt" in _src)
check("... und das steht als Quelltextpruefung da, nicht als Verhaltenstest", True)

print("\n[12] Unscharf gibt er nichts aus")

c = Clock(); r = T.TrialRecorder(clock=c)          # NICHT scharf
feed(r, c, 60.0, LOUD, score=0.4)
check("kein Beifang ohne Scharfschaltung", r.drain() == [])
check("aber der Boden ist eingeschwungen", r.floor_db is not None)

print("\n" + ("ALLE TESTS GRUEN" if not FAILED else f"FEHLGESCHLAGEN: {FAILED}"))
sys.exit(1 if FAILED else 0)
