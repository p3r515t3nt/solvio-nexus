"""Der Offsite-Lauf — er berichtet, er plant nicht (ADR-0023).

Ein Lauf ist eine Kette, und jedes Glied kann ehrlich scheitern:

```
Gates → Staging-Satz → Huelle (age) → PUT (daily) → Ruecklade-Vergleich
      → COPY (weekly/monthly, wenn Erstling) → Buch → Meldung
```

**Die Gates** (§11), in dieser Reihenfolge und alle still, wo Stille richtig
ist: nicht eingeschaltet → Exit 0; flock schon vergeben → Exit 0 (der
Verlierer einer Kollision ist kein Fehler); vor 07:00 Ortszeit → Exit 0; fuer
den heutigen UTC-KALENDERTAG existiert schon eine gute Generation → Exit 0.
Die Verankerung ist kalendertaeglich und ausdruecklich NICHT „verstrichene
Zeit" wie beim lokalen Job — sonst wandert die Laufzeit rueckwaerts und die
Zusage „1 je UTC-Kalendertag" bricht.

**Kein falsches Gruen** (§18). Ein Upload allein ist kein Erfolg: erst der
Ruecklade-Vergleich (`GET` + SHA-256 gegen das, was wir geschrieben haben)
macht aus `uploaded` ein `verified`. Ein Prozess, der mittendrin stirbt,
hinterlaesst `uploading` im Buch — und `uploading` ist beim naechsten Start
eine offene Frage, kein geerbter Erfolg. Ein `ok=False` des Quellsatzes wird
**durchgereicht**, nicht verschwiegen: eine Offsite-Kopie eines kranken
Satzes ist besser als nichts, aber sie heisst nicht gesund.

**Was dieser Job NICHT tut:** loeschen (es gibt kein Recht und keine
Methode), Retention setzen (der Bucket-Default tut das), planen (launchd
plant), oder ein Modell fragen. Es gibt kein `offsite_*` im
Faehigkeitsvertrag — kein Modell kann diesen Lauf ausloesen, lesen oder
anhalten; es erfaehrt davon wie der Mensch: aus dem Posteingang.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from typing import Any

from solvio.logging_setup import get_logger
from solvio.storage import engine
from solvio.storage.offsite import config as OC
from solvio.storage.offsite import identity as OI
from solvio.storage.offsite import ledger as OL
from solvio.storage.offsite import pack as OP
from solvio.storage.offsite import provenance as OPV
from solvio.storage.offsite import s3 as OS3

log = get_logger("offsite")

#: Frueheste Ortszeit fuer einen Versuch (§11). Der Stundentick davor ist
#: still — nicht gescheitert.
EARLIEST_LOCAL_HOUR = 7

#: Ruhefenster je Lage. Dieselbe Zahl wie lokal, aus demselben Grund:
#: dieselbe Lage soll morgen wieder gemeldet werden koennen, heute aber
#: nicht viermal.
QUIET_SECONDS = 24 * 3600.0

#: Reserve auf der internen Platte, unter die kein Staging geschrieben wird.
MIN_FREE_BYTES = 5 * 1024 ** 3

GENERATIONS_PREFIX = OC.GENERATIONS_PREFIX


class OffsiteLocked(RuntimeError):
    """Es laeuft schon einer. Kein Fehler, ein Zustand."""


# ------------------------------------------------------------------ Betriebszeug
def state_path() -> str:
    return os.path.join(OC.offsite_dir(), "state.json")


def lock_path() -> str:
    return os.path.join(OC.offsite_dir(), "job.lock")


def log_path() -> str:
    return os.path.join(OC.offsite_dir(), "offsite.log")


LOG_MAX_BYTES = 1 << 20
LOG_KEEP = 3


def _log(message: str) -> None:
    """Eine Betriebszeile. Zahlen und Namen, nie Inhalt, nie ein Geheimnis."""
    path = log_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            for index in range(LOG_KEEP - 1, 0, -1):
                older, newer = f"{path}.{index}", f"{path}.{index + 1}"
                if os.path.exists(older):
                    os.replace(older, newer)
            os.replace(path, path + ".1")
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{OL.utcnow_iso()} {message}\n")


def load_state() -> dict[str, Any]:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _open_lock():
    """Ein ECHTER flock — von Anfang an lebendig, nicht wie DEBT-0160."""
    path = lock_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise OffsiteLocked("es laeuft bereits ein Offsite-Lauf")
    return handle


# ------------------------------------------------------------------- Meldungen
async def notify(summary: str, findings: list[str], *, kind: str,
                 now: float | None = None, store: Any = None) -> bool:
    """Legt eine Meldung in den BESTEHENDEN proaktiven Eingang.

    Kein zweiter Posteingang (ADR-0023). Zwei Fallen, beide vom lokalen Job
    geerbt und beide teuer bezahlt: `task_id` ist der LEERSTRING und nicht
    `None` (SQLite haelt NULL in einer UNIQUE-Bedingung fuer verschieden —
    die Entdopplung greift sonst nie), und das Ruhefenster steckt IM
    Fingerabdruck.
    """
    now = now if now is not None else time.time()
    if store is None:
        from solvio.proactive.store import ProactiveStore
        store = ProactiveStore()
    window = int(now // QUIET_SECONDS)
    item = {
        "notification_id": "off-" + hashlib.sha256(
            f"{kind}|{window}".encode()).hexdigest()[:20],
        "task_id": "", "run_id": None, "created_at": now,
        "priority": "wichtig", "summary": summary[:1200], "findings": findings,
        "source_capability": "storage", "content_trust": "",
        "fingerprint": f"offsite:{kind}:{window}",
    }
    return bool(await store.add_item(item))


#: Klartext je Fehlerkategorie (§18). Was hier fehlt, bekommt den
#: Auffangtext — ortlos ist kein Fehler.
_MESSAGES = {
    "staging_failed": "Die Offsite-Sicherung konnte den Satz nicht anlegen.",
    "encryption_failed": "Die Offsite-Sicherung konnte nicht verschluesseln — "
                         "es hat NICHTS das Haus verlassen.",
    "auth_failed": "Die Offsite-Sicherung kommt nicht an ihren Zugang. "
                   "Ist der Schluesselbund gesperrt?",
    "clock_skew": "Die Uhr dieses Rechners weicht zu stark ab — die "
                  "Offsite-Sicherung wird abgewiesen. Das repariert die "
                  "Zeitsynchronisierung, NICHT ein neuer Zugang.",
    "remote_unavailable": "Der Offsite-Speicher ist gerade nicht erreichbar.",
    "quota_exceeded": "Beim Offsite-Speicher ist das Kontingent erschoepft — "
                      "das wird von selbst nicht besser.",
    "integrity_failed": "Eine Offsite-Generation stimmt nicht mit dem "
                        "ueberein, was ich hochgeladen habe.",
    "key_unavailable": "Die Offsite-Identitaet ist nicht verfuegbar.",
    "unexpected": "Die Offsite-Sicherung ist an einer Stelle gescheitert, "
                  "die ich nicht eingeordnet bekomme.",
}


async def maybe_notify(category: str, reason: str, *,
                       extra: list[str] | None = None,
                       now: float | None = None, store: Any = None) -> bool:
    if not category:
        return False
    template = _MESSAGES.get(category, "Die Offsite-Sicherung braucht dich.")
    return await notify(f"{template} ({reason})", (extra or []) + [reason],
                        kind=category, now=now, store=store)


# ------------------------------------------------------------------ GFS-Klassen
def generation_id(now: dt.datetime | None = None) -> str:
    """`%Y%m%dT%H%M%SZ` in UTC (§4) — die lokale/UTC-Doppeldeutigkeit der
    lokalen `backup_id` wird bewusst nicht wiederholt."""
    moment = now or dt.datetime.now(dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def classes_for(moment: dt.datetime, book: OL.OffsiteLedger) -> tuple[str, ...]:
    """Welche Klassen bekommt diese Generation (§9)?

    Jede Generation ist `daily`. Ist sie die ERSTE gute ihrer ISO-Woche bzw.
    ihres Monats, kommt `weekly` bzw. `monthly` dazu — und wird spaeter per
    CopyObject in den jeweiligen Bucket kopiert, nicht ein zweites Mal
    hochgeladen.
    """
    moment = moment.astimezone(dt.timezone.utc)
    classes = ["daily"]

    monday = moment - dt.timedelta(days=moment.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if not book.has_success_in(since=_stamp(week_start),
                               until=_stamp(week_start + dt.timedelta(days=7))):
        classes.append("weekly")

    month_start = moment.replace(day=1, hour=0, minute=0, second=0,
                                 microsecond=0)
    next_month = (month_start + dt.timedelta(days=32)).replace(day=1)
    if not book.has_success_in(since=_stamp(month_start),
                               until=_stamp(next_month)):
        classes.append("monthly")
    return tuple(classes)


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def object_key(gen_id: str) -> str:
    return f"{GENERATIONS_PREFIX}{gen_id}.tar.zst.age"


# ------------------------------------------------------------------- der Lauf
def _recipient_fingerprint(recipient: str) -> str:
    """Kurzer, stabiler Fingerabdruck des OEFFENTLICHEN Empfaengers (§5).

    Er steht je Generation im Buch, damit nach einer Rotation nachvollziehbar
    bleibt, welche Identitaet eine Generation oeffnet. Der Recipient ist kein
    Geheimnis; der Fingerabdruck haelt die Zeile trotzdem kurz.
    """
    return "sha256-" + hashlib.sha256(recipient.encode()).hexdigest()[:32]


def _write_offsite_json(staging_set: str, *, gen_id: str,
                        classes: tuple[str, ...], recipient: str,
                        manifest: dict[str, Any],
                        proof: OPV.Proof) -> str:
    """Legt `offsite.json` NEBEN das Satz-Manifest — INNEN in der Huelle (§8).

    `core_commit` beschreibt weiterhin den Baum aus `inventory.CORE_REPO` —
    das ist eine Beobachtung, keine Herkunft. Die **Herkunft** steht in
    `producer_commit`/`core_bundle_commit`, und die kommt aus dem Beweis, nicht
    aus einer Konstante. Die Verwechslung dieser beiden Dinge war der Defekt,
    der Generation `20260901T054003Z` unbrauchbar machte.
    """
    manifest_path = os.path.join(staging_set, engine.MANIFEST_NAME)
    payload = {
        "offsite_format": 1,
        "generation_id": gen_id,
        "classes": list(classes),
        "recipient_fingerprint": _recipient_fingerprint(recipient),
        "snapshot_manifest_sha256": OP.sha256_file(manifest_path),
        "core_commit": (manifest.get("revisions", {})
                        .get("core", {}).get("commit")),
        "core_branch": (manifest.get("revisions", {})
                        .get("core", {}).get("branch")),
        "dirty": (manifest.get("revisions", {})
                  .get("core", {}).get("dirty")),
        "provenance": proof.as_dict(),
        "producer_commit": proof.producer_commit,
        "core_bundle_commit": proof.core_bundle_commit,
        "created_at": OL.utcnow_iso(),
        "host": manifest.get("host", ""),
        "source": "staging",
        "local_backup_state": {"ok": bool(manifest.get("ok")),
                               "errors": list(manifest.get("errors") or [])[:10]},
    }
    path = os.path.join(staging_set, "offsite.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.chmod(path, 0o600)
    return payload["snapshot_manifest_sha256"]


def run_once(*, force: bool = False, now: dt.datetime | None = None,
             client: Any = None, book: OL.OffsiteLedger | None = None,
             keep_staging: bool = False) -> dict[str, Any]:
    """EIN Lauf. Wirft nie — er berichtet. Der Rueckgabewert ist das Urteil.

    `ok=True` heisst: hochgeladen UND zurueckverglichen. `ok=None` heisst
    uebersprungen (ein Gate, kein Fehler). `ok=False` heisst gescheitert,
    mit genau einer Kategorie aus §18.
    """
    started = time.time()
    moment = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    outcome: dict[str, Any] = {"ok": None, "skipped": True, "reason": "",
                               "generation_id": "", "classes": [],
                               "category": "", "notified": False}

    # -- Gate 1: eingeschaltet? (DEBT-0109 — dreifach strukturell) ---------
    try:
        cfg = OC.load()
    except OC.OffsiteConfigError as exc:
        return _fail(outcome, "unexpected", f"Konfiguration unlesbar: {exc}",
                     started)
    if cfg is None:
        outcome["reason"] = "nicht eingerichtet"
        return outcome
    if not cfg.enabled and not force:
        outcome["reason"] = "nicht eingeschaltet"
        return outcome

    # -- Gate 2: laeuft schon einer? --------------------------------------
    try:
        lock = _open_lock()
    except OffsiteLocked as exc:
        # Der Verlierer einer Kollision ist kein Fehler (§11): stiller Exit 0.
        outcome["reason"] = str(exc)
        return outcome

    try:
        return _run_locked(outcome, cfg, moment, started, force=force,
                           client=client, book=book,
                           keep_staging=keep_staging)
    finally:
        lock.close()


def _fail(outcome: dict[str, Any], category: str, reason: str,
          started: float) -> dict[str, Any]:
    outcome.update({"ok": False, "skipped": False, "category": category,
                    "reason": reason,
                    "duration_seconds": round(time.time() - started, 2)})
    return outcome


def _run_locked(outcome: dict[str, Any], cfg: OC.OffsiteConfig,
                moment: dt.datetime, started: float, *, force: bool,
                client: Any, book: OL.OffsiteLedger | None,
                keep_staging: bool) -> dict[str, Any]:
    book = book or OL.OffsiteLedger()
    state = load_state()
    state["last_attempt_at"] = time.time()

    # -- Gate 3: fruehester Start (Ortszeit) ------------------------------
    # Die Ortszeit kommt aus dem Zeitpunkt DIESES Laufs, nicht aus einer
    # zweiten Uhr: ein Lauf, dessen Gates verschiedene Zeitquellen befragen,
    # kann sich selbst widersprechen — und ein Test kann ihn nicht stellen.
    local_hour = moment.astimezone().hour
    if not force and local_hour < EARLIEST_LOCAL_HOUR:
        outcome["reason"] = f"vor {EARLIEST_LOCAL_HOUR}:00 Ortszeit"
        save_state(state)
        return outcome

    # -- Gate 4: heute schon eine gute Generation? (KALENDERTAG, §11) -----
    utc_day = moment.strftime("%Y%m%d")
    existing = book.successful_on(utc_day)
    if existing is not None and not force:
        outcome["reason"] = (f"fuer heute existiert bereits "
                             f"{existing.generation_id}")
        save_state(state)
        return outcome

    # -- Offene Zeilen ehrlich abschliessen, statt sie zu erben -----------
    for open_row in book.open_generations():
        if open_row.generation_id.startswith(utc_day) and force:
            continue
        book.mark(open_row.generation_id, OL.FAILED,
                  failure_category="unexpected",
                  error="Lauf wurde unterbrochen; beim naechsten Start "
                        "als abgebrochen verbucht")
        _log(f"recovered open generation {open_row.generation_id} -> failed")

    gen_id = generation_id(moment)
    classes = classes_for(moment, book)
    outcome.update({"generation_id": gen_id, "classes": list(classes)})

    staging_dir = os.path.join(OC.offsite_dir(), "staging", gen_id)
    archive_path = os.path.join(OC.offsite_dir(), "staging",
                                f"{gen_id}.tar.zst.age")

    # -- Zugang FRUEH pruefen: ein gesperrter Tresor soll nicht erst nach
    # -- 90 MB Staging-Arbeit auffallen. Der Wert lebt nur in diesem Aufruf;
    # -- zurueck kommt die Kennung, die ins Buch darf (§16).
    client = client or OS3.S3Client(region=cfg.region)
    try:
        access_key_id = client.access_key_id(cfg.bucket("daily"))
    except OS3.S3Error as exc:
        return _finish_failed(outcome, book, state, "", exc.category,
                              f"{exc} ({exc.detail})" if exc.detail else str(exc),
                              started, staging_dir, archive_path, keep_staging)

    result = None
    try:
        # -- Gate 5: Platz auf der internen Platte ------------------------
        from solvio.storage import volume as V
        free = V.free_bytes_at(os.path.expanduser("~"))
        if free and free < MIN_FREE_BYTES:
            raise engine.BackupError(
                f"zu wenig Platz fuer das Staging: {free / 1024**3:.1f} GB frei")

        # -- Satz erzeugen (Staging-Modus; FileVault-Gate steckt darin) ---
        result = engine.run_backup(staging_root=staging_dir)
        staging_set = result.path

        # -- Gate 6: traegt der Satz sein eigenes Werkzeug? ---------------
        # Vor der Huelle, nicht danach: eine Generation, aus der sich nicht
        # restaurieren laesst, darf gar nicht erst verschluesselt werden.
        # Sie scheitert hier — und wird nie hochgeladen, nie verifiziert,
        # nie gesund.
        proof = OPV.prove_staging_set(staging_set)

        snapshot_sha = _write_offsite_json(
            staging_set, gen_id=gen_id, classes=classes,
            recipient=cfg.recipient, manifest=result.manifest, proof=proof)
    except OPV.ProvenanceError as exc:
        return _finish_failed(outcome, book, state, gen_id, "staging_failed",
                              f"provenance_unproven/{exc}", started,
                              staging_dir, archive_path, keep_staging)
    except engine.StagingUnprotected as exc:
        return _finish_failed(outcome, book, state, gen_id, "staging_failed",
                              f"staging_unprotected: {exc}", started,
                              staging_dir, archive_path, keep_staging)
    except engine.BackupLocked as exc:
        # Die LOKALE Maschine sichert gerade. Kein Fehler — morgen wieder.
        outcome["reason"] = f"lokale Sicherung laeuft: {exc}"
        save_state(state)
        _cleanup(staging_dir, archive_path, keep_staging)
        return outcome
    except Exception as exc:
        return _finish_failed(outcome, book, state, gen_id, "staging_failed",
                              f"{type(exc).__name__}: {exc}", started,
                              staging_dir, archive_path, keep_staging)

    # -- Huelle: verschluesseln, BEVOR etwas das Haus verlaesst ----------
    try:
        packed = OP.pack(staging_set, recipient=cfg.recipient,
                         out_path=archive_path)
    except Exception as exc:
        return _finish_failed(outcome, book, state, gen_id,
                              "encryption_failed",
                              f"{type(exc).__name__}: {exc}", started,
                              staging_dir, archive_path, keep_staging)

    outcome.update({"cipher_bytes": packed.cipher_bytes,
                    "cipher_sha256": packed.cipher_sha256,
                    "plain_bytes": int(result.manifest.get("total_bytes") or 0),
                    "source_ok": bool(result.ok),
                    "producer_commit": proof.producer_commit,
                    "core_bundle_commit": proof.core_bundle_commit,
                    "core_bundle_ref": proof.core_bundle_ref})

    # -- Das Buch traegt die Generation, BEVOR ein Byte hinausgeht -------
    book.start(generation_id=gen_id, classes=classes,
               snapshot_manifest_sha256=snapshot_sha,
               plain_bytes=int(result.manifest.get("total_bytes") or 0),
               cipher_bytes=packed.cipher_bytes,
               cipher_sha256=packed.cipher_sha256,
               recipient_fingerprint=_recipient_fingerprint(cfg.recipient),
               source_ok=bool(result.ok), access_key_id=access_key_id)

    key = object_key(gen_id)
    daily_bucket = cfg.bucket("daily")
    object_keys: dict[str, Any] = {}

    try:
        # `uploading` VOR dem ersten Byte: ein Absturz hier hinterlaesst eine
        # offene Frage im Buch, keinen geerbten Erfolg.
        book.mark(gen_id, OL.UPLOADING)
        info = client.put_object(daily_bucket, key,
                                 file_path=archive_path,
                                 sha256=packed.cipher_sha256)
        object_keys["daily"] = {"bucket": daily_bucket, "key": key,
                                **info.as_dict()}

        # -- Klassen-Kopien: COPY, nie ein zweiter Upload (§9) ------------
        for klass in classes:
            if klass == "daily":
                continue
            target = cfg.bucket(klass)
            copied = client.copy_object(src_bucket=daily_bucket, src_key=key,
                                        dst_bucket=target, dst_key=key)
            object_keys[klass] = {"bucket": target, "key": key,
                                  **copied.as_dict()}

        book.mark(gen_id, OL.UPLOADED, object_keys=object_keys)
        outcome["object_keys"] = object_keys

        # -- Ruecklade-Vergleich: OHNE ihn ist nichts gruen (§18) ---------
        verify_started = time.time()
        readback = os.path.join(OC.offsite_dir(), "staging",
                                f"{gen_id}.readback")
        client.get_object(daily_bucket, key, dest_path=readback)
        actual = OP.sha256_file(readback)
        os.remove(readback)
        if actual != packed.cipher_sha256:
            raise OS3.S3NotFound(
                "Ruecklade-Vergleich weicht ab",
                detail=f"erwartet {packed.cipher_sha256[:16]}, "
                       f"gelesen {actual[:16]}")
        book.record_verification(
            generation_id=gen_id, kind="readback", result="ok",
            duration_seconds=round(time.time() - verify_started, 2),
            details={"cipher_bytes": packed.cipher_bytes})
        book.mark(gen_id, OL.VERIFIED, object_keys=object_keys)
    except OS3.S3Error as exc:
        book.record_verification(generation_id=gen_id, kind="readback",
                                 result="failed",
                                 details={"category": exc.category})
        return _finish_failed(outcome, book, state, gen_id, exc.category,
                              f"{exc} ({exc.detail})" if exc.detail else str(exc),
                              started, staging_dir, archive_path, keep_staging)
    except Exception as exc:
        return _finish_failed(outcome, book, state, gen_id, "unexpected",
                              f"{type(exc).__name__}: {exc}", started,
                              staging_dir, archive_path, keep_staging)

    _cleanup(staging_dir, archive_path, keep_staging)
    duration = round(time.time() - started, 2)
    state.update({"last_success_at": time.time(), "last_generation_id": gen_id,
                  "last_error": None, "consecutive_failures": 0,
                  "last_duration_seconds": duration,
                  "last_cipher_bytes": packed.cipher_bytes,
                  "last_classes": list(classes),
                  "last_source_ok": bool(result.ok)})
    save_state(state)
    _log(f"verified {gen_id} classes={'+'.join(classes)} "
         f"bytes={packed.cipher_bytes} source_ok={result.ok} in {duration}s")
    outcome.update({"ok": True, "skipped": False, "verified": True,
                    "duration_seconds": duration})
    if not result.ok:
        # Durchgereicht, nicht verschwiegen (§18): hochgeladen ja, gesund nein.
        outcome["source_ok"] = False
        outcome["reason"] = ("Quellsatz meldet Fehler — hochgeladen, aber "
                             "nicht gesund")
    return outcome


def _finish_failed(outcome: dict[str, Any], book: OL.OffsiteLedger,
                   state: dict[str, Any], gen_id: str, category: str,
                   error: str, started: float, staging_dir: str,
                   archive_path: str, keep_staging: bool) -> dict[str, Any]:
    """Ein Fehler bekommt IMMER: Buch, State, Log und eine Kategorie (§18)."""
    try:
        if book.get(gen_id) is not None:
            book.mark(gen_id, OL.FAILED, failure_category=category,
                      error=error)
    except OL.LedgerError:
        pass
    state["last_error"] = error[:400]
    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    state["last_failure_category"] = category
    save_state(state)
    _log(f"failed {gen_id or '-'} category={category}: {error[:200]}")
    _cleanup(staging_dir, archive_path, keep_staging)
    return _fail(outcome, category, error, started)


def _cleanup(staging_dir: str, archive_path: str, keep: bool) -> None:
    """Staging und Archiv sind transient (§7, DEBT-0055) — sofort weg."""
    if keep:
        return
    shutil.rmtree(staging_dir, ignore_errors=True)
    for path in (archive_path, archive_path + ".incoming"):
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------- Einstieg
async def run_and_report(*, force: bool = False, notify_store: Any = None,
                         keep_staging: bool = False) -> dict[str, Any]:
    """Laeuft und MELDET — jede Fehlerkategorie erreicht den Posteingang.

    Der lokale Job hat hier eine Asymmetrie (eine unerwartete Ausnahme
    erzeugt dort keine Meldung). Der Vertrag verlangt fuer Offsite
    ausdruecklich das Gegenteil: „jede Kategorie aus §18 erreicht
    Buch+State+Meldung+Gesundheit" (§22).
    """
    outcome = await asyncio.to_thread(run_once, force=force,
                                      keep_staging=keep_staging)
    if outcome.get("ok") is False and outcome.get("category"):
        outcome["notified"] = await maybe_notify(
            str(outcome["category"]), str(outcome.get("reason") or ""),
            store=notify_store)
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(prog="solvio-offsite")
    parser.add_argument("--force", action="store_true",
                        help="Gates ueberspringen (Abnahme, nicht Betrieb)")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Staging und Archiv stehen lassen (Diagnose)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    outcome = asyncio.run(run_and_report(force=args.force,
                                         keep_staging=args.keep_staging))
    if args.json:
        print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str))
    else:
        status = ("ok" if outcome.get("ok") else
                  "uebersprungen" if outcome.get("ok") is None else "FEHLER")
        print(f"{status}: {outcome.get('reason') or outcome.get('generation_id')}")
    # Ein uebersprungener Lauf ist kein Fehler — launchd soll nicht neu starten.
    return 0 if outcome.get("ok") is not False else 1


if __name__ == "__main__":
    sys.exit(main())
