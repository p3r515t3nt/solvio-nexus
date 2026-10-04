#!/usr/bin/env python3
"""Was der Dauerlauf des Satelliten wirklich zeigt — aus dem Journal, nicht aus Erinnerung.

Der Vorgaenger dieses Milestones scheiterte daran, dass niemand das Journal
ausgerechnet hat: die Behauptung "der Pi wird nach Stunden taub" stuetzte sich
auf zwei Luecken, die kuerzer waren als eine im selben Prozess nachgewiesene
gesunde Stille. Dieses Skript rechnet, damit das nicht noch einmal passiert.

Es liest ausschliesslich das Journal des Satelliten ueber SSH — lesend, ohne
Zustand auf dem Pi zu veraendern. Es interpretiert nichts hinzu: ein niedriger
Score wird NIE als Weckversuch gewertet. Nur Datensaetze, die der Satellit im
scharfgeschalteten Testmodus selbst als absichtlichen Versuch markiert hat,
zaehlen als Versuch.

    python3 scripts/satellite_soak_report.py            # seit dem letzten Dienststart
    python3 scripts/satellite_soak_report.py --since "2026-08-23 22:38"
    python3 scripts/satellite_soak_report.py --json     # maschinenlesbar
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import re
import statistics
import subprocess
import sys
from collections import Counter
from datetime import datetime

PI = "gregor@192.168.178.194"
KEY = "~/.ssh/solvio_satellite"
UNIT = "solvio-satellite"

TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
TELEM = re.compile(
    r"\[TELEM\] chunks=(\d+)/(\d+) frames=(\d+) nz=(\d+) "
    r"rmsL=(-?[\d.]+)/(-?[\d.]+) rmsR=(-?[\d.]+)/(-?[\d.]+) "
    r"smax=([\d.]+) gap=(\d+)ms urteil=(\w+)")
WAKE = re.compile(r"\[WAKE_DETECTED\] score ([\d.]+) \(\+(\d+) ms Vorlauf\)")
PHANTOM = re.compile(r"\[PHANTOM\] score ([\d.]+)")
TRIAL = re.compile(r"\[TRIAL\] (\{.*\})$")
CLOSING = re.compile(r"\[CLOSING\] Session beendet \(reason=(\w+)\)")


def _run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        print(out.stderr[:400], file=sys.stderr)
        raise SystemExit(f"journalctl fehlgeschlagen ({out.returncode})")
    return out.stdout


def fetch(since: str | None) -> list[str]:
    # Der Zeitraum wandert durch eine Fernshell und traegt ein Leerzeichen —
    # ohne shlex.quote zerfaellt "2026-08-23 22:38" dort in zwei Argumente und
    # journalctl lehnt es ab. Der Fehler war einmal da; er bleibt es nicht.
    remote = f"journalctl -u {UNIT} --since {shlex.quote(since or '-24 hours')} --no-pager -o short-iso"
    key = os.path.expanduser(KEY)
    return _run(["ssh", "-o", "BatchMode=yes", "-i", key, PI, remote]).splitlines()


def _ts(line: str):
    m = TS.match(line)
    return datetime.fromisoformat(m.group(1)) if m else None


def analyse(lines: list[str]) -> dict:
    telem, wakes, phantoms, trials, closings = [], [], [], [], []
    starts, stalls, repairs = [], [], []
    for line in lines:
        when = _ts(line)
        if "Started solvio-satellite" in line or "lausche lokal" in line:
            starts.append((when, line))
        if m := TELEM.search(line):
            telem.append({
                "t": when, "chunks": int(m.group(1)), "expected": int(m.group(2)),
                "frames": int(m.group(3)), "nz": int(m.group(4)),
                "rms_l_mean": float(m.group(5)), "rms_l_peak": float(m.group(6)),
                "rms_r_mean": float(m.group(7)), "rms_r_peak": float(m.group(8)),
                "smax": float(m.group(9)), "gap": int(m.group(10)),
                "verdict": m.group(11)})
        if m := WAKE.search(line):
            wakes.append({"t": when, "score": float(m.group(1)), "preroll": int(m.group(2))})
        if m := PHANTOM.search(line):
            phantoms.append({"t": when, "score": float(m.group(1))})
        if m := CLOSING.search(line):
            closings.append({"t": when, "reason": m.group(1)})
        if m := TRIAL.search(line):
            try:
                rec = json.loads(m.group(1))
                rec["t"] = when
                trials.append(rec)
            except ValueError:
                pass
        if "[STALL]" in line:
            stalls.append(when)
        if "[REPAIR]" in line:
            repairs.append((when, line.split("[REPAIR]", 1)[1].strip()))
    return {"telem": telem, "wakes": wakes, "phantoms": phantoms, "trials": trials,
            "closings": closings, "starts": starts, "stalls": stalls, "repairs": repairs}


#: Die Weckschwelle. Sie steht hier nur, um ein Etikett nachzurechnen — der
#: Satellit entscheidet mit seiner eigenen.
THRESHOLD = 0.5


def rederive_outcome(rec: dict) -> str:
    """Das Etikett aus den Rohfeldern neu bilden.

    Der Satellit vergibt `refractory`, sobald die Sperre beim Oeffnen des
    Fensters noch lief — auch dann, wenn das Fenster danach noch 58 Sekunden
    voll scharf war und der Score nie in die Naehe der Schwelle kam. Bei
    60-s-Fenstern, die genau am Sitzungsende aufgehen, waere damit JEDER Versuch
    nach einem Gespraech als "bauartbedingt unterdrueckt" abgestempelt — und das
    ist ausgerechnet die Messung, um die es geht.

    Die Sperre dauert 16 Frames, also 1,28 s. Sie erklaert einen ausgebliebenen
    Weckvorgang nur dann, wenn ueberhaupt etwas zu unterdruecken war: ein Score
    ueber der Schwelle. Sonst ist das Fenster ein gewoehnliches `miss`.

    Die Rohdaten bleiben unangetastet; nur die Lesart wird korrigiert.
    """
    if rec.get("outcome") == "hit":
        return "hit"
    chunks = rec.get("chunks", 0)
    expected = rec.get("chunks_expected", 0) or 0
    if chunks == 0 or (expected and chunks * 2 < expected):
        return "no_audio"
    if chunks and rec.get("zero_l", 0) == chunks:
        return "no_audio"
    if rec.get("outcome") == "not_idle":
        return "not_idle"
    if rec.get("refractory_left", 0) > 0 and rec.get("score_max", 0.0) >= THRESHOLD:
        return "refractory"
    return "miss"


def _dist(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    return {"n": len(s), "min": round(s[0], 3), "median": round(statistics.median(s), 3),
            "max": round(s[-1], 3),
            "mean": round(statistics.fmean(s), 3)}


def report(data: dict) -> dict:
    telem = data["telem"]
    windows = len(telem)
    # Framekontinuitaet: das Verhaeltnis gelieferter zu erwarteten Haeppchen.
    # Es ist die einzige Groesse, die "der Zufluss stockt" beweisen kann.
    deficits = [t["expected"] - t["chunks"] for t in telem]
    worst = max(deficits) if deficits else 0
    # Ein Wecken mit weniger als 25 Haeppchen Vorlauf kurz nach einem Sessionende
    # ist die Signatur des Phantom-Wake — genau die, die vor dem Fix 12 von 28
    # Ereignissen trug.
    short_preroll = [w for w in data["wakes"] if w["preroll"] < 500]
    suspicious = []
    for w in short_preroll:
        recent = [c for c in data["closings"] if c["t"] and w["t"]
                  and 0 <= (w["t"] - c["t"]).total_seconds() <= 2]
        if recent:
            suspicious.append(w)
    # NUR markierte Versuche zaehlen. Beifang (`src == "burst"`) ist Kontext,
    # niemals Fehlerstatistik — ein Datensatz, den der Pegel geoeffnet hat, sagt
    # nichts darueber, ob ein Mensch etwas versucht hat.
    marked = [t for t in data["trials"] if t.get("src") == "mark"]
    bycatch = [t for t in data["trials"] if t.get("src") == "burst"]
    for t in marked:
        t["outcome_roh"] = t.get("outcome")
        t["outcome"] = rederive_outcome(t)
    by_outcome = Counter(t["outcome"] for t in marked)
    umetikettiert = sum(1 for t in marked if t["outcome"] != t["outcome_roh"])
    hits = [t for t in marked if t.get("outcome") == "hit"]
    # Ein Fehlschlag zaehlt nur, wenn der Satellit ueberhaupt haette wecken
    # koennen: `refractory` und `not_idle` sind Konstruktion, `no_audio` ist
    # Taubheit des Zuflusses, nicht der Erkennung.
    misses = [t for t in marked if t.get("outcome") == "miss"]
    uptimes = []
    if telem:
        span = (telem[-1]["t"] - telem[0]["t"]).total_seconds() if telem[0]["t"] else 0
        uptimes.append(span)
    return {
        "fenster": windows,
        "beobachtungsdauer_s": int(uptimes[0]) if uptimes else 0,
        "framekontinuitaet": {
            "groesstes_defizit_haeppchen": worst,
            "fenster_mit_defizit_ueber_2_prozent": sum(1 for t in telem
                                                       if t["expected"] and
                                                       (t["expected"] - t["chunks"]) > t["expected"] * 0.02),
            "groesste_luecke_ms": max((t["gap"] for t in telem), default=0),
        },
        "urteile": dict(Counter(t["verdict"] for t in telem)),
        "score_max_je_fenster": _dist([t["smax"] for t in telem]),
        "pegel_links_spitze_db": _dist([t["rms_l_peak"] for t in telem]),
        "wakes": {"n": len(data["wakes"]),
                  "scores": _dist([w["score"] for w in data["wakes"]]),
                  "vorlauf_verteilung": dict(Counter(w["preroll"] for w in data["wakes"]))},
        "phantom_signatur_nach_sessionende": len(suspicious),
        "phantome_unterdrueckt": len(data["phantoms"]),
        "sessions_beendet": len(data["closings"]),
        "stalls": len(data["stalls"]),
        "reparaturen": [r[1] for r in data["repairs"]],
        "dienststarts": len(data["starts"]),
        "versuche": {
            "markiert": len(marked),
            "beifang": len(bycatch),
            "ausgaenge": dict(by_outcome),
            "etikett_korrigiert": umetikettiert,
            "treffer": len(hits),
            "echte_fehlschlaege": len(misses),
            "score_treffer": _dist([t.get("score_max", 0.0) for t in hits]),
            "score_fehlschlaege": _dist([t.get("score_max", 0.0) for t in misses]),
            "pegel_treffer_spitze_db": _dist([t.get("rms_l_peak_db", 0.0) for t in hits]),
            "pegel_fehlschlaege_spitze_db": _dist([t.get("rms_l_peak_db", 0.0)
                                                   for t in misses]),
            "nach_sessionende_s": _dist([t["since_session_end_s"] for t in marked
                                         if t.get("since_session_end_s") is not None]),
            "laufzeit_bei_versuch_s": _dist([t.get("uptime_s", 0) for t in marked]),
            "framekontinuitaet_je_versuch": {
                "schlechtestes_verhaeltnis": round(min(
                    (t["chunks"] / max(1, t["chunks_expected"]) for t in marked),
                    default=1.0), 3),
                "groesste_luecke_ms": max((t.get("gap_max_ms", 0) for t in marked),
                                          default=0),
            },
            "umgebung": {
                "temp_c": _dist([t["temp_c"] for t in marked if "temp_c" in t]),
                "rss_mb": _dist([t["rss_mb"] for t in marked if "rss_mb" in t]),
            },
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = analyse(fetch(args.since))
    out = report(data)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if out["versuche"]["gesamt"] == 0:
        print("\nHinweis: keine markierten Versuche im Fenster. Niedrige Scores "
              "werden hier NICHT als Weckversuche gewertet — dafuer gibt es den "
              "Testmodus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
