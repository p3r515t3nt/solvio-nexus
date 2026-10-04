"""Kleiner Speicher fuer ausdruecklich bestaetigte Empfaengerbindungen.

Eine Bindung beantwortet „wer ist gemeint": welcher Mensch und welcher
Kontaktweg hinter „mich", „mein Sohn" oder „das Hotel" steht. Das ist
Identitaetswahrheit, und sie gehoert SOLVIO. Seit Contact Binding Authority
Hardening V1 traegt jede Zeile eine **Fassung** (`version`), und der einzige
Schreibweg kann gegen den Stand pruefen, den der Beschreiber vor der Freigabe
gesehen hat: wer zwischen Anzeige und Schreiben etwas anderes vorfindet,
schreibt nicht (`BindingChanged`).

Der Speicher ist damit eine Nebenlaeufigkeits- und TOCTOU-Schranke, KEINE
Autoritaetsschranke. Ob geschrieben werden darf, entscheidet die Freigabepolitik
im Router (`communication_confirm_binding` ist VERY_CRITICAL_BY_BIRTH); dieser
Speicher stellt nur sicher, dass genau das geschrieben wird, was der Mensch auf
dem Display gesehen hat.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any

DEFAULT_PATH = "~/.solvio/contacts.sqlite3"
PATH_ENV = "SOLVIO_CONTACTS_DB"

#: Der Fingerabdruck einer nicht vorhandenen Bindung. Ein Wort, kein Hash:
#: „es gab keine" soll sich von jedem echten Stand unterscheiden und im Journal
#: lesbar bleiben.
ABSENT = "absent"


class BindingChanged(Exception):
    """Der Speicher zeigt einen anderen Stand als den, der beschrieben wurde.

    Kein Fehler des Speichers, sondern seine Aussage: zwischen der Anzeige auf
    dem Geraet und diesem Schreibversuch hat jemand anderes geschrieben. Der
    Aufrufer soll NICHT schreiben — der Mensch hat etwas anderes bestaetigt.
    """


def normalize_alias(alias: str) -> str:
    value = " ".join(str(alias or "").strip().lower().split())
    return value.translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))


def clean_handles(handles: Any) -> list[dict[str, str]]:
    """Dieselbe Normalisierung fuer Speicher, Beschreiber und Vergleich.

    Drei Stellen mit drei eigenen Regeln liefen frueher oder spaeter
    auseinander — und dann waere „unveraendert" an einer Stelle etwas anderes
    als an der anderen.
    """
    return [{"channel": str(item.get("channel", "")).strip().lower(),
             "value": str(item.get("value", "")).strip()}
            for item in (handles or []) if isinstance(item, dict)]


def fingerprint(binding: dict[str, Any] | None) -> str:
    """Der Stand EINER Bindung als ein Wert — Inhalt UND Fassung.

    Der Inhalt allein wuerde eine Aenderung erkennen; die Fassung erkennt
    zusaetzlich, dass zwischendurch geschrieben wurde, selbst wenn danach wieder
    derselbe Inhalt steht. Keine Bindung ist `ABSENT`.
    """
    if binding is None:
        return ABSENT
    roh = json.dumps([str(binding.get("alias_norm", "")),
                      str(binding.get("display_name", "")),
                      clean_handles(binding.get("handles")),
                      int(binding.get("version", 0))],
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()


def same_binding(binding: dict[str, Any] | None, display_name: str,
                 handles: list[dict[str, str]]) -> bool:
    """Ist das die Bindung, die schon steht — in allem, was Autoritaet traegt?

    Verglichen werden Name und Kontaktwege (als Menge, die Reihenfolge traegt
    keine Bedeutung). `source` und `confirmed_at` sind Buchhaltung; wer nur sie
    „aendern" wuerde, aendert nicht, wer gemeint ist.
    """
    if binding is None:
        return False
    if str(binding.get("display_name", "")).strip() != str(display_name or "").strip():
        return False
    stehend = {(h["channel"], h["value"]) for h in clean_handles(binding.get("handles"))}
    gewollt = {(h["channel"], h["value"]) for h in clean_handles(handles)}
    return stehend == gewollt


class BindingStore:
    """SQLite besitzt nur bestaetigte Bindungen; Suchtreffer gehoeren nie hierher."""

    def __init__(self, path: str = "") -> None:
        chosen = path or os.environ.get(PATH_ENV, "") or DEFAULT_PATH
        self.path = os.path.abspath(os.path.expanduser(chosen))
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        os.chmod(self.path, 0o600)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS bindings (
                alias_norm TEXT PRIMARY KEY,
                alias TEXT NOT NULL,
                display_name TEXT NOT NULL,
                handles TEXT NOT NULL,
                confirmed_at REAL NOT NULL,
                source TEXT NOT NULL)
        """)
        # Die Fassung. Bestehende Zeilen (vor diesem Milestone geschrieben)
        # bekommen 0; der erste Schreibvorgang danach macht daraus 1. Ein
        # Bestand ohne Spalte wird beim Oeffnen nachgezogen — additiv, ohne
        # eine Zeile anzufassen.
        if "version" not in self._spalten():
            try:
                self._db.execute(
                    "ALTER TABLE bindings ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                # Zwei Prozesse oeffnen denselben Altbestand zum ersten Mal:
                # der zweite ALTER scheitert an „duplicate column". Das ist
                # kein Fehler des Bestands — nachsehen, ob die Spalte jetzt da
                # ist, und nur dann weiterreichen, wenn sie es nicht ist.
                if "version" not in self._spalten():
                    raise

    def _spalten(self) -> set[str]:
        return {row[1] for row in self._db.execute("PRAGMA table_info(bindings)")}

    def get(self, alias: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT alias_norm, alias, display_name, handles, confirmed_at, source, "
            "version FROM bindings WHERE alias_norm = ?",
            (normalize_alias(alias),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["handles"] = json.loads(result["handles"])
        return result

    def snapshot(self, alias: str) -> str:
        """Der Stand, gegen den ein spaeteres `confirm` pruefen kann."""
        return fingerprint(self.get(alias))

    def matching_name(self, name: str) -> list[dict[str, Any]]:
        """Exact normalized display names only; never guess an email address.

        Aliases remain the unambiguous primary key. Two people with the same
        display name are two candidates, even if their addresses happen to match.
        """
        wanted = normalize_alias(name)
        if not wanted:
            return []
        rows = self._db.execute(
            "SELECT alias_norm, alias, display_name, handles, confirmed_at, source, version "
            "FROM bindings ORDER BY alias_norm").fetchall()
        result = []
        for row in rows:
            if normalize_alias(row['display_name']) != wanted:
                continue
            item = dict(row)
            item['handles'] = json.loads(item['handles'])
            result.append(item)
        return result

    def confirm(self, alias: str, display_name: str,
                handles: list[dict[str, str]], source: str, *,
                expected: str | None = None) -> dict[str, Any]:
        """Der einzige Schreibpfad dieses Speichers.

        `expected` ist der Fingerabdruck, den der Aufrufer VORHER gesehen hat
        (`snapshot`). Steht jetzt ein anderer Stand, wird nicht geschrieben:
        `BindingChanged`. Geprueft und geschrieben wird in EINER Transaktion,
        damit zwischen Blick und Schreiben kein zweiter Schreiber passt.

        Ohne `expected` schreibt der Speicher bedingungslos. Das ist die
        Aussaat in Tests und nichts sonst — die Faehigkeit uebergibt immer den
        Stand, den der Mensch bestaetigt hat.
        """
        alias = " ".join(str(alias or "").strip().split())
        alias_norm = normalize_alias(alias)
        display_name = str(display_name or "").strip()
        source = str(source or "").strip()
        clean = clean_handles(handles)
        if not alias_norm or not display_name or not clean or any(
                not item["channel"] or not item["value"] for item in clean):
            raise ValueError("incomplete binding")
        confirmed_at = time.time()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            current = self.get(alias_norm)
            if expected is not None and fingerprint(current) != expected:
                raise BindingChanged(alias_norm)
            version = int((current or {}).get("version", 0)) + 1
            self._db.execute(
                "INSERT OR REPLACE INTO bindings "
                "(alias_norm, alias, display_name, handles, confirmed_at, source, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (alias_norm, alias, display_name,
                 json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                 confirmed_at, source, version))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return self.get(alias) or {}
