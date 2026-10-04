"""Open Knowledge Format v0.2 — das Format, in dem SOLVIO Wissen herausgibt.

Spezifikation: GoogleCloudPlatform/knowledge-catalog, `okf/SPEC.md`,
Fassung **v0.2**, Stand Commit `6243209` vom 2026-08-21 („make every timestamp
an ISO 8601 datetime with an explicit offset"). Die Spezifikation liegt NICHT
im Repository und wird zur Laufzeit NICHT geholt — was von ihr gilt, steht
hier und in `docs/architecture/KNOWLEDGE_ARCHITECTURE.md`.

WAS OKF SCHON HAT, WIRD NICHT NEU ERFUNDEN. In der ersten Ausbaustufe hiessen
die Felder `solvio_memory_type`, `solvio_created_at`, `solvio_generated` —
alles Dinge, fuer die OKF `type`, `generated.at` und `generated.by` kennt. Ein
eigener Name fuer eine fremde Idee macht ein Buendel unlesbar fuer jeden, der
nicht SOLVIO ist. Nur was OKF NICHT ausdrueckt, steht unter `solvio:`.

DIE WICHTIGSTE ZEILE DIESES MODULS ist `authority_effect: none`, und sie steht
in jedem erzeugten Dokument. OKF kennt `verified: {by: "human:owner"}` und
leitet daraus eine Vertrauensstufe ab — „human-reviewed". Das ist eine Aussage
ueber die Sorgfalt eines Textes und **niemals** eine Aussage ueber Befugnis.
Ein Mensch, der eine Notiz gegengelesen hat, hat damit keine Freigabe erteilt,
kein Face ID ersetzt und kein Risiko herabgestuft. Der Validator faellt
geschlossen aus, wenn ein Dokument etwas anderes behauptet.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote

#: Die Fassung, gegen die dieses Modul gebaut ist.
OKF_VERSION = "0.2"

#: Woher sie stammt — fuer die Aufzeichnung, nicht fuer die Laufzeit.
OKF_SPEC_SOURCE = ("GoogleCloudPlatform/knowledge-catalog okf/SPEC.md "
                   "@6243209 (2026-08-21)")

#: Reservierte Dateinamen (SPEC §3.1). `index.md` traegt KEIN Frontmatter —
#: ausser in der Buendelwurzel, wo genau ein Schluessel erlaubt ist.
INDEX = "index.md"
LOG = "log.md"
RESERVED = (INDEX, LOG)

#: Wer etwas erzeugt hat (SPEC §7). Drei Gestalten: Werkzeug mit Fassung,
#: Mensch mit `human:`-Vorsatz, Prozess mit `process:`-Vorsatz. Verbraucher
#: erkennen an genau diesem Vorsatz, ob ein Mensch beteiligt war.
COMPILER_ACTOR = "solvio-knowledge-compiler/2"

#: Erlaubte Lebenszyklus-Werte (SPEC §5.4). Fehlt `status`, gilt `stable`.
STATUSES = ("draft", "stable", "deprecated")

#: Der Namensraum fuer alles, was OKF nicht ausdrueckt (SPEC §4.1 erlaubt
#: zusaetzliche Schluessel ausdruecklich; Verbraucher duerfen sie nicht
#: zurueckweisen).
NAMESPACE = "solvio"

#: Schluessel, die in einem Wissensdokument NIEMALS vorkommen duerfen — weder
#: oben noch im Namensraum. Sie wuerden aussehen, als koennten sie etwas
#: erlauben.
FORBIDDEN_KEYS = (
    "authority", "approval", "approval_required", "approved", "permission",
    "permissions", "grant", "grants", "risk", "risk_level", "trust_context",
    "trustcontext", "face_id", "faceid", "capability", "capabilities",
    "allow", "allowed", "authorize", "authorized", "authorised",
)

#: Der einzige erlaubte Wert fuer `solvio.authority_effect`.
NO_AUTHORITY = "none"

_ID_OK = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")
_MEMORY_URI = re.compile(r"\Asolvio://memory/([0-9a-f]{8,64})\Z")


class OkfInvalid(ValueError):
    """Ein Dokument haelt sich nicht an die Spezifikation oder an SOLVIOs Regeln."""


def utc(value: Any) -> str | None:
    """ISO 8601 mit ausdruecklichem Versatz — genau das verlangt SPEC v0.2.

    Die Fassung vom 2026-08-21 hat das vereinheitlicht: jeder Zeitwert traegt
    einen Versatz. Ein Zeitstempel ohne Zone ist eine Behauptung ueber einen
    Moment, den niemand nachrechnen kann.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    return str(value)


def memory_uri(memory_id: str) -> str:
    """Die stabile Kennung einer kanonischen Erinnerung.

    Sie benennt die EXISTENZ eines Datensatzes, nie seinen Inhalt. Ein
    geschuetzter Eintrag kann so als Herkunft auftauchen, ohne dass sein Wert
    die Datenbank verlaesst.
    """
    ident = (memory_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8,64}", ident):
        raise OkfInvalid(f"Keine gueltige Gedaechtniskennung: {memory_id!r}")
    return f"solvio://memory/{ident}"


def memory_id_of(uri: str) -> str | None:
    """Die Kennung aus einer SOLVIO-Herkunftsangabe — oder `None`.

    `None` heisst: das ist keine Herkunft, die dieses System kennt. Eine
    erfundene `solvio://memory/...`-Angabe in einer von Hand geaenderten Datei
    faellt genau hier durch.
    """
    match = _MEMORY_URI.match((uri or "").strip())
    return match.group(1) if match else None


# --------------------------------------------------------------- Datenmodell

@dataclass
class Source:
    """Eine Herkunftsangabe (SPEC §5.1)."""

    id: str
    resource: str
    title: str | None = None
    author: str | None = None
    last_modified: str | None = None


@dataclass
class Concept:
    """Ein Wissensdokument im Sinne von OKF.

    `type` ist das einzige Pflichtfeld der Spezifikation. Alles andere ist
    empfohlen oder optional — und was SOLVIO zusaetzlich braucht, liegt unter
    `solvio`.
    """

    type: str
    title: str
    body: str
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    generated_by: str = COMPILER_ACTOR
    generated_at: str | None = None
    verified: list[dict[str, str]] = field(default_factory=list)
    status: str = "stable"
    stale_after: str | None = None
    solvio: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------- Erzeugung

def _scalar(value: Any) -> str:
    """Ein YAML-Wert, der das Frontmatter nicht verlassen kann.

    Alles wird zitiert und maskiert. Ohne das koennte ein Gedaechtnisinhalt mit
    einem Zeilenumbruch eigene Schluessel erfinden — etwa `authority: true`.
    Genau davor schuetzt diese Funktion, und der Validator prueft danach noch
    einmal nach.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + text.replace("\n", "\\n").replace("\r", "") + '"'


def _block(name: str, mapping: dict[str, Any], indent: str = "  ") -> list[str]:
    lines = [f"{name}:"]
    for key, value in mapping.items():
        if value is None:
            continue
        lines.append(f"{indent}{key}: {_scalar(value)}")
    return lines if len(lines) > 1 else []


def render_concept(concept: Concept) -> str:
    """Ein vollstaendiges OKF-Dokument. Deterministisch."""
    if not (concept.type or "").strip():
        raise OkfInvalid("`type` ist Pflicht und darf nicht leer sein")
    if concept.status not in STATUSES:
        raise OkfInvalid(f"Unbekannter Lebenszyklus-Wert: {concept.status!r}")

    lines = ["---", f"type: {_scalar(concept.type)}",
             f"title: {_scalar(concept.title)}"]
    if concept.description:
        lines.append(f"description: {_scalar(concept.description)}")
    if concept.tags:
        lines.append("tags:")
        lines += [f"  - {_scalar(tag)}" for tag in sorted(concept.tags)]
    lines.append(f"status: {_scalar(concept.status)}")
    if concept.stale_after:
        lines.append(f"stale_after: {_scalar(concept.stale_after)}")

    if concept.sources:
        lines.append("sources:")
        for source in concept.sources:
            lines.append(f"  - id: {_scalar(source.id)}")
            lines.append(f"    resource: {_scalar(source.resource)}")
            for key in ("title", "author", "last_modified"):
                value = getattr(source, key)
                if value is not None:
                    lines.append(f"    {key}: {_scalar(value)}")

    lines += _block("generated", {"by": concept.generated_by,
                                  "at": concept.generated_at})
    if concept.verified:
        lines.append("verified:")
        for entry in concept.verified:
            lines.append(f"  - by: {_scalar(entry.get('by'))}")
            lines.append(f"    at: {_scalar(entry.get('at'))}")

    # Der Namensraum. `authority_effect` steht immer drin und immer auf `none`.
    space = dict(concept.solvio)
    space.setdefault("authority_effect", NO_AUTHORITY)
    lines.append(f"{NAMESPACE}:")
    for key in sorted(space):
        value = space[key]
        if isinstance(value, list):
            lines.append(f"  {key}:")
            lines += [f"    - {_scalar(item)}" for item in value]
        else:
            lines.append(f"  {key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + concept.body.rstrip("\n") + "\n"


def render_index(title: str, intro: str,
                 sections: list[tuple[str, list[tuple[str, str, str]]]],
                 *, is_root: bool = False) -> str:
    """Ein `index.md` — Abschnitte mit Verweis und einzeiliger Beschreibung.

    OKF verbietet Frontmatter in `index.md`; nur die Buendelwurzel darf genau
    einen Schluessel tragen, `okf_version`. Ein Index mit vollem Frontmatter
    waere kein Index, sondern ein weiteres Wissensdokument — und ein Verbraucher
    wuesste nicht mehr, wo das Buendel anfaengt.

    Die Verweise sind gewoehnliche Markdown-Links mit relativem Pfad. So kann
    ein Leser, der Obsidian nicht kennt, dem Buendel folgen.
    """
    lines: list[str] = []
    if is_root:
        lines += ["---", f'okf_version: "{OKF_VERSION}"', "---", ""]
    lines += [f"# {title}", "", intro.strip(), ""]
    for heading, entries in sections:
        if not entries:
            continue
        lines += [f"## {heading}", ""]
        for label, href, note in entries:
            suffix = f" — {note}" if note else ""
            lines.append(f"- [{label}]({link(href)}){suffix}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def link(href: str) -> str:
    """Ein Verweis, dem auch ein fremder Markdown-Leser folgen kann.

    Die Ordner heissen „01 Ich", „06 Wissen" — mit Leerzeichen, weil ein Mensch
    sie liest. In einem Markdown-Verweis beendet ein Leerzeichen aber das Ziel.
    Obsidian verzeiht das; ein gewoehnlicher Markdown-Verbraucher nicht, und
    genau der soll dieses Buendel lesen koennen.
    """
    return quote(href, safe="/#.-_~")


def render_log(entries: list[tuple[str, list[str]]]) -> str:
    """Ein `log.md` — nach Datum gruppiert, neueste zuerst (SPEC §9).

    Was hier hineingehoert: dass ein Begriff entstand, neu gebaut wurde, dass
    eine Quelle ueberholt ist. Was NICHT: Aufforderungen, Gespraeche,
    Gedankengaenge, Geheimnisse. Ein Protokoll ist eine Chronik, kein Archiv.
    """
    lines = ["# Verlauf", "",
             "Was sich am Wissen geaendert hat, und wann. Keine Gespraeche, "
             "keine Aufforderungen, keine Geheimnisse.", ""]
    for day, items in entries:
        lines += [f"## {day}", ""]
        lines += [f"- {item}" for item in items]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ------------------------------------------------------------------ Pruefung

def split_frontmatter(text: str) -> tuple[str, str]:
    """Trennt Frontmatter und Rumpf. Wirft, wenn das Dokument missgestaltet ist."""
    if not text.startswith("---"):
        raise OkfInvalid("kein Frontmatter")
    end = text.find("\n---", 3)
    if end < 0:
        raise OkfInvalid("Frontmatter ist nicht geschlossen")
    return text[4:end], text[end + 4:].lstrip("\n")


def parse_frontmatter(raw: str) -> dict[str, Any]:
    """Ein kleiner, absichtlich strenger YAML-Leser fuer Frontmatter.

    Kein `yaml`-Modul: das Buendel soll ohne zusaetzliche Abhaengigkeit
    pruefbar sein, und ein voller YAML-Leser kann sehr viel mehr, als hier
    vorkommen darf — Ankerverweise, Typ-Marken, ausfuehrbare Konstrukte. Was
    dieser Leser nicht versteht, ist kein gueltiges SOLVIO-Frontmatter.

    Er kennt genau drei Gestalten: `schluessel: wert`, eine Liste aus `- wert`,
    und eine Liste aus Abbildungen, deren erster Schluessel am Strich klebt.
    Mehr erzeugt der Compiler nicht.
    """
    rows: list[tuple[int, str]] = []
    for line in raw.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rows.append((len(line) - len(line.lstrip(" ")), line.strip()))
    value, index = _parse_block(rows, 0, rows[0][0] if rows else 0)
    if index != len(rows):
        raise OkfInvalid("Einrueckung ergibt keinen Sinn")
    return value if isinstance(value, dict) else {}


def _parse_block(rows: list[tuple[int, str]], index: int,
                 indent: int) -> tuple[Any, int]:
    """Liest alles, was auf `indent` oder tiefer eingerueckt ist."""
    if index >= len(rows):
        return {}, index
    if rows[index][1].startswith("- "):
        items: list[Any] = []
        while index < len(rows) and rows[index][0] == indent \
                and rows[index][1].startswith("- "):
            inner = rows[index][1][2:].strip()
            column = rows[index][0] + 2
            if _looks_like_key(inner):
                key, _, rest = inner.partition(":")
                entry: dict[str, Any] = {}
                index += 1
                if rest.strip() == "":
                    child, index = _parse_block(rows, index, column + 2)
                    entry[key.strip()] = child
                else:
                    entry[key.strip()] = _unscalar(rest.strip())
                while index < len(rows) and rows[index][0] == column \
                        and not rows[index][1].startswith("- "):
                    more, index = _parse_pair(rows, index, column)
                    entry.update(more)
                items.append(entry)
            else:
                items.append(_unscalar(inner))
                index += 1
        return items, index

    mapping: dict[str, Any] = {}
    while index < len(rows) and rows[index][0] == indent:
        if rows[index][1].startswith("- "):
            break
        pair, index = _parse_pair(rows, index, indent)
        mapping.update(pair)
    return mapping, index


def _parse_pair(rows: list[tuple[int, str]], index: int,
                indent: int) -> tuple[dict[str, Any], int]:
    body = rows[index][1]
    if not _looks_like_key(body):
        raise OkfInvalid(f"Zeile ohne Schluessel: {body[:40]!r}")
    key, _, value = body.partition(":")
    index += 1
    if value.strip() != "":
        return {key.strip(): _unscalar(value.strip())}, index
    if index < len(rows) and rows[index][0] > indent:
        child, index = _parse_block(rows, index, rows[index][0])
        return {key.strip(): child}, index
    return {key.strip(): None}, index


def _looks_like_key(text: str) -> bool:
    """Ob diese Zeile ein Schluessel ist — ein zitierter Wert ist keiner."""
    if not text or text[0] in "\"'":
        return False
    head = text.partition(":")[0]
    return ":" in text and "\"" not in head and " " not in head.strip()


def _unscalar(text: str) -> Any:
    """Ein Frontmatter-Wert zurueck in einen Python-Wert.

    Die Gegenrichtung zu `_scalar`. Zitierte Werte werden entzitiert und ihre
    Maskierung aufgeloest; alles andere ist `null`, ein Wahrheitswert oder eine
    Zahl — oder bleibt schlicht Text. Es gibt keinen Weg, ueber den ein
    Frontmatter-Wert hier zu etwas Ausfuehrbarem wird.
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return (inner.replace('\\"', '"').replace("\\n", "\n")
                .replace("\\\\", "\\"))
    if text == "null":
        return None
    if text in ("true", "false"):
        return text == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


@dataclass
class Finding:
    """Ein Befund des Validators. `fatal` heisst: das Buendel ist nicht konform."""

    path: str
    message: str
    fatal: bool = True


def _walk_keys(node: Any, trail: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else str(key)
            yield here, value
            yield from _walk_keys(value, here)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item, trail)


def validate_document(path: str, text: str, *, is_root: bool = False,
                      known_memory_ids: set[str] | None = None) -> list[Finding]:
    """Prueft ein einzelnes Dokument. Kein Netz, keine Abhaengigkeit."""
    name = os.path.basename(path)
    out: list[Finding] = []

    if name == LOG:
        for line in text.split("\n"):
            if line.startswith("## ") and not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", line[3:].strip()):
                out.append(Finding(path, f"Verlaufsueberschrift ist kein Datum: "
                                         f"{line[3:].strip()[:20]!r}"))
        return out

    if name == INDEX:
        if text.startswith("---"):
            if not is_root:
                out.append(Finding(path, "Nur die Buendelwurzel darf im Index "
                                         "Frontmatter tragen (SPEC §3.1)"))
                return out
            try:
                head = parse_frontmatter(split_frontmatter(text)[0])
            except OkfInvalid as exc:
                return [Finding(path, f"Frontmatter unlesbar: {exc}")]
            extra = set(head) - {"okf_version"}
            if extra:
                out.append(Finding(path, f"Die Buendelwurzel darf nur "
                                         f"`okf_version` tragen, hat aber: "
                                         f"{sorted(extra)}"))
            if str(head.get("okf_version")) != OKF_VERSION:
                out.append(Finding(path, f"Nicht unterstuetzte OKF-Fassung: "
                                         f"{head.get('okf_version')!r}"))
        elif is_root:
            out.append(Finding(path, "Die Buendelwurzel muss `okf_version` "
                                     "erklaeren"))
        return out

    # Ab hier: ein Wissensdokument — falls es eines sein will.
    #
    # Eine Datei ganz OHNE Frontmatter ist kein OKF-Dokument, sondern eine
    # Seite, die ein Mensch dort abgelegt hat. Sie als fehlerhaft zu melden
    # hiesse, ihm sein eigenes Verzeichnis vorzuwerfen — und ein Validator,
    # der an fremden Dateien scheitert, wird nach dem dritten Mal ignoriert.
    # Ob eine Datei ein Begriff SEIN MUSS, weiss nur die Buchhaltung des
    # Compilers; das prueft `compiler.lint()`.
    if not text.lstrip().startswith("---"):
        return out
    try:
        raw, body = split_frontmatter(text)
        head = parse_frontmatter(raw)
    except OkfInvalid as exc:
        return [Finding(path, f"Frontmatter unlesbar: {exc}")]

    if not str(head.get("type") or "").strip():
        out.append(Finding(path, "`type` fehlt oder ist leer (SPEC §11)"))

    status = head.get("status", "stable")
    if status not in STATUSES:
        out.append(Finding(path, f"Unbekannter Lebenszyklus-Wert: {status!r}"))

    for key in ("stale_after",):
        value = head.get(key)
        if value is not None and not _is_iso(str(value)):
            out.append(Finding(path, f"`{key}` ist kein ISO-8601-Zeitpunkt mit "
                                     f"Versatz: {value!r}"))

    generated = head.get("generated")
    if isinstance(generated, dict):
        if not generated.get("by"):
            out.append(Finding(path, "`generated.by` fehlt"))
        if generated.get("at") and not _is_iso(str(generated["at"])):
            out.append(Finding(path, f"`generated.at` ohne Versatz: "
                                     f"{generated['at']!r}"))

    for entry in _as_list(head.get("verified")):
        if isinstance(entry, dict) and entry.get("at") and not _is_iso(
                str(entry["at"])):
            out.append(Finding(path, f"`verified.at` ohne Versatz: "
                                     f"{entry['at']!r}"))

    # Herkunft.
    for source in _as_list(head.get("sources")):
        if not isinstance(source, dict):
            out.append(Finding(path, "Herkunftsangabe ist keine Abbildung"))
            continue
        ident = str(source.get("id") or "")
        if not _ID_OK.match(ident):
            out.append(Finding(path, f"Herkunftskennung unbrauchbar: {ident!r}"))
        resource = str(source.get("resource") or "")
        memory = memory_id_of(resource)
        if resource.startswith("solvio://memory/") and memory is None:
            out.append(Finding(path, f"Missgestaltete Gedaechtnisherkunft: "
                                     f"{resource!r}"))
        elif memory is not None and known_memory_ids is not None:
            if memory not in known_memory_ids:
                out.append(Finding(path, f"Herkunft verweist auf eine "
                                         f"Erinnerung, die es nicht gibt: "
                                         f"{resource}"))

    # SOLVIOs Namensraum.
    space = head.get(NAMESPACE)
    if not isinstance(space, dict):
        out.append(Finding(path, f"`{NAMESPACE}` fehlt oder ist keine Abbildung"))
    else:
        effect = space.get("authority_effect")
        if effect != NO_AUTHORITY:
            out.append(Finding(path, f"`{NAMESPACE}.authority_effect` muss "
                                     f"{NO_AUTHORITY!r} sein, ist aber "
                                     f"{effect!r}"))
        if space.get("canonical") is not False:
            out.append(Finding(path, f"`{NAMESPACE}.canonical` muss `false` "
                                     f"sein — dies ist eine Ansicht, keine "
                                     f"Wahrheit"))

    # Kein Schluessel, der aussieht, als koennte er etwas erlauben.
    for trail, _ in _walk_keys(head):
        leaf = trail.split(".")[-1].lower()
        if leaf in FORBIDDEN_KEYS:
            out.append(Finding(path, f"Schluessel `{trail}` sieht aus wie eine "
                                     f"Befugnis und hat in Wissen nichts zu "
                                     f"suchen"))
    return out


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _is_iso(text: str) -> bool:
    """ISO 8601 MIT ausdruecklichem Versatz — so verlangt es v0.2."""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_bundle(root: str,
                    known_memory_ids: set[str] | None = None) -> list[Finding]:
    """Prueft ein ganzes Buendel. Deterministisch, ohne Netz.

    Was NICHT geprueft wird, weil die Spezifikation es ausdruecklich verbietet
    (SPEC §11): fehlende optionale Felder, unbekannte `type`-Werte, unbekannte
    Schluessel, gebrochene Verweise, fehlende `index.md`. Ein Verbraucher darf
    ein Buendel daran nicht scheitern lassen — und ein Validator, der es
    trotzdem tut, macht das Format kaputt statt es zu schuetzen.
    """
    findings: list[Finding] = []
    root = os.path.abspath(root)
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except (OSError, UnicodeDecodeError) as exc:
                findings.append(Finding(path, f"nicht lesbar: {exc}"))
                continue
            is_root = (folder == root and name == INDEX)
            findings += validate_document(path, text, is_root=is_root,
                                          known_memory_ids=known_memory_ids)
    if not os.path.exists(os.path.join(root, INDEX)):
        findings.append(Finding(os.path.join(root, INDEX),
                                "Die Buendelwurzel hat keinen Index"))
    return findings
