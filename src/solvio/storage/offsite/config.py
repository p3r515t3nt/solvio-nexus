"""Die Offsite-Konfiguration: Schalter, Recipient, Buckets, Prefix, Klassen.

Eine Datei, `~/.solvio/offsite/config.json`, und eine klare Arbeitsteilung:

* Der **Schalter** (`enabled`) ist die dritte der drei Besitzerhandlungen
  (Vertrag §11): er steht nach `setup` auf `False` und wird erst von
  `solvio offsite-setup --enable` umgelegt — nachdem das Quellgesundheits-
  Gate bestanden ist. Nichts in diesem Modul legt ihn um.
* Der **Recipient** ist der OEFFENTLICHE age-Schluessel (§5). Er ist kein
  Geheimnis — der taegliche Upload braucht nur ihn, und genau deshalb kann
  der Upload-Pfad verschluesseln, ohne je ein Geheimnis zu halten.
* Die **Klassen** (daily/weekly/monthly) tragen Bucket, Lock-Dauer und
  Lifecycle aus dem Vertrag (§6/§9). Eine Bucket-Default-Retention ist
  genau EINE Dauer — deshalb drei Buckets, nie einer.

Umlenkbar ueber `SOLVIO_OFFSITE_DIR` — aus demselben Grund wie
`SOLVIO_STORAGE_STATE_DIR` in der lokalen Maschine: ein Testlauf, der in
den produktiven Betriebszustand schreibt, ist ein Werkzeug, das Schaden
anrichtet, um Schaden zu verhindern.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

FORMAT_VERSION = 1

DEFAULT_DIR = "~/.solvio/offsite"

#: Vertragswerte (§6/§9): Bucket je Klasse, Compliance-Lock-Tage,
#: Lifecycle-Expiry-Tage. Die Buckets entstehen erst in B3 — die Namen sind
#: trotzdem Vertragsbestand und stehen hier, nicht in einem Kopf.
CONTRACT_CLASSES: dict[str, dict[str, Any]] = {
    "daily": {"bucket": "solvio-offsite-daily", "lock_days": 14,
              "lifecycle_days": 21},
    "weekly": {"bucket": "solvio-offsite-weekly", "lock_days": 56,
               "lifecycle_days": 70},
    "monthly": {"bucket": "solvio-offsite-monthly", "lock_days": 365,
                "lifecycle_days": 400},
}

CONTRACT_REGION = "eu-central-1"
GENERATIONS_PREFIX = "v1/generations/"
RECOVERY_PREFIX = "v1/recovery/"


class OffsiteConfigError(RuntimeError):
    """Die Konfiguration ist nicht brauchbar. Fail-closed, nie geraten."""


def offsite_dir() -> str:
    return os.path.expanduser(os.environ.get("SOLVIO_OFFSITE_DIR")
                              or DEFAULT_DIR)


def config_path() -> str:
    return os.path.join(offsite_dir(), "config.json")


def envelope_path(version: int = 1) -> str:
    """Wo der Wiederherstellungsumschlag kanonisch liegt (§5)."""
    return os.path.join(offsite_dir(), f"offsite-identity-v{int(version)}.age")


@dataclass(frozen=True)
class OffsiteConfig:
    """Die gelesene Wahrheit. Unvollstaendiges wird nicht ergaenzt, es faellt."""

    enabled: bool
    recipient: str
    recipient_version: int
    region: str
    classes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def bucket(self, klass: str) -> str:
        try:
            return str(self.classes[klass]["bucket"])
        except KeyError as exc:
            raise OffsiteConfigError(f"unknown class: {klass}") from exc

    def bucket_url(self, klass: str) -> str:
        """Das Ziel im Sinne der Tresor-Policy: Schema + Host, sonst nichts."""
        return f"https://{self.bucket(klass)}.s3.{self.region}.amazonaws.com"

    def as_dict(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "enabled": bool(self.enabled),
            "recipient": self.recipient,
            "recipient_version": int(self.recipient_version),
            "region": self.region,
            "classes": self.classes,
        }


def _valid_recipient(value: str) -> bool:
    """Ein age-X25519-Recipient: `age1` + Bech32-Rest, ein Wort, kein Raetsel.

    Bewusst KEINE vollstaendige Bech32-Pruefung — die macht age selbst beim
    Verschluesseln, und ein Fehler dort ist laut und frueh. Hier wird nur
    verhindert, dass ein leerer String oder ein Pfad als Empfaenger gilt.
    """
    v = (value or "").strip()
    return (v.startswith("age1") and 50 <= len(v) <= 90
            and v == v.lower() and " " not in v)


def load(path: str | None = None) -> OffsiteConfig | None:
    """Liest die Konfiguration. `None` heisst: nicht eingerichtet.

    Alles andere als eine vollstaendige, gueltige Datei ist ein FEHLER, kein
    Zustand — eine halb gelesene Offsite-Konfiguration, die stumm als „aus"
    gilt, waere genau die Sorte gruen, gegen die dieses Projekt baut.
    """
    where = path or config_path()
    if not os.path.exists(where):
        return None
    try:
        with open(where, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise OffsiteConfigError(f"config unreadable: {exc}") from exc
    if int(raw.get("format_version") or 0) != FORMAT_VERSION:
        raise OffsiteConfigError(
            f"unknown format_version: {raw.get('format_version')!r}")
    recipient = str(raw.get("recipient") or "")
    if not _valid_recipient(recipient):
        raise OffsiteConfigError("recipient is not an age recipient")
    classes = raw.get("classes")
    if not isinstance(classes, dict) or set(classes) != set(CONTRACT_CLASSES):
        raise OffsiteConfigError("classes must be exactly daily/weekly/monthly")
    for name, spec in classes.items():
        if not str(spec.get("bucket") or "").strip():
            raise OffsiteConfigError(f"class {name} has no bucket")
        for key in ("lock_days", "lifecycle_days"):
            if int(spec.get(key) or 0) <= 0:
                raise OffsiteConfigError(f"class {name}: {key} must be > 0")
    return OffsiteConfig(
        enabled=bool(raw.get("enabled")),
        recipient=recipient,
        recipient_version=int(raw.get("recipient_version") or 1),
        region=str(raw.get("region") or ""),
        classes={k: dict(v) for k, v in classes.items()},
    )


def save(config: OffsiteConfig, path: str | None = None) -> str:
    where = path or config_path()
    os.makedirs(os.path.dirname(where), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(where), 0o700)
    tmp = where + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(config.as_dict(), fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, where)
    os.chmod(where, 0o600)
    return where


def fresh(recipient: str, *, recipient_version: int = 1) -> OffsiteConfig:
    """Die Konfiguration, wie `setup` sie anlegt: Vertragswerte, Schalter AUS.

    `enabled=False` ist hier kein Standardwert, sondern die Zusage aus
    DEBT-0109: eingeschaltet wird durch eine eigene, spaetere
    Besitzerhandlung — nie durch das Einrichten.
    """
    if not _valid_recipient(recipient):
        raise OffsiteConfigError("recipient is not an age recipient")
    return OffsiteConfig(
        enabled=False,
        recipient=recipient.strip(),
        recipient_version=int(recipient_version),
        region=CONTRACT_REGION,
        classes={k: dict(v) for k, v in CONTRACT_CLASSES.items()},
    )
