"""Offsite V1 B6 — was eingeschaltet werden darf, und was nicht.

Die Aktivierung ist die dritte und letzte der drei Besitzerhandlungen
(DEBT-0109: „Nichts wird automatisch eingeschaltet"). Diese Suite haelt
die Bedingungen fest, unter denen sie stattfinden darf — und die
Eigenschaften, die der laufende Zeitplan danach haben muss:

* **Das bindende Quellgesundheits-Gate** (§11): ein roter lokaler
  Sicherungsstand verhindert das Einschalten. Eine Offsite-Sicherung auf
  einem kranken Satz traegt den Fehler mit hinaus.
* **Kein Katastropheneinstieg, kein Einschalten:** fehlt der
  Recovery-Umschlag beim Anbieter, ist das Backup im Ernstfall wertlos —
  und das muss BEIM EINSCHALTEN auffallen, nicht wenn jemand ihn braucht.
* **Kein Geheimnis im Zeitplan:** die plist traegt keine Werte, keine
  Umgebungsvariablen, keine Argumente mit Inhalt.
* **Kein Retry-Sturm:** der Backoff ist gedeckelt und endlich.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import atexit
import os
import plistlib
import shutil
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-offsite-b6-")
os.environ["SOLVIO_OFFSITE_DIR"] = os.path.join(_SANDBOX, "offsite")
os.environ["SOLVIO_OFFSITE_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "okeys")
os.environ["SOLVIO_OFFSITE_LEDGER"] = os.path.join(_SANDBOX, "offsite.sqlite3")
os.environ["SOLVIO_STORAGE_STATE_DIR"] = os.path.join(_SANDBOX, "storage")
atexit.register(shutil.rmtree, _SANDBOX, True)

import offsite_admin as OA                                    # noqa: E402
from solvio.storage.offsite import config as OC               # noqa: E402
from solvio.storage.offsite import job as OJ                  # noqa: E402
from solvio.storage.offsite import s3 as OS3                  # noqa: E402


# --------------------------------------------------- das Aktivierungs-Gate
def t_a_red_source_set_blocks_activation() -> None:
    """§11, bindend: kein Einschalten auf einem kranken Quellsatz."""
    import json

    root = tempfile.mkdtemp(prefix="b6-rot-", dir=_SANDBOX)
    sets_dir = os.path.join(root, "Backups", "sets", "20260901-000000")
    os.makedirs(sets_dir)
    with open(os.path.join(sets_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"format_version": 1, "backup_id": "20260901-000000",
                   "ok": False, "errors": ["probe: erfundener Quellfehler"],
                   "entries": []}, fh)

    from solvio.storage import engine as SE, volume as SV
    original_state, original_root = SE.load_state, SV.storage_root
    original_config, original_probe = SV.load_config, SV.probe
    try:
        SE.load_state = lambda: {"consecutive_failures": 0}
        SV.load_config = lambda: object()
        SV.probe = lambda _cfg: object()
        SV.storage_root = lambda _vol, _cfg: root
        ok, detail = OA._source_health()
        require(not ok, "ein roter Quellsatz galt als gesund")
        require("Fehler" in detail, f"unklarer Grund: {detail}")

        # Und die Gegenprobe: derselbe Satz gruen laesst durch.
        with open(os.path.join(sets_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"format_version": 1, "backup_id": "20260901-000000",
                       "ok": True, "errors": [], "entries": []}, fh)
        ok, detail = OA._source_health()
        require(ok, f"ein gesunder Satz wurde abgewiesen: {detail}")
    finally:
        SE.load_state, SV.storage_root = original_state, original_root
        SV.load_config, SV.probe = original_config, original_probe


def t_repeated_failures_block_activation() -> None:
    """Ein Lauf, der wiederholt scheitert, ist kein Fundament."""
    from solvio.storage import engine as SE
    original = SE.load_state
    try:
        SE.load_state = lambda: {"consecutive_failures": 3}
        ok, detail = OA._source_health()
        require(not ok, "drei Fehlschlaege hintereinander galten als gesund")
        require("scheiterte" in detail, f"unklarer Grund: {detail}")
    finally:
        SE.load_state = original


class _Args:
    no_launchd = True


class _Lage:
    """Eine gestellte Lage fuer `cmd_enable` — als Kontextmanager, damit
    jede Attrappe am Ende wieder verschwindet. Eine Suite, die ihre
    Monkeypatches stehen laesst, prueft ab dem zweiten Test sich selbst."""

    def __init__(self, *, quelle_gruen: bool = True,
                 umschlag_lokal: bool = True, umschlag_da: bool = True,
                 identitaet: bool = True, credential: bool = True,
                 baum: str = "/Users/solvio/solvio-core") -> None:
        self.quelle_gruen = quelle_gruen
        #: Aus welchem Baum wird geschaltet. Der Standard ist der produktive —
        #: sonst kaeme jeder Fall unten nur bis Gate 0 und die spaeteren Gates
        #: waeren ungeprueft.
        self.baum = baum
        #: Der Umschlag hat ZWEI Orte, und sie muessen getrennt pruefbar
        #: sein: lokal (fuer Verify/Rotation) und beim Anbieter (der
        #: Katastropheneinstieg). Eine erste Fassung dieses Geschirrs liess
        #: beide gemeinsam fehlen — dann faengt das lokale Gate den Fall ab,
        #: die Anbieter-Pruefung wird nie erreicht, und eine Mutation an ihr
        #: ueberlebt.
        self.umschlag_lokal = umschlag_lokal
        self.umschlag_da = umschlag_da
        self.identitaet = identitaet
        self.credential = credential
        self._alt: dict = {}

    def __enter__(self) -> "_Lage":
        from solvio.storage.offsite import identity as OI
        root = tempfile.mkdtemp(prefix="b6-enable-", dir=_SANDBOX)
        self._alt = {
            "dir": os.environ.get("SOLVIO_OFFSITE_DIR", ""),
            "health": OA._source_health, "present": OI.present,
            "broker": OA.SecretBroker, "client": OS3.S3Client,
            "repo": OA.REPO,
        }
        self._OI = OI
        OA.REPO = self.baum
        os.environ["SOLVIO_OFFSITE_DIR"] = os.path.join(root, "offsite")
        OC.save(OC.fresh("age1" + "q" * 55))
        if self.umschlag_lokal:
            envelope = OC.envelope_path(1)
            os.makedirs(os.path.dirname(envelope), mode=0o700, exist_ok=True)
            with open(envelope, "wb") as fh:
                fh.write(b"age-encryption.org/v1\n")

        gruen, da = self.quelle_gruen, self.umschlag_da
        OA._source_health = lambda: ((True, "probe meldet ok=true") if gruen
                                     else (False, "der juengste Satz meldet "
                                                  "Fehler: probe"))
        OI.present = lambda _v=1: self.identitaet
        OA.SecretBroker = lambda: type(
            "_B", (), {"exists": lambda _s, _r: self.credential})()

        class _Client:
            def __init__(self, **_kw) -> None:
                pass

            def head_object(self, bucket, key):
                if not da:
                    raise OS3.S3NotFound(f"{key} fehlt")
                return OS3.ObjectInfo(key=key, bucket=bucket, size=371)

        OS3.S3Client = _Client
        return self

    def __exit__(self, *_exc) -> bool:
        os.environ["SOLVIO_OFFSITE_DIR"] = self._alt["dir"]
        OA._source_health = self._alt["health"]
        self._OI.present = self._alt["present"]
        OA.SecretBroker = self._alt["broker"]
        OS3.S3Client = self._alt["client"]
        OA.REPO = self._alt["repo"]
        return False

    def enable(self) -> tuple[int, bool]:
        code = OA.cmd_enable(_Args())
        return code, bool(OC.load().enabled)


def t_a_development_worktree_may_not_install_the_scheduler() -> None:
    """Die Freigabe-Grenze, hart gestellt.

    `_render_plist` traegt die Pfade DIESER Installation. Aus einem Worktree
    geschaltet, zeigte der dauerhafte Zeitplan in ein Verzeichnis, das laut
    Projektregel nicht produktiv ist und jederzeit verschwinden darf — das
    Backup fiele dann still aus. Geprueft wird der echte Aufruf und der
    Schalter danach, nicht ein Textvorkommen.
    """
    worktree = ("/Users/solvio/solvio-core/.claude/worktrees/"
                "offsite-encrypted-backup-v1-0f6878")
    with _Lage(baum=worktree) as lage:
        code, an = lage.enable()
    require_equal(code, 1, "ein Worktree durfte den Zeitplan installieren")
    require(not an, "der Schalter ging trotzdem an")

    # Und die Gegenprobe: aus dem produktiven Baum trifft dasselbe Gate nicht.
    with _Lage() as lage:
        code, an = lage.enable()
    require_equal(code, 0,
                  "der produktive Baum wurde faelschlich abgewiesen")
    require(an, "der Schalter blieb aus, obwohl alle Gates hielten")


def t_the_rendered_plist_points_at_the_tree_that_renders_it() -> None:
    """Warum Gate 0 noetig ist: die plist erfindet den Pfad nicht.

    Sie nimmt den Baum, aus dem sie gerendert wird. Damit ist der Ort des
    Aufrufs die einzige Sicherung gegen einen Zeitplan, der ins Leere zeigt.
    """
    import plistlib
    alt = OA.REPO
    try:
        OA.REPO = "/tmp/ein-erfundener-baum"
        payload = plistlib.loads(OA._render_plist())
    finally:
        OA.REPO = alt
    require_equal(payload["WorkingDirectory"], "/tmp/ein-erfundener-baum",
                  "die plist folgt nicht dem Baum, aus dem sie kommt")
    require(payload["ProgramArguments"][0].startswith("/tmp/ein-erfundener-baum"),
            "der Interpreter kommt aus einem anderen Baum als das Arbeitsverzeichnis")


def t_a_red_source_really_prevents_the_switch() -> None:
    """VERHALTEN, nicht Textvorkommen.

    Eine fruehere Fassung dieser Suite prueste nur, dass die Zeichenkette
    `quellsatz_gruen` im Quelltext steht — eine Mutation, die das `if` auf
    `False` setzte, ueberlebte das muehelos. Geprueft wird deshalb der
    ECHTE Aufruf und der Schalter danach.
    """
    with _Lage(quelle_gruen=False) as lage:
        require_equal(OC.load().enabled, False,
                      "die Lage startet eingeschaltet")
        code, an = lage.enable()
    require_equal(code, 1, "ein roter Quellsatz liess das Einschalten zu")
    require_equal(an, False,
                  "der Schalter steht auf AN, obwohl das Gate nicht hielt")


def t_a_missing_envelope_at_the_provider_really_prevents_the_switch() -> None:
    """Ohne Katastropheneinstieg kein Einschalten — §14 Schritt 2.

    Der Umschlag liegt LOKAL, fehlt aber beim Anbieter. Genau diese Lage
    ist die gefaehrliche: alles sieht heil aus, und im Ernstfall stuende
    der Eigentuemer mit der richtigen Passphrase vor einem leeren Prefix.
    """
    with _Lage(umschlag_lokal=True, umschlag_da=False) as lage:
        code, an = lage.enable()
    require_equal(code, 1, "ohne Umschlag beim Anbieter wurde eingeschaltet")
    require_equal(an, False, "der Schalter steht auf AN ohne Umschlag")


def t_a_missing_local_envelope_also_prevents_the_switch() -> None:
    with _Lage(umschlag_lokal=False) as lage:
        code, an = lage.enable()
    require_equal(code, 1, "ohne lokalen Umschlag wurde eingeschaltet")
    require_equal(an, False, "der Schalter steht auf AN ohne Umschlag")


def t_missing_identity_or_credential_prevents_the_switch() -> None:
    for kwargs, was in (({"identitaet": False}, "Identitaet"),
                        ({"credential": False}, "Tresorzugang")):
        with _Lage(**kwargs) as lage:
            code, an = lage.enable()
        require_equal(code, 1, f"ohne {was} wurde eingeschaltet")
        require_equal(an, False, f"der Schalter steht auf AN ohne {was}")


def t_the_switch_only_moves_when_every_gate_held() -> None:
    """Die Gegenprobe: stimmt alles, schaltet es auch wirklich ein."""
    with _Lage() as lage:
        code, an = lage.enable()
    require_equal(code, 0, "eine vollstaendig gesunde Lage wurde abgewiesen")
    require_equal(an, True, "der Schalter blieb aus, obwohl alles stimmte")


# ------------------------------------------------------------- der Zeitplan
def t_the_scheduler_carries_no_secret() -> None:
    """§16: kein `.env`, keine Datei, kein launchd-Environment."""
    payload = plistlib.loads(OA._render_plist())
    require("EnvironmentVariables" not in payload,
            "die plist setzt Umgebungsvariablen — dort landen Geheimnisse")
    text = plistlib.dumps(payload).decode("utf-8", errors="replace").lower()
    for needle in ("secret", "aws_", "access_key", "password", "token",
                   "age-secret"):
        require(needle not in text, f"die gerenderte plist nennt {needle!r}")
    require_equal(payload["Label"], OA.LAUNCH_LABEL, "falsches Label")
    for arg in payload["ProgramArguments"]:
        require("=" not in arg or arg.startswith("-"),
                f"ein Argument sieht aus wie ein Wert: {arg}")


def t_the_scheduler_matches_the_contract() -> None:
    """§11: 07:30 kalendarisch PLUS stuendlicher Retry-Traeger, Hintergrund,
    kein KeepAlive (das baute eine Endlosschleife gegen einen fremden
    Server), kein StartOnMount (Offsite haengt an keiner Platte)."""
    payload = plistlib.loads(OA._render_plist())
    require_equal(payload["StartCalendarInterval"], {"Hour": 7, "Minute": 30},
                  "die kalendarische Startzeit weicht vom Vertrag ab")
    require_equal(payload["StartInterval"], 3600,
                  "der Retry-Traeger ist nicht stuendlich")
    require_equal(payload["KeepAlive"], False,
                  "KeepAlive baut eine Endlosschleife gegen einen fremden "
                  "Server")
    require("StartOnMount" not in payload,
            "Offsite haengt bewusst an keiner Platte")
    require_equal(payload["ProcessType"], "Background", "kein Hintergrundlauf")
    require_equal(payload["LowPriorityIO"], True, "kein LowPriorityIO")
    require_equal(payload["Nice"], 10, "der Job draengelt")
    # Der Renderer bindet den Job exakt an DIESEN Installationsbaum.
    # Dass dessen .venv installiert ist, ist eine Betriebs-Vorbedingung,
    # keine Voraussetzung zum Testen eines isolierten Quellbaums. Hier wird
    # weder eine .venv erzeugt noch ein Scheduler/Interpreter gestartet.
    # Auch ein existierender, aber fremder Interpreter muss durchfallen.
    for installation in (OA.REPO, os.path.join(_SANDBOX, "andere Installation")):
        with patch.object(OA, "REPO", installation):
            rendered = plistlib.loads(OA._render_plist())
        require_equal(rendered["ProgramArguments"],
                      [os.path.join(installation, ".venv", "bin", "python"),
                       "-m", "solvio.storage.offsite.job"],
                      "der Job ist nicht an den Interpreter dieser Installation gebunden")
        require_equal(rendered["WorkingDirectory"], installation,
                      "Interpreter und Arbeitsverzeichnis gehoeren nicht zum selben Baum")


def t_the_retry_is_capped_and_finite() -> None:
    """Kein Retry-Sturm: der Backoff ist eine kurze, endliche Liste."""
    require(len(OS3.RETRY_DELAYS) <= 3,
            f"zu viele Wiederholungen: {OS3.RETRY_DELAYS}")
    require(sum(OS3.RETRY_DELAYS) <= 60,
            f"der Backoff zieht zu lange: {sum(OS3.RETRY_DELAYS)}s")
    require(all(d > 0 for d in OS3.RETRY_DELAYS),
            "eine Wiederholung ohne Pause ist ein Sturm")
    require(list(OS3.RETRY_DELAYS) == sorted(OS3.RETRY_DELAYS),
            "der Backoff waechst nicht")
    # Und der Job selbst wiederholt NICHT — er ueberlaesst das dem
    # naechsten Stundentick.
    source = open(OJ.__file__, encoding="utf-8").read()
    require("while True" not in source, "der Job hat eine Endlosschleife")


def t_disable_unloads_and_says_what_stays() -> None:
    """§21 Rollback: ausschalten geht — loeschen nicht, und das wird gesagt."""
    import ast
    source = open(OA.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    disable = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "cmd_disable")
    block = ast.get_source_segment(source, disable) or ""
    require("bootout" in block, "`disable` entlaedt den Zeitplan nicht")
    require("enabled=False" in block or "enabled=False" in block.replace(" ", ""),
            "`disable` legt den Schalter nicht um")
    require("nichts loeschen" in block,
            "`disable` verschweigt, dass Hochgeladenes liegen bleibt")


def t_activation_is_not_a_config_edit() -> None:
    """Der Schalter gehoert an den kanonischen Pfad, nicht in einen Editor."""
    require(hasattr(OA, "cmd_enable") and hasattr(OA, "cmd_disable"),
            "es gibt keinen kanonischen Ein-/Ausschaltweg")
    fresh = OC.fresh("age1" + "q" * 55)
    require_equal(fresh.enabled, False,
                  "eine frische Konfiguration ist eingeschaltet — DEBT-0109")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
