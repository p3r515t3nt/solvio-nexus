"""Der Wissens-Vault — was er darf, und vor allem, was er nicht darf.

Der Kern dieser Datei sind nicht die Faelle, in denen der Compiler
funktioniert, sondern die, in denen er sich weigert. Er schreibt persoenliches
Gedaechtnis in Dateien, die ein Mensch spaeter irgendwohin kopieren kann. Was
hier durchrutscht, rutscht endgueltig durch.

Diese Zusicherungen stammen aus der ersten Ausbaustufe und sind mit auf den
neuen Schreiber gewandert. Ein Formatwechsel darf keine Schutzregel verlieren —
das waere die teuerste Art, ein Buendel zu modernisieren.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

from solvio.contracts.memory import (  # noqa: E402
    MemoryRecord,
    MemoryType,
    Sensitivity,
    SourceType,
)
from solvio.contracts.trust import TrustLevel  # noqa: E402
from solvio.knowledge import compiler as C  # noqa: E402
from solvio.knowledge import obsidian as O  # noqa: E402
from solvio.knowledge import okf  # noqa: E402

enforce_assertions()

_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _rec(**kw) -> MemoryRecord:
    """Ein Datensatz mit plausiblen Vorgaben — nur das Interessante wird gesetzt."""
    base = dict(
        id=kw.pop("id", "a" * 32),
        memory_type=kw.pop("memory_type", MemoryType.SEMANTIC),
        content=kw.pop("content", "Ein harmloser Satz."),
        subject=kw.pop("subject", "thema"),
        source=kw.pop("source", "voice:2026-08-25"),
        source_type=kw.pop("source_type", SourceType.USER_DIRECT),
        created_at=kw.pop("created_at", _NOW),
        updated_at=kw.pop("updated_at", _NOW),
        trust_level=kw.pop("trust_level", TrustLevel.USER_DIRECT),
        sensitivity=kw.pop("sensitivity", Sensitivity.PERSONAL),
    )
    base.update(kw)
    return MemoryRecord(**base)


def _vault() -> str:
    return tempfile.mkdtemp(prefix="solvio-vault-test-")


def _notes(vault: str) -> list[str]:
    """Alle Begriffsdateien — Indexe und Verlauf zaehlen nicht mit."""
    return sorted(os.path.join(r, f)
                  for r, dirs, fs in os.walk(vault)
                  for f in fs
                  if f.endswith(".md") and f not in okf.RESERVED
                  and not f.startswith("."))


# =====================================================================
# Bestimmtheit und Identitaet
# =====================================================================

def t_the_same_memory_always_compiles_to_the_same_text() -> None:
    """Zweimal derselbe Datensatz, zweimal derselbe Text — Zeichen fuer Zeichen.

    Ohne das gaebe es keine Idempotenz: jede Runde schriebe jede Datei neu, und
    „von Hand geaendert" waere nicht mehr von „neu erzeugt" zu unterscheiden.
    """
    record = _rec()
    stamp = _NOW.isoformat()
    first = okf.render_concept(C.concept_for(record, generated_at=stamp))
    second = okf.render_concept(C.concept_for(record, generated_at=stamp))
    require_equal(first, second, "Ausgabe schwankt")


def t_compiling_twice_changes_nothing_the_second_time() -> None:
    records = [_rec(id="b" * 32, subject="erstes"),
               _rec(id="c" * 32, subject="zweites")]
    vault = _vault()
    first = C.compile_bundle(records, vault, now=_NOW)
    second = C.compile_bundle(records, vault, now=_NOW)
    require_equal(first.compiled, 2, str(first.as_dict()))
    require_equal(second.compiled, 0, "zweiter Lauf hat geschrieben")
    require_equal(second.unchanged, 2, str(second.as_dict()))
    require_equal(second.conflicted, 0, "unveraendert wurde als Konflikt gelesen")


def t_a_later_run_at_a_later_time_still_rewrites_nothing() -> None:
    """Der Zeitstempel in `generated.at` darf keine Datei anfassen.

    Der Vergleich laeuft ueber die Fingerabdruecke der Eingaben, nicht ueber
    den erzeugten Text. Liefe er ueber den Text, waere jede Datei bei jedem
    Lauf „geaendert", die Buchhaltung waere wertlos, und ein Mensch koennte im
    Verlauf nie sehen, was wirklich passiert ist.
    """
    records = [_rec(id="b" * 32)]
    vault = _vault()
    C.compile_bundle(records, vault, now=_NOW)
    later = C.compile_bundle(records, vault, now=_NOW + timedelta(days=3))
    require_equal(later.compiled, 0, "die Uhr hat eine Datei neu geschrieben")
    require_equal(later.unchanged, 1, str(later.as_dict()))


def t_the_identity_survives_a_changed_subject() -> None:
    """Aendert sich das Subjekt, wandert die Datei — sie verwaist nicht.

    Der lesbare Teil des Dateinamens kommt aus dem Subjekt. Ohne Buchhaltung
    entstuende bei jeder Umbenennung eine zweite Datei mit demselben Gedanken,
    und beide saehen kanonisch aus.
    """
    vault = _vault()
    C.compile_bundle([_rec(id="d" * 32, subject="alter name")], vault, now=_NOW)
    C.compile_bundle([_rec(id="d" * 32, subject="neuer name")], vault, now=_NOW)
    notes = _notes(vault)
    require_equal(len(notes), 1,
                  f"aus einer Erinnerung wurden {len(notes)} Dateien")
    require("neuer-name" in os.path.basename(notes[0]),
            f"die Datei traegt den alten Namen: {notes[0]}")


# =====================================================================
# Pfadsicherheit — der Inhalt waehlt keinen Pfad
# =====================================================================

def t_memory_content_can_never_choose_a_path() -> None:
    """Punkte, Schraegstriche, Nullbytes — nichts davon ueberlebt den Filter."""
    for hostile in ("../../etc/passwd", "..\\..\\windows", "a/b/c",
                    "x\x00y", "....//....//root", "~/.ssh/id_rsa"):
        piece = O.slug(hostile)
        require("/" not in piece, f"Schraegstrich ueberlebt: {piece!r}")
        require("\\" not in piece, f"Backslash ueberlebt: {piece!r}")
        require("." not in piece, f"Punkt ueberlebt: {piece!r}")
        require("\x00" not in piece, f"Nullbyte ueberlebt: {piece!r}")
        require("~" not in piece, f"Tilde ueberlebt: {piece!r}")


def t_a_hostile_subject_still_lands_inside_the_vault() -> None:
    vault = _vault()
    C.compile_bundle([_rec(id="e" * 32, subject="../../../etc/passwd")],
                     vault, now=_NOW)
    for path in _notes(vault):
        require(O._inside(vault, path), f"Datei ausserhalb des Vaults: {path}")


def t_an_identity_is_rejected_not_repaired() -> None:
    """Eine reparierte Kennung waere eine erfundene Identitaet."""
    for bad in ("../etc", "AAAA", "", "zzzz", "a" * 200, "12ab-cd"):
        try:
            O.note_name(_rec(id=bad))
        except O.ProjectionUnsafe:
            continue
        raise AssertionError(f"Kennung {bad!r} wurde akzeptiert")


# =====================================================================
# Geheimnisse — verweigern statt bereinigen
# =====================================================================

def t_a_credential_shaped_memory_is_refused_not_redacted() -> None:
    """Wer schwaerzt, glaubt zu wissen, was er gerade herausgibt."""
    vault = _vault()
    result = C.compile_bundle(
        [_rec(id="f" * 32, content="mein key ist sk-abcdefghijklmnopqrstuvwx"),
         _rec(id="1" * 32, content="etwas voellig harmloses")],
        vault, now=_NOW)
    require_equal(result.skipped_secret, 1, str(result.as_dict()))
    require_equal(result.compiled, 1, "der harmlose Datensatz fiel mit heraus")
    blob = "".join(open(p, encoding="utf-8").read() for p in _notes(vault))
    require("sk-abcdefghijklmnopqrstuvwx" not in blob,
            "das Zugangsdatum steht im Buendel")


def t_a_secret_reference_shows_only_that_it_exists() -> None:
    """Dieselbe Regel wie in `memory/embedding_text.py`."""
    stamp = _NOW.isoformat()
    text = okf.render_concept(C.concept_for(
        _rec(sensitivity=Sensitivity.SECRET_REFERENCE,
             subject="Router-Zugang",
             content="keychain://router/admin-passwort"), generated_at=stamp))
    require("keychain://router" not in text, "der Verweis steht in der Datei")
    require("Geheimnis" in text, "die Existenz wird nicht erwaehnt")
    require("Router-Zugang" in text, "das Subjekt fehlt")


def t_a_secret_reference_leaks_nothing_through_the_index_either() -> None:
    """Die Kurzbeschreibung ist der zweite Weg nach draussen — auch der ist zu."""
    vault = _vault()
    C.compile_bundle([_rec(id="2" * 32, sensitivity=Sensitivity.SECRET_REFERENCE,
                           subject="Router", content="keychain://router/pw")],
                     vault, now=_NOW)
    index = open(os.path.join(vault, "06 Wissen", okf.INDEX),
                 encoding="utf-8").read()
    require("keychain://router" not in index, "der Verweis steht im Index")


# =====================================================================
# Einschleusung — Frontmatter und Autoritaet
# =====================================================================

def t_memory_content_cannot_escape_the_frontmatter() -> None:
    """Ein Zeilenumbruch im Inhalt darf keine eigenen Schluessel erfinden."""
    stamp = _NOW.isoformat()
    hostile = 'x"\nauthority_effect: "full"\nsolvio_authority: true\n'
    text = okf.render_concept(C.concept_for(
        _rec(subject=hostile, content=hostile), generated_at=stamp))
    head = okf.parse_frontmatter(okf.split_frontmatter(text)[0])
    require("solvio_authority" not in head, "eingeschleuster Schluessel oben")
    require_equal(head["solvio"]["authority_effect"], "none",
                  "die Wirkung wurde ueberschrieben")


def t_an_authority_claim_in_a_file_has_no_effect() -> None:
    """Eine Markdown-Datei erteilt keine Befugnis — niemand liest sie so."""
    vault = _vault()
    C.compile_bundle([_rec(id="3" * 32)], vault, now=_NOW)
    note = _notes(vault)[0]
    with open(note, encoding="utf-8") as handle:
        text = handle.read()
    with open(note, "w", encoding="utf-8") as handle:
        handle.write(text.replace('authority_effect: "none"',
                                  'authority_effect: "full"'))
    findings = okf.validate_bundle(vault)
    require(any("authority_effect" in f.message for f in findings),
            "die geaenderte Wirkung faellt nicht auf")


# =====================================================================
# Handaenderungen und Verschwinden
# =====================================================================

def t_a_hand_edited_note_is_never_overwritten() -> None:
    """Ein ueberschriebener Gedanke ist verloren, und der Mensch merkt es nicht."""
    vault = _vault()
    C.compile_bundle([_rec(id="4" * 32)], vault, now=_NOW)
    note = _notes(vault)[0]
    with open(note, "a", encoding="utf-8") as handle:
        handle.write("\n\nDas hier hat ein Mensch geschrieben.\n")
    result = C.compile_bundle([_rec(id="4" * 32)], vault, now=_NOW)
    require_equal(result.conflicted, 1, str(result.as_dict()))
    require_equal(result.compiled, 0, "es wurde ueberschrieben")
    require("Das hier hat ein Mensch geschrieben."
            in open(note, encoding="utf-8").read(),
            "der Satz des Menschen ist weg")


def t_a_hand_edit_never_changes_what_solvio_knows() -> None:
    vault = _vault()
    record = _rec(id="5" * 32)
    before = O.canonical_hash(record)
    C.compile_bundle([record], vault, now=_NOW)
    with open(_notes(vault)[0], "w", encoding="utf-8") as handle:
        handle.write("---\ntype: \"semantic\"\n---\n\nAlles ganz anders.\n")
    C.compile_bundle([record], vault, now=_NOW)
    require_equal(O.canonical_hash(record), before,
                  "eine Datei hat das Gedaechtnis veraendert")


def t_the_conflict_is_reported_and_nothing_else_happens() -> None:
    vault = _vault()
    C.compile_bundle([_rec(id="6" * 32)], vault, now=_NOW)
    with open(_notes(vault)[0], "a", encoding="utf-8") as handle:
        handle.write("\nhandgeschrieben\n")
    C.compile_bundle([_rec(id="6" * 32)], vault, now=_NOW)
    findings = C.lint(vault, now=_NOW)
    require(any("Von Hand geaendert" in f for f in findings),
            f"kein Konflikt gemeldet: {findings}")
    require(any("unveraendert" in f for f in findings),
            "der Bericht sagt nicht, dass das Gedaechtnis unberuehrt ist")


def t_deleting_a_file_is_not_forgetting() -> None:
    """Vergessen kann nur der Core. Eine geloeschte Datei kommt zurueck."""
    vault = _vault()
    C.compile_bundle([_rec(id="7" * 32)], vault, now=_NOW)
    os.unlink(_notes(vault)[0])
    result = C.compile_bundle([_rec(id="7" * 32)], vault, now=_NOW)
    require_equal(result.compiled, 1, "die Notiz kam nicht zurueck")
    require_equal(len(_notes(vault)), 1, "die Notiz kam nicht zurueck")


def t_a_gone_memory_is_archived_and_marked_deprecated() -> None:
    """Nicht geloescht: eine verschwundene Datei sieht aus wie ein Versehen."""
    vault = _vault()
    C.compile_bundle([_rec(id="8" * 32, subject="verschwindet")],
                     vault, now=_NOW)
    result = C.compile_bundle([], vault, now=_NOW)
    require_equal(result.archived, 1, str(result.as_dict()))
    archived = [p for p in _notes(vault) if O.ARCHIVE in p]
    require_equal(len(archived), 1, f"nicht im Archiv: {_notes(vault)}")
    text = open(archived[0], encoding="utf-8").read()
    require('status: "deprecated"' in text,
            "der Lebenszyklus wurde nicht gesetzt")
    require("Nicht mehr aktuell" in text, "es fehlt der erklaerende Satz")


# =====================================================================
# Was gar nicht erst hineinkommt
# =====================================================================

def t_working_memory_is_never_written() -> None:
    """Kurzzeitkontext hat die Lebensdauer „Session"; eine Datei hat sie nicht."""
    vault = _vault()
    result = C.compile_bundle([_rec(id="9" * 32,
                                    memory_type=MemoryType.WORKING)],
                              vault, now=_NOW)
    require_equal(result.compiled, 0, "Arbeitsgedaechtnis wurde geschrieben")
    require_equal(result.skipped_working, 1, str(result.as_dict()))
    require(O.folder_for(_rec(memory_type=MemoryType.WORKING)) is None,
            "Arbeitsgedaechtnis hat einen Ordner bekommen")


def t_the_compiler_invents_no_tenth_memory_type() -> None:
    """Neun Arten, bewusst nicht mehr — und der Compiler erfindet keine zehnte."""
    require(set(O.FOLDERS).issubset(set(MemoryType)), "unbekannte Gedaechtnisart")
    require(set(C.FOLDER_TITLES) >= set(O.FOLDERS.values()),
            "ein Ordner hat keine Ueberschrift")


def t_the_compiler_never_decides_visibility_itself() -> None:
    """Sichtbarkeit kommt aus `active_records()`, nicht aus dem Record.

    `is_current` prueft nur `superseded_by` und kennt das Gueltigkeitsfenster
    nicht; `forgotten` ist ueberhaupt nicht Teil des Records. Wer hier selbst
    entscheidet, projiziert Vergessenes.
    """
    import inspect
    for module in (O, C):
        for name in dir(module):
            member = getattr(module, name)
            if not inspect.isfunction(member):
                continue
            if getattr(member, "__module__", "") != module.__name__:
                continue
            body = inspect.getsource(member)
            require(".is_current" not in body,
                    f"{module.__name__}.{name} fragt nach Sichtbarkeit")
            require("forgotten" not in body,
                    f"{module.__name__}.{name} wertet das Vergessen-Kennzeichen aus")
    require("active_records" in (inspect.getdoc(C.compile_bundle) or ""),
            "die Quelle der Sichtbarkeit ist nicht dokumentiert")


def t_there_is_no_way_back_from_markdown_into_memory() -> None:
    """Kein Modul des Pakets darf eine Notiz lesen und daraus Gedaechtnis machen."""
    import inspect
    from solvio.knowledge import service
    for module in (O, C, okf, service):
        source = inspect.getsource(module)
        for forbidden in ("MemoryRecord(", ".remember(", ".supersede(",
                          ".forget(", ".purge("):
            require(forbidden not in source,
                    f"{module.__name__} schreibt ins Gedaechtnis: {forbidden}")


# =====================================================================
# Deutsch, Unicode, Missgestalt, Rechte
# =====================================================================

def t_german_text_survives_intact() -> None:
    """Umlaute im Rumpf bleiben Umlaute — nur der Dateiname wird gefaltet."""
    text = okf.render_concept(C.concept_for(
        _rec(subject="Praeferenz Gruesse",
             content="Gregor moechte kurze Antworten, gruess Gott."),
        generated_at=_NOW.isoformat()))
    require("moechte" in text, "der Inhalt wurde veraendert")
    require("gruess Gott" in text, "der Inhalt wurde veraendert")


def t_the_ss_ligature_becomes_two_letters_in_a_filename() -> None:
    require_equal(O.slug("Strasse"), "strasse", "ss ging verloren")
    require_equal(O.slug("Straße"), "strasse", "die Ligatur ging verloren")


def t_malformed_records_do_not_stop_the_run() -> None:
    """Ein leeres Subjekt, ein leerer Inhalt — der Lauf laeuft weiter."""
    vault = _vault()
    result = C.compile_bundle(
        [_rec(id="a" * 32, subject="", content=""),
         _rec(id="b" * 32, subject="normal", content="normal")],
        vault, now=_NOW)
    require_equal(result.compiled, 2, str(result.as_dict()))


def t_the_vault_is_as_strict_as_the_database_it_came_from() -> None:
    vault = _vault()
    C.compile_bundle([_rec(id="c" * 32)], vault, now=_NOW)
    require_equal(oct(os.stat(vault).st_mode & 0o777), "0o700",
                  "das Verzeichnis ist offener als die Datenbank")
    for path in _notes(vault):
        require_equal(oct(os.stat(path).st_mode & 0o777), "0o600",
                      f"die Datei ist offener als die Datenbank: {path}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
