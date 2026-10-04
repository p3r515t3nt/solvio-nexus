"""Das Buendel als FORMAT — und die eine Zeile, die alles zusammenhaelt.

Diese Datei prueft zwei Dinge, die zusammengehoeren.

Das erste ist Konformitaet: SOLVIO gibt Wissen im Open Knowledge Format v0.2
heraus (Spezifikation `GoogleCloudPlatform/knowledge-catalog`, `okf/SPEC.md`,
Commit `6243209` vom 2026-08-21). Ein Buendel, das nur SOLVIO lesen kann, ist
kein portables Format, sondern eine Ausrede.

Das zweite ist die Grenze, die genau deshalb gefaehrdet ist: OKF kennt
`verified: {by: "human:owner"}` und leitet daraus eine Vertrauensstufe ab.
**Das ist eine Aussage ueber Sorgfalt und niemals ueber Befugnis.** Wer die
beiden verwechselt, hat eine Datei gebaut, die sich selbst genehmigt. Mehrere
Zusicherungen hier existieren nur, um diese Verwechslung unmoeglich zu machen.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import json
import os
import re
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
_SRC = os.path.join(os.path.dirname(__file__), "..", "src", "solvio")


def _rec(**kw) -> MemoryRecord:
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


def _built(*records: MemoryRecord, now: datetime | None = None) -> str:
    vault = tempfile.mkdtemp(prefix="solvio-okf-test-")
    C.compile_bundle(list(records) or [_rec()], vault, now=now or _NOW)
    return vault


def _concepts(vault: str) -> list[str]:
    return sorted(os.path.join(r, f)
                  for r, _, fs in os.walk(vault)
                  for f in fs
                  if f.endswith(".md") and f not in okf.RESERVED
                  and not f.startswith("."))


def _head(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return okf.parse_frontmatter(okf.split_frontmatter(handle.read())[0])


# =====================================================================
# Konformitaet
# =====================================================================

def t_the_adopted_spec_version_is_pinned_and_written_down() -> None:
    """Eine Fassung ohne Herkunft ist keine Fassung.

    Die Spezifikation liegt NICHT im Repository und wird zur Laufzeit nicht
    geholt. Was gilt, muss deshalb im Code stehen — samt Commit und Datum,
    damit ein spaeterer Leser weiss, gegen welchen Text hier gebaut wurde.
    """
    require_equal(okf.OKF_VERSION, "0.2", "die Fassung ist nicht festgelegt")
    require("6243209" in okf.OKF_SPEC_SOURCE, "der Commit fehlt")
    require("2026-08-21" in okf.OKF_SPEC_SOURCE, "das Datum fehlt")


def t_every_concept_declares_a_type() -> None:
    """`type` ist das einzige Pflichtfeld der Spezifikation (SPEC §11)."""
    vault = _built(_rec(id="b" * 32), _rec(id="c" * 32,
                                           memory_type=MemoryType.PREFERENCE))
    for path in _concepts(vault):
        require(str(_head(path).get("type") or "").strip(),
                f"ohne `type`: {path}")


def t_the_bundle_root_declares_the_format_and_nothing_else() -> None:
    """Nur die Wurzel darf im Index Frontmatter tragen, und nur `okf_version`."""
    vault = _built()
    root = open(os.path.join(vault, okf.INDEX), encoding="utf-8").read()
    head = okf.parse_frontmatter(okf.split_frontmatter(root)[0])
    require_equal(set(head), {"okf_version"}, f"die Wurzel traegt mehr: {head}")
    require_equal(str(head["okf_version"]), "0.2", "falsche Fassung")


def t_a_folder_index_carries_no_frontmatter_at_all() -> None:
    """Ein Index mit Frontmatter waere ein weiteres Wissensdokument.

    Dann wuesste ein Verbraucher nicht mehr, wo das Buendel anfaengt — und die
    Wurzel waere nicht mehr eindeutig.
    """
    vault = _built()
    for folder, _, files in os.walk(vault):
        if okf.INDEX not in files or folder == vault:
            continue
        text = open(os.path.join(folder, okf.INDEX), encoding="utf-8").read()
        require(not text.startswith("---"),
                f"Ordner-Index mit Frontmatter: {folder}")


def t_the_validator_catches_frontmatter_in_a_folder_index() -> None:
    vault = _built()
    inner = os.path.join(vault, "06 Wissen", okf.INDEX)
    text = open(inner, encoding="utf-8").read()
    open(inner, "w", encoding="utf-8").write(
        '---\ntype: "index"\n---\n\n' + text)
    findings = okf.validate_bundle(vault)
    require(any("Buendelwurzel" in f.message for f in findings),
            f"nicht bemerkt: {[f.message for f in findings]}")


def t_the_log_is_grouped_by_date_newest_first() -> None:
    vault = _built(_rec(id="d" * 32))
    C.compile_bundle([_rec(id="d" * 32), _rec(id="e" * 32)], vault,
                     now=_NOW + timedelta(days=1))
    text = open(os.path.join(vault, okf.LOG), encoding="utf-8").read()
    days = [line[3:].strip() for line in text.split("\n")
            if line.startswith("## ")]
    require(days, "der Verlauf hat keine Tage")
    for day in days:
        require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day),
                f"keine Datumsueberschrift: {day!r}")
    require_equal(days, sorted(days, reverse=True), "nicht neueste zuerst")


def t_the_log_stays_bounded() -> None:
    """Ein Verlauf ueber einen Menschen, der ewig waechst, ist seine Chronik."""
    vault = tempfile.mkdtemp(prefix="solvio-okf-log-")
    for day in range(6):
        records = [_rec(id=f"{n:032x}", subject=f"t{day}-{n}")
                   for n in range(120)]
        C.compile_bundle(records, vault, now=_NOW + timedelta(days=day))
    text = open(os.path.join(vault, okf.LOG), encoding="utf-8").read()
    lines = [x for x in text.split("\n") if x.startswith("- ")]
    require(len(lines) <= C.LOG_KEEP,
            f"{len(lines)} Zeilen, erlaubt sind {C.LOG_KEEP}")


def t_the_log_records_events_never_content() -> None:
    """Was jemand gesagt hat, gehoert nicht in eine Chronik."""
    secret_ish = "mein Lieblingsessen ist Kaesespaetzle mit Zwiebeln"
    vault = _built(_rec(id="f" * 32, subject="essen", content=secret_ish))
    text = open(os.path.join(vault, okf.LOG), encoding="utf-8").read()
    require(secret_ish not in text, "der Inhalt steht im Verlauf")
    require("Kaesespaetzle" not in text, "der Inhalt steht im Verlauf")


# =====================================================================
# Herkunft
# =====================================================================

def t_provenance_points_at_the_memory_and_never_copies_it() -> None:
    """Eine Kopie des Gesagten waere ein zweiter Ort, den kein `purge` erreicht."""
    vault = _built(_rec(id="1" * 32))
    head = _head(_concepts(vault)[0])
    source = head["sources"][0]
    require_equal(source["resource"], "solvio://memory/" + "1" * 32,
                  "die Herkunft zeigt woandershin")
    require_equal(okf.memory_id_of(source["resource"]), "1" * 32,
                  "die Kennung ist nicht wiederzugewinnen")


def t_a_forged_provenance_uri_is_caught() -> None:
    """Eine erfundene `solvio://memory/...`-Angabe ist keine Herkunft."""
    vault = _built(_rec(id="2" * 32))
    path = _concepts(vault)[0]
    text = open(path, encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(
        text.replace("solvio://memory/" + "2" * 32,
                     "solvio://memory/" + "9" * 32))
    findings = okf.validate_bundle(vault, known_memory_ids={"2" * 32})
    require(any("nicht gibt" in f.message for f in findings),
            f"nicht bemerkt: {[f.message for f in findings]}")


def t_a_human_source_is_marked_as_such_without_promising_anything() -> None:
    vault = _built(_rec(id="3" * 32, source_type=SourceType.USER_DIRECT),
                   _rec(id="4" * 32, source_type=SourceType.WEB_PAGE,
                        trust_level=TrustLevel.UNTRUSTED_WEB, subject="web"))
    authors = {os.path.basename(p): _head(p)["sources"][0]["author"]
               for p in _concepts(vault)}
    require("human:owner" in authors.values(), f"kein Mensch benannt: {authors}")
    require(any(a.startswith("process:") for a in authors.values()),
            f"kein Prozess benannt: {authors}")


# =====================================================================
# Die Grenze: OKF-Verifikation ist keine SOLVIO-Autoritaet
# =====================================================================

def t_the_compiler_never_claims_a_document_was_verified() -> None:
    """Ein Feld, das ein Werkzeug sich selbst ausstellt, ist kein Beleg."""
    vault = _built(_rec(id="5" * 32))
    for path in _concepts(vault):
        require("verified" not in _head(path),
                f"der Compiler hat sich selbst bescheinigt: {path}")


def t_a_hand_added_verification_grants_nothing() -> None:
    """`verified: human:owner` sagt „gegengelesen", nie „genehmigt".

    Der Beleg ist nicht, dass etwas gefiltert wird, sondern dass niemand es
    liest: keine Funktion im Paket verzweigt auf `verified`, und die Wirkung
    des Dokuments bleibt `none`.
    """
    import inspect
    from solvio.knowledge import service

    vault = _built(_rec(id="6" * 32))
    path = _concepts(vault)[0]
    text = open(path, encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(text.replace(
        "generated:",
        'verified:\n  - by: "human:owner"\n    at: "2026-08-25T12:00:00+00:00"\n'
        "generated:"))
    head = _head(path)
    require(head.get("verified"), "die Vorbedingung des Tests trifft nicht zu")
    require_equal(head["solvio"]["authority_effect"], "none",
                  "eine Gegenlesung hat die Wirkung veraendert")
    require_equal(okf.validate_bundle(vault, known_memory_ids={"6" * 32}), [],
                  "ein gegengelesenes Dokument gilt ploetzlich als fehlerhaft")

    for module in (O, C, okf, service):
        for name in dir(module):
            member = getattr(module, name)
            if not inspect.isfunction(member):
                continue
            if getattr(member, "__module__", "") != module.__name__:
                continue
            body = inspect.getsource(member)
            for branch in ("if verified", "if head.get(\"verified\")",
                           "if concept.verified and"):
                require(branch not in body,
                        f"{module.__name__}.{name} verzweigt auf `verified`")


def t_every_document_declares_that_it_grants_nothing() -> None:
    vault = _built(_rec(id="7" * 32), _rec(id="8" * 32,
                                           memory_type=MemoryType.RULE))
    for path in _concepts(vault):
        space = _head(path).get("solvio") or {}
        require_equal(space.get("authority_effect"), "none", f"in {path}")
        require_equal(space.get("canonical"), False, f"in {path}")


def t_no_document_may_carry_a_key_that_looks_like_a_permission() -> None:
    """Ein Schluessel namens `approval_required` waere frueher oder spaeter geglaubt."""
    vault = _built(_rec(id="9" * 32))
    path = _concepts(vault)[0]
    text = open(path, encoding="utf-8").read()
    # Nur den Frontmatter-Schluessel treffen: `solvio://memory/...` steht auch
    # im Rumpf, und ein Treffer dort haette die Datei zerschossen statt sie
    # zu faelschen — der Test haette dann den Parser gemessen, nicht die Regel.
    open(path, "w", encoding="utf-8").write(
        text.replace("\nsolvio:\n", "\napproval_required: false\nsolvio:\n", 1))
    findings = okf.validate_bundle(vault)
    require(any("Befugnis" in f.message for f in findings),
            f"nicht bemerkt: {[f.message for f in findings]}")


def t_the_security_core_does_not_know_this_package_exists() -> None:
    """Kein Freigabe- oder Sicherheitsmodul darf das Buendel lesen.

    Das ist die strukturelle Fassung von „Markdown erteilt keine Befugnis":
    selbst wenn jemand eine Datei perfekt faelscht, gibt es keinen Aufrufer,
    dem sie etwas sagen koennte.
    """
    import ast

    # Geprueft werden IMPORTE, nicht Prosa. Die erste Fassung suchte den
    # Namen irgendwo in der Datei und schlug an einem Kommentar an, der genau
    # diese Regel ERKLAERTE. Ein Test, der seine eigene Begruendung als
    # Verstoss liest, wird frueher oder spaeter aufgeweicht statt geschaerft —
    # und dann faengt er auch den echten Import nicht mehr.
    for folder in ("security", "capabilities", "approval"):
        base = os.path.join(_SRC, folder)
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    else:
                        continue
                    for module in modules:
                        require(not module.startswith("solvio.knowledge"),
                                f"{folder}/{name} importiert {module}")


# =====================================================================
# Lebenszyklus
# =====================================================================

def t_web_research_never_becomes_stable_personal_knowledge() -> None:
    """„SOLVIO hat es gelesen" ist nicht „SOLVIO weiss es".

    Ohne eine neue Kategorie zu erfinden: der eingefrorene Contract stellt die
    `untrusted_*`-Klassen und `agent_generated` schon so, dass sie nichts
    autorisieren. OKFs Lebenszyklus bildet das ab.
    """
    for trust in (TrustLevel.UNTRUSTED_WEB, TrustLevel.UNTRUSTED_EMAIL,
                  TrustLevel.UNTRUSTED_DOCUMENT, TrustLevel.AGENT_GENERATED,
                  TrustLevel.EXTERNAL_TOOL_RESULT):
        record = _rec(source_type=SourceType.WEB_PAGE, trust_level=trust)
        require_equal(C.status_of(record), "draft",
                      f"{trust.value} gilt als gesichertes Wissen")
    for trust in (TrustLevel.USER_DIRECT, TrustLevel.SYSTEM_TRUSTED,
                  TrustLevel.MEMORY_CURATED):
        require_equal(C.status_of(_rec(trust_level=trust)), "stable",
                      f"{trust.value} gilt nicht als gesichert")


def t_freshness_is_never_invented() -> None:
    """`stale_after` steht nur da, wo das Gedaechtnis wirklich ein Ende kennt."""
    without = C.concept_for(_rec(id="a" * 32), generated_at=_NOW.isoformat())
    require(without.stale_after is None, "eine Haltbarkeit wurde erfunden")
    with_end = C.concept_for(
        _rec(id="b" * 32, valid_until=_NOW + timedelta(days=5)),
        generated_at=_NOW.isoformat())
    require_equal(with_end.stale_after, (_NOW + timedelta(days=5)).isoformat(),
                  "das Gueltigkeitsende wurde nicht uebernommen")


def t_every_timestamp_carries_an_explicit_offset() -> None:
    """Die Fassung v0.2 hat genau das vereinheitlicht (Commit 6243209)."""
    vault = _built(_rec(id="c" * 32, valid_until=_NOW + timedelta(days=1)))
    head = _head(_concepts(vault)[0])
    stamps = [head["generated"]["at"], head["stale_after"],
              head["sources"][0]["last_modified"]]
    for stamp in stamps:
        require(okf._is_iso(str(stamp)), f"ohne Versatz: {stamp!r}")


def t_the_lifecycle_has_no_invented_values() -> None:
    """Kein `learned`, kein `proposed` — die gibt es im Contract nicht."""
    require_equal(set(okf.STATUSES), {"draft", "stable", "deprecated"},
                  "der Lebenszyklus wurde erweitert")
    vault = _built(_rec(id="d" * 32))
    for path in _concepts(vault):
        require(_head(path)["status"] in okf.STATUSES, f"in {path}")


# =====================================================================
# Nur Betroffenes wird neu gebaut
# =====================================================================

def t_only_affected_concepts_are_recompiled() -> None:
    vault = tempfile.mkdtemp(prefix="solvio-okf-dep-")
    stable = _rec(id="e" * 32, subject="bleibt", content="unveraendert")
    changing = _rec(id="f" * 32, subject="aendert", content="alt")
    C.compile_bundle([stable, changing], vault, now=_NOW)
    again = C.compile_bundle(
        [stable, _rec(id="f" * 32, subject="aendert", content="neu")],
        vault, now=_NOW)
    require_equal(again.compiled, 1, str(again.as_dict()))
    require_equal(again.unchanged, 1, str(again.as_dict()))


def t_the_manifest_says_which_memory_a_concept_came_from() -> None:
    """Ohne diese Abhaengigkeit muesste jeder Lauf alles neu schreiben."""
    vault = _built(_rec(id="1" * 32))
    with open(os.path.join(vault, C.MANIFEST), encoding="utf-8") as handle:
        book = json.load(handle)
    require_equal(book["okf_version"], "0.2", "die Fassung fehlt")
    entry = book["concepts"]["1" * 32]
    require_equal(list(entry["inputs"]), ["1" * 32],
                  f"keine Abhaengigkeit verzeichnet: {entry}")


# =====================================================================
# Der Validator: offline, und nicht ueberstreng
# =====================================================================

def t_validation_needs_neither_network_nor_memory() -> None:
    """Ein Validator, der eine Datenbank oeffnen muss, taugt niemandem sonst."""
    import inspect
    source = inspect.getsource(okf)
    for forbidden in ("import requests", "import aiohttp", "urlopen",
                      "import socket", "sqlite3", "SolvioMemory"):
        require(forbidden not in source, f"okf.py braucht {forbidden}")
    vault = _built(_rec(id="2" * 32))
    require_equal(okf.validate_bundle(vault), [],
                  "ein frisches Buendel gilt als fehlerhaft")


def t_the_validator_does_not_reject_what_the_spec_forbids_rejecting() -> None:
    """SPEC §11: fehlende optionale Felder, unbekannte `type`-Werte und
    unbekannte Schluessel sind KEIN Fehler.

    Ein Validator, der daran scheitert, macht das Format kaputt statt es zu
    schuetzen — er verbietet genau die Erweiterbarkeit, die OKF zusagt.
    """
    vault = tempfile.mkdtemp(prefix="solvio-okf-lenient-")
    os.makedirs(os.path.join(vault, "fremd"))
    open(os.path.join(vault, okf.INDEX), "w", encoding="utf-8").write(
        '---\nokf_version: "0.2"\n---\n\n# Fremdes Buendel\n')
    open(os.path.join(vault, "fremd", "x.md"), "w", encoding="utf-8").write(
        '---\ntype: "voellig-unbekannter-typ"\n'
        'irgendein_fremdes_feld: "ja"\n'
        'solvio:\n  authority_effect: "none"\n  canonical: false\n'
        "---\n\n# Fremd\n")
    require_equal(okf.validate_bundle(vault), [],
                  "der Validator ist strenger als die Spezifikation")


def t_a_timestamp_without_an_offset_is_caught() -> None:
    vault = _built(_rec(id="3" * 32))
    path = _concepts(vault)[0]
    text = open(path, encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(
        text.replace('at: "2026-08-25T12:00:00+00:00"',
                     'at: "2026-08-25T12:00:00"'))
    findings = okf.validate_bundle(vault)
    require(any("Versatz" in f.message for f in findings),
            f"nicht bemerkt: {[f.message for f in findings]}")


# =====================================================================
# Ein fremder Verbraucher — ohne Obsidian
# =====================================================================

def t_a_plain_markdown_consumer_can_walk_the_whole_bundle() -> None:
    """Kein Obsidian, kein Plugin, keine Wiki-Verweise.

    Der Test ist absichtlich dumm: Frontmatter abschneiden, Verweise mit einer
    Zeile Regex finden, Prozentkodierung aufloesen, Datei oeffnen. Was so nicht
    zu lesen ist, ist nicht portabel.
    """
    from urllib.parse import unquote

    vault = _built(_rec(id="4" * 32, subject="erstes"),
                   _rec(id="5" * 32, subject="zweites",
                        memory_type=MemoryType.PREFERENCE))
    seen: set[str] = set()
    queue = [okf.INDEX]
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        path = os.path.join(vault, rel)
        require(os.path.exists(path), f"Verweis ins Leere: {rel}")
        text = open(path, encoding="utf-8").read()
        require("[[" not in text, f"Wiki-Verweis in {rel} — nicht portabel")
        for href in re.findall(r"\]\(([^)]+)\)", text):
            if href.startswith(("http://", "https://", "solvio://", "#")):
                continue
            queue.append(os.path.normpath(
                os.path.join(os.path.dirname(rel), unquote(href))))
    require(len([s for s in seen if s.endswith(".md")]) >= 5,
            f"der Verbraucher fand nur: {sorted(seen)}")
    require(any(s.endswith("erstes--44444444.md") for s in seen),
            f"ein Begriff war nicht erreichbar: {sorted(seen)}")


def t_the_frontmatter_is_readable_by_a_standard_yaml_parser() -> None:
    """Der eigene Leser ist streng; ein fremder darf trotzdem nicht scheitern."""
    try:
        import yaml
    except ImportError:
        print("SKIP: PyYAML nicht vorhanden")
        return
    vault = _built(_rec(id="6" * 32, subject="Praeferenz: kurz & knapp",
                        content='Er sagte: "kurz!"\nund meinte es ernst.',
                        tags=["a", "b"]))
    for path in _concepts(vault) + [os.path.join(vault, okf.INDEX)]:
        raw = okf.split_frontmatter(open(path, encoding="utf-8").read())[0]
        head = yaml.safe_load(raw)
        require(isinstance(head, dict), f"kein Frontmatter in {path}")
    theirs = yaml.safe_load(okf.split_frontmatter(
        open(_concepts(vault)[0], encoding="utf-8").read())[0])
    mine = _head(_concepts(vault)[0])
    require_equal(theirs["solvio"]["authority_effect"],
                  mine["solvio"]["authority_effect"],
                  "die beiden Leser sind sich uneinig")
    require_equal(theirs["title"], mine["title"],
                  "die beiden Leser sind sich uneinig")


# =====================================================================
# Migration aus der ersten Ausbaustufe
# =====================================================================

def _v1_note(record: MemoryRecord) -> str:
    """Eine Notiz, wie die erste Ausbaustufe sie geschrieben hat."""
    body = "\n".join([f"# {record.subject}", "", record.content, "",
                      "---", "", "*Diese Notiz wird von SOLVIO erzeugt.*"])
    head = "\n".join([
        "---",
        f'solvio_memory_id: "{record.id}"',
        f'solvio_memory_type: "{record.memory_type.value}"',
        f'solvio_subject: "{record.subject}"',
        f'solvio_body_hash: "{O._body_fingerprint(body)}"',
        "solvio_generated: true",
        "---"])
    return f"{head}\n\n{body}\n"


def t_a_v1_vault_becomes_a_v2_bundle_without_losing_anything() -> None:
    vault = tempfile.mkdtemp(prefix="solvio-okf-mig-")
    record = _rec(id="7" * 32, subject="altbestand", content="stand schon da")
    old = os.path.join(vault, "06 Wissen", O.note_name(record))
    os.makedirs(os.path.dirname(old))
    open(old, "w", encoding="utf-8").write(_v1_note(record))
    os.makedirs(os.path.join(vault, "00 Uebersicht"))
    open(os.path.join(vault, "00 Uebersicht", "Wissen.md"), "w",
         encoding="utf-8").write("# Wissen\n\n- [[x|y]]\n\n*Erzeugt.*\n")
    open(os.path.join(vault, O.LEGACY_MANIFEST), "w",
         encoding="utf-8").write('{"notes": {}}')

    result = C.compile_bundle([record], vault, now=_NOW)
    require_equal(result.conflicted, 0,
                  "eine Notiz der ersten Ausbaustufe galt als handgeschrieben")
    require_equal(result.compiled, 1, str(result.as_dict()))
    require_equal(_head(old)["solvio"]["authority_effect"], "none",
                  "die Notiz wurde nicht ins neue Format gebracht")
    require(not os.path.exists(os.path.join(vault, "00 Uebersicht")),
            "die alte Uebersicht steht noch")
    require(os.path.exists(os.path.join(vault, O.ARCHIVE,
                                        "uebersicht-Wissen.md")),
            "die alte Uebersicht wurde geloescht statt abgelegt")
    require(not os.path.exists(os.path.join(vault, O.LEGACY_MANIFEST)),
            "zwei Buecher ueber denselben Vault")
    require_equal(okf.validate_bundle(vault, known_memory_ids={"7" * 32}), [],
                  "das migrierte Buendel ist nicht konform")


def t_a_hand_written_page_survives_the_migration() -> None:
    """Was ein Mensch abgelegt hat, wird nicht mit aufgeraeumt."""
    vault = tempfile.mkdtemp(prefix="solvio-okf-mig2-")
    os.makedirs(os.path.join(vault, "00 Uebersicht"))
    mine = os.path.join(vault, "00 Uebersicht", "Meine Notizen.md")
    open(mine, "w", encoding="utf-8").write("# Meine Notizen\n\nvon Hand.\n")
    C.compile_bundle([_rec(id="8" * 32)], vault, now=_NOW)
    require(os.path.exists(mine), "die Seite des Menschen wurde weggeraeumt")


# =====================================================================
# Was das Buendel NICHT enthaelt
# =====================================================================

def t_the_raw_source_string_never_reaches_the_bundle() -> None:
    """Provenienz belegt man mit einer Kennung, nicht mit einem Mitschnitt.

    `MemoryRecord.source` ist der Rohbeleg — eine Nachrichtenkennung, eine
    Sitzung, eine URL. Er gehoert in die Datenbank und nicht in eine Datei,
    die ein Mensch weiterreicht. Im Buendel steht stattdessen `source_type`:
    WER es gesagt hat, ohne WO es steht.
    """
    marker = "gmail:msg-7f3a-vertraulich@example.invalid"
    vault = _built(_rec(id="9" * 32, source=marker,
                        source_type=SourceType.GMAIL_MESSAGE,
                        trust_level=TrustLevel.UNTRUSTED_EMAIL))
    for root, dirs, files in os.walk(vault):
        for name in files:
            text = open(os.path.join(root, name), encoding="utf-8",
                        errors="ignore").read()
            require(marker not in text,
                    f"der Rohbeleg steht in {name}")


def t_there_is_no_raw_conversation_archive() -> None:
    """Kein Ordner, in dem sich Gesagtes sammelt."""
    vault = _built(_rec(id="8" * 32))
    folders = {d for _, dirs, _ in os.walk(vault) for d in dirs}
    for unwanted in ("Gespraeche", "conversations", "transcripts", "audio"):
        require(unwanted not in folders, f"es gibt einen Ordner {unwanted}")


def t_nothing_in_the_bundle_is_canonical() -> None:
    vault = _built(_rec(id="a" * 32))
    root = open(os.path.join(vault, okf.INDEX), encoding="utf-8").read()
    require("keine Wahrheit" in root,
            "die Wurzel sagt nicht, dass sie eine Ansicht ist")
    require("kein Vergessen" in root,
            "die Wurzel sagt nicht, was Loeschen bedeutet")


def t_a_hand_written_page_is_not_reported_as_broken() -> None:
    """Eine Seite, die ein Mensch abgelegt hat, ist kein kaputter Begriff.

    Am echten Vault gefunden: die `README.md` aus der ersten Ausbaustufe wurde
    als „Frontmatter unlesbar" gemeldet. Ein Validator, der einem Menschen sein
    eigenes Verzeichnis vorwirft, wird nach dem dritten Mal ignoriert — und
    dann uebersieht man mit ihm auch die echten Befunde.
    """
    vault = _built(_rec(id="b" * 32))
    open(os.path.join(vault, "README.md"), "w", encoding="utf-8").write(
        "# Meine Notizen\n\nDas hier habe ich selbst geschrieben.\n")
    os.makedirs(os.path.join(vault, "06 Wissen", "eigenes"), exist_ok=True)
    open(os.path.join(vault, "06 Wissen", "eigenes", "idee.md"), "w",
         encoding="utf-8").write("nur ein Gedanke\n")
    require_equal(okf.validate_bundle(vault, known_memory_ids={"b" * 32}), [],
                  "eine handgeschriebene Seite gilt als fehlerhaft")
    require_equal(C.lint(vault, now=_NOW), [], "dasselbe im Bericht")


def t_a_damaged_concept_is_still_caught() -> None:
    """Formlos ist erlaubt — ausser fuer eine Datei, die der Compiler besitzt."""
    vault = _built(_rec(id="c" * 32))
    path = _concepts(vault)[0]
    open(path, "w", encoding="utf-8").write("nur noch Text, kein Frontmatter\n")
    findings = C.lint(vault, now=_NOW)
    require(any("beschaedigt" in f for f in findings),
            f"ein zerschossener Begriff faellt durch: {findings}")


def t_the_archive_is_reachable_through_an_index() -> None:
    """Ein Verweis auf ein VERZEICHNIS oeffnet bei einem fremden Leser nichts.

    Am echten Vault gefunden: die Wurzel verwies auf `99 Archiv/`. Obsidian
    macht daraus etwas Benutzbares, ein gewoehnlicher Markdown-Leser nicht.
    """
    from urllib.parse import unquote

    vault = _built(_rec(id="d" * 32, subject="verschwindet"))
    C.compile_bundle([], vault, now=_NOW)
    index = os.path.join(vault, O.ARCHIVE, okf.INDEX)
    require(os.path.exists(index), "das Archiv hat keinen Index")
    root = open(os.path.join(vault, okf.INDEX), encoding="utf-8").read()
    for href in re.findall(r"\]\(([^)]+)\)", root):
        target = os.path.join(vault, unquote(href))
        require(os.path.isfile(target),
                f"die Wurzel verweist nicht auf eine Datei: {href}")
    require("verschwindet" in open(index, encoding="utf-8").read(),
            "der abgelegte Begriff steht nicht im Archiv-Index")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
