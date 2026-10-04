"""Der Wissens-Compiler — aus kanonischem Gedaechtnis wird ein OKF-Buendel.

Das Muster stammt von Andrej Karpathys „LLM Wiki" (Gist
`442a6bf555914893e9891c11519de94f`, 2026-04-04): Wissen wird einmal
zusammengetragen und bleibt dann liegen, statt bei jeder Frage neu erarbeitet
zu werden. Drei Schichten — unveraenderliche Quellen, kompiliertes Wissen,
Beschreibung des Formats — und drei Handlungen: **einpflegen**, **abfragen**,
**pruefen**.

WAS HIER ANDERS IST ALS BEI KARPATHY, und warum:

**Die Quellenschicht ist keine Dateisammlung.** Bei Karpathy liegen Rohquellen
unveraendert auf der Platte. Hier ist die Quelle die kanonische
Gedaechtnisdatenbank, und ein Begriff verweist auf sie ueber
`solvio://memory/<id>`. Das ist Absicht: eine Kopie des Gesagten waere ein
zweiter Ort, an dem persoenliche Inhalte liegen — einer, den kein `purge()`
erreicht. Herkunft belegt man mit einer Kennung, nicht mit einem Mitschnitt.

**Kein Modell schreibt hier.** Karpathys Wiki wird von einem LLM verfasst.
Dieser Compiler ist eine Funktion: gleiches Gedaechtnis, gleiches Buendel, Byte
fuer Byte. Ein Modell, das persoenliches Wissen frei formulieren darf, kann
etwas behaupten, das so nie gesagt wurde — und es saehe hinterher aus wie eine
Erinnerung. Wo spaeter Verdichtung dazukommt, braucht sie einen eigenen,
ausdruecklich entworfenen Weg und eine eigene Herkunftsangabe.

**Nur Betroffenes wird neu gebaut.** Die Buchhaltung haelt zu jedem Begriff
fest, aus welchen Erinnerungen er entstand und wie deren Fingerabdruck lautete.
Aendert sich keiner, bleibt die Datei unangetastet.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from solvio.contracts.memory import MemoryRecord, Sensitivity, SourceType
from solvio.contracts.trust import TrustLevel
from solvio.knowledge import obsidian, okf
from solvio.knowledge.obsidian import (ARCHIVE, FOLDERS, ProjectionUnsafe,
                                       canonical_hash, folder_for, note_name)

#: Die Buchhaltung dieses Compilers. Sie beantwortet genau eine Frage, die die
#: aeltere `.solvio-projection.json` nicht beantworten konnte: **aus welchen
#: Erinnerungen ist dieser Begriff entstanden?** Ohne diese Frage muesste jeder
#: Lauf alles neu schreiben.
MANIFEST = ".solvio-knowledge.json"

#: Wie viele Verlaufszeilen aufgehoben werden. Karpathys `log.md` ist
#: anfuegend und waechst unbegrenzt; ein persoenliches Buendel, das ewig
#: waechst, ist eine Chronik der Person. Aeltere Zeilen fallen heraus.
LOG_KEEP = 400

#: Die Ueberschrift eines Ordners im Buendel.
FOLDER_TITLES: dict[str, str] = {
    "01 Ich": "Ueber mich",
    "02 Menschen": "Menschen",
    "03 Projekte": "Projekte",
    "05 Regeln": "Regeln und dauerhafte Absichten",
    "06 Wissen": "Wissen",
    "07 Ereignisse": "Ereignisse",
    ARCHIVE: "Archiv",
}

#: Der Ordner der ersten Ausbaustufe. Er wird abgeloest durch `index.md` je
#: Ordner — OKF hat dafuer ein reserviertes Dateiformat, und zwei Wege zu
#: derselben Uebersicht sind einer zu viel.
LEGACY_OVERVIEW = "00 Uebersicht"

_GENERATED_MARK = "*Erzeugt."


@dataclass
class CompileResult:
    """Was ein Lauf getan hat. Zahlen und Pfade, nie Inhalt."""

    compiled: int = 0
    unchanged: int = 0
    conflicted: int = 0
    archived: int = 0
    skipped_secret: int = 0
    skipped_working: int = 0
    migrated: int = 0
    indexes: int = 0
    log_lines: list[str] = field(default_factory=list)
    #: Dateinamen, die aus dem Verlauf zu entfernen sind, weil ihr Inhalt
    #: entfernt wurde und der Name ihn verriet.
    scrub_from_log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"compiled": self.compiled, "unchanged": self.unchanged,
                "conflicted": self.conflicted, "archived": self.archived,
                "skipped_secret": self.skipped_secret,
                "skipped_working": self.skipped_working,
                "migrated": self.migrated, "indexes": self.indexes,
                "log_lines": len(self.log_lines)}


# ------------------------------------------------------------------- Herkunft

#: Wer eine Erinnerung in die Welt gesetzt hat, in OKFs Schreibweise (SPEC §7).
#:
#: Der `human:`-Vorsatz sagt: ein Mensch hat es gesagt. Er sagt NICHT, dass ein
#: Mensch etwas erlaubt hat. Diese beiden Saetze auseinanderzuhalten ist der
#: ganze Zweck von `solvio.authority_effect`.
_AUTHORS: dict[SourceType, str] = {
    SourceType.USER_DIRECT: "human:owner",
    SourceType.HOME_ASSISTANT: "process:home-assistant",
    SourceType.CODEX_RESULT: "process:codex",
    SourceType.SOLVIO_INFERENCE: "process:solvio-inference",
    SourceType.SYSTEM_OBSERVATION: "process:solvio-system",
    SourceType.GMAIL_MESSAGE: "process:gmail",
    SourceType.WEB_PAGE: "process:web",
}

#: Vertrauensstufen, aus denen ein Begriff `stable` werden darf.
#:
#: Alles andere wird `draft` — und zwar OHNE eine neue Kategorie zu erfinden.
#: Der eingefrorene Memory Contract stellt `agent_generated` und die
#: `untrusted_*`-Klassen ausdruecklich so, dass sie **keine Handlung
#: autorisieren**; ein Rechercheergebnis aus dem Netz ist damit im Core schon
#: kein gesichertes persoenliches Wissen. OKFs Lebenszyklus bildet genau das
#: ab, statt daneben eine zweite Wahrheit aufzumachen.
_STABLE_TRUST = frozenset({
    TrustLevel.SYSTEM_TRUSTED,
    TrustLevel.USER_DIRECT,
    TrustLevel.LOCAL_TRUSTED_TOOL,
    TrustLevel.MEMORY_CURATED,
})


def author_of(record: MemoryRecord) -> str:
    return _AUTHORS.get(record.source_type, "process:unknown")


def status_of(record: MemoryRecord) -> str:
    """`stable` nur, wo der Core den Inhalt selbst als belastbar fuehrt.

    Ein Rechercheergebnis aus dem Netz landet als `draft` im Buendel. Es steht
    dort, weil es zum Gedaechtnis gehoert — aber ein Verbraucher, der nur
    `stable` liest, sieht es nicht. Das ist der Unterschied zwischen „SOLVIO
    hat es gelesen" und „SOLVIO weiss es".
    """
    return "stable" if record.trust_level in _STABLE_TRUST else "draft"


def _enum(value: Any) -> Any:
    return getattr(value, "value", value)


def _description(record: MemoryRecord) -> str:
    """Eine Zeile, die im Index steht. Bei Geheimnissen: nur die Existenz."""
    if record.sensitivity == Sensitivity.SECRET_REFERENCE:
        return "Verweis auf ein Geheimnis; der Verweis selbst bleibt im Core."
    text = " ".join((record.content or "").split())
    return text[:160]


def _body(record: MemoryRecord) -> str:
    """Die Menschenseite eines Begriffs.

    Bei `SECRET_REFERENCE` steht hier NUR, DASS es einen Verweis gibt — nie,
    worauf er zeigt. Woertlich dieselbe Regel wie in
    `memory/embedding_text.py`, wo aus demselben Grund nur das Subjekt
    eingebettet wird.
    """
    title = (record.subject or "").strip().replace("\n", " ")[:120]
    if not title:
        title = (record.content or "").strip().split("\n", 1)[0][:80] or "Erinnerung"
    lines = [f"# {title}", ""]
    if record.sensitivity == Sensitivity.SECRET_REFERENCE:
        lines += ["> Dieser Eintrag verweist auf ein Geheimnis.",
                  "> Der Verweis selbst steht bewusst nicht in dieser Datei —",
                  "> er lebt nur im Gedaechtnis des Cores.", ""]
    else:
        lines += [(record.content or "").strip(), ""]
    lines += ["## Herkunft", "",
              f"- Kanonische Erinnerung: `{okf.memory_uri(record.id)}`",
              f"- Gesagt von: `{author_of(record)}`",
              f"- Vertrauensstufe im Core: `{_enum(record.trust_level)}`", "",
              "---", "",
              "*Von SOLVIO erzeugt. Diese Datei BESCHREIBT Gedaechtnis und",
              "erzeugt keines. Aenderungen hier aendern SOLVIOs Gedaechtnis",
              "nicht und erteilen keine Befugnis — sie werden beim naechsten",
              "Lauf als Vorschlag erkannt und gemeldet.*"]
    return "\n".join(lines)


def concept_for(record: MemoryRecord, *, generated_at: str) -> okf.Concept:
    """Ein Begriff im Sinne von OKF, aus genau einer Erinnerung.

    `verified` bleibt LEER, und das ist die wichtigste Auslassung dieses
    Moduls. OKF leitet aus `verified.by: human:*` eine Vertrauensstufe ab —
    „von einem Menschen gegengelesen". Der Compiler kann das nicht wissen, und
    selbst wenn er es wuesste, waere es eine Aussage ueber Sorgfalt und nie
    ueber Befugnis. Ein Feld, das ein Werkzeug sich selbst ausstellt, ist kein
    Beleg.
    """
    body = _body(record)
    space: dict[str, Any] = {
        "authority_effect": okf.NO_AUTHORITY,
        "canonical": False,
        "memory_id": record.id,
        "memory_type": _enum(record.memory_type),
        "source_type": _enum(record.source_type),
        "trust_level": _enum(record.trust_level),
        "sensitivity": _enum(record.sensitivity),
        "confidence": record.confidence,
        "importance": record.importance,
        "canonical_hash": canonical_hash(record),
        "body_hash": obsidian._body_fingerprint(body),
    }
    if record.valid_from is not None:
        space["valid_from"] = okf.utc(record.valid_from)

    return okf.Concept(
        type=str(_enum(record.memory_type)),
        title=(record.subject or "").strip().replace("\n", " ")[:120]
              or (record.content or "").strip().split("\n", 1)[0][:80]
              or "Erinnerung",
        description=_description(record),
        tags=list(record.tags or []),
        sources=[okf.Source(id=record.id, resource=okf.memory_uri(record.id),
                            title=(record.subject or "").strip()[:120] or None,
                            author=author_of(record),
                            last_modified=okf.utc(record.updated_at))],
        generated_by=okf.COMPILER_ACTOR,
        generated_at=generated_at,
        verified=[],
        status=status_of(record),
        # Eine Erinnerung mit Gueltigkeitsende IST danach ueberholt. Das ist
        # der einzige Fall, in dem `stale_after` etwas Wahres sagt — eine
        # erfundene Haltbarkeit waere schlimmer als gar keine.
        stale_after=okf.utc(record.valid_until),
        body=body,
        solvio=space,
    )


# ------------------------------------------------------------------ Handedits

def declared_body_hash(text: str) -> str | None:
    """Der Abdruck, den der letzte Lauf hinterlassen hat — neu oder alt.

    Zwei Schreibweisen, weil ein Vault aus der ersten Ausbaustufe die flachen
    `solvio_*`-Schluessel traegt. Ein Migrationslauf, der sie nicht erkennt,
    haelt JEDE vorhandene Notiz fuer handgeschrieben und ruehrt sie nie wieder
    an.
    """
    for line in text.split("\n")[:80]:
        stripped = line.strip()
        if stripped.startswith("solvio_body_hash:") or stripped.startswith("body_hash:"):
            return stripped.split(":", 1)[1].strip().strip('"')
    return None


def was_edited_by_hand(text: str) -> bool:
    declared = declared_body_hash(text)
    if declared is None:
        return True
    return declared != obsidian._body_fingerprint(obsidian.body_of(text))


# -------------------------------------------------------------------- Compile

def compile_bundle(records: Iterable[MemoryRecord], vault: str,
                   *, now: datetime | None = None,
                   removal_reasons: dict[str, str] | None = None) -> CompileResult:
    """Baut das Buendel. `records` MUSS aus `active_records()` kommen.

    Diese Funktion entscheidet NICHT selbst ueber Sichtbarkeit — sie kann es
    nicht: das Vergessen-Kennzeichen ist nicht Teil eines `MemoryRecord`, und
    `is_current` kennt das Gueltigkeitsfenster nicht.

    `removal_reasons` beantwortet die Frage, die dieser Funktion frueher fehlte:
    WARUM ist eine Kennung nicht mehr dabei? Sie sah nur, dass etwas fehlt, und
    legte den vollen Text ins Archiv — auch fuer Vergessenes (DEBT-0092). Der
    Core kennt den Grund und reicht ihn hier herein; ohne die Angabe wird
    fail-closed das Strengste angenommen, was ohne Wissen vertretbar ist:
    Rumpf weg, Titel bleibt.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = moment.isoformat()
    vault = os.path.abspath(vault)
    os.makedirs(vault, exist_ok=True)
    os.chmod(vault, 0o700)

    book = _load(vault)
    known: dict[str, Any] = dict(book.get("concepts") or {})
    result = CompileResult()
    result.migrated = _retire_legacy_overviews(vault, stamp)
    reasons = dict(removal_reasons or {})

    seen: set[str] = set()
    kept: list[tuple[MemoryRecord, str]] = []

    for record in records:
        folder = folder_for(record)
        if folder is None:
            result.skipped_working += 1
            continue
        if obsidian.looks_like_a_secret(record.content or ""):
            result.skipped_secret += 1
            continue
        try:
            name = note_name(record)
            concept = concept_for(record, generated_at=stamp)
            text = okf.render_concept(concept)
        except (ProjectionUnsafe, okf.OkfInvalid):
            result.skipped_secret += 1
            continue

        rel = os.path.join(folder, name)
        path = os.path.join(vault, rel)
        if not obsidian._inside(vault, path):
            result.skipped_secret += 1
            continue

        seen.add(record.id)
        entry = known.get(record.id) or {}
        previous = entry.get("path")
        if previous and previous != rel:
            old = os.path.join(vault, previous)
            if os.path.exists(old) and obsidian._inside(vault, old):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.replace(old, path)

        current = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    current = handle.read()
            except OSError:
                current = None

        if current is not None and was_edited_by_hand(current):
            result.conflicted += 1
            known[record.id] = {**entry, "path": rel, "conflict": True,
                                "inputs": {record.id: canonical_hash(record)}}
            kept.append((record, rel))
            continue

        # Nur Betroffenes neu bauen. Der Vergleich laeuft ueber die
        # Eingangs-Fingerabdruecke, nicht ueber den erzeugten Text: sonst
        # wuerde der Zeitstempel in `generated.at` jede Datei bei jedem Lauf
        # veraendern, und „unveraendert" hiesse nie etwas.
        inputs = {record.id: canonical_hash(record)}
        if current is not None and entry.get("inputs") == inputs:
            result.unchanged += 1
        else:
            obsidian._atomic_write(path, text)
            result.compiled += 1
            result.log_lines.append(
                f"Begriff `{rel}` "
                f"{'neu gebaut' if current is not None else 'angelegt'}.")
        known[record.id] = {"path": rel, "conflict": False, "inputs": inputs,
                            "type": str(_enum(record.memory_type)),
                            "title": concept.title,
                            "description": concept.description,
                            "compiled_at": stamp}
        kept.append((record, rel))

    result.archived = _archive_gone(vault, known, seen, stamp, result,
                                    reasons)
    result.indexes = _write_indexes(vault, known, kept, stamp)

    book["okf_version"] = okf.OKF_VERSION
    book["concepts"] = known
    book["compiled_at"] = stamp
    book["compiler"] = okf.COMPILER_ACTOR
    book["note"] = ("Buchhaltung des SOLVIO-Wissens-Compilers. Nicht von Hand "
                    "aendern — sie beschreibt nur, was gebaut wurde, und "
                    "erteilt keine Befugnis.")
    obsidian._atomic_write(os.path.join(vault, MANIFEST),
                           json.dumps(book, indent=2, sort_keys=True,
                                      ensure_ascii=False))
    _append_log(vault, moment, result.log_lines,
                scrub=tuple(result.scrub_from_log))
    return result


def known_ids(vault: str) -> set[str]:
    """Welche Gedaechtniskennungen dieses Buendel schon einmal gesehen hat.

    Der Aufrufer braucht sie, um den Core nach dem GRUND ihres Verschwindens zu
    fragen. Ohne diese Frage kann ein Vergessen nicht von einer Abloesung
    unterschieden werden — und das Archiv behielte den Text.
    """
    book = _load(os.path.abspath(vault))
    # Auch ein archivierter Eintrag kann spaeter vergessen oder gepurgt
    # werden. Seinen neuen Grund liefert weiterhin nur das kanonische Buch.
    return {ident for ident, entry in (book.get("concepts") or {}).items()
            if isinstance(entry, dict)}


def _load(vault: str) -> dict[str, Any]:
    try:
        with open(os.path.join(vault, MANIFEST), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


#: Gruende, bei denen im Archiv NICHTS vom Inhalt bleiben darf — auch kein
#: Titel. Wer vergessen laesst, hat nicht um eine gekuerzte Fassung gebeten.
ERASING_REASONS = frozenset({"forgotten", "purged"})


def _archive_gone(vault: str, known: dict[str, Any], seen: set[str],
                  stamp: str, result: CompileResult,
                  reasons: dict[str, str]) -> int:
    """Was nicht mehr aktuelle Wahrheit ist, wandert ins Archiv — ohne Rumpf.

    OKF hat fuer den Lebenszyklus einen Wert: ein Begriff, der verschwindet,
    sieht aus wie ein Versehen; einer mit `status: deprecated` im Archiv sieht
    aus wie eine Entscheidung.

    DEBT-0092: frueher wanderte die Datei MIT Inhalt hierher. Fuer eine
    Abloesung war das harmlos, fuer ein `forget()` war es ein Vergessen, das in
    einer kopierbaren Datei weiterlebte. Jetzt gilt in zwei Stufen:

    * **vergessen oder gepurgt** -> restlos inhaltsfrei. Kein Rumpf, kein
      Titel, keine Beschreibung, keine Schlagworte. Es bleibt die Kennung, ein
      Zeitpunkt und ein Satz darueber, dass hier etwas war. Genau so viel,
      dass nichts unbemerkt verschwindet — und keine Silbe mehr.
    * **abgeloest, abgelaufen, Grund unbekannt** -> Titel und Verweis bleiben,
      der Rumpf faellt weg. Die Historie gehoert dem Core (`history()`), nicht
      einem Ordner, den jemand kopieren kann.
    """
    archived = 0
    for ident, entry in list(known.items()):
        if ident in seen:
            continue
        rel = entry.get("path")
        if not rel:
            known.pop(ident, None)
            continue
        source = os.path.join(vault, rel)
        reason = reasons.get(ident, "")
        erased = reason in ERASING_REASONS
        # Der Dateiname ist Inhalt. „geheimnis-ort--4bfbb484.md" verraet das
        # Subjekt eines vergessenen Eintrags noch im Verzeichnislisting — ein
        # inhaltsfreier Rumpf hinter einem sprechenden Namen waere Theater.
        name = (f"entfernt--{ident[:8]}.md" if erased
                else os.path.basename(rel))
        target = os.path.join(vault, ARCHIVE, name)
        if (not os.path.exists(source) or not obsidian._inside(vault, source)
                or not obsidian._inside(vault, target)):
            known.pop(ident, None)
            continue
        obsidian._atomic_write(target, _tombstone_note(ident, entry, stamp,
                                                       reason, erased))
        try:
            # Bereits archiviert: Quelle und Ziel koennen dieselbe Datei
            # sein. Die aktualisierte Grabnotiz darf dann nicht verschwinden.
            # Trotzdem neu rendern: ein spaeteres Vergessen/Purge kann auch
            # einen bisherigen Archivtitel noch inhaltsfrei machen muessen.
            if os.path.normcase(os.path.abspath(source)) != os.path.normcase(os.path.abspath(target)):
                os.unlink(source)
        except OSError:
            pass
        entry["path"] = os.path.join(ARCHIVE, name)
        entry["status"] = "deprecated"
        entry["archived_at"] = stamp
        entry["removal_reason"] = reason or "unknown"
        # Die Kurzfassung IST der Anfang des Rumpfs. Fuer einen abgelegten
        # Begriff hat sie im Buch nichts mehr zu suchen — gemessen an einem
        # abgeloesten Eintrag, dessen Inhalt so in `.solvio-knowledge.json`
        # weiterlebte, obwohl die Datei ihn laengst nicht mehr trug.
        entry.pop("description", None)
        if erased:
            # Auch die Buchhaltung vergisst: Titel und Kurzfassung standen dort
            # im Klartext, und die Datei liegt im selben Vault.
            entry.pop("title", None)
            entry.pop("description", None)
            # Und der Verlauf. Er hat den ALTEN Dateinamen protokolliert, und
            # der trug das Subjekt („geheimnis-ort--4bfbb484.md"). Ein Vergessen,
            # das die Chronik ausspart, ist keines. Der Verlauf ist eine
            # Bequemlichkeit; die Historie gehoert dem Core.
            result.scrub_from_log.append(os.path.basename(rel))
        archived += 1
        result.log_lines.append(
            f"Begriff `{entry['path']}` ist nicht mehr aktuelle Wahrheit "
            f"(`deprecated`{', inhaltsfrei' if erased else ''}).")
    return archived


def _tombstone_note(ident: str, entry: dict[str, Any], stamp: str,
                    reason: str, erased: bool) -> str:
    """Der Archiveintrag. Ein konformes OKF-Dokument ohne persoenlichen Inhalt.

    Er wird NEU GESCHRIEBEN statt aus der alten Datei abgeleitet — sonst
    ueberlebte ein Rest davon jede noch so gute Absicht.
    """
    uri = f"solvio://memory/{ident}"
    if erased:
        title = f"Entfernte Erinnerung {ident[:8]}"
        description = ("Der Inhalt wurde auf Wunsch entfernt und steht "
                       "bewusst nicht in dieser Datei.")
        body = "\n".join([
            f"# {title}", "",
            f"> **Entfernt.** SOLVIO fuehrt diese Erinnerung seit {stamp} nicht",
            f"> mehr — Grund: `{reason}`.",
            ">",
            "> **Der Inhalt steht hier nicht.** Vergessen heisst vergessen, auch",
            "> in einer Datei, die jemand kopieren kann. Was es an Historie gibt,",
            "> gehoert dem Core und wird dort erfragt.", "",
            "## Herkunft", "",
            f"- Kanonische Erinnerung: `{uri}`",
            f"- Zustand: `{reason or 'entfernt'}`", ""])
    else:
        title = str(entry.get("title") or f"Erinnerung {ident[:8]}")
        description = "Nicht mehr aktuelle Wahrheit; der Rumpf steht im Core."
        body = "\n".join([
            f"# {title}", "",
            f"> **Nicht mehr aktuell.** SOLVIO fuehrt diesen Begriff seit {stamp}",
            f"> nicht mehr als aktuelle Wahrheit"
            f"{f' (Grund: `{reason}`)' if reason else ''}.",
            ">",
            "> Der Rumpf steht bewusst nicht mehr hier: die Historie gehoert dem",
            "> Core, nicht einem Ordner. Geloescht hat den Begriff niemand.", "",
            "## Herkunft", "",
            f"- Kanonische Erinnerung: `{uri}`", ""])

    return okf.render_concept(okf.Concept(
        type="retired-memory", title=title, description=description,
        generated_by=okf.COMPILER_ACTOR, generated_at=stamp,
        status="deprecated",
        solvio={"authority_effect": okf.NO_AUTHORITY, "canonical": False,
                "memory_id": ident, "removal_reason": reason or "unknown",
                "content_erased": erased, "retired_at": stamp},
        body=body))


def _retire_legacy_overviews(vault: str, stamp: str) -> int:
    """Die Uebersichten der ersten Ausbaustufe weichen `index.md`.

    Sie werden NICHT geloescht, sondern ins Archiv gelegt — dieselbe Regel wie
    fuer alles andere im Buendel. Und beruehrt wird nur, was den
    Erzeugt-Vermerk traegt: was ein Mensch dort abgelegt hat, bleibt liegen.
    """
    folder = os.path.join(vault, LEGACY_OVERVIEW)
    if not os.path.isdir(folder) or not obsidian._inside(vault, folder):
        return 0
    moved = 0
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        if _GENERATED_MARK not in text:
            continue
        target = os.path.join(vault, ARCHIVE, f"uebersicht-{name}")
        if not obsidian._inside(vault, target):
            continue
        # Auch eine abgelegte Seite bleibt ein konformes Dokument. Formloses
        # Markdown im Buendel waere fuer einen fremden Verbraucher ein
        # kaputter Begriff — und fuer den Validator zu Recht ein Befund.
        obsidian._atomic_write(target, okf.render_concept(okf.Concept(
            type="retired-overview",
            title=f"Abgeloeste Uebersicht: {name[:-3]}",
            description=("Uebersichtsseite der ersten Ausbaustufe, ersetzt "
                         "durch `index.md`."),
            generated_by=okf.COMPILER_ACTOR,
            generated_at=stamp,
            status="deprecated",
            solvio={"authority_effect": okf.NO_AUTHORITY, "canonical": False,
                    "retired_at": stamp},
            body=(f"> **Abgeloest.** Diese Uebersicht wurde am {stamp} durch "
                  f"`index.md` ersetzt (Open Knowledge Format "
                  f"{okf.OKF_VERSION}).\n> Sie bleibt hier stehen, damit "
                  f"nichts unbemerkt verschwindet.\n\n"
                  + okf.split_frontmatter(text)[1]
                  if text.startswith("---") else
                  f"> **Abgeloest.** Ersetzt durch `index.md` am {stamp}.\n"
                  f"> Sie bleibt hier stehen, damit nichts unbemerkt "
                  f"verschwindet.\n\n" + text))))
        try:
            os.unlink(path)
        except OSError:
            continue
        moved += 1
    try:
        os.rmdir(folder)
    except OSError:
        pass

    # Und die alte Buchhaltung. Zwei Buecher ueber denselben Vault wuerden
    # frueher oder spaeter Verschiedenes behaupten, und niemand wuesste, welches
    # gilt.
    stale = os.path.join(vault, obsidian.LEGACY_MANIFEST)
    if os.path.exists(stale) and obsidian._inside(vault, stale):
        try:
            os.unlink(stale)
            moved += 1
        except OSError:
            pass
    return moved


# --------------------------------------------------------------------- Indexe

def _write_indexes(vault: str, known: dict[str, Any],
                   kept: list[tuple[MemoryRecord, str]], stamp: str) -> int:
    """Ein `index.md` je Ordner und eines in der Wurzel.

    OKF reserviert diesen Dateinamen und verbietet Frontmatter darin — mit
    genau einer Ausnahme: die Buendelwurzel erklaert `okf_version`. Ein Leser,
    der SOLVIO nicht kennt, erkennt daran, welches Format er in der Hand haelt.
    """
    by_folder: dict[str, list[tuple[str, str, str]]] = {}
    for record, rel in kept:
        folder, name = os.path.split(rel)
        entry = known.get(record.id) or {}
        by_folder.setdefault(folder, []).append(
            (entry.get("title") or name[:-3], name,
             entry.get("description") or ""))

    # Auch LEER gewordene Ordner werden neu geschrieben. Ohne das behielt ein
    # Ordner, aus dem der letzte Begriff verschwand, seinen alten Index — mit
    # Titel UND Kurzfassung des vergessenen Eintrags. Gefunden im ersten
    # Rauchtest von DEBT-0092: der Rumpf war weg, die Uebersicht nicht.
    existing = {name for name in os.listdir(vault)
                if os.path.isdir(os.path.join(vault, name))
                and os.path.exists(os.path.join(vault, name, okf.INDEX))
                } if os.path.isdir(vault) else set()
    written = 0
    for folder in sorted(set(by_folder) | (existing - {ARCHIVE})):
        entries = sorted(by_folder.get(folder, []))
        title = FOLDER_TITLES.get(folder, folder)
        text = okf.render_index(
            title,
            f"{len(entries)} Begriff(e), erzeugt aus SOLVIOs kanonischem "
            f"Gedaechtnis. Diese Seite fasst zusammen und besitzt nichts."
            if entries else
            "Zurzeit kein Begriff in diesem Bereich. Diese Seite fasst "
            "zusammen und besitzt nichts.",
            [("Begriffe", entries)])
        obsidian._atomic_write(os.path.join(vault, folder, okf.INDEX), text)
        written += 1

    # NUR Ordner, die es wirklich gibt. Ein Verweis auf einen leeren Bereich
    # sieht im Buendel harmlos aus und ist fuer einen fremden Verbraucher ein
    # Verweis ins Leere — er faellt genau dort um, wo Obsidian noch verzeiht.
    # Auch das Archiv bekommt einen Index. Ein Verweis auf ein VERZEICHNIS
    # sieht in Obsidian aus wie ein Verweis; ein fremder Markdown-Leser
    # oeffnet damit nichts.
    # Je Datei GENAU EIN Eintrag. Die Buchhaltung gewinnt, weil nur sie den
    # Zeitpunkt kennt; das Verzeichnis ergaenzt, was sie nicht mehr fuehrt
    # (etwa abgeloeste Uebersichten der ersten Ausbaustufe).
    by_file: dict[str, tuple[str, str, str]] = {}
    archive_dir = os.path.join(vault, ARCHIVE)
    if os.path.isdir(archive_dir):
        for name in sorted(os.listdir(archive_dir)):
            if name.endswith(".md") and name != okf.INDEX:
                by_file[name] = (name[:-3], name, "")
    for entry in known.values():
        if entry.get("status") != "deprecated" or not entry.get("path"):
            continue
        name = os.path.basename(entry["path"])
        when = str(entry.get("archived_at") or "")[:10]
        by_file[name] = (name[:-3], name,
                         f"abgeloest am {when}" if when else "abgeloest")
    retired = sorted(by_file.values())
    if os.path.isdir(archive_dir):
        obsidian._atomic_write(os.path.join(archive_dir, okf.INDEX),
                               okf.render_index(
                                   FOLDER_TITLES[ARCHIVE],
                                   "Was SOLVIO nicht mehr als aktuelle Wahrheit "
                                   "fuehrt. Nichts davon wurde geloescht — eine "
                                   "verschwundene Datei saehe aus wie ein "
                                   "Versehen.",
                                   [("Abgeloest", retired)]))
        written += 1

    folders: list[tuple[str, str, str]] = []
    for folder in sorted(by_folder):
        count = len(by_folder[folder])
        folders.append((FOLDER_TITLES.get(folder, folder),
                        f"{folder}/{okf.INDEX}", f"{count} Begriff(e)"))
    if os.path.isdir(archive_dir):
        folders.append((FOLDER_TITLES[ARCHIVE], f"{ARCHIVE}/{okf.INDEX}",
                        f"{len(retired)} abgeloest, nicht geloescht"))

    intro = (
        "SOLVIOs persoenliches Wissen, kompiliert aus dem kanonischen "
        "Gedaechtnis des Cores.\n\n"
        "**Dies ist eine Ansicht, keine Wahrheit.** Die Wahrheit liegt in "
        "`~/.solvio/memory/memory.sqlite3` und gehoert dem Core. Eine Datei "
        "hier zu aendern aendert SOLVIOs Gedaechtnis nicht. Eine Datei hier "
        "zu loeschen ist kein Vergessen.\n\n"
        "**Keine Datei in diesem Buendel erteilt eine Befugnis.** Jeder "
        "Begriff traegt `solvio.authority_effect: none`. Auch ein Eintrag, "
        "den ein Mensch gegengelesen hat, genehmigt damit nichts — "
        "Freigaben entstehen ausschliesslich ueber SOLVIOs "
        "Freigabe-Weg mit Face ID.\n\n"
        f"Format: Open Knowledge Format {okf.OKF_VERSION}. "
        f"Zuletzt gebaut: {stamp}.")
    obsidian._atomic_write(
        os.path.join(vault, okf.INDEX),
        okf.render_index("SOLVIO Knowledge", intro,
                         [("Bereiche", folders),
                          ("Verlauf", [("Was sich geaendert hat", okf.LOG, "")])],
                         is_root=True))
    return written + 1


def _append_log(vault: str, moment: datetime, lines: list[str],
                *, scrub: tuple[str, ...] = ()) -> None:
    """Anfuegen, nach Datum gruppiert, neueste zuerst — und begrenzt.

    Ein Buendel ueber einen Menschen, dessen Verlauf ewig waechst, ist eine
    Chronik dieses Menschen. Aeltere Zeilen fallen heraus; was zaehlt, ist der
    aktuelle Stand, und der steht in den Begriffen selbst.
    """
    path = os.path.join(vault, okf.LOG)
    days: list[tuple[str, list[str]]] = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                current: list[str] | None = None
                for raw in handle:
                    if raw.startswith("## "):
                        current = []
                        days.append((raw[3:].strip(), current))
                    elif raw.startswith("- ") and current is not None:
                        item = raw[2:].rstrip()
                        if any(name and name in item for name in scrub):
                            continue      # dieser Name verriet, was entfernt ist
                        current.append(item)
        except OSError:
            days = []

    if not lines and not scrub:
        # Ein Lauf, der nichts geaendert hat, hat nichts zu erzaehlen. Ein
        # Verlauf, in dem jeder Leerlauf eine Zeile bekommt, verdeckt die
        # Zeilen, die etwas bedeuten.
        if os.path.exists(path):
            return
        lines = ["Buendel angelegt."] if not os.path.exists(path) else []
    today = moment.date().isoformat()
    if lines:
        if days and days[0][0] == today:
            days[0][1][:0] = lines
        else:
            days.insert(0, (today, list(lines)))
    days = [(day, items) for day, items in days if items]

    budget = LOG_KEEP
    trimmed: list[tuple[str, list[str]]] = []
    for day, items in days:
        if budget <= 0:
            break
        trimmed.append((day, items[:budget]))
        budget -= len(items[:budget])
    obsidian._atomic_write(path, okf.render_log(trimmed))


# ----------------------------------------------------------------------- Lint

def lint(vault: str, *, now: datetime | None = None) -> list[str]:
    """Karpathys dritte Handlung: was stimmt in diesem Buendel nicht?

    Gibt Saetze zurueck, keine Inhalte. Gefunden werden Dinge, die ein Mensch
    entscheiden muss — nicht Dinge, die dieser Compiler still korrigieren
    koennte. Was er still korrigieren koennte, korrigiert er schon.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    vault = os.path.abspath(vault)
    book = _load(vault)
    known: dict[str, Any] = book.get("concepts") or {}
    out: list[str] = []

    for ident, entry in sorted(known.items()):
        rel = entry.get("path") or ""
        if entry.get("conflict"):
            out.append(f"Von Hand geaendert, nicht ueberschrieben: `{rel}`. "
                       f"SOLVIOs Gedaechtnis ist unveraendert.")
        if not rel:
            continue
        path = os.path.join(vault, rel)
        if not os.path.exists(path):
            out.append(f"Datei fehlt: `{rel}`. Sie kommt beim naechsten Lauf "
                       f"zurueck — eine geloeschte Datei ist kein Vergessen.")
            continue
        # Der Validator laesst formlose Dateien in Ruhe, weil sie einem
        # Menschen gehoeren koennen. Ein Begriff, den DIESER Compiler gebaut
        # hat, darf aber nicht formlos sein — sonst faellt genau der Fall
        # durch, in dem eine Datei zerschossen wurde.
        try:
            with open(path, encoding="utf-8") as handle:
                if not handle.read(3).startswith("---"):
                    out.append(f"Begriff ohne Frontmatter: `{rel}`. Die Datei "
                               f"gehoert dem Compiler und ist beschaedigt.")
        except (OSError, UnicodeDecodeError):
            out.append(f"Begriff nicht lesbar: `{rel}`.")

    findings = okf.validate_bundle(vault, known_memory_ids=set(known))
    for finding in findings:
        out.append(f"Formfehler in `{os.path.relpath(finding.path, vault)}`: "
                   f"{finding.message}")

    for folder, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            if not name.endswith(".md") or name in okf.RESERVED:
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                head = okf.parse_frontmatter(okf.split_frontmatter(text)[0])
            except (OSError, okf.OkfInvalid, UnicodeDecodeError):
                continue
            stale = head.get("stale_after")
            if stale and okf._is_iso(str(stale)):
                if datetime.fromisoformat(str(stale).replace("Z", "+00:00")) < moment:
                    out.append(f"Ueberholt seit `{stale}`: "
                               f"`{os.path.relpath(path, vault)}`.")
            if head.get("status") == "draft":
                out.append(f"Noch Entwurf: `{os.path.relpath(path, vault)}`.")
    return out
