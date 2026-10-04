#!/usr/bin/env python3
"""Kosten-Schema v4 → v3 zurueckbauen — fuer den Rollback auf einen Core vor N8/C3.

    <checkout>/.venv/bin/python -B scripts/cost_schema_downgrade_v4_to_v3.py [--db PFAD] [--core-stopped] [--dry-run]
    (der Interpreter des Cores — das System-python3 hat die Abhaengigkeiten nicht; das Skript
    importiert `solvio.agent_runtime` aus `<script>/../src`, also aus dem Baum, in dem es liegt:
    nach `git reset --keep aa6d2ae` NICHT mehr aus der Produktion, sondern aus der Rueckweg-Stage
    `~/.solvio-nexus/release/<sha7>/candidate/`)

**Warum es das gibt.** Ein Core vor C3 (`aa6d2ae`) liest `agent_cost_schema.version`
und wirft bei allem ausser 3 `unsupported_cost_schema`; `CostLedger.__init__` ruft
das, der Orchestrator instanziiert `CostLedger` — die gesamte Agentenlaufzeit waere
nach einem Rollback ohne diese Gegenmassnahme nicht verfuegbar (§1.6 des
C3-Vertrags).

**Was es tut.** Es parkt alle `text_chat`-Aktivitaeten und deren Invocations
spaltengleich in `agent_cost_activities_textchat_park` /
`agent_provider_invocations_textchat_park` (Zeilenzahl geprueft), baut die beiden
v3-Tabellen mit engem `CHECK(purpose IN ('adaptive_extract','voice_delegate'))`
zurueck und setzt `version=3`. Reservierungen (`agent_cost_reservations`, an
`subject_id` gebunden) bleiben stehen: die Geldwahrheit wird nicht angefasst, und
`reserve_subject` verweigert dieselbe `invocation_id` weiterhin. Die spaetere
v3→v4-Migration (`cost_subjects._upgrade_activities_v4`) importiert die geparkten
Zeilen spaltengleich zurueck — inklusive ihres `state`.

**Wann es verweigert.** Nur bei TATSAECHLICH laufenden Claims: eine
`text_chat`-Invocation `claimed`, oder eine `text_chat`-Aktivitaet
`pending AND held_reason='' AND expires_at > now` — es sei denn `--core-stopped`
ist gesetzt. Damit bestaetigt der Operator, dass kein Prozess mehr einen Claim
halten kann; verwaiste `claimed`-Zeilen sind nach einem Stopp Buchungsleichen,
die `_recovery_pending` ohnehin als offen behandelt. Es verweigert NIE wegen
`unknown`-Invocations oder wegen `pending`-Aktivitaeten mit `held_reason` oder
abgelaufener Frist.

**Vorbedingung: Core gestoppt** (Runbook-Schritt). Das Skript schreibt in genau
einer `BEGIN IMMEDIATE`-Transaktion; ein laufender Core, der gleichzeitig
schreibt, ist der Grund fuer den Runbook-Schritt, nicht fuer eine Sperre hier.

Es gibt keine Werte aus: keine Texte, keine Digests — nur Zaehler und Zustaende.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

TARGET_VERSION = 3
SOURCE_VERSION = 4


class DowngradeRefused(RuntimeError):
    """Der Rueckbau darf so nicht laufen. Der Grund steht in der Nachricht."""


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (name,)).fetchone())


def inspect(db: sqlite3.Connection, *, now: float) -> dict:
    """Was ein Rueckbau vorfindet — als Zaehler, nie als Inhalt."""
    version = db.execute("SELECT version FROM agent_cost_schema WHERE singleton=1").fetchone()
    report = {"version": version[0] if version else None}
    # Die Version zuerst: eine Datenbank ohne Aktivitaetstabelle (leere oder
    # unversionierte Schema-Tabelle) soll REFUSED sagen, nicht mit einem
    # OperationalError enden (Review Runde 8, C8-2).
    report["tables_missing"] = [name for name in ("agent_cost_activities", "agent_provider_invocations")
                                if not _table_exists(db, name)]
    if report["version"] != SOURCE_VERSION or report["tables_missing"]:
        report.update(text_chat_activities=0, text_chat_invocations=0, claimed_invocations=0,
                      open_activities=0, unknown_invocations=0)
        return report
    report["text_chat_activities"] = db.execute(
        "SELECT COUNT(*) FROM agent_cost_activities WHERE purpose='text_chat'").fetchone()[0]
    report["text_chat_invocations"] = db.execute(
        "SELECT COUNT(*) FROM agent_provider_invocations i JOIN agent_cost_activities a "
        "ON a.activity_id=i.activity_id WHERE a.purpose='text_chat'").fetchone()[0]
    report["claimed_invocations"] = db.execute(
        "SELECT COUNT(*) FROM agent_provider_invocations i JOIN agent_cost_activities a "
        "ON a.activity_id=i.activity_id WHERE a.purpose='text_chat' AND i.state='claimed'").fetchone()[0]
    report["open_activities"] = db.execute(
        "SELECT COUNT(*) FROM agent_cost_activities WHERE purpose='text_chat' AND state='pending' "
        "AND held_reason='' AND expires_at > ?", (now,)).fetchone()[0]
    report["unknown_invocations"] = db.execute(
        "SELECT COUNT(*) FROM agent_provider_invocations i JOIN agent_cost_activities a "
        "ON a.activity_id=i.activity_id WHERE a.purpose='text_chat' AND i.state='unknown'").fetchone()[0]
    return report


def refusal(report: dict, *, core_stopped: bool) -> str:
    """Der eine Grund, aus dem der Rueckbau nicht laeuft — oder leer."""
    if report["version"] != SOURCE_VERSION:
        return f"schema_version_is_{report['version']}_not_{SOURCE_VERSION}"
    if report.get("tables_missing"):
        return "cost_tables_missing:" + ",".join(report["tables_missing"])
    if core_stopped:
        return ""
    if report["claimed_invocations"]:
        return f"text_chat_invocations_claimed:{report['claimed_invocations']}"
    if report["open_activities"]:
        return f"text_chat_activities_open:{report['open_activities']}"
    return ""


def downgrade(path: str, *, core_stopped: bool = False, dry_run: bool = False,
              now: float | None = None) -> dict:
    """Der Rueckbau. Gibt die Zaehler zurueck; wirft `DowngradeRefused`."""
    from solvio.agent_runtime import cost_subjects as A
    # Das Modul muss aus DIESEM Baum kommen (<script>/../src), nicht aus der editable-Installation
    # der Produktions-venv — nach `git reset --keep aa6d2ae` waere das der alte Code ohne die
    # v4-Konstanten, und der Abbruch kaeme als Traceback statt als Grund (Review Runde 6, C6-H1).
    expected_root = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
    origin = os.path.realpath(getattr(A, "__file__", "") or "")
    if not origin.startswith(expected_root + os.sep):
        raise DowngradeRefused("wrong_source_tree: cost_subjects kam aus " + (origin or "?") + ", erwartet unter "
                               + expected_root + " (Skript zusammen mit seinem src/-Baum ausfuehren, z. B. aus der Rueckweg-Stage)")
    if not hasattr(A, "TEXT_CHAT_PARK_ACTIVITIES"):
        raise DowngradeRefused("source_tree_without_v4: der src/-Baum neben dem Skript (" + expected_root + ") kennt das Kosten-Schema 4 nicht "
                               "— das ist der alte Code, nicht der Kandidat (Review Runde 7, C7-H1)")

    moment = time.time() if now is None else now
    db = _connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not _table_exists(db, "agent_cost_schema"):
                raise DowngradeRefused("cost_schema_missing")
            report = inspect(db, now=moment)
            reason = refusal(report, core_stopped=core_stopped)
            if reason:
                raise DowngradeRefused(reason)
            if dry_run:
                db.execute("ROLLBACK")
                return dict(report, dry_run=True)
            activities = A.TEXT_CHAT_PARK_ACTIVITIES
            invocations = A.TEXT_CHAT_PARK_INVOCATIONS
            # 1. Parken — Invocations zuerst lesen (sie haengen an der Aktivitaet),
            #    beide Tabellen spaltengleich, ohne Fremdschluessel, damit die
            #    geparkten Zeilen niemanden blockieren.
            parked = {}
            for table, park, where in (
                    ("agent_provider_invocations", invocations,
                     "activity_id IN (SELECT activity_id FROM agent_cost_activities WHERE purpose='text_chat')"),
                    ("agent_cost_activities", activities, "purpose='text_chat'")):
                columns = ",".join(r["name"] for r in db.execute(f"PRAGMA table_info({table})"))
                expected = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
                if _table_exists(db, park):
                    before = db.execute(f"SELECT COUNT(*) FROM {park}").fetchone()[0]
                    db.execute(f"INSERT INTO {park} ({columns}) SELECT {columns} FROM {table} WHERE {where}")
                elif expected:
                    before = 0
                    db.execute(f"CREATE TABLE {park} AS SELECT {columns} FROM {table} WHERE {where}")
                else:
                    # Keine leere Park-Tabelle: der neue Code raeumt jede Park-Tabelle
                    # beim naechsten Start ab, und der Uebernahmeweg liest ein
                    # verschwundenes Vorher-Objekt als Verlust (agent_table_lost) —
                    # ein Rollback OHNE Chat blockierte so den zweiten Anlauf
                    # (Review Runde 8, C8-1).
                    parked[table] = 0
                    continue
                if db.execute(f"SELECT COUNT(*) FROM {park}").fetchone()[0] != before + expected:
                    raise DowngradeRefused("park_count_mismatch")
                db.execute(f"DELETE FROM {table} WHERE {where}")
                parked[table] = expected
            # 2. Die v3-Tabellen mit engem CHECK zurueckbauen — derselbe Helfer,
            #    der auch aufwaerts migriert; Zeilenzahl und Fremdschluessel geprueft.
            A._rebuild_activities(db, purposes=A._PURPOSES_V3, suffix="_v3down")
            db.execute("UPDATE agent_cost_schema SET version=? WHERE singleton=1", (TARGET_VERSION,))
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise DowngradeRefused("foreign_key_check_failed")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        report["parked_activities"] = parked["agent_cost_activities"]
        report["parked_invocations"] = parked["agent_provider_invocations"]
        report["version_after"] = TARGET_VERSION
        return report
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="", help="Pfad zu agent_runs.sqlite3 (Vorgabe: das produktive Buch)")
    parser.add_argument("--core-stopped", action="store_true",
                        help="Bestaetigung des Operators: kein Prozess haelt mehr einen Claim")
    parser.add_argument("--dry-run", action="store_true", help="nur pruefen, nichts schreiben")
    args = parser.parse_args(argv)
    from solvio.agent_runtime.store import resolve_path
    path = resolve_path(args.db)
    if not os.path.exists(path):
        print(f"REFUSED: database_missing {path}", file=sys.stderr)
        return 2
    try:
        report = downgrade(path, core_stopped=args.core_stopped, dry_run=args.dry_run)
    except DowngradeRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    label = "DRY-RUN" if args.dry_run else "DONE"
    print(f"{label}: " + " ".join(f"{key}={value}" for key, value in sorted(report.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
