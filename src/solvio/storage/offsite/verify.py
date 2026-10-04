"""Der Beweis — dass eine Generation nicht nur DA ist, sondern TRAEGT.

Zwei Aufgaben, beide misstrauisch:

**Der Restore-Beweis** (woechentlich, §12) laedt eine echte Generation vom
Anbieter zurueck, entschluesselt sie mit der Identitaet aus dem
Schluesselbund, packt sie in ein Wegwerfverzeichnis aus und rechnet nach:
Pruefsumme des Geheimtexts, `generation_id` im Objektnamen gegen die in
`offsite.json`, jede Datei gegen ihren SHA-256 im Manifest, `integrity_check`
je SQLite-Speicher, Tabellenzaehler und Schemastand gegen das Manifest. Erst
das ist ein Beweis. Ein Upload-Erfolg ist keiner: er sagt „ich habe etwas
hingelegt", nicht „ich koennte es zurueckholen".

**Der Retention-Abgleich** (§10) ist reines NACHSEHEN — SOLVIO hat kein
Loeschrecht, also kann er nur beobachten: LIST beim Anbieter gegen das Buch.
Ein Objekt, das fehlt und laut Lifecycle fehlen DARF, ist
`expired_as_planned`. Ein Objekt, das VOR seiner Zeit fehlt, ist
`missing_early` und schreit. Ein Objekt, das der Anbieter hat und das Buch
nicht kennt, ist ein Fremdobjekt (T4) und schreit ebenso.

**Die scharfe Kante, die beim Bauen gemessen wurde:** `head_object` wirft
`S3NotFound` mit der Kategorie `integrity_failed`. Fuer eine ABGELAUFENE
Generation ist das falsch — sie fehlt planmaessig. Deshalb wird jedes Fehlen
gegen das Lifecycle-Alter ihrer Klasse gehalten, bevor es beurteilt wird.
Wer das ueberspringt, meldet jeden 22. Tag einen Integritaetsverlust.

**Klartext, transient:** der Beweis legt entschluesselte Daten fuer Sekunden
in ein 0700-Wegwerfverzeichnis auf der FileVault-Platte und raeumt es
danach weg — dieselbe bewusste Grenze wie die lokale Probe (§25.7).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger
from solvio.storage.offsite import config as OC
from solvio.storage.offsite import identity as OI
from solvio.storage.offsite import ledger as OL
from solvio.storage.offsite import pack as OP
from solvio.storage.offsite import s3 as OS3

log = get_logger("offsite")

#: Der Beweis gilt so lange als frisch. §12: ab acht Tagen ist er
#: ueberfaellig — bei woechentlichem Lauf ist ein verpasster Termin ein
#: Zeichen, kein Zufall.
PROOF_FRESH_SECONDS = 8 * 24 * 3600.0

#: Wie viele Zeilen der Abgleich hoechstens meldet. Ein Bericht, der alles
#: nennt, wird beim ersten echten Vorfall unlesbar.
MAX_REPORTED = 20

KIND_RESTORE = "restore"
KIND_RETENTION = "retention"

RESULT_OK = "ok"
RESULT_FAILED = "failed"

EVENT_EXPIRED = "expired_as_planned"
EVENT_MISSING_EARLY = "missing_early"
EVENT_FOREIGN = "foreign_object"
EVENT_PRESENT = "present"


class VerifyError(RuntimeError):
    """Der Beweis ist nicht zustandegekommen. Traegt eine §18-Kategorie."""

    def __init__(self, message: str, *, category: str = "unexpected") -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ProofResult:
    """Was der Beweis ergeben hat. Zahlen und Namen, nie Inhalt."""

    ok: bool
    generation_id: str = ""
    category: str = ""
    reason: str = ""
    duration_seconds: float = 0.0
    checked_entries: int = 0
    checked_bytes: int = 0
    sqlite_checked: int = 0
    findings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["findings"] = list(self.findings)
        return data


@dataclass(frozen=True)
class RetentionResult:
    """Was der Abgleich gesehen hat. Nachsehen, nicht eingreifen."""

    ok: bool
    checked: int = 0
    expired: tuple[str, ...] = ()
    missing_early: tuple[str, ...] = ()
    foreign: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checked": self.checked,
                "expired": list(self.expired),
                "missing_early": list(self.missing_early),
                "foreign": list(self.foreign), "reason": self.reason}


# ------------------------------------------------------------------- Werkzeug
def _generation_moment(generation_id: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(generation_id, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _age_days(generation_id: str, now: dt.datetime) -> float | None:
    moment = _generation_moment(generation_id)
    if moment is None:
        return None
    return (now - moment).total_seconds() / 86400.0


def _staging_root() -> str:
    """Wegwerfverzeichnis auf der internen, FileVault-geschuetzten Platte.

    Bewusst UNTER `~/.solvio/offsite/` — derselbe Ort, den der Staging-Modus
    der Maschine akzeptiert, und derselbe, den das Aufraeumen kennt.
    """
    root = os.path.join(OC.offsite_dir(), "verify")
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _identity_or_raise(version: int) -> str:
    """Die Identitaet — mit der Unterscheidung, die zaehlt.

    Ein GESPERRTER Schluesselbund ist `auth_required`, kein Schluesselverlust.
    Wer beides zusammenfasst, erklaert eine Sperre zur Katastrophe.
    """
    try:
        line = OI.read_identity(version)
    except OI.OffsiteKeychainLocked as exc:
        raise VerifyError(f"Schluesselbund gesperrt: {exc}",
                          category="auth_failed") from None
    except OI.OffsiteIdentityError as exc:
        raise VerifyError(f"Identitaet unbrauchbar: {exc}",
                          category="key_unavailable") from None
    if not line:
        raise VerifyError("keine Offsite-Identitaet im Schluesselbund",
                          category="key_unavailable")
    return line


# ------------------------------------------------------- der Restore-Beweis
def restore_proof(*, generation_id: str = "", client: Any = None,
                  book: OL.OffsiteLedger | None = None,
                  cfg: OC.OffsiteConfig | None = None,
                  keep: bool = False) -> ProofResult:
    """Holt eine Generation zurueck und rechnet sie nach. Wirft nie.

    Ohne `generation_id` nimmt er die juengste VERIFIZIERTE Generation —
    `verified`, nicht `uploaded`: eine Generation, die nie zurueckverglichen
    wurde, ist kein Beweisgegenstand, sondern eine Behauptung.
    """
    started = time.time()
    book = book or OL.OffsiteLedger()
    try:
        cfg = cfg or OC.load()
    except OC.OffsiteConfigError as exc:
        return ProofResult(ok=False, category="unexpected",
                           reason=f"Konfiguration unlesbar: {exc}")
    if cfg is None:
        return ProofResult(ok=False, category="unexpected",
                           reason="Offsite ist nicht eingerichtet")

    generation = _pick_generation(book, generation_id)
    if generation is None:
        return ProofResult(
            ok=False, category="unexpected",
            reason=("keine verifizierte Generation im Buch — ein Beweis "
                    "braucht etwas, das er beweisen kann"))

    work = tempfile.mkdtemp(prefix=f"proof-{generation.generation_id}-",
                            dir=_staging_root())
    findings: list[str] = []
    try:
        result = _run_proof(generation, cfg, client, work, findings, started)
    except VerifyError as exc:
        result = ProofResult(ok=False, generation_id=generation.generation_id,
                             category=exc.category, reason=str(exc),
                             duration_seconds=round(time.time() - started, 2),
                             findings=tuple(findings[:MAX_REPORTED]))
    except Exception as exc:  # noqa: BLE001 - das Auffangbecken (§18)
        result = ProofResult(ok=False, generation_id=generation.generation_id,
                             category="unexpected",
                             reason=f"{type(exc).__name__}: {exc}",
                             duration_seconds=round(time.time() - started, 2),
                             findings=tuple(findings[:MAX_REPORTED]))
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)

    book.record_verification(
        generation_id=generation.generation_id, kind=KIND_RESTORE,
        result=RESULT_OK if result.ok else RESULT_FAILED,
        duration_seconds=result.duration_seconds,
        details={"entries": result.checked_entries,
                 "bytes": result.checked_bytes,
                 "sqlite": result.sqlite_checked,
                 **({"category": result.category} if not result.ok else {}),
                 **({"findings": list(result.findings)[:5]}
                    if result.findings else {})})
    log.info("offsite.restore_proof", generation=generation.generation_id,
             ok=result.ok, entries=result.checked_entries,
             seconds=result.duration_seconds)
    return result


def _pick_generation(book: OL.OffsiteLedger,
                     generation_id: str) -> OL.Generation | None:
    if generation_id:
        row = book.get(generation_id)
        return row if row is not None and row.state == OL.VERIFIED else None
    for row in book.recent(50):
        # `verified`, NICHT `succeeded`: `SUCCESS_STATES` enthaelt auch
        # `uploaded`, und eine nie zurueckverglichene Generation taugt nicht
        # als Beweisgegenstand.
        if row.state == OL.VERIFIED:
            return row
    return None


def _run_proof(generation: OL.Generation, cfg: OC.OffsiteConfig,
               client: Any, work: str, findings: list[str],
               started: float) -> ProofResult:
    identity = _identity_or_raise(cfg.recipient_version)
    client = client or OS3.S3Client(region=cfg.region)
    bucket = cfg.bucket("daily")
    entry = (generation.object_keys or {}).get("daily") or {}
    key = str(entry.get("key") or "")
    if entry.get("bucket"):
        bucket = str(entry["bucket"])
    if not key:
        raise VerifyError("die Generation nennt keinen Objektschluessel",
                          category="unexpected")

    archive = os.path.join(work, "generation.tar.zst.age")
    try:
        info = client.get_object(bucket, key, dest_path=archive)
    except OS3.S3Error as exc:
        raise VerifyError(f"Ruecklade gescheitert: {exc} ({exc.detail})",
                          category=exc.category) from None

    # 1. Der Geheimtext ist noch derselbe, den wir geschrieben haben.
    actual = OP.sha256_file(archive)
    if generation.cipher_sha256 and actual != generation.cipher_sha256:
        raise VerifyError(
            f"der Geheimtext weicht ab (erwartet "
            f"{generation.cipher_sha256[:16]}, gelesen {actual[:16]}) — "
            f"moeglicherweise wurde die Generation ueberschattet",
            category="integrity_failed")

    # 2. Die Huelle laesst sich mit der Identitaet oeffnen.
    dest = os.path.join(work, "auf")
    try:
        OP.unpack(archive, identity_line=identity, dest_dir=dest)
    except OP.PackError as exc:
        # Ein Fehler, kein Orakel: falscher Schluessel und beschaedigter
        # Geheimtext sind hier ununterscheidbar — und das bleibt so.
        raise VerifyError(f"die Huelle liess sich nicht oeffnen: {exc}",
                          category="integrity_failed") from None
    finally:
        del identity

    # 3. Der Objektname bindet an den Inhalt (§8, T4-Restfall).
    offsite_json = os.path.join(dest, "offsite.json")
    if not os.path.isfile(offsite_json):
        raise VerifyError("die Generation traegt keine offsite.json",
                          category="integrity_failed")
    with open(offsite_json, encoding="utf-8") as fh:
        inner = json.load(fh)
    inner_id = str(inner.get("generation_id") or "")
    if inner_id != generation.generation_id:
        raise VerifyError(
            f"der Objektname nennt {generation.generation_id}, der Inhalt "
            f"{inner_id or 'nichts'} — eine fremde oder umbenannte Generation",
            category="integrity_failed")

    # 4. Das Satz-Manifest ist unveraendert.
    manifest_path = os.path.join(dest, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise VerifyError("der Satz traegt kein Manifest",
                          category="integrity_failed")
    manifest_sha = OP.sha256_file(manifest_path)
    if (generation.snapshot_manifest_sha256
            and manifest_sha != generation.snapshot_manifest_sha256):
        raise VerifyError("das Manifest im Archiv ist nicht das gebuchte",
                          category="integrity_failed")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    # 5. Jede Datei gegen ihre Pruefsumme, jeder Speicher gegen sich selbst.
    checked, checked_bytes, sqlite_checked = _check_entries(
        dest, manifest, findings)

    if findings:
        raise VerifyError(
            f"{len(findings)} Abweichung(en) im Satz: {findings[0]}",
            category="restore_verification_failed")

    return ProofResult(ok=True, generation_id=generation.generation_id,
                       duration_seconds=round(time.time() - started, 2),
                       checked_entries=checked, checked_bytes=checked_bytes,
                       sqlite_checked=sqlite_checked,
                       reason=(f"{checked} Eintraege, {sqlite_checked} "
                               f"Speicher geprueft"),
                       findings=())


def _check_entries(dest: str, manifest: dict[str, Any],
                   findings: list[str]) -> tuple[int, int, int]:
    """Rechnet den entpackten Satz gegen sein Manifest nach.

    Drei Ebenen, und die dritte ist die, die ein blosses „die Datei ist da"
    von einem Beweis unterscheidet: `integrity_check` je SQLite-Speicher,
    Tabellenzaehler und Schemastand gegen das, was beim Sichern galt.
    """
    checked = checked_bytes = sqlite_checked = 0
    for item in manifest.get("entries", []):
        rel = str(item.get("dest") or "")
        if not rel:
            continue
        path = os.path.join(dest, rel)
        if not os.path.exists(path):
            findings.append(f"{item.get('name')}: fehlt im Archiv")
            continue
        if item.get("kind") == "tree":
            for member in item.get("files") or []:
                inner_path = os.path.join(path, str(member.get("rel") or ""))
                if not os.path.isfile(inner_path):
                    findings.append(f"{item.get('name')}/{member.get('rel')}: "
                                    f"fehlt")
                    continue
                if OP.sha256_file(inner_path) != member.get("sha256"):
                    findings.append(f"{item.get('name')}/{member.get('rel')}: "
                                    f"Pruefsumme weicht ab")
                checked += 1
                checked_bytes += int(member.get("bytes") or 0)
            continue

        digest = OP.sha256_file(path)
        if item.get("sha256") and digest != item["sha256"]:
            findings.append(f"{item.get('name')}: Pruefsumme weicht ab")
        checked += 1
        checked_bytes += int(item.get("bytes") or 0)

        if item.get("kind") == "sqlite":
            sqlite_checked += 1
            _check_sqlite(path, item, findings)
    return checked, checked_bytes, sqlite_checked


def _check_sqlite(path: str, item: dict[str, Any],
                  findings: list[str]) -> None:
    """Oeffnet schreibgeschuetzt und vergleicht. Ein Beweis fasst nichts an."""
    name = item.get("name")
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            findings.append(f"{name}: integrity_check meldet {integrity[:60]}")
            return
        expected_tables = item.get("tables") or {}
        for table, count in expected_tables.items():
            if int(count) < 0:
                continue          # eine FTS-Schattentabelle zaehlt nicht mit
            quoted = '"' + str(table).replace('"', '""') + '"'
            try:
                actual = int(conn.execute(
                    f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
            except sqlite3.Error:
                findings.append(f"{name}.{table}: nicht zaehlbar")
                continue
            if actual != int(count):
                findings.append(f"{name}.{table}: {actual} statt {count} Zeilen")
        if "user_version" in item:
            actual_version = int(
                conn.execute("PRAGMA user_version").fetchone()[0])
            if actual_version != int(item["user_version"] or 0):
                findings.append(
                    f"{name}: Schemastand {actual_version} statt "
                    f"{item['user_version']} — Restore in einen anderen "
                    f"Codestand")
    except sqlite3.DatabaseError as exc:
        findings.append(f"{name}: nicht lesbar ({exc})")
    finally:
        if conn is not None:
            conn.close()


# --------------------------------------------------------- Retention-Abgleich
def retention_sweep(*, client: Any = None, book: OL.OffsiteLedger | None = None,
                    cfg: OC.OffsiteConfig | None = None,
                    now: dt.datetime | None = None) -> RetentionResult:
    """LIST beim Anbieter gegen das Buch. NACHSEHEN, nie eingreifen (§10).

    SOLVIO hat kein Loeschrecht — dieser Lauf kann also nur beobachten und
    buchen. Drei Befunde: planmaessig ausgelaufen, VOR der Zeit verschwunden
    (das schreit), und ein Objekt, das der Anbieter hat und das Buch nicht
    kennt (das schreit auch).
    """
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    book = book or OL.OffsiteLedger()
    try:
        cfg = cfg or OC.load()
    except OC.OffsiteConfigError as exc:
        return RetentionResult(ok=False, reason=f"Konfiguration unlesbar: {exc}")
    if cfg is None:
        return RetentionResult(ok=False, reason="Offsite ist nicht eingerichtet")

    client = client or OS3.S3Client(region=cfg.region)
    sweep_id = now.strftime("%Y%m%dT%H%M%SZ")
    expired: list[str] = []
    missing_early: list[str] = []
    foreign: list[str] = []
    checked = 0

    for klass in ("daily", "weekly", "monthly"):
        bucket = cfg.bucket(klass)
        lifecycle_days = int(cfg.classes[klass].get("lifecycle_days") or 0)
        try:
            remote = client.list_objects(bucket,
                                         prefix=OC.GENERATIONS_PREFIX)
        except OS3.S3Error as exc:
            return RetentionResult(
                ok=False, checked=checked,
                reason=f"{klass}: LIST gescheitert ({exc.category})")
        remote_keys = {o.key for o in remote}

        booked = [g for g in book.recent(500) if klass in g.classes
                  and g.state in (OL.UPLOADED, OL.VERIFIED)]
        for generation in booked:
            checked += 1
            entry = (generation.object_keys or {}).get(klass) or {}
            key = str(entry.get("key")
                      or f"{OC.GENERATIONS_PREFIX}"
                         f"{generation.generation_id}.tar.zst.age")
            if key in remote_keys:
                remote_keys.discard(key)
                continue
            age = _age_days(generation.generation_id, now)
            if age is not None and lifecycle_days and age >= lifecycle_days:
                # Planmaessig fort: die Lifecycle-Regel des Anbieters hat
                # getan, was SOLVIO nicht darf.
                expired.append(f"{klass}:{generation.generation_id}")
                book.record_retention(generation_id=generation.generation_id,
                                      event=EVENT_EXPIRED, sweep_id=sweep_id)
            else:
                missing_early.append(f"{klass}:{generation.generation_id}")
                book.record_retention(generation_id=generation.generation_id,
                                      event=EVENT_MISSING_EARLY,
                                      sweep_id=sweep_id)
        for stray in sorted(remote_keys):
            # Ein Objekt, das das Buch nicht kennt (T4). Es wird NICHT
            # angefasst — SOLVIO kann es ohnehin nicht — aber es steht ab
            # jetzt im Buch und im Befund.
            foreign.append(f"{klass}:{stray}")
            book.record_retention(generation_id=stray, event=EVENT_FOREIGN,
                                  sweep_id=sweep_id)

    ok = not missing_early and not foreign
    reason = "sauber"
    if missing_early:
        reason = (f"{len(missing_early)} Generation(en) fehlen vor der Zeit: "
                  f"{', '.join(missing_early[:3])}")
    elif foreign:
        reason = (f"{len(foreign)} unbekannte(s) Objekt(e) beim Anbieter: "
                  f"{', '.join(foreign[:3])}")
    elif expired:
        reason = f"{len(expired)} planmaessig ausgelaufen"
    log.info("offsite.retention_sweep", checked=checked, expired=len(expired),
             missing_early=len(missing_early), foreign=len(foreign))
    return RetentionResult(ok=ok, checked=checked,
                           expired=tuple(expired[:MAX_REPORTED]),
                           missing_early=tuple(missing_early[:MAX_REPORTED]),
                           foreign=tuple(foreign[:MAX_REPORTED]),
                           reason=reason)


# ------------------------------------------------------------- Buch-Auskunft
def last_proof(book: OL.OffsiteLedger | None = None) -> dict[str, Any] | None:
    """Der juengste Restore-Beweis — bestanden ODER gescheitert.

    Bewusst BEIDE: ein gescheiterter Beweis ist die wichtigere Auskunft, und
    wer nur nach dem letzten BESTANDENEN sucht, sieht einen alten Erfolg und
    uebersieht das frische Scheitern daneben.
    """
    book = book or OL.OffsiteLedger()
    for row in book.verifications(limit=200):
        if row.get("kind") == KIND_RESTORE:
            return row
    return None


def last_passed_proof(book: OL.OffsiteLedger | None = None) -> dict[str, Any] | None:
    book = book or OL.OffsiteLedger()
    for row in book.verifications(limit=200):
        if row.get("kind") == KIND_RESTORE and row.get("result") == RESULT_OK:
            return row
    return None


def proof_age_seconds(now: float | None = None,
                      book: OL.OffsiteLedger | None = None) -> float | None:
    """Wie alt ist der letzte BESTANDENE Beweis? `None` heisst: es gibt keinen.

    `None` ist NICHT „in Ordnung" — es ist „nicht gemessen", und der
    Gesundheitsbegriff behandelt es entsprechend.
    """
    row = last_passed_proof(book)
    if row is None:
        return None
    stamp = str(row.get("at") or "")
    try:
        moment = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    reference = now if now is not None else time.time()
    return max(0.0, reference - moment.timestamp())
