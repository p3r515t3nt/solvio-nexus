"""Der Speicherverwalter — WER die Platte ist, nicht wie sie heisst.

Eine externe Platte ist ein Stueck Hardware, das jemand abziehen und durch ein
anderes ersetzen kann. Ein Volume-Name ist dabei kein Ausweis: er ist ein Feld,
das jeder Mensch in zwei Sekunden aendert. Deshalb kennt dieser Verwalter die
SOLVIO-Platte ausschliesslich an ihrer **Volume-UUID**, und der Name taucht nur
in Meldungen fuer Menschen auf.

Vier Bedingungen muessen zusammen erfuellt sein, bevor etwas Privates auf die
Platte geschrieben werden darf:

1. **Identitaet** — die konfigurierte Volume-UUID ist angeschlossen.
2. **Dateisystem** — es ist APFS. Ein FAT- oder ExFAT-Volume kann keine Rechte
   und keine Symlink-Semantik, und eine Sicherung, die Rechte verliert, ist
   keine.
3. **Verschluesselung** — das Volume ist verschluesselt. Ohne das ist eine
   Sicherung ein Ordner, den man mitnehmen kann.
4. **Marke** — im Wurzelverzeichnis liegt eine Marke, die dieselbe UUID nennt.

Die vierte ist Guertel zum Hosenträger: `diskutil` loest die UUID selbst auf,
also KANN dort eigentlich keine fremde Platte haengen. Aber die Konfiguration
ist eine Datei, und eine Datei kann falsch sein. Wer nur der Konfiguration
glaubt, glaubt am Ende einer Zeile Text.

Alles hier ist **fail-closed**: was nicht beweisbar in Ordnung ist, gilt als
nicht verfuegbar. Eine fehlende Platte ist kein Fehler des Cores — sie ist ein
Zustand, den der Aufrufer sauber behandeln muss.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Any

DISKUTIL = "/usr/sbin/diskutil"

#: Wo die Speicherkonfiguration liegt. INTERN, mit Absicht: die Beschreibung,
#: welche Platte SOLVIOs Platte ist, darf nicht auf dieser Platte liegen.
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.solvio/storage.json")

#: Der Wurzelordner auf der Platte. Alles von SOLVIO liegt darunter, damit die
#: Platte auch noch einem Menschen gehoeren kann.
DEFAULT_ROOT_NAME = "SOLVIO"

#: Die Marke im Wurzelordner. Enthaelt die UUID und sonst nichts Interessantes —
#: insbesondere kein Geheimnis.
MARKER_NAME = ".solvio-storage.json"

#: Frist fuer jeden `diskutil`-Aufruf. Eine haengende Platte darf keinen Dienst
#: stillegen; sie darf nur „nicht verfuegbar" bedeuten.
DISKUTIL_TIMEOUT = 15.0

#: Unterhalb dieser Grenze meldet die Gesundheit „fast voll". Kein Schwellenwert
#: fuer Ablehnung — voll ist ein Zustand, kein Vertrauensbruch.
LOW_SPACE_BYTES = 50 * 1024 ** 3


class StorageError(RuntimeError):
    """Etwas an der Speicherschicht stimmt nicht."""


class StorageUnavailable(StorageError):
    """Die SOLVIO-Platte ist nicht benutzbar.

    Das ist der NORMALFALL bei abgezogener Platte und kein Defekt. Wer diesen
    Fehler faengt, muss weiterarbeiten koennen.
    """


class StorageUntrusted(StorageError):
    """Es haengt etwas da, aber es ist nicht die SOLVIO-Platte.

    Ausdruecklich ein anderer Fehler als `StorageUnavailable`: „nicht da" darf
    man ueberspringen, „falsch" muss auffallen.
    """


@dataclass(frozen=True)
class StorageConfig:
    """Welche Platte SOLVIOs Platte ist. Klein und ohne Geheimnisse."""

    volume_uuid: str
    root_name: str = DEFAULT_ROOT_NAME
    #: Ob Verschluesselung Pflicht ist. Standard: ja. Der Schalter existiert,
    #: damit ein Test die Bedingung pruefen kann — nicht als Bequemlichkeit.
    require_encryption: bool = True
    #: Menschenlesbar, rein informativ. Wird NIE zur Erkennung benutzt.
    label_hint: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class VolumeState:
    """Der gemessene Zustand. Jedes Feld ist entweder gemessen oder None.

    `None` heisst „nicht gemessen", nie „in Ordnung" — dieselbe Regel wie beim
    Diagnostiker.
    """

    configured: bool
    volume_uuid: str | None = None
    present: bool = False
    mounted: bool = False
    mount_point: str | None = None
    volume_name: str | None = None
    filesystem: str | None = None
    encrypted: bool = False
    locked: bool = False
    internal: bool | None = None
    ownership_enabled: bool | None = None
    total_bytes: int = 0
    free_bytes: int = 0
    smart_status: str | None = None
    smart: dict[str, Any] = field(default_factory=dict)
    marker_ok: bool | None = None
    problems: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """Darf hier etwas Privates hingeschrieben werden?"""
        return self.configured and not self.problems

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["problems"] = list(self.problems)
        d["usable"] = self.usable
        return d


# --------------------------------------------------------------------- Konfiguration
def config_path() -> str:
    return os.environ.get("SOLVIO_STORAGE_CONFIG", DEFAULT_CONFIG_PATH)


def load_config(path: str | None = None) -> StorageConfig | None:
    """Liest die Speicherkonfiguration. `None`, wenn keine eingerichtet ist.

    Eine kaputte Konfiguration ist NICHT dasselbe wie keine: sie wirft, damit
    niemand still auf „keine Platte eingerichtet" zurueckfaellt und die
    Sicherung damit lautlos ausfaellt.
    """
    p = path or config_path()
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        raw = json.load(fh)
    uuid = str(raw.get("volume_uuid") or "").strip()
    if not uuid:
        raise StorageError(f"storage config without volume_uuid: {p}")
    return StorageConfig(
        volume_uuid=uuid.upper(),
        root_name=str(raw.get("root_name") or DEFAULT_ROOT_NAME),
        require_encryption=bool(raw.get("require_encryption", True)),
        label_hint=str(raw.get("label_hint") or ""),
    )


def save_config(config: StorageConfig, path: str | None = None) -> str:
    p = path or config_path()
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(config.to_json())
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    return p


# ------------------------------------------------------------------------- Messung
def _diskutil_info(identifier: str) -> dict[str, Any] | None:
    """Fragt `diskutil` nach einem Volume. `None`, wenn es das nicht gibt."""
    try:
        proc = subprocess.run(
            [DISKUTIL, "info", "-plist", identifier],
            capture_output=True, timeout=DISKUTIL_TIMEOUT, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return plistlib.loads(proc.stdout)
    except Exception:
        return None


def _statvfs(mount_point: str) -> tuple[int, int]:
    """(gesamt, frei) in Bytes. (0, 0), wenn nicht messbar."""
    try:
        st = os.statvfs(mount_point)
    except OSError:
        return 0, 0
    return st.f_frsize * st.f_blocks, st.f_frsize * st.f_bavail


def _read_marker(root: str) -> dict[str, Any] | None:
    path = os.path.join(root, MARKER_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def probe(config: StorageConfig | None = None) -> VolumeState:
    """Misst den Zustand der SOLVIO-Platte. Wirft nie — Messen ist kein Urteil."""
    if config is None:
        try:
            config = load_config()
        except StorageError as exc:
            return VolumeState(configured=False, problems=(str(exc),))
    if config is None:
        return VolumeState(configured=False,
                           problems=("keine SOLVIO-Speicherplatte eingerichtet",))

    info = _diskutil_info(config.volume_uuid)
    if info is None:
        return VolumeState(configured=True, volume_uuid=config.volume_uuid,
                           present=False,
                           problems=("Speicherplatte nicht angeschlossen",))

    problems: list[str] = []
    mount_point = info.get("MountPoint") or None
    fs = (info.get("FilesystemType") or "").lower() or None
    # `Encryption` ist das Feld fuer „dieses Volume ist verschluesselt".
    # `FileVault` bezieht sich auf die Startplatte und ist hier immer False.
    encrypted = bool(info.get("Encryption") or info.get("EncryptionThisVolumeProper"))
    locked = bool(info.get("Locked"))
    # `GlobalPermissionsEnabled` ist die plist-Fassung von „Owners: Enabled".
    ownership = info.get("GlobalPermissionsEnabled")
    ownership_enabled = bool(ownership) if ownership is not None else None

    if not mount_point:
        problems.append("Speicherplatte angeschlossen, aber nicht montiert")
    if locked:
        problems.append("Speicherplatte ist gesperrt (Passphrase nicht eingegeben)")
    if fs != "apfs":
        problems.append(f"falsches Dateisystem: {fs or 'unbekannt'} statt apfs")
    if config.require_encryption and not encrypted:
        problems.append("Speicherplatte ist NICHT verschluesselt")

    total = free = 0
    marker_ok: bool | None = None
    if mount_point and os.path.isdir(mount_point):
        total, free = _statvfs(mount_point)
        root = os.path.join(mount_point, config.root_name)
        if os.path.isdir(root):
            marker = _read_marker(root)
            if marker is None:
                marker_ok = False
                problems.append("Wurzelordner ohne gueltige Marke")
            elif str(marker.get("volume_uuid", "")).upper() != config.volume_uuid:
                marker_ok = False
                problems.append("Marke nennt eine andere Platte als die Konfiguration")
            else:
                marker_ok = True
        # Kein Wurzelordner ist KEIN Problem: so sieht eine frisch
        # eingerichtete Platte vor dem ersten Anlegen aus.

    return VolumeState(
        configured=True,
        volume_uuid=config.volume_uuid,
        present=True,
        mounted=bool(mount_point),
        mount_point=mount_point,
        volume_name=info.get("VolumeName") or None,
        filesystem=fs,
        encrypted=encrypted,
        locked=locked,
        internal=bool(info.get("Internal")) if "Internal" in info else None,
        ownership_enabled=ownership_enabled,
        total_bytes=total,
        free_bytes=free,
        smart_status=info.get("SMARTStatus") or None,
        smart=dict(info.get("SMARTDeviceSpecificKeysMayVaryNotGuaranteed") or {}),
        marker_ok=marker_ok,
        problems=tuple(problems),
    )


# -------------------------------------------------------------------------- Zugriff
def storage_root(state: VolumeState | None = None,
                 config: StorageConfig | None = None) -> str:
    """Der geprüfte Wurzelpfad auf der Platte — oder eine Ausnahme.

    Das ist der EINZIGE Weg, an einen Schreibpfad auf der externen Platte zu
    kommen. Wer `os.path.join("/Volumes/...")` selbst baut, umgeht die Pruefung.
    """
    cfg = config or load_config()
    if cfg is None:
        raise StorageUnavailable("keine SOLVIO-Speicherplatte eingerichtet")
    st = state if state is not None else probe(cfg)

    if not st.present:
        raise StorageUnavailable("Speicherplatte nicht angeschlossen")
    # Zwischen „nicht da" und „falsch" wird unterschieden: das erste darf man
    # ueberspringen, das zweite muss auffallen.
    hard = [p for p in st.problems
            if "nicht montiert" not in p and "gesperrt" not in p]
    if hard:
        raise StorageUntrusted("; ".join(hard))
    if st.problems:
        raise StorageUnavailable("; ".join(st.problems))

    assert st.mount_point  # von probe() garantiert, wenn keine Probleme
    return os.path.join(st.mount_point, cfg.root_name)


def write_marker(root: str, config: StorageConfig) -> str:
    """Legt die Marke an. Enthaelt Identitaet, nie Inhalt."""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, MARKER_NAME)
    payload = {
        "volume_uuid": config.volume_uuid,
        "root_name": config.root_name,
        "purpose": "SOLVIO storage volume marker",
        "note": "Diese Datei ist eine Marke, keine Autoritaet. "
                "Ihr Inhalt darf nichts erlauben.",
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


#: Die Struktur auf der Platte. Bewusst flach und bewusst kurz — ein leerer
#: Ordner, den nie jemand fuellt, ist Buerokratie und kein Aufbau.
LAYOUT = (
    "Backups/sets",          # die versionierten Saetze
    "Recovery/manifests",    # Zweitschriften, damit man nicht suchen muss
    "Recovery/restore-tests",# wohin eine Probe auspacken darf
    "Knowledge",             # der lesbare Vault, wenn er umzieht
    "Models/Optional",       # grosse, neu ladbare Modelle
    "Archives",              # Berichte und Artefakte, die niemand mehr braucht
    "Cache/disposable",      # Wegwerfbares. Verlust ist hier kein Datenverlust.
)


def find_candidates() -> list[dict[str, Any]]:
    """Alle externen APFS-Volumes, die als SOLVIO-Speicher in Frage kaemen.

    Nur fuer die Einrichtung durch einen Menschen. Der Betrieb sucht NIE — er
    kennt genau eine UUID.
    """
    out: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir("/Volumes"))
    except OSError:
        return out
    for name in names:
        mount = os.path.join("/Volumes", name)
        info = _diskutil_info(mount)
        if not info:
            continue
        if info.get("Internal") or (info.get("FilesystemType") or "").lower() != "apfs":
            continue
        uuid = info.get("VolumeUUID")
        if not uuid:
            continue
        out.append({
            "volume_uuid": str(uuid).upper(),
            "name": info.get("VolumeName") or name,
            "mount_point": mount,
            "encrypted": bool(info.get("Encryption")),
            "size_bytes": int(info.get("TotalSize") or 0),
        })
    return out


def boot_volume_encrypted() -> bool | None:
    """Ist das STARTVOLUME FileVault-verschluesselt?

    `True`/`False` sind Messungen, **`None` heisst „nicht gemessen"** — und
    ein Aufrufer, der `None` wie `True` behandelt, hat die Regel dieses
    Moduls gebrochen. Der Offsite-Lauf braucht das, weil sein Staging auf der
    internen Platte liegt: dort liegen fuer Minuten die Klartext-Snapshots
    aller Speicher, und die duerfen nur auf verschluesseltem Grund liegen
    (Vertrag §7, fail-closed `staging_unprotected`).

    Gelesen wird dasselbe `diskutil`-Feld wie fuer die externe Platte, nur
    fuer `/`: dort ist `FileVault` das aussagekraeftige Feld (bei den
    externen Volumes ist es immer False, deshalb steht dort `Encryption`).
    """
    info = _diskutil_info("/")
    if not info:
        return None
    for key in ("FileVault", "Encryption"):
        value = info.get(key)
        if value is not None:
            return bool(value)
    return None


def free_bytes_at(path: str) -> int:
    """Freier Platz am Pfad. 0 heisst „nicht messbar", nie „unbegrenzt"."""
    _total, free = _statvfs(path)
    return free


def secure_dir(path: str) -> str:
    """Legt ein Verzeichnis an und macht es privat. Idempotent.

    `0700` ueberall, und zwar auf JEDER Ebene: der erste echte Lauf hat
    `Backups/` als `0755` hinterlassen, weil nur das Blatt gesetzt wurde. Die
    Wurzel darueber war zwar dicht, aber eine Sicherung, deren Schutz an genau
    einem Verzeichnis haengt, ist eine Sicherung mit einem Einzelpunkt.

    Wirkt nur, wenn das Volume Eigentuemerrechte durchsetzt — mit
    `Owners: Disabled` ignoriert macOS die Bits. Deshalb steht dieser Zustand in
    jedem Manifest, statt still angenommen zu werden.
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def create_layout(root: str, config: StorageConfig) -> list[str]:
    """Legt die Struktur an und setzt die Marke. Idempotent.

    Rechte `0700` auf allem: die Verschluesselung schuetzt die Platte im
    Ruhezustand, die Rechte schuetzen sie im Betrieb. Auf einem Volume mit
    abgeschalteten Eigentuemern (`Owners: Disabled`) wirken sie allerdings
    nicht — deshalb steht der Eigentuemerzustand in jedem Manifest, statt still
    angenommen zu werden.
    """
    created: list[str] = []
    secure_dir(root)
    for rel in LAYOUT:
        parts = rel.split("/")
        path = root
        for part in parts:
            path = os.path.join(path, part)
            existed = os.path.isdir(path)
            secure_dir(path)
            if not existed:
                created.append(os.path.relpath(path, root))
    write_marker(root, config)
    return created
