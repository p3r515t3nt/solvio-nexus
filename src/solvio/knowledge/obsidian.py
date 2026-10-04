"""Die Ablage des Vaults — Identitaet, Pfadsicherheit, atomares Schreiben.

Dieses Modul besitzt die **Ablage**. Was in eine Datei kommt, entscheidet
`compiler.py`; welche Gestalt es dort hat, entscheidet `okf.py`. Hier steht
nur, wie aus einem Datensatz ein Pfad wird und wie dieser Pfad sicher
beschrieben wird.

EINE RICHTUNG. Der Core schreibt, Obsidian zeigt. Es gibt hier keinen Weg
zurueck: keine Funktion in diesem Paket liest eine Markdown-Datei, um daraus
Gedaechtnis zu machen. Eine geaenderte Datei ist eine INFORMATION ueber einen
Wunsch, niemals eine Aenderung an der Wahrheit.

Der Grund steht in `ROADMAP.md` unter LOCKED ARCHITECTURE DECISIONS: „Obsidian
may become the human-readable Knowledge UI, never canonical machine truth."

DREI DINGE, DIE HIER NICHT PASSIEREN DUERFEN, und warum:

**Der Inhalt waehlt keinen Pfad.** Ein Dateiname entsteht aus einem streng
gefilterten Namensteil PLUS der Kennung des Datensatzes. Was durch den Filter
faellt, faellt weg — es wird nicht ersetzt, nicht umgeschrieben, nicht
gerettet. Ein Gedaechtnisinhalt, der `../` enthaelt, ist damit genauso harmlos
wie einer, der es nicht enthaelt.

**Ein Geheimnis wird nicht bereinigt, sondern verweigert.** Dafuer gibt es im
Repository bereits zwei Praezedenzfaelle, und beide werden hier woertlich
uebernommen statt neu erfunden: `memory/embedding_text.py` bettet bei
`SECRET_REFERENCE` NUR das Subjekt ein und nie den Inhalt, und
`bots/knowledge.py` wirft `BundleUnsafe`, wenn eine Herausgabe nach einer
Bereinigung anders aussaehe als vorher. Wer bereinigt, glaubt zu wissen, was
er gerade herausgibt.

**Sichtbarkeit wird nicht selbst berechnet.** Es waere naheliegend, ueber alle
Datensaetze zu laufen und `record.is_current` zu fragen. Das waere falsch:
`is_current` prueft nur `superseded_by` und kennt das Gueltigkeitsfenster
nicht — und `forgotten` ist ueberhaupt nicht Teil des Records, die Spalte wird
beim Lesen nicht abgebildet. Wer aus Record-Objekten heraus entscheidet,
projiziert Vergessenes. Einzig richtig ist `SolvioMemory.active_records()`:
dort filtert die Ablage `forgotten=0 AND superseded_by IS NULL` und der
Privacy-Ledger blendet Gepurgtes zur Lesezeit aus.

WAS FRUEHER HIER STAND UND BEWUSST WEG IST: ein zweiter Schreiber. Die erste
Ausbaustufe erzeugte Notizen mit flachen `solvio_*`-Schluesseln und eigene
Uebersichtsseiten; beides ist durch das Open Knowledge Format abgeloest. Der
alte Schreiber wurde **entfernt und nicht schlafen gelegt** — ein zweiter
Schreiber im selben Vault haette die Migration beim naechsten Aufruf lautlos
rueckgaengig gemacht.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from typing import Any

from solvio.contracts.memory import MemoryRecord, MemoryType

#: Die Buchhaltung der ERSTEN Ausbaustufe. Sie wird nicht mehr geschrieben;
#: der Compiler raeumt sie beim ersten Lauf weg, damit nicht zwei Buecher
#: ueber denselben Vault verschiedene Dinge behaupten.
LEGACY_MANIFEST = ".solvio-projection.json"

#: Ordner, in den Notizen wandern, deren Gedaechtnis nicht mehr aktuell ist.
#: Gelöscht wird nichts: eine verschwundene Datei sieht aus wie ein Versehen,
#: eine abgelegte sieht aus wie eine Entscheidung.
ARCHIVE = "99 Archiv"

#: Wohin welche Gedaechtnisart projiziert wird.
#:
#: `WORKING` fehlt mit Absicht: der Contract gibt ihm die Lebensdauer
#: „Session / Minuten". Kurzzeitkontext in eine Datei zu schreiben, die Wochen
#: liegen bleibt, waere eine Luege ueber seine Natur.
FOLDERS: dict[MemoryType, str] = {
    MemoryType.USER: "01 Ich",
    MemoryType.PREFERENCE: "01 Ich",
    MemoryType.PEOPLE: "02 Menschen",
    MemoryType.PROJECT: "03 Projekte",
    MemoryType.RULE: "05 Regeln",
    MemoryType.STANDING_INTENT: "05 Regeln",
    MemoryType.SEMANTIC: "06 Wissen",
    MemoryType.EPISODIC: "07 Ereignisse",
}

#: Was nie in einer Datei landet — Namen, die nach Zugangsdaten aussehen.
#: Uebernommen aus `bots/knowledge.py`, damit es genau eine solche Liste im
#: Projekt gibt und nicht zwei, die auseinanderlaufen.
FORBIDDEN_NAMES = (
    "api_key", "apikey", "api-key", "token", "secret", "password", "passwort",
    "passphrase", "credential", "private_key", "privatekey", "hmac",
    "client_secret", "access_token", "refresh_token", "bearer",
)

#: Und was nach einem Zugangsdatum AUSSIEHT, unabhaengig vom Namen daneben.
SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:%s)\b\s*[:=]\s*\S{8,}" % "|".join(FORBIDDEN_NAMES)),
)

#: Erlaubte Zeichen in einem Dateinamensteil. Bewusst eng: Kleinbuchstaben,
#: Ziffern, Bindestrich. Kein Punkt (keine Endungstricks), kein Schraegstrich
#: (kein Verzeichniswechsel), kein Leerzeichen.
_SLUG_OK = re.compile(r"[^a-z0-9-]+")

#: Wie eine Kennung aussehen MUSS, damit wir sie in einen Pfad schreiben.
#: `SolvioMemory` vergibt `uuid4().hex`. Alles andere wird abgelehnt, statt
#: bereinigt zu werden.
_ID_OK = re.compile(r"\A[0-9a-f]{8,64}\Z")

_SLUG_MAX = 40


class ProjectionUnsafe(RuntimeError):
    """Etwas soll in eine Datei, das dort nicht hingehoert.

    Fail-closed: die Projektion bricht ab, statt zu bereinigen. Wer bereinigt,
    behauptet zu wissen, was er gerade herausgibt.
    """


def looks_like_a_secret(text: str) -> bool:
    """Ob dieser Text ein Zugangsdatum enthaelt.

    Die Frage ist absichtlich nicht „welches" — es gibt keine Bereinigung, auf
    die eine Antwort hinauslaufen koennte.

    Zwei Netze, und das zweite ist das staerkere. `SECRET_SHAPES` kennt FORMEN
    (`sk-…`, `ghp_…`, ein PEM-Kopf); es haette „Meine PIN ist 4711" durchgelassen.
    Der Zaun des Tresors kennt zusaetzlich den KONTEXT — ein benannter Zugang mit
    zugewiesenem Wert, auch ueber die Wortzerlegung der Spracherkennung hinweg.

    Dass hier ueberhaupt noch geprueft wird, obwohl das Gedaechtnis solche
    Saetze seit dem Tresor gar nicht mehr aufnimmt, ist Absicht: die Projektion
    liest auch Datensaetze, die VOR dieser Aenderung entstanden sind.
    """
    raw = text or ""
    if any(shape.search(raw) for shape in SECRET_SHAPES):
        return True
    from solvio.secret_vault.firewall import is_credential
    return is_credential(raw)


# ------------------------------------------------------------------ Identitaet

def slug(text: str) -> str:
    """Ein Dateinamensteil aus beliebigem Text — streng gefiltert.

    Umlaute werden zerlegt und ihre Grundbuchstaben behalten, damit „Praeferenz"
    lesbar bleibt. Alles, was danach nicht in `[a-z0-9-]` faellt, verschwindet.
    Was uebrig bleibt, kann kein Verzeichnis wechseln und keine Endung faelschen.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    folded = folded.replace("\u00df", "ss")
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = _SLUG_OK.sub("-", ascii_only).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:_SLUG_MAX].strip("-")


def note_name(record: MemoryRecord) -> str:
    """Der Dateiname eines Datensatzes: lesbarer Teil PLUS Kennung.

    Der lesbare Teil ist Bequemlichkeit und darf leer sein. Die Kennung ist die
    Identitaet und darf es nie — deshalb wird sie geprueft und nicht bereinigt.
    """
    ident = (record.id or "").strip().lower()
    if not _ID_OK.match(ident):
        raise ProjectionUnsafe(
            "Kennung hat nicht die erwartete Gestalt; daraus wird kein Pfad.")
    readable = slug(record.subject or "") or slug(record.content or "")[:24]
    return f"{readable}--{ident[:8]}.md" if readable else f"{ident[:8]}.md"


def folder_for(record: MemoryRecord) -> str | None:
    """In welchen Ordner dieser Datensatz gehoert — oder `None`, wenn gar nicht."""
    return FOLDERS.get(record.memory_type)


# ------------------------------------------------------------------- Schreiben

def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _enum(value: Any) -> Any:
    return getattr(value, "value", value)


def canonical_hash(record: MemoryRecord) -> str:
    """Fingerabdruck dessen, was der Core ueber diesen Datensatz weiss.

    Aus genau den Feldern gebildet, die in die Notiz gehen. Aendert sich einer,
    aendert sich der Abdruck, und die Notiz wird neu geschrieben. Aendert sich
    keiner, bleibt die Datei unangetastet — das ist die Idempotenz.
    """
    material = json.dumps({
        "id": record.id,
        "type": _enum(record.memory_type),
        "content": record.content,
        "subject": record.subject,
        "source_type": _enum(record.source_type),
        "trust_level": _enum(record.trust_level),
        "sensitivity": _enum(record.sensitivity),
        "confidence": record.confidence,
        "importance": record.importance,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "valid_from": _iso(record.valid_from),
        "valid_until": _iso(record.valid_until),
        "tags": sorted(record.tags or []),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
def _body_fingerprint(body: str) -> str:
    """Der Abdruck eines Rumpfs — auf beiden Seiten gleich gebildet.

    Die Randzeilenumbrueche fallen weg. Ohne diese Normalisierung war der
    Abdruck beim Schreiben ein anderer als beim Lesen (die Datei endet auf
    einem Zeilenumbruch, der Rumpf im Speicher nicht), und JEDE Notiz galt
    beim zweiten Durchlauf als von Hand geaendert. Gemessen: `conflicted: 2`,
    wo `unchanged: 2` stehen musste.
    """
    return hashlib.sha256(body.strip("\n").encode("utf-8")).hexdigest()
def body_of(text: str) -> str:
    """Der Teil einer Notiz unterhalb des Frontmatters."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    return text[end + 4:].lstrip("\n")
def _atomic_write(path: str, text: str) -> None:
    """Schreiben oder gar nicht. Kein halb geschriebener Gedanke."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".solvio-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
def _inside(vault: str, path: str) -> bool:
    """Ob dieser Pfad wirklich im Vault liegt — nach Aufloesung aller Tricks."""
    root = os.path.realpath(vault)
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)
