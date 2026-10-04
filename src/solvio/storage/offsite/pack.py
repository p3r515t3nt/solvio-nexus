"""Die Huelle: Staging → tar → zstd → age. Streaming, nie Vollpuffer im RAM.

Zwei Unterprozesse und die im Prozess laufende age-Stufe (Vertrag §4):

```
tar -cf - -C <staging> .   |   zstd -3   |   pyrage.encrypt_io  →  Objekt
```

* **Kompression VOR Verschluesselung** — danach ist nichts mehr
  komprimierbar. Level 3, weil die groessten Stuecke (git-Bundles) schon
  komprimiert sind.
* **Das Manifest liegt INNEN** (DEBT-0121): dieses Modul verweigert das
  Packen eines Stagings ohne `manifest.json`. Beim Provider sichtbar bleiben
  Objektname, Groesse, Zeitpunkt — nie eine Dateiliste.
* **Verschluesselt wird ohne Geheimnis**: der Recipient ist der oeffentliche
  Schluessel aus der Konfiguration. Die Identitaet braucht nur `unpack` —
  und sie bleibt dort im Prozessspeicher; keine Datei, kein `argv`, kein
  `/dev/stdin`-Kunstgriff.
* Werkzeug ist **pyrage**, nach dem harten §4-Kriterium GEMESSEN, nicht
  angenommen (B2, 2026-08-31): Encrypt UND Decrypt streamen mit ~18 MB
  Peak-RSS bei 90 MB Nutzlast, der scrypt-Passphrasen-Weg funktioniert, und
  ein pyrage-Objekt oeffnet mit stock `age` (PTY-Beweis) — der Vertrag
  haengt am FORMAT, nicht am Werkzeug. Zahlen:
  `docs/design/offsite-encrypted-backup-v1/b2/reports/`.

Fehlersemantik: eine falsche Identitaet, ein manipulierter Geheimtext und
ein abgeschnittenes Archiv sind EIN Fehler (`PackError`) — kein Orakel, das
Angreifern erzaehlt, woran genau es scheiterte.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass

import pyrage

from solvio.logging_setup import get_logger

log = get_logger("offsite")

TAR = "/usr/bin/tar"
ZSTD_LEVEL = "-3"

#: Frist fuer die gesamte Pipeline. Ein ~90-MB-Satz braucht Sekunden; eine
#: Stunde ist die Grenze zwischen „langsam" und „haengt".
PIPELINE_TIMEOUT = 3600.0

_CHUNK = 1 << 20

_TOOL_FALLBACKS = ("/opt/homebrew/bin", "/usr/local/bin")

AGE_MAGIC = b"age-encryption.org/v1"


class PackError(RuntimeError):
    """Die Huelle ist nicht zustandegekommen oder nicht zu oeffnen."""


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for prefix in _TOOL_FALLBACKS:
        candidate = os.path.join(prefix, name)
        if os.path.exists(candidate):
            return candidate
    raise PackError(f"{name} not installed")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_age_object(path: str) -> bool:
    """Traegt die Datei den age-Kopf? Die billigste aller Zusicherungen —
    ein Objekt, das hier `False` liefert, verlaesst das Haus nicht."""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(AGE_MAGIC)) == AGE_MAGIC
    except OSError:
        return False


@dataclass(frozen=True)
class PackResult:
    """Zahlen und Pruefsummen, nie Inhalt."""

    archive: str
    cipher_bytes: int
    cipher_sha256: str
    recipient: str


def _fail(step: str, stderr: bytes) -> PackError:
    # stderr eines Werkzeugs kann Pfade nennen, nie Klartext-Inhalte — er wird
    # trotzdem auf eine Zeile gekuerzt, damit ein Log kein Werkzeug-Echo wird.
    hint = stderr.decode("utf-8", errors="replace").strip().splitlines()
    return PackError(f"{step} failed: {hint[0][:160] if hint else 'no detail'}")


def _recipient(value: str):
    try:
        return pyrage.x25519.Recipient.from_str((value or "").strip())
    except Exception as exc:  # pyrage.RecipientError, ValueError
        raise PackError("recipient is not an age recipient") from exc


def _identity(line: str):
    try:
        return pyrage.x25519.Identity.from_str((line or "").strip())
    except Exception as exc:  # pyrage.IdentityError, ValueError
        raise PackError("identity is not an age identity line") from exc


def pack(staging_dir: str, *, recipient: str, out_path: str) -> PackResult:
    """Verpackt einen Staging-Satz in genau EIN age-Objekt.

    Geschrieben wird nach `<out_path>.incoming` und erst nach fehlerfreiem
    Ende umbenannt — ein Objekt, das man sieht, ist fertig (dasselbe Muster
    wie die lokalen Saetze).
    """
    if not os.path.isdir(staging_dir):
        raise PackError(f"staging missing: {staging_dir}")
    if not os.path.isfile(os.path.join(staging_dir, "manifest.json")):
        # DEBT-0121: das Manifest gehoert IN die Huelle. Ein Satz ohne
        # Manifest ist kein Satz — und ein Archiv, dessen Inhalt nur der
        # Objektname beschreibt, waere genau das Metadaten-Leck, das der
        # Vertrag schliesst.
        raise PackError("staging has no manifest.json — nothing leaves "
                        "the house undescribed")
    rcpt = _recipient(recipient)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    incoming = out_path + ".incoming"
    if os.path.exists(incoming):
        os.remove(incoming)

    tar_proc = zstd_proc = None
    ok = False
    try:
        tar_proc = subprocess.Popen(
            [TAR, "-cf", "-", "-C", staging_dir, "."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        zstd_proc = subprocess.Popen(
            [_tool("zstd"), ZSTD_LEVEL, "-q", "-c"],
            stdin=tar_proc.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        tar_proc.stdout.close()  # zstd haelt das Leseende; SIGPIPE erreicht tar

        with open(incoming, "wb") as sink:
            try:
                # Die age-Stufe laeuft HIER, streamend: ~18 MB Peak-RSS bei
                # 90 MB Nutzlast (B2-Messung). Sie liest, bis zstd schliesst.
                pyrage.encrypt_io(zstd_proc.stdout, sink, [rcpt])
            except Exception as exc:
                raise PackError("encrypt failed") from exc

        tar_err = tar_proc.stderr.read() if tar_proc.stderr else b""
        zstd_err = zstd_proc.stderr.read() if zstd_proc.stderr else b""
        if tar_proc.wait(timeout=60) != 0:
            raise _fail("tar", tar_err)
        if zstd_proc.wait(timeout=60) != 0:
            raise _fail("zstd", zstd_err)
        ok = True
    except subprocess.TimeoutExpired as exc:
        raise PackError("pipeline did not finish in time") from exc
    finally:
        for proc in (tar_proc, zstd_proc):
            if proc is not None and proc.poll() is None:
                proc.kill()
        if not ok and os.path.exists(incoming):
            os.remove(incoming)

    if not is_age_object(incoming):
        # Die Zusicherung hinter DEBT-0109: was hier liegt, ist age-Format —
        # oder es verlaesst das Haus nicht. Ein leeres oder unverschluesselt
        # durchgerutschtes Objekt faellt an dieser Zeile.
        os.remove(incoming)
        raise PackError("output is not an age object — refusing to keep it")

    os.chmod(incoming, 0o600)
    os.replace(incoming, out_path)
    result = PackResult(archive=out_path,
                        cipher_bytes=os.path.getsize(out_path),
                        cipher_sha256=sha256_file(out_path),
                        recipient=(recipient or "").strip())
    log.info("offsite.packed", bytes=result.cipher_bytes,
             sha256=result.cipher_sha256[:16])
    return result


def unpack(archive_path: str, *, identity_line: str, dest_dir: str) -> str:
    """Oeffnet ein Objekt in ein leeres Zielverzeichnis (Verify/Restore).

    Die Identitaet bleibt im Prozessspeicher — pyrage nimmt sie als Objekt,
    nicht als Datei. Das Ziel entsteht mit 0700 und muss leer sein: eine
    Probe, die in belegte Verzeichnisse entpackt, prueft irgendwann ihre
    eigenen Reste.
    """
    if not os.path.isfile(archive_path):
        raise PackError(f"archive missing: {archive_path}")
    if not is_age_object(archive_path):
        raise PackError("archive is not an age object")
    ident = _identity(identity_line)
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    if os.listdir(dest_dir):
        raise PackError(f"destination not empty: {dest_dir}")

    zstd_proc = tar_proc = None
    try:
        zstd_proc = subprocess.Popen(
            [_tool("zstd"), "-d", "-q", "-c"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        tar_proc = subprocess.Popen(
            [TAR, "-xf", "-", "-C", dest_dir],
            stdin=zstd_proc.stdout, stderr=subprocess.PIPE)
        zstd_proc.stdout.close()

        with open(archive_path, "rb") as source:
            try:
                pyrage.decrypt_io(source, zstd_proc.stdin, [ident])
            except Exception as exc:
                # Falsche Identitaet, manipulierter Geheimtext, kaputte
                # Datei: bewusst EIN Fehlertext. Wer den Unterschied braucht,
                # ist ein Mensch mit Terminal, kein Log-Leser.
                raise PackError("unpack failed") from exc
            finally:
                try:
                    zstd_proc.stdin.close()
                except OSError:
                    pass

        if zstd_proc.wait(timeout=PIPELINE_TIMEOUT) != 0:
            raise PackError("unpack failed")
        if tar_proc.wait(timeout=PIPELINE_TIMEOUT) != 0:
            raise PackError("unpack failed")
    except subprocess.TimeoutExpired as exc:
        raise PackError("unpack did not finish in time") from exc
    finally:
        for proc in (zstd_proc, tar_proc):
            if proc is not None and proc.poll() is None:
                proc.kill()

    if not os.path.isfile(os.path.join(dest_dir, "manifest.json")):
        raise PackError("unpacked set has no manifest.json")
    return dest_dir
