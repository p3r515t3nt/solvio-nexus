"""Zurueckholen — und zwar so, dass niemand dabei etwas kaputtmacht.

Drei Wege, streng getrennt (Vertrag §13):

**A) Einzelne Speicher** — `--only memory --target <dir>`: laedt die
Generation, oeffnet sie, entpackt NUR die angefragten Eintraege in ein
Wegwerfziel und rechnet nach. Danach gelten die bestehenden Store-Wege;
fuer das Gedaechtnis ist das `memory/backup.py`, weil dort der AKTUELLE
Vergessens-Ledger angewandt wird (tombstone-bewusst, MEMORY_CONTRACT §7.1).

**B) Vollstaendiger Restore auf demselben Mac** — derselbe Weg mit allen
Eintraegen, danach der Reconcile.

**C) Katastrophe auf einem NEUEN Mac** — §14, ein Runbook fuer Menschen.
Dieses Modul traegt davon genau die Schritte, die Code sein duerfen:
`--verify` gegen einen bereits entpackten Satz (Schritt 5) und
`--finalize` (Schritt 7). Der Einstieg davor ist bewusst **kein
SOLVIO-Code**: Provider-Konsole plus stock `age` — sonst braeuchte der
Katastrophenfall genau das, was gerade verloren ging.

**Die zwei Regeln, die dieses Modul streng halten muss:**

1. **Ein Restore schreibt nie in Produktionspfade** — `assert_disposable`
   und `PRODUCTION_PATHS` der lokalen Maschine gelten unveraendert. Das
   Ziel muss ein Wegwerfverzeichnis sein, und es muss leer sein.
2. **Reconcile heisst pruefen, zaehlen, berichten — NIE fremde Zustaende
   schreiben.** Jedes betroffene Subsystem hat seine Aufraeum-Mechanik
   schon, beim jeweiligen Eigentuemer; ein Offsite-Werkzeug, das fremde
   Datenbanken beschriebe, umginge Audit-Spuren und Writer-Disziplin — beim
   Freigabespeicher die eines EINGEFRORENEN Moduls. Geschrieben wird genau
   EINE eigene Marke: `restored_from`.

**Fail-closed, und zwar an jeder Stufe.** Ein Restore ist erst erfolgreich,
wenn die GANZE Kette bewiesen ist: Ciphertext-Pruefsumme, Entschluesselung,
inneres Manifest, `generation_id`-Bindung, jede Datei gegen ihren SHA-256,
`integrity_check` je SQLite-Speicher, Tabellenzaehler, Schemastand und die
Vollstaendigkeit gegen die Inventur. Ein teilweiser Restore ist KEIN Erfolg
— er ist ein Fehlschlag mit Resten.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger
from solvio.storage import inventory
from solvio.storage.restore import (PRODUCTION_PATHS, RestoreRefused,
                                    assert_disposable, restore_tree_file_mode)
from solvio.storage.offsite import config as OC
from solvio.storage.offsite import identity as OI
from solvio.storage.offsite import ledger as OL
from solvio.storage.offsite import pack as OP
from solvio.storage.offsite import s3 as OS3
from solvio.storage.offsite import verify as OV

log = get_logger("offsite")

#: Die Fassung des Manifests, die dieses Werkzeug versteht. Ein Satz aus
#: einer NEUEREN Fassung wird nicht „so gut es geht" gelesen — er wird
#: abgelehnt. Ein Restore, der ein Format raet, restauriert Vermutungen.
SUPPORTED_MANIFEST_VERSION = 1

#: Dieselbe Zahl fuer die Offsite-Beilage.
SUPPORTED_OFFSITE_FORMAT = 1

#: Die Marke, die ein `--finalize` setzt. Das EINZIGE, was dieses Modul in
#: einen wiederhergestellten Baum schreibt.
MARKER_NAME = "restored_from.json"


class RestoreError(RuntimeError):
    """Der Restore ist nicht zustandegekommen. Traegt eine §18-Kategorie."""

    def __init__(self, message: str, *, category: str = "unexpected") -> None:
        super().__init__(message)
        self.category = category


@dataclass
class OffsiteRestoreReport:
    """Was wirklich geschah. Zahlen und Namen, nie Inhalt, nie ein Geheimnis."""

    ok: bool
    generation_id: str = ""
    target: str = ""
    category: str = ""
    reason: str = ""
    duration_seconds: float = 0.0
    restored_entries: int = 0
    restored_bytes: int = 0
    sqlite_checked: int = 0
    only: tuple[str, ...] = ()
    findings: list[str] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["only"] = list(self.only)
        return data


# ------------------------------------------------------------------- Auswahl
def pick_generation(book: OL.OffsiteLedger, generation_id: str = "",
                    ) -> OL.Generation:
    """Waehlt EINE Generation — eindeutig, oder gar nicht.

    Ohne Angabe die juengste VERIFIZIERTE. `verified`, nicht `uploaded`:
    eine nie zurueckverglichene Generation ist eine Behauptung, und aus
    einer Behauptung stellt man nichts wieder her.
    """
    if generation_id:
        row = book.get(generation_id)
        if row is None:
            raise RestoreError(f"unbekannte Generation: {generation_id}",
                               category="unexpected")
        if row.state != OL.VERIFIED:
            raise RestoreError(
                f"Generation {generation_id} steht auf {row.state!r} — "
                f"nur eine verifizierte Generation ist eine Quelle",
                category="integrity_failed")
        return row
    for row in book.recent(50):
        if row.state == OL.VERIFIED:
            return row
    raise RestoreError("keine verifizierte Generation im Buch",
                       category="unexpected")


# ---------------------------------------------------------------- Herunterladen
def fetch_generation(generation: OL.Generation, *, dest_path: str,
                     cfg: OC.OffsiteConfig, client: Any = None) -> str:
    """Holt das Objekt und BINDET es an die Buchhaltung.

    Gebunden wird an drei Dinge, und alle drei muessen stimmen: Bucket und
    Objektschluessel aus dem Buch, und die Ciphertext-Pruefsumme. Wer nur
    den Namen prueft, laedt, was unter dem Namen liegt — nicht, was dort
    liegen soll.
    """
    entry = (generation.object_keys or {}).get("daily") or {}
    bucket = str(entry.get("bucket") or cfg.bucket("daily"))
    key = str(entry.get("key")
              or f"{OC.GENERATIONS_PREFIX}{generation.generation_id}.tar.zst.age")
    client = client or OS3.S3Client(region=cfg.region)
    try:
        client.get_object(bucket, key, dest_path=dest_path)
    except OS3.S3Error as exc:
        raise RestoreError(f"Ruecklade gescheitert: {exc} ({exc.detail})",
                           category=exc.category) from None
    actual = OP.sha256_file(dest_path)
    if generation.cipher_sha256 and actual != generation.cipher_sha256:
        raise RestoreError(
            f"der Geheimtext weicht ab (erwartet "
            f"{generation.cipher_sha256[:16]}, gelesen {actual[:16]})",
            category="integrity_failed")
    return key


def open_generation(archive: str, *, dest_dir: str, identity_line: str = "",
                    cfg: OC.OffsiteConfig | None = None) -> str:
    """Oeffnet die Huelle in ein LEERES Wegwerfziel.

    Ohne `identity_line` kommt die Identitaet aus dem Schluesselbund. Der
    Katastrophenfall gibt sie ausdruecklich MIT — dort gibt es keinen
    Schluesselbund, und genau deshalb darf dieses Modul ihn nicht
    voraussetzen (Zirkelfreiheit, §5).
    """
    if not identity_line:
        version = cfg.recipient_version if cfg else 1
        try:
            identity_line = OI.read_identity(version) or ""
        except OI.OffsiteKeychainLocked as exc:
            raise RestoreError(f"Schluesselbund gesperrt: {exc}",
                               category="auth_failed") from None
        except OI.OffsiteIdentityError as exc:
            raise RestoreError(f"Identitaet unbrauchbar: {exc}",
                               category="key_unavailable") from None
        if not identity_line:
            raise RestoreError(
                "keine Identitaet im Schluesselbund — im Katastrophenfall "
                "kommt sie aus dem Umschlag, siehe docs/runbooks/OFFSITE.md",
                category="key_unavailable")
    try:
        OP.unpack(archive, identity_line=identity_line, dest_dir=dest_dir)
    except OP.PackError as exc:
        raise RestoreError(f"die Huelle liess sich nicht oeffnen: {exc}",
                           category="integrity_failed") from None
    finally:
        del identity_line
    return dest_dir


# ------------------------------------------------------------------ Pruefen
def verify_unpacked(set_dir: str, *, expect_generation: str = "",
                    require_complete: bool = True) -> OffsiteRestoreReport:
    """Prueft einen bereits ENTPACKTEN Satz — §14 Schritt 5.

    Das ist der Schritt, den ein Mensch im Katastrophenfall fahren kann,
    nachdem er mit stock `age` entpackt hat: jede SHA-256, jede Datenbank,
    Tabellen- und Schemastand gegen das Manifest, `generation_id`-Bindung.
    Es braucht dafuer weder Netz noch Schluesselbund noch Tresor.
    """
    started = time.time()
    findings: list[str] = []

    manifest_path = os.path.join(set_dir, "manifest.json")
    offsite_path = os.path.join(set_dir, "offsite.json")
    if not os.path.isfile(manifest_path):
        return OffsiteRestoreReport(
            ok=False, target=set_dir, category="integrity_failed",
            reason="der Satz traegt kein Manifest")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    version = int(manifest.get("format_version") or 0)
    if version != SUPPORTED_MANIFEST_VERSION:
        # Fail-closed nach BEIDEN Seiten: ein neueres Format kann dieses
        # Werkzeug nicht kennen, ein aelteres nicht mehr garantieren.
        return OffsiteRestoreReport(
            ok=False, target=set_dir, category="integrity_failed",
            reason=(f"Manifest-Fassung {version} — dieses Werkzeug versteht "
                    f"{SUPPORTED_MANIFEST_VERSION}. Ein Restore, der ein "
                    f"Format raet, restauriert Vermutungen."))

    generation_id = ""
    if os.path.isfile(offsite_path):
        with open(offsite_path, encoding="utf-8") as fh:
            inner = json.load(fh)
        offsite_format = int(inner.get("offsite_format") or 0)
        if offsite_format != SUPPORTED_OFFSITE_FORMAT:
            return OffsiteRestoreReport(
                ok=False, target=set_dir, category="integrity_failed",
                reason=(f"offsite.json-Fassung {offsite_format} — dieses "
                        f"Werkzeug versteht {SUPPORTED_OFFSITE_FORMAT}"))
        generation_id = str(inner.get("generation_id") or "")
        if expect_generation and generation_id != expect_generation:
            return OffsiteRestoreReport(
                ok=False, target=set_dir, generation_id=generation_id,
                category="integrity_failed",
                reason=(f"der Objektname nennt {expect_generation}, der "
                        f"Inhalt {generation_id or 'nichts'} — eine fremde "
                        f"oder umbenannte Generation"))
    elif expect_generation:
        return OffsiteRestoreReport(
            ok=False, target=set_dir, category="integrity_failed",
            reason="die Generation traegt keine offsite.json")

    checked, checked_bytes, sqlite_checked = OV._check_entries(
        set_dir, manifest, findings)

    if require_complete:
        findings.extend(_missing_required(manifest))

    duration = round(time.time() - started, 2)
    if findings:
        return OffsiteRestoreReport(
            ok=False, generation_id=generation_id, target=set_dir,
            category="restore_verification_failed",
            reason=f"{len(findings)} Abweichung(en): {findings[0]}",
            duration_seconds=duration, restored_entries=checked,
            restored_bytes=checked_bytes, sqlite_checked=sqlite_checked,
            findings=findings[:20])
    return OffsiteRestoreReport(
        ok=True, generation_id=generation_id, target=set_dir,
        reason=(f"{checked} Eintraege, {sqlite_checked} Speicher geprueft"),
        duration_seconds=duration, restored_entries=checked,
        restored_bytes=checked_bytes, sqlite_checked=sqlite_checked)


def _missing_required(manifest: dict[str, Any]) -> list[str]:
    """Vollstaendigkeit gegen die INVENTUR, nicht gegen sich selbst.

    Ein Manifest, das nur seine eigenen Eintraege auflistet, ist immer
    vollstaendig — das ist die bequemste Art, eine Luecke zu uebersehen.
    Geprueft wird deshalb gegen `inventory.items()`: jedes Pflichtstueck
    (`required=True`) MUSS im Satz stehen. Fehlt eines, war der Satz beim
    Sichern schon unvollstaendig, und das darf ein Restore nicht glaetten.
    """
    present = {str(e.get("name")) for e in manifest.get("entries", [])}
    skipped = {str(s.get("name")): str(s.get("reason") or "")
               for s in manifest.get("skipped", [])}
    missing: list[str] = []
    for item in inventory.items():
        if not item.required:
            continue
        if item.name in present:
            continue
        reason = skipped.get(item.name, "fehlt ohne Begruendung")
        missing.append(f"Pflichtstueck {item.name} fehlt im Satz ({reason})")
    return missing


# ---------------------------------------------------------------- Restore A/B
def restore(*, generation_id: str = "", target: str, only: tuple[str, ...] = (),
            client: Any = None, book: OL.OffsiteLedger | None = None,
            cfg: OC.OffsiteConfig | None = None,
            identity_line: str = "",
            keep_archive: bool = False) -> OffsiteRestoreReport:
    """Der Weg A/B: holen, oeffnen, pruefen, in ein LEERES Wegwerfziel legen.

    `only` waehlt Eintraege nach Namen (Inventur-Namen, z. B. `memory`) —
    dann wird die Vollstaendigkeit NICHT verlangt, weil ausdruecklich nur
    ein Teil gewollt ist. Ohne `only` gilt: unvollstaendig ist gescheitert.
    """
    started = time.time()
    book = book or OL.OffsiteLedger()
    try:
        cfg = cfg or OC.load()
    except OC.OffsiteConfigError as exc:
        return OffsiteRestoreReport(ok=False, target=target,
                                    category="unexpected",
                                    reason=f"Konfiguration unlesbar: {exc}")
    if cfg is None:
        return OffsiteRestoreReport(ok=False, target=target,
                                    category="unexpected",
                                    reason="Offsite ist nicht eingerichtet")

    # Die Sperre ZUERST: bevor irgendetwas geladen wird, muss feststehen,
    # dass das Ziel nichts Produktives beruehrt.
    try:
        safe_target = assert_disposable(target)
    except RestoreRefused as exc:
        return OffsiteRestoreReport(ok=False, target=target,
                                    category="unexpected", reason=str(exc))
    os.makedirs(safe_target, mode=0o700, exist_ok=True)
    if os.listdir(safe_target):
        return OffsiteRestoreReport(
            ok=False, target=target, category="unexpected",
            reason=(f"das Ziel ist nicht leer: {target} — ein Restore in "
                    f"einen belegten Baum mischt zwei Wahrheiten"))

    try:
        generation = pick_generation(book, generation_id)
    except RestoreError as exc:
        return OffsiteRestoreReport(ok=False, target=target,
                                    category=exc.category, reason=str(exc))

    work = os.path.join(safe_target, ".incoming")
    os.makedirs(work, mode=0o700, exist_ok=True)
    archive = os.path.join(work, "generation.tar.zst.age")
    unpacked = os.path.join(work, "satz")
    try:
        key = fetch_generation(generation, dest_path=archive, cfg=cfg,
                               client=client)
        open_generation(archive, dest_dir=unpacked,
                        identity_line=identity_line, cfg=cfg)
        report = verify_unpacked(unpacked,
                                 expect_generation=generation.generation_id,
                                 require_complete=not only)
        if not report.ok:
            report.target = target
            report.duration_seconds = round(time.time() - started, 2)
            _record(book, generation, report)
            return report

        moved, moved_bytes = _place(unpacked, safe_target, only)
        if only:
            wanted = set(only)
            got = {name for name, _ in moved}
            fehlend = sorted(wanted - got)
            if fehlend:
                report.ok = False
                report.category = "restore_verification_failed"
                report.reason = (f"angefragt, aber nicht im Satz: "
                                 f"{', '.join(fehlend)}")
                report.findings = [report.reason]
                _record(book, generation, report)
                return report
    except RestoreError as exc:
        report = OffsiteRestoreReport(
            ok=False, generation_id=generation.generation_id, target=target,
            category=exc.category, reason=str(exc),
            duration_seconds=round(time.time() - started, 2))
        _record(book, generation, report)
        return report
    except Exception as exc:  # noqa: BLE001 - das Auffangbecken (§18)
        report = OffsiteRestoreReport(
            ok=False, generation_id=generation.generation_id, target=target,
            category="unexpected", reason=f"{type(exc).__name__}: {exc}",
            duration_seconds=round(time.time() - started, 2))
        _record(book, generation, report)
        return report
    finally:
        if not keep_archive:
            shutil.rmtree(work, ignore_errors=True)

    report.target = target
    report.only = tuple(only)
    report.duration_seconds = round(time.time() - started, 2)
    report.source = {"bucket": (generation.object_keys.get("daily") or {}).get(
        "bucket", ""), "key": key,
        "version_id": (generation.object_keys.get("daily") or {}).get(
            "version_id", "")}
    report.restored_entries = len(moved)
    report.restored_bytes = moved_bytes
    _record(book, generation, report)
    log.info("offsite.restored", generation=generation.generation_id,
             entries=report.restored_entries, target=target, ok=report.ok)
    return report


def _place(unpacked: str, target: str, only: tuple[str, ...]
           ) -> tuple[list[tuple[str, str]], int]:
    """Legt die geprueften Eintraege ins Ziel. Nur Namen aus dem Manifest."""
    with open(os.path.join(unpacked, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    wanted = set(only)
    moved: list[tuple[str, str]] = []
    total = 0
    for entry in manifest.get("entries", []):
        name = str(entry.get("name") or "")
        if wanted and name not in wanted:
            continue
        rel = str(entry.get("dest") or "")
        source = os.path.join(unpacked, rel)
        if not os.path.exists(source):
            continue
        destination = _safe_join(target, rel)
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        if os.path.isdir(source):
            shutil.copytree(source, destination, dirs_exist_ok=False)
            for item in entry.get("files") or []:
                path = _safe_join(destination, str(item.get("rel") or ""))
                if item.get("sha256") and OP.sha256_file(path) != item["sha256"]:
                    raise RestoreError("Datei bei der Ablage veraendert",
                                       category="integrity_failed")
                try:
                    restore_tree_file_mode(path, item)
                except RestoreRefused as exc:
                    raise RestoreError(str(exc), category="integrity_failed") from None
        else:
            shutil.copy2(source, destination)
        moved.append((name, rel))
        total += int(entry.get("bytes") or 0)
    # Das Manifest und die Offsite-Beilage reisen mit — ein Satz ohne sie
    # ist nicht mehr pruefbar.
    for extra in ("manifest.json", "offsite.json"):
        source = os.path.join(unpacked, extra)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(target, extra))
    return moved, total


def _safe_join(base: str, rel: str) -> str:
    """Verbindet und beweist, dass das Ergebnis unter `base` bleibt.

    Ein Manifest ist Information, nie Autoritaet — und schon gar keine
    Autoritaet ueber Schreibpfade. Dieselbe Regel wie lokal.
    """
    if not rel or rel.startswith("/") or rel.startswith("~"):
        raise RestoreError(f"unsicherer Pfad im Manifest: {rel!r}",
                           category="integrity_failed")
    joined = os.path.realpath(os.path.join(base, rel))
    root = os.path.realpath(base)
    if joined != root and not joined.startswith(root + os.sep):
        raise RestoreError(f"Pfad zeigt aus dem Ziel heraus: {rel!r}",
                           category="integrity_failed")
    return joined


def _record(book: OL.OffsiteLedger, generation: OL.Generation,
            report: OffsiteRestoreReport) -> None:
    """Jeder Restore-Versuch geht ins Buch — der gescheiterte zuerst.

    B4 hat die Verifikationswahrheit gebaut; ein Restore, der daran
    vorbeiliefe, erzeugte eine zweite. Ein Fehlschlag bleibt damit
    sichtbar und wird nicht von einem aelteren Erfolg ueberdeckt.
    """
    try:
        book.record_verification(
            generation_id=generation.generation_id, kind="restore_run",
            result=OV.RESULT_OK if report.ok else OV.RESULT_FAILED,
            duration_seconds=report.duration_seconds,
            details={"entries": report.restored_entries,
                     "only": list(report.only),
                     **({"category": report.category} if not report.ok else {})})
    except OL.LedgerError:
        pass


# -------------------------------------------------------------- Reconcile
def finalize(target: str, *, generation_id: str = "",
             book: OL.OffsiteLedger | None = None) -> dict[str, Any]:
    """Der Pflichtschritt nach einem vollstaendigen Restore (§13).

    **Pruefen, zaehlen, berichten — NIE fremde Zustaende schreiben.** Die
    einzige Datei, die dieses Werkzeug anlegt, ist die eigene Marke
    `restored_from.json`. Alle fremden Datenbanken werden ausschliesslich
    SCHREIBGESCHUETZT geoeffnet; das Aufraeumen tun ihre Eigentuemer beim
    ersten Start, mit ihren eigenen Audit-Spuren.

    Was gezaehlt wird und warum:

    * **Freigaben** — offene Zeilen. Expiriert wird NICHT von aussen: der
      eingefrorene Store tut das selbst (`_expire_due()` bei jedem
      `list_pending`, transaktional, MIT Audit-Eintrag). Die `request_ttl`
      ist 600 s; jede wiederhergestellte Zeile ist beim Restore laengst
      ueberfaellig.
    * **Zahlung** — aktive Instrumente und offene Absichten. Die
      Revalidierung ist eine EIGENTUEMER-Handlung ueber
      `scripts/payment_admin.py`; dieser Bericht sagt das woertlich.
    * **Agentenlaufzeit** — offene Laeufe. Der erste Core-Start faehrt
      `orchestrator.reconcile()`, der `WAITING_USER` bewusst ausnimmt.
    """
    book = book or OL.OffsiteLedger()
    root = os.path.abspath(os.path.expanduser(target))
    if not os.path.isdir(root):
        raise RestoreError(f"kein Restore-Ziel: {target}",
                           category="unexpected")

    if not generation_id:
        offsite_path = os.path.join(root, "offsite.json")
        if os.path.isfile(offsite_path):
            with open(offsite_path, encoding="utf-8") as fh:
                generation_id = str(json.load(fh).get("generation_id") or "")

    counts = {
        "approvals": _count_approvals(root),
        "payment": _count_payment(root),
        "agent_runs": _count_agent_runs(root),
    }
    notes = [
        "Freigaben werden NICHT von hier expiriert — der eingefrorene "
        "Freigabespeicher tut das beim ersten Core-Start selbst, "
        "transaktional und mit Audit-Eintrag.",
        "Die Revalidierung der Zahlungsmittel ist eine Eigentuemer-Handlung "
        "ueber scripts/payment_admin.py (put_instrument). Sie steht an.",
        "Offene Agentenlaeufe markiert orchestrator.reconcile() beim ersten "
        "Start als INTERRUPTED; WAITING_USER bleibt bewusst ausgenommen.",
        "Offene Broker-Leases sind mit dem alten Core gestorben; der Broker "
        "praegt beim ersten Start neu.",
        "Im Katastrophenfall gilt der Vergessens-Ledger DIESER Generation. "
        "Purges danach sind physisch verloren (MEMORY_CONTRACT §7.1).",
    ]
    marker = {
        "restored_from": generation_id,
        "finalized_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "counts": counts,
        "notes": notes,
    }
    path = os.path.join(root, MARKER_NAME)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(marker, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    if generation_id:
        try:
            book.record_verification(
                generation_id=generation_id, kind="reconcile",
                result=OV.RESULT_OK, details=counts)
        except OL.LedgerError:
            pass
    log.info("offsite.finalized", generation=generation_id, **{
        k: v.get("open", -1) if isinstance(v, dict) else -1
        for k, v in counts.items()})
    return marker


def _readonly(path: str):
    """Oeffnet eine fremde Datenbank SCHREIBGESCHUETZT — oder gar nicht."""
    import sqlite3
    if not os.path.isfile(path):
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.DatabaseError:
        return None


def _group(conn, table: str, column: str) -> dict[str, int] | None:
    """Zaehlt eine fremde Tabelle nach einer Spalte. `None`, wenn nicht da.

    Die Namen sind an einem ECHTEN wiederhergestellten Satz abgelesen, nicht
    geraten (2026-09-01): `approval_requests.state`, `intents.state`,
    `instruments.status`, `agent_runs.state`. Ein Reconcile, der falsche
    Namen raet, meldet ueberall Null und sieht dabei gruen aus — genau die
    Sorte Stille, gegen die dieses Projekt baut.
    """
    try:
        rows = conn.execute(
            f'SELECT "{column}", COUNT(*) FROM "{table}" GROUP BY 1'
        ).fetchall()
    except Exception:  # noqa: BLE001 - ein fremdes Schema ist kein Absturz
        return None
    return {str(r[0]): int(r[1]) for r in rows}


def _count_approvals(root: str) -> dict[str, Any]:
    """Offene Freigabezeilen — gezaehlt, nie angefasst.

    `PENDING` und `APPROVED` sind die unverbrauchten Zustaende; expiriert
    werden sie beim ersten Core-Start vom eingefrorenen Store selbst.
    """
    path = os.path.join(root, "Approval", "approval_control.sqlite3")
    conn = _readonly(path)
    if conn is None:
        return {"present": False}
    try:
        by_state = _group(conn, "approval_requests", "state")
    finally:
        conn.close()
    if by_state is None:
        return {"present": True, "readable": False}
    open_rows = sum(v for k, v in by_state.items()
                    if k.upper() in ("PENDING", "APPROVED"))
    return {"present": True, "readable": True, "by_state": by_state,
            "open": open_rows}


def _count_payment(root: str) -> dict[str, Any]:
    """Aktive Instrumente und offene Absichten — und der Satz, der ansteht.

    Die Revalidierung ist eine Eigentuemer-Handlung (§13). Dieser Bericht
    sagt das woertlich, statt sie stillschweigend zu unterlassen.
    """
    path = os.path.join(root, "RuntimeState", "payments.sqlite3")
    conn = _readonly(path)
    if conn is None:
        return {"present": False}
    try:
        instruments = _group(conn, "instruments", "status")
        intents = _group(conn, "intents", "state")
    finally:
        conn.close()
    if instruments is None and intents is None:
        return {"present": True, "readable": False}
    active = sum(v for k, v in (instruments or {}).items()
                 if k.lower() == "active")
    # Was noch nicht abgeschlossen ist. Ein `succeeded` ist fertig, ein
    # `ready_for_approval` wartet — und wartet nach einem Restore auf
    # einen Menschen, nicht auf eine Wiederholung.
    terminal = {"succeeded", "failed", "cancelled", "expired", "denied"}
    open_intents = sum(v for k, v in (intents or {}).items()
                       if k.lower() not in terminal)
    return {"present": True, "readable": True,
            "instruments_by_status": instruments or {},
            "instruments_active": active,
            "intents_by_state": intents or {},
            "open": open_intents,
            "owner_action": ("Instrumente revalidieren ueber "
                             "scripts/payment_admin.py — Eigentuemer-Handlung, "
                             "kein automatischer Schritt")}


def _count_agent_runs(root: str) -> dict[str, Any]:
    """Offene Laeufe — gezaehlt. Markiert werden sie vom Orchestrator."""
    path = os.path.join(root, "RuntimeState", "agent_runs.sqlite3")
    conn = _readonly(path)
    if conn is None:
        return {"present": False}
    try:
        by_state = _group(conn, "agent_runs", "state")
    finally:
        conn.close()
    if by_state is None:
        return {"present": True, "readable": False}
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED", "EXPIRED"}
    open_rows = sum(v for k, v in by_state.items()
                    if k.upper() not in terminal)
    return {"present": True, "readable": True, "by_state": by_state,
            "open": open_rows}


# ------------------------------------------------------------------ Einstieg
def main(argv: list[str] | None = None) -> int:
    """Der Weg fuer Menschen. Drei Betriebsarten, klar getrennt.

    `--verify` braucht ausdruecklich WEDER Netz NOCH Schluesselbund NOCH
    Tresor: es prueft einen bereits entpackten Satz. Genau das ist Schritt 5
    des Katastrophenfalls, und es muss auf einem frischen Mac laufen, auf
    dem es SOLVIO noch gar nicht gibt — ausser als ausgepacktem
    `Repos/core.bundle`.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="solvio-offsite-restore",
        description="Holt eine Offsite-Generation zurueck — in ein LEERES "
                    "Wegwerfziel, nie in einen Produktionspfad.")
    parser.add_argument("--target", required=True,
                        help="Zielverzeichnis (muss leer und wegwerfbar sein)")
    parser.add_argument("--generation", default="",
                        help="generation_id; ohne Angabe die juengste "
                             "verifizierte")
    parser.add_argument("--only", action="append", default=[],
                        help="nur diese Eintraege (Inventur-Namen, z. B. "
                             "memory); mehrfach erlaubt")
    parser.add_argument("--verify", action="store_true",
                        help="NUR pruefen: ein bereits entpackter Satz unter "
                             "--target, ohne Netz und ohne Schluesselbund")
    parser.add_argument("--finalize", action="store_true",
                        help="Reconcile: zaehlen und berichten, Marke setzen "
                             "— schreibt NIE in fremde Datenbanken")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.verify and args.finalize:
        print("--verify und --finalize sind zwei verschiedene Schritte.")
        return 2

    if args.verify:
        report = verify_unpacked(args.target,
                                 require_complete=not args.only)
        payload = report.as_dict()
        ok = report.ok
    elif args.finalize:
        payload = finalize(args.target, generation_id=args.generation)
        ok = True
    else:
        report = restore(generation_id=args.generation, target=args.target,
                         only=tuple(args.only))
        payload = report.as_dict()
        ok = report.ok

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif args.finalize:
        print(f"Marke gesetzt: restored_from={payload['restored_from']}")
        for name, counts in payload["counts"].items():
            offen = counts.get("open")
            print(f"  {name}: {'nicht vorhanden' if not counts.get('present') else f'{offen} offen'}")
        for note in payload["notes"]:
            print(f"  · {note}")
    else:
        print(f"{'ok' if ok else 'FEHLER'}: {payload.get('reason')}")
        for finding in (payload.get("findings") or [])[:10]:
            print(f"  · {finding}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
