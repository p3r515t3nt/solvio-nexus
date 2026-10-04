"""Ist die Sicherung ausserhalb des Hauses in Ordnung? — und wann NICHT.

Dieselbe Dreiteilung wie die lokale Sicherung: `collect()` misst, `assess()`
urteilt, `summary()` erzaehlt. Die Trennung ist kein Stil: `assess()` gibt
Zeichenketten zurueck, damit dieses Modul ohne das Kontrollzentrum pruefbar
bleibt — die Sonde wandelt sie in `State` um.

**Der eine Satz, an dem alles haengt (§12/§18):**

> Ein Upload-Erfolg macht NIE gruen. `healthy` verlangt zusaetzlich einen
> frischen Restore-Beweis.

Das ist die Antwort auf T9 („Upload erfolgreich gemeldet, Restore
unmoeglich"). Ein Objekt, das beim Anbieter liegt, ist ein Objekt — kein
wiederherstellbares Backup. Erst der Beweis macht daraus eine Zusage.

**Fail-closed heisst hier woertlich:** was nicht nachweisbar in Ordnung ist,
ist nicht gruen. Kein Beweis vorhanden → nicht gruen. Beweis ueberfaellig →
nicht gruen. Buch unlesbar → nicht gruen. Zustandsdatei kaputt → nicht gruen.
`None` heisst „nicht gemessen", nie „in Ordnung" — dieselbe Regel wie beim
Diagnostiker.

**Die Bewertung liest zwei Quellen und erzeugt keine dritte:** das
Offsite-Buch (kanonische Transportwahrheit aus B3) und
`~/.solvio/offsite/state.json` (Betriebszustand). Beide liegen oertlich —
die Gesundheit ist deshalb auch ohne Netz abfragbar (§12). Was der Anbieter
sagt, kommt ausschliesslich ueber das Buch herein: AWS besitzt Providerzustand,
SOLVIO besitzt die Wahrheit.
"""
from __future__ import annotations

import os
import time
from typing import Any

from solvio.storage.offsite import config as OC
from solvio.storage.offsite import identity as OI
from solvio.storage.offsite import job as OJ
from solvio.storage.offsite import ledger as OL
from solvio.storage.offsite import verify as OV

#: Ab hier ist eine Offsite-Generation alt (§12). Der Job laeuft
#: kalendertaeglich; 26 Stunden lassen einen verspaeteten Lauf durch und
#: schlagen bei einem ausgefallenen an.
STALE_AFTER_SECONDS = 26 * 3600.0

#: Ab hier ist der Restore-Beweis ueberfaellig (§12). Er laeuft
#: woechentlich; acht Tage lassen einen verschobenen Termin durch.
PROOF_STALE_AFTER_SECONDS = OV.PROOF_FRESH_SECONDS

#: Ab so vielen Fehlschlaegen hintereinander ist es eine Lage, kein Zucken.
FAILURES_BEFORE_DEGRADED = 2


def _age_text(seconds: float) -> str:
    """Ein Alter, wie man es sagt."""
    if seconds < 90 * 60:
        return f"vor {max(1, int(seconds // 60))} Minuten"
    if seconds < 36 * 3600:
        return f"vor {int(seconds // 3600)} Stunden"
    return f"vor {int(seconds // 86400)} Tagen"


def collect(*, now: float | None = None,
            book: OL.OffsiteLedger | None = None,
            state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Misst. Bewusst ohne Urteil — `assess()` urteilt.

    Wirft nie: eine Messung, die am eigenen Fehler stirbt, macht aus einer
    Unklarheit einen Ausfall. Was nicht messbar war, steht als `None` oder
    als Fehlertext drin — und `None` ist nie „in Ordnung".
    """
    now = now if now is not None else time.time()
    report: dict[str, Any] = {
        "configured": False, "enabled": False, "config_error": "",
        "identity": None, "ledger_error": "",
        "last_generation": None, "open_generations": [],
        "last_proof": None, "proof_age_seconds": None,
        "last_proof_failed": False,
        "state": {}, "state_readable": False,
    }

    # -- Konfiguration ----------------------------------------------------
    try:
        cfg = OC.load()
        report["configured"] = cfg is not None
        report["enabled"] = bool(cfg and cfg.enabled)
        if cfg is not None:
            report["recipient_version"] = cfg.recipient_version
            report["buckets"] = {k: cfg.bucket(k)
                                 for k in ("daily", "weekly", "monthly")}
            report["expected_classes"] = sorted(cfg.classes)
    except OC.OffsiteConfigError as exc:
        report["config_error"] = str(exc)

    # -- Schluesselbund: gesperrt ist NICHT dasselbe wie fehlend ----------
    try:
        version = int(report.get("recipient_version") or 1)
        report["identity"] = OI.present(version)
    except OI.OffsiteKeychainLocked:
        report["identity"] = None
        report["identity_locked"] = True
    except OI.OffsiteIdentityError as exc:
        report["identity"] = None
        report["identity_error"] = str(exc)

    # -- Betriebszustand --------------------------------------------------
    path = OJ.state_path()
    if os.path.exists(path):
        raw = OJ.load_state()
        # `load_state` schluckt kaputtes JSON still. Eine leere Datei, die
        # es GIBT, ist deshalb verdaechtig — und Verdacht ist kein Gruen.
        report["state_readable"] = bool(raw) or os.path.getsize(path) <= 2
        report["state"] = raw
    else:
        report["state_readable"] = True    # es gab noch nie einen Lauf

    last_success = float(report["state"].get("last_success_at") or 0.0)
    report["upload_age_seconds"] = (now - last_success) if last_success else None
    report["consecutive_failures"] = int(
        report["state"].get("consecutive_failures") or 0)
    report["last_error"] = report["state"].get("last_error") or ""

    # -- Das Buch: die kanonische Transportwahrheit ------------------------
    try:
        book = book or OL.OffsiteLedger()
        recent = book.recent(50)
        verified = [g for g in recent if g.state == OL.VERIFIED]
        if verified:
            newest = verified[0]
            report["last_generation"] = {
                "generation_id": newest.generation_id,
                "state": newest.state,
                "classes": list(newest.classes),
                "destinations": sorted(newest.object_keys or {}),
                "cipher_bytes": newest.cipher_bytes,
                "source_ok": newest.source_ok,
            }
        report["open_generations"] = [
            {"generation_id": g.generation_id, "state": g.state}
            for g in book.open_generations()]
        report["failed_generations"] = [
            {"generation_id": g.generation_id,
             "category": g.failure_category}
            for g in recent if g.state == OL.FAILED][:5]
        proof = OV.last_proof(book)
        report["last_proof"] = proof
        report["last_proof_failed"] = bool(
            proof and proof.get("result") != OV.RESULT_OK)
        report["proof_age_seconds"] = OV.proof_age_seconds(now=now, book=book)
    except Exception as exc:  # noqa: BLE001 - messen darf nicht sterben
        report["ledger_error"] = f"{type(exc).__name__}: {exc}"
    return report


def assess(report: dict[str, Any] | None = None, *,
           now: float | None = None) -> tuple[str, str]:
    """Urteilt. Gibt (Zustandswort, Grund) — beides fuer Menschen.

    Die REIHENFOLGE ist die Aussage, wie bei der lokalen Sicherung: erst
    „das kann nur ein Mensch" (`auth_required`), dann „hier stimmt etwas
    nicht" (`unavailable`, lauter als „nichts da"), dann Alter und
    Frische (`degraded`). `healthy` steht am Ende als REST — gruen ist,
    was keinen Zweig ausgeloest hat.
    """
    rep = report if report is not None else collect(now=now)

    # -- Opt-in ist kein Defekt (§11: dreifach strukturell) ---------------
    if rep.get("config_error"):
        return "unavailable", f"Offsite-Konfiguration unlesbar: {rep['config_error']}"
    if not rep.get("configured"):
        return "unknown", "nicht eingerichtet"
    if not rep.get("enabled"):
        return "unknown", "nicht eingeschaltet"

    # -- Menschensachen zuerst --------------------------------------------
    if rep.get("identity_locked"):
        return "auth_required", ("der Schluesselbund ist gesperrt — ohne die "
                                 "Offsite-Identitaet gibt es keinen Beweis")
    if rep.get("identity") is False:
        return "auth_required", ("die Offsite-Identitaet fehlt im "
                                 "Schluesselbund")
    if rep.get("identity") is None:
        return "auth_required", ("die Offsite-Identitaet ist nicht "
                                 "feststellbar")
    if str(rep.get("last_error") or "").startswith("offsite credential"):
        return "auth_required", "der Anbieterzugang fehlt oder wird abgewiesen"
    if rep.get("state", {}).get("last_failure_category") in (
            "auth_failed", "key_unavailable") and rep.get(
                "consecutive_failures", 0) > 0:
        return "auth_required", ("der letzte Lauf kam nicht an seinen Zugang "
                                 "— Tresor oder Schluesselbund gesperrt?")

    # -- Was nicht messbar ist, ist nicht gruen ---------------------------
    if rep.get("ledger_error"):
        return "unavailable", f"das Offsite-Buch ist unlesbar: {rep['ledger_error']}"
    if not rep.get("state_readable"):
        return "unavailable", ("der Offsite-Betriebszustand ist beschaedigt — "
                               "ich weiss nicht, was zuletzt geschah")

    # -- Lauter als „nichts da" -------------------------------------------
    if rep.get("last_proof_failed"):
        proof = rep.get("last_proof") or {}
        return "unavailable", (
            f"der letzte Restore-Beweis ist GESCHEITERT "
            f"({proof.get('generation_id', 'unbekannt')}) — die Sicherung "
            f"ausserhalb des Hauses ist nicht nachweislich brauchbar")
    if rep.get("state", {}).get("last_failure_category") == "integrity_failed":
        return "unavailable", ("ein Ruecklade-Vergleich hat nicht gestimmt "
                               "oder ein Objekt fehlte vor der Zeit")

    # -- Ohne Generation gibt es nichts zu beweisen ------------------------
    last = rep.get("last_generation")
    if last is None:
        return "degraded", ("eingeschaltet, aber es gibt noch keine "
                            "verifizierte Generation")

    # -- Die erwarteten Ziele muessen wirklich dastehen -------------------
    expected = set(last.get("classes") or ())
    reached = set(last.get("destinations") or ())
    missing = sorted(expected - reached)
    if missing:
        return "degraded", (f"der letzten Generation fehlen Ziele: "
                            f"{', '.join(missing)}")

    # -- Alter des Uploads --------------------------------------------------
    upload_age = rep.get("upload_age_seconds")
    failures = int(rep.get("consecutive_failures") or 0)
    if failures >= FAILURES_BEFORE_DEGRADED:
        return "degraded", f"die Offsite-Sicherung scheiterte {failures}-mal hintereinander"
    if upload_age is None:
        return "degraded", "es ist keine erfolgreiche Offsite-Sicherung vermerkt"
    if upload_age >= STALE_AFTER_SECONDS:
        return "degraded", (f"letzte Offsite-Sicherung {_age_text(upload_age)}")

    # -- Und erst jetzt: der Beweis. Upload allein macht NIE gruen. --------
    proof_age = rep.get("proof_age_seconds")
    if proof_age is None:
        return "degraded", ("hochgeladen, aber noch NIE zurueckgeholt — ein "
                            "Upload allein ist kein Backup")
    if proof_age >= PROOF_STALE_AFTER_SECONDS:
        return "degraded", (f"der Restore-Beweis ist ueberfaellig "
                            f"(zuletzt {_age_text(proof_age)})")

    # -- Offene Zeilen sind sichtbar, aber kein Defekt --------------------
    open_rows = rep.get("open_generations") or []
    if open_rows:
        return "degraded", (f"{len(open_rows)} unterbrochene(r) Lauf/Laeufe "
                            f"im Buch — beim naechsten Lauf wird das geklaert")

    if last.get("source_ok") is False:
        # Durchgereicht, nicht verschwiegen (§18): hochgeladen ja, gesund nein.
        return "degraded", ("die letzte Generation stammt aus einem Satz, der "
                            "Fehler meldete — hochgeladen, aber nicht gesund")

    return "healthy", (f"Sicherung {_age_text(upload_age)}, Restore-Beweis "
                       f"{_age_text(proof_age)}, Ziele "
                       f"{'+'.join(sorted(reached))}")


def summary(report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Was ein Mensch wissen will. Deutsche Schluessel, fuer CLI und Runbook.

    §12 verlangt ausdruecklich BEIDE Zeitstempel im Befund: „letzte
    erfolgreiche Sicherung" und „letzter bestandener Restore-Beweis" — sie
    stehen deshalb hier, nicht nur in `state.json`.
    """
    rep = report if report is not None else collect()
    word, reason = assess(rep)
    upload_age = rep.get("upload_age_seconds")
    proof_age = rep.get("proof_age_seconds")
    return {
        "zustand": word,
        "grund": reason,
        "eingerichtet": bool(rep.get("configured")),
        "eingeschaltet": bool(rep.get("enabled")),
        "letzte_sicherung": (_age_text(upload_age) if upload_age is not None
                             else "noch nie"),
        "letzter_restore_beweis": (_age_text(proof_age) if proof_age is not None
                                   else "noch nie"),
        "letzte_generation": rep.get("last_generation"),
        "offene_laeufe": rep.get("open_generations") or [],
        "gescheiterte_generationen": rep.get("failed_generations") or [],
        "fehlschlaege_hintereinander": rep.get("consecutive_failures") or 0,
        "buckets": rep.get("buckets") or {},
    }
