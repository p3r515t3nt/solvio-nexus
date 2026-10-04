"""Offsite Encrypted Backup V1 — was Schluessel, Huelle und Konfiguration tun.

Die Schwester von `test_offsite_security.py`: dort steht, was NIE passiert;
hier steht, dass das Richtige wirklich passiert. Der Schwerpunkt liegt auf
den Zusicherungen aus §22 des Vertrags:

* Krypto-Roundtrip: gepackt, geoeffnet, byte-identisch,
* das Manifest liegt IM Archiv — ein Staging ohne Manifest wird verweigert,
* falsche Identitaet und manipulierter Geheimtext sind EIN Fehler,
* Streaming-Grenze: der Peak-RSS bleibt unter einem Deckel,
* die Konfiguration entsteht mit Schalter AUS (DEBT-0109) und faellt bei
  jedem Defekt, statt zu raten.

Alle Schluessel sind Wegwerf-Erzeugnisse dieses Laufs; der erste Test
weigert sich, wenn irgendetwas Produktives im Spiel ist.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-offsite-fn-")
os.environ["SOLVIO_OFFSITE_DIR"] = os.path.join(_SANDBOX, "offsite")
os.environ["SOLVIO_OFFSITE_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "keys")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.storage.offsite import config as OC   # noqa: E402
from solvio.storage.offsite import identity as OI  # noqa: E402
from solvio.storage.offsite import pack as OP      # noqa: E402

#: Deckel fuer die Streaming-Zusicherung (§22). Die B2-Messung lag bei
#: ~18 MB fuer die age-Stufe und ~55 MB fuer die Kinder — 256 MiB ist die
#: Grenze, ab der „Streaming" eine Behauptung waere.
RSS_CEILING_MB = 256
_STREAM_PAYLOAD_MB = 48


#: Der Arbeitsfaktor, den der PRODUKTIVE Wiederherstellungsumschlag
#: `offsite-identity-v1.age` traegt — age-CLI-Standard, Vertrag §4.
#: Ein neu erzeugter Umschlag darf nie darunter liegen.
AGE_CLI_LOG_N = 18


def _tmp(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=f"offsite-fn-{prefix}-", dir=_SANDBOX)


def _staging(work: str, *, payload: bytes = b"") -> str:
    staging = os.path.join(work, "staging")
    os.makedirs(staging, exist_ok=True)
    with open(os.path.join(staging, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"format_version": 1, "backup_id": "probe"}, fh)
    if payload:
        os.makedirs(os.path.join(staging, "Memory"), exist_ok=True)
        with open(os.path.join(staging, "Memory", "daten.bin"), "wb") as fh:
            fh.write(payload)
    return staging


def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------ Testisolation
def t_this_suite_cannot_touch_the_production_offsite_state() -> None:
    require("/.solvio/" not in OC.offsite_dir() + "/",
            "Offsite-Verzeichnis nicht umgelenkt")
    require(OI.is_test_backend(), "Offsite-Schluesselspeicher nicht umgelenkt")


# ------------------------------------------------------------- Konfiguration
def t_config_is_born_disabled_and_survives_the_roundtrip() -> None:
    line, recipient = OI.create_identity()
    del line
    cfg = OC.fresh(recipient)
    require_equal(cfg.enabled, False,
                  "eine frische Konfiguration ist EINGESCHALTET — DEBT-0109")
    where = OC.save(cfg)
    require_equal(oct(os.stat(where).st_mode & 0o777), "0o600",
                  "config.json ist nicht 0600")
    loaded = OC.load()
    require(loaded is not None, "die gespeicherte Konfiguration ist unlesbar")
    require_equal(loaded.as_dict(), cfg.as_dict(), "Roundtrip veraendert")
    require_equal(loaded.bucket("daily"), "solvio-offsite-daily",
                  "Vertrags-Bucketname verschoben")
    require_equal(
        loaded.bucket_url("monthly"),
        "https://solvio-offsite-monthly.s3.eu-central-1.amazonaws.com",
        "das Policy-Ziel ist nicht der Vertrags-Bucket")
    require_equal(set(loaded.classes), {"daily", "weekly", "monthly"},
                  "Klassen unvollstaendig")
    require_equal(loaded.classes["daily"]["lock_days"], 14, "daily-Lock")
    require_equal(loaded.classes["weekly"]["lock_days"], 56, "weekly-Lock")
    require_equal(loaded.classes["monthly"]["lock_days"], 365, "monthly-Lock")


def t_a_missing_config_is_none_a_broken_one_is_an_error() -> None:
    empty = os.path.join(_tmp("cfg"), "config.json")
    require(OC.load(empty) is None, "eine fehlende Datei war nicht None")
    with open(empty, "w", encoding="utf-8") as fh:
        fh.write("{kaputt")
    require_raises(OC.OffsiteConfigError, OC.load, empty,
                   message="kaputtes JSON wurde still geschluckt")
    with open(empty, "w", encoding="utf-8") as fh:
        json.dump({"format_version": 1, "enabled": False,
                   "recipient": "age1xyz", "recipient_version": 1,
                   "region": "eu-central-1", "classes": {}}, fh)
    require_raises(OC.OffsiteConfigError, OC.load, empty,
                   message="leere Klassen wurden akzeptiert")


def t_a_config_without_valid_recipient_is_refused() -> None:
    require_raises(OC.OffsiteConfigError, OC.fresh, "",
                   message="leerer Recipient akzeptiert")
    require_raises(OC.OffsiteConfigError, OC.fresh, "/etc/passwd",
                   message="ein Pfad wurde als Recipient akzeptiert")
    require_raises(OC.OffsiteConfigError, OC.fresh,
                   "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"
                   "QQQQQQQQQQQQQQQQQQQ",
                   message="eine IDENTITAET wurde als Recipient akzeptiert")


# ----------------------------------------------------------------- Identitaet
def t_identity_roundtrip_in_the_test_backend() -> None:
    line, recipient = OI.create_identity()
    require(not OI.present(7), "Fassung 7 existierte schon")
    OI.store_identity(line, version=7)
    require(OI.present(7), "present() sieht den Eintrag nicht")
    require_equal(OI.read_identity(7), line, "Rueckweg veraendert den Wert")
    require_equal(OI.recipient_of(line), recipient,
                  "recipient_of widerspricht age-keygen")
    require(OI.forget_identity(7), "forget fand nichts")
    require(OI.read_identity(7) is None, "vergessen ist nicht weg")


def t_identity_storage_rejects_garbage() -> None:
    require_raises(OI.OffsiteIdentityError, OI.store_identity,
                   "kein-schluessel", 8,
                   message="Nicht-Identitaet wurde gespeichert")
    require_raises(OI.OffsiteIdentityError, OI.recipient_of, "age1abc",
                   message="ein Recipient wurde als Identitaet genommen")


def t_envelope_header_parsing_reads_the_work_factor() -> None:
    """Der Arbeitsfaktor wird GELESEN, nicht angenommen.

    Diese Zusicherung stand bis 2026-09-02 auf `== 20` und fiel auf einer
    belegten Maschine. Zehn Umschlaege hintereinander gemessen: neunmal 20,
    einmal 19. **pyrage stimmt scrypt auf die Maschine ab** — ein fester Wert
    ist hier keine strengere Pruefung, sondern eine falsche Aussage.

    Was bleibt, ist die Grenze, die etwas bedeutet: nie unter dem
    age-CLI-Standard 18, mit dem der produktive Wiederherstellungsumschlag
    heute tatsaechlich liegt (Vertrag §4). Ein pyrage-Umschlag, der schwaecher
    waere als der bereits produktive, waere ein echter Rueckschritt — und den
    faengt diese Zeile.
    """
    import pyrage
    work = _tmp("env")
    env_path = os.path.join(work, "u.age")
    with open(env_path, "wb") as fh:
        fh.write(pyrage.passphrase.encrypt(b"synthetic-drill-value-0004",
                                           "wegwerf-passphrase"))
    faktor = OI.envelope_scrypt_log_n(env_path)
    require(faktor >= AGE_CLI_LOG_N,
            f"pyrage-Umschlag schwaecher als der produktive age-CLI-Standard: "
            f"log2(N)={faktor} < {AGE_CLI_LOG_N}")
    require(faktor <= 24,
            f"unplausibel hoher Arbeitsfaktor log2(N)={faktor} — eher ein "
            f"Lesefehler im Kopf als eine echte Messung")
    plain = os.path.join(work, "plain.txt")
    with open(plain, "w") as fh:
        fh.write("kein umschlag")
    require_raises(OI.OffsiteIdentityError, OI.envelope_scrypt_log_n, plain,
                   message="eine Nicht-age-Datei wurde als Umschlag gelesen")


# --------------------------------------------------------------------- Huelle
def t_pack_roundtrip_preserves_every_byte() -> None:
    line, recipient = OI.create_identity()
    work = _tmp("roundtrip")
    payload = os.urandom(1 << 20) + b"\x00" * (1 << 20)
    staging = _staging(work, payload=payload)
    archive = os.path.join(work, "20260831T210000Z.tar.zst.age")
    result = OP.pack(staging, recipient=recipient, out_path=archive)
    require(OP.is_age_object(archive), "das Objekt traegt keinen age-Kopf")
    require_equal(result.cipher_sha256, _sha(archive),
                  "die Pruefsumme beschreibt nicht das Objekt")
    require(result.cipher_bytes > 0 and result.cipher_bytes < len(payload) * 2,
            "unplausible Objektgroesse")
    dest = os.path.join(work, "zurueck")
    OP.unpack(archive, identity_line=line, dest_dir=dest)
    require_equal(_sha(os.path.join(dest, "Memory", "daten.bin")),
                  _sha(os.path.join(staging, "Memory", "daten.bin")),
                  "der Roundtrip veraendert Bytes")
    require(os.path.isfile(os.path.join(dest, "manifest.json")),
            "das Manifest fehlt im entpackten Satz")
    require(not os.path.exists(archive + ".incoming"),
            "ein .incoming-Rest blieb liegen")


def t_pack_refuses_a_staging_without_manifest() -> None:
    _line, recipient = OI.create_identity()
    work = _tmp("ohnemanifest")
    staging = os.path.join(work, "staging")
    os.makedirs(staging)
    with open(os.path.join(staging, "daten.bin"), "wb") as fh:
        fh.write(b"x" * 1024)
    require_raises(OP.PackError, OP.pack, staging, recipient=recipient,
                   out_path=os.path.join(work, "g.age"),
                   message="ein Satz ohne Manifest verliess das Haus")
    require(not os.path.exists(os.path.join(work, "g.age")),
            "trotz Verweigerung entstand ein Objekt")


def t_pack_refuses_a_bad_recipient() -> None:
    work = _tmp("badrcpt")
    staging = _staging(work, payload=b"y" * 512)
    for bad in ("", "nicht-age", "age1zzz"):
        require_raises(OP.PackError, OP.pack, staging, recipient=bad,
                       out_path=os.path.join(work, "g.age"),
                       message=f"Recipient {bad!r} wurde akzeptiert")


def t_wrong_identity_and_tampering_are_one_error() -> None:
    """§22: kein Orakel. Wer das Objekt nicht oeffnen kann, erfaehrt nicht,
    ob der Schluessel falsch oder der Geheimtext manipuliert war."""
    line, recipient = OI.create_identity()
    stranger, _ = OI.create_identity()
    work = _tmp("einfehler")
    staging = _staging(work, payload=os.urandom(1 << 16))
    archive = os.path.join(work, "g.age")
    OP.pack(staging, recipient=recipient, out_path=archive)

    wrong = require_raises(OP.PackError, OP.unpack, archive,
                           identity_line=stranger,
                           dest_dir=os.path.join(work, "a"),
                           message="die falsche Identitaet oeffnete das Objekt")
    data = bytearray(open(archive, "rb").read())
    data[-3] ^= 0xFF
    with open(archive, "wb") as fh:
        fh.write(bytes(data))
    tampered = require_raises(OP.PackError, OP.unpack, archive,
                              identity_line=line,
                              dest_dir=os.path.join(work, "b"),
                              message="der manipulierte Geheimtext oeffnete sich")
    require_equal(str(wrong), str(tampered),
                  "zwei verschiedene Fehlertexte sind ein Orakel")


def t_unpack_refuses_a_nonempty_destination() -> None:
    line, recipient = OI.create_identity()
    work = _tmp("vollziel")
    staging = _staging(work, payload=b"z" * 256)
    archive = os.path.join(work, "g.age")
    OP.pack(staging, recipient=recipient, out_path=archive)
    dest = os.path.join(work, "belegt")
    os.makedirs(dest)
    with open(os.path.join(dest, "rest.txt"), "w") as fh:
        fh.write("alt")
    require_raises(OP.PackError, OP.unpack, archive, identity_line=line,
                   dest_dir=dest,
                   message="in ein belegtes Ziel wurde entpackt")


def t_a_plain_file_is_not_accepted_as_archive() -> None:
    line, _recipient = OI.create_identity()
    work = _tmp("klartext")
    fake = os.path.join(work, "fake.age")
    with open(fake, "wb") as fh:
        fh.write(b"das ist kein age-Objekt")
    require_raises(OP.PackError, OP.unpack, fake, identity_line=line,
                   dest_dir=os.path.join(work, "d"),
                   message="eine Klartextdatei wurde als Archiv genommen")


def t_pack_streaming_stays_under_the_rss_ceiling() -> None:
    """§22 Streaming-Grenze: gemessen in einem EIGENEN Prozess (ru_maxrss ist
    ein Prozessmaximum). 48 MB Nutzlast, Deckel 256 MiB — die B2-Messung lag
    bei ~18 MB fuer die age-Stufe."""
    work = _tmp("rss")
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    code = (
        "import json, os, resource, sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "from solvio.storage.offsite import identity as OI, pack as OP\n"
        f"work = {work!r}\n"
        "line, recipient = OI.create_identity()\n"
        "staging = os.path.join(work, 'staging')\n"
        "os.makedirs(os.path.join(staging, 'Memory'))\n"
        "json.dump({'format_version': 1},"
        " open(os.path.join(staging, 'manifest.json'), 'w'))\n"
        "with open(os.path.join(staging, 'Memory', 'gross.bin'), 'wb') as fh:\n"
        f"    for _ in range({_STREAM_PAYLOAD_MB}):\n"
        "        fh.write(os.urandom(1 << 19) + b'\\x11' * (1 << 19))\n"
        "archive = os.path.join(work, 'g.age')\n"
        "OP.pack(staging, recipient=recipient, out_path=archive)\n"
        "OP.unpack(archive, identity_line=line,"
        " dest_dir=os.path.join(work, 'zurueck'))\n"
        "print('RSS', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,\n"
        "      resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=600,
                          env={**os.environ, "PYTHONWARNINGS": "ignore"})
    require_equal(proc.returncode, 0,
                  f"der Messlauf scheiterte: {proc.stderr.strip()[-300:]}")
    tokens = [t for t in proc.stdout.split() if t.isdigit()]
    require(len(tokens) >= 2, f"keine Messwerte: {proc.stdout!r}")
    driver_mb = int(tokens[-2]) / (1 << 20)
    children_mb = int(tokens[-1]) / (1 << 20)
    require(driver_mb < RSS_CEILING_MB,
            f"die age-Stufe puffert: {driver_mb:.0f} MB Peak-RSS")
    require(children_mb < RSS_CEILING_MB,
            f"ein Pipeline-Kind puffert: {children_mb:.0f} MB Peak-RSS")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
