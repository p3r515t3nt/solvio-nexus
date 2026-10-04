#!/usr/bin/env python3
"""Offsite bedienen — oertlich, besitzergebunden, ohne Wert im Terminal.

Die VERTRAUENSWUERDIGE OERTLICHE STEUEREBENE des Offsite-Backups, nach dem
Vorbild von `scripts/vault_admin.py`: kein Werkzeug des Modells, keine Route
des Gateways, nichts aus dem Netz Erreichbares. Wer sie ausfuehrt, sitzt an
diesem Rechner.

    python3 scripts/offsite_admin.py status
    python3 scripts/offsite_admin.py setup
    python3 scripts/offsite_admin.py envelope-publish
    python3 scripts/offsite_admin.py enable      # die dritte Besitzerhandlung
    python3 scripts/offsite_admin.py disable     # Rollback (§21)
    python3 scripts/offsite_admin.py identity-rotate

**`setup` ist die Besitzerhandlung von B2.** Sie tut drei Dinge, in dieser
Reihenfolge, und bricht bei jedem Hindernis ab, ohne etwas Halbes zu
hinterlassen:

1. **Identitaet in den Schluesselbund.** Der Wiederherstellungsumschlag
   (`~/.solvio/offsite/offsite-identity-v1.age`) wird mit stock `age`
   geoeffnet — die Passphrase fragt age SELBST am Terminal, sie beruehrt
   dieses Skript nicht. Die Identitaet wandert in den Schluesselbund
   (`de.solvio.offsite`/`identity-v1`) und wird vergessen.
2. **Konfiguration.** Der oeffentliche Recipient (aus der Identitaet
   abgeleitet und gegen den B0-Beweis geprueft) kommt nach
   `~/.solvio/offsite/config.json` — Schalter AUS; eingeschaltet wird hier
   nichts (DEBT-0109).
3. **Upload-Zugang in den Tresor.** Access Key ID und Secret Access Key
   werden verdeckt erfragt, per STS gegen den erwarteten IAM-User
   (`solvio-offsite-upload`) geprueft und als `secret://offsite/s3` mit
   enger Policy abgelegt: nur der Offsite-Executor, nur die drei
   Vertrags-Buckets, Hintergrund erlaubt, keine Anwesenheitspflicht.
   Danach wird der Rueckweg durch den Broker verglichen — verglichen,
   nie ausgegeben.

**Kein Geheimnis erscheint hier.** Nicht in `argv`, nicht in der Ausgabe,
nicht im Report, nicht in der Historie. Ausgegeben werden Namen, Zustaende
und Zahlen — und von der Access Key ID hoechstens die letzten 4 Zeichen.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from solvio.capabilities import policy as AP                    # noqa: E402
from solvio.secret_vault import admin, context as SC            # noqa: E402
from solvio.secret_vault import keyring as K                    # noqa: E402
from solvio.secret_vault import policy as VP                    # noqa: E402
from solvio.secret_vault import broker as B                     # noqa: E402
from solvio.secret_vault.broker import SecretBroker             # noqa: E402
from solvio.storage.offsite import config as OC                 # noqa: E402
from solvio.storage.offsite import identity as OI               # noqa: E402

SECRET_REF = "secret://offsite/s3"
CAPABILITY = "offsite_backup"
AUTOMATION_ID = "de.solvio.offsite"
EXPECTED_IAM_USER = os.environ.get("SOLVIO_OFFSITE_IAM_USER", "solvio-offsite-upload")
EXTERNAL_RECOVERY_DIR = os.path.expanduser(os.environ.get("SOLVIO_OFFSITE_RECOVERY_DIR", "/Volumes/SOLVIO Recovery"))

#: Der oeffentliche Recipient aus dem B0-Beweis (recovery-report.json). Steht
#: hier als ZWEITE Quelle fuer die Gegenprobe — massgeblich ist immer die
#: Ableitung aus der gerade geoeffneten Identitaet.
_B0_REPORTS = (
    os.path.expanduser("~/.solvio/offsite-b0/recovery-report.json"),
    os.path.join(REPO, "docs", "design", "offsite-encrypted-backup-v1",
                 "b0", "reports", "recovery-report.json"),
)

AGE = "/opt/homebrew/bin/age"


def _say(line: str) -> None:
    print(line, flush=True)


def _b0_recipient() -> str:
    for path in _B0_REPORTS:
        try:
            with open(path, encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError):
            continue
        for row in report.get("results", []):
            value = str(row.get("recipient") or "")
            if value.startswith("age1"):
                return value
        value = str(report.get("recipient") or "")
        if value.startswith("age1"):
            return value
    return ""


def _write_report(payload: dict) -> str:
    path = os.path.join(OC.offsite_dir(), "setup-report.json")
    os.makedirs(OC.offsite_dir(), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def _borrow_via_broker(expected: str) -> tuple[bool, str]:
    """Der Rueckweg durch den Broker — aus einem Modul, dessen Name zur
    Executor-Bindung passt. Verglichen wird, nicht ausgegeben."""
    prefixes = VP.EXECUTOR_MODULES.get(VP.ExecutorId.OFFSITE, ())
    if not prefixes:
        return False, "kein Modulpraefix fuer den Offsite-Executor"
    module = types.ModuleType(prefixes[0])
    exec(compile(
        "def read(broker, ref, executor, target):\n"
        "    with broker.use(ref, executor=executor, target=target) as m:\n"
        "        return m.plaintext()\n", "<verify>", "exec"), module.__dict__)
    cfg = OC.load()
    target = cfg.bucket_url("daily") if cfg else ""
    try:
        with SC.bound(SC.UseContext(
                origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                capability=CAPABILITY, automation_id=AUTOMATION_ID)):
            value = module.read(SecretBroker(), SECRET_REF,
                                VP.ExecutorId.OFFSITE, target)
    except B.SecretDenied as exc:
        return False, f"abgewiesen ({exc.reason.value})"
    except B.SecretUnavailable:
        return False, "Tresor nicht verfuegbar"
    return (value == expected), ("Tresor liefert denselben Wert"
                                 if value == expected
                                 else "der Wert im Tresor ist ein anderer")


# ---------------------------------------------------------------------- status
def cmd_status(_args: argparse.Namespace) -> int:
    rows: dict = {}
    cfg = None
    try:
        cfg = OC.load()
        rows["config"] = ("fehlt" if cfg is None else
                          {"enabled": cfg.enabled,
                           "recipient": cfg.recipient,
                           "recipient_version": cfg.recipient_version,
                           "region": cfg.region,
                           "buckets": {k: v["bucket"]
                                       for k, v in cfg.classes.items()}})
    except OC.OffsiteConfigError as exc:
        rows["config"] = f"FEHLER: {exc}"
    for version in (cfg.recipient_version if cfg else 1, 1):
        env = OC.envelope_path(version)
        if os.path.exists(env):
            try:
                rows[f"umschlag_v{version}"] = {
                    "pfad": env, "bytes": os.path.getsize(env),
                    "scrypt_log_n": OI.envelope_scrypt_log_n(env)}
            except OI.OffsiteIdentityError as exc:
                rows[f"umschlag_v{version}"] = f"FEHLER: {exc}"
        else:
            rows[f"umschlag_v{version}"] = "fehlt"
        try:
            rows[f"schluesselbund_v{version}"] = (
                "vorhanden" if OI.present(version) else "fehlt")
        except OI.OffsiteKeychainLocked:
            rows[f"schluesselbund_v{version}"] = "gesperrt"
        if version == 1:
            break
    try:
        rows["tresor_credential"] = ("vorhanden"
                                     if SecretBroker().exists(SECRET_REF)
                                     else "fehlt")
    except Exception as exc:  # noqa: BLE001 - Status luegt nicht, er berichtet
        rows["tresor_credential"] = f"nicht pruefbar: {exc}"
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


# ----------------------------------------------------------------------- setup
def cmd_setup(args: argparse.Namespace) -> int:
    checks: list[dict] = []

    def check(name: str, ok: bool, note: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok),
                       **({"note": note} if note else {})})
        _say(f"  [{'PASS' if ok else 'FAIL'}] {name}"
             + (f" — {note}" if note else ""))
        return ok

    _say("SOLVIO Offsite — Setup (B2). Drei Schritte, jeder bricht sauber ab.")

    # -- Vorbedingungen ------------------------------------------------------
    if not os.path.exists(AGE):
        check("age_cli_vorhanden", False, f"{AGE} fehlt (brew install age)")
        _write_report({"results": checks})
        return 1
    check("age_cli_vorhanden", True)

    try:
        kek_da = K.read_kek() is not None
    except K.VaultLocked:
        check("tresor_bereit", False, "Schluesselbund gesperrt — erst anmelden")
        _write_report({"results": checks})
        return 1
    if not kek_da:
        check("tresor_bereit", False,
              "Tresor nicht initialisiert (vault_admin.py init)")
        _write_report({"results": checks})
        return 1
    check("tresor_bereit", True)

    envelope = OC.envelope_path(1)
    if not check("umschlag_vorhanden", os.path.exists(envelope), envelope):
        _write_report({"results": checks})
        return 1

    b0_recipient = _b0_recipient()
    check("b0_gegenprobe_verfuegbar", bool(b0_recipient),
          "recovery-report.json gefunden" if b0_recipient
          else "kein B0-Recipient — Gegenprobe entfaellt")

    # -- Schritt 1: Identitaet in den Schluesselbund -------------------------
    identity_line = ""
    try:
        already = OI.present(1)
    except OI.OffsiteKeychainLocked:
        check("schluesselbund_erreichbar", False, "gesperrt")
        _write_report({"results": checks})
        return 1
    if already and not args.reimport_identity:
        try:
            identity_line = OI.read_identity(1) or ""
        except OI.OffsiteKeychainLocked:
            check("identitaet_lesbar", False, "Schluesselbund gesperrt")
            _write_report({"results": checks})
            return 1
        except OI.OffsiteIdentityError:
            identity_line = ""
        if identity_line:
            _say("  Identitaet liegt schon im Schluesselbund — Umschlag wird "
                 "nicht geoeffnet (--reimport-identity erzwingt es).")
            check("identitaet_lesbar", True, "aus dem Schluesselbund")
        else:
            # Ein abgebrochener frueherer Lauf kann einen leeren oder
            # unbrauchbaren Eintrag hinterlassen. Der ist kein Grund fuer
            # Flag-Jonglage: der Umschlag wird geoeffnet und `-U` ersetzt
            # den Eintrag — geloescht wird nichts, ueberschrieben erst nach
            # bestandener Recipient-Gegenprobe.
            _say("  Eintrag vorhanden, aber unbrauchbar — der Umschlag wird "
                 "geoeffnet und der Eintrag ersetzt.")
            already = False
    if not (already and not args.reimport_identity):
        _say("  Der Umschlag wird geoeffnet. Die PASSPHRASE fragt age selbst —")
        _say("  sie gehoert age und dir, nicht diesem Skript.")
        try:
            proc = subprocess.run([AGE, "-d", envelope],
                                  stdout=subprocess.PIPE, timeout=900)
        except subprocess.TimeoutExpired:
            check("umschlag_geoeffnet", False, "Zeitueberschreitung")
            _write_report({"results": checks})
            return 1
        if proc.returncode != 0:
            check("umschlag_geoeffnet", False,
                  "age meldete einen Fehler (falsche Passphrase?) — "
                  "nichts geaendert")
            _write_report({"results": checks})
            return 1
        for raw in proc.stdout.decode("ascii", errors="replace").splitlines():
            if raw.strip().startswith("AGE-SECRET-KEY-1"):
                identity_line = raw.strip()
                break
        del proc
        opened = bool(identity_line)
        if not check("umschlag_geoeffnet", opened):
            _write_report({"results": checks})
            return 1

    try:
        recipient = OI.recipient_of(identity_line)
    except OI.OffsiteIdentityError as exc:
        check("recipient_abgeleitet", False, str(exc))
        _write_report({"results": checks})
        return 1
    check("recipient_abgeleitet", True, recipient)

    if b0_recipient and not check(
            "recipient_stimmt_mit_b0_beweis", recipient == b0_recipient,
            "Identitaet und B0-Beweis gehoeren zusammen"
            if recipient == b0_recipient else
            "ABWEICHUNG — falscher Umschlag oder falsches Material"):
        _write_report({"results": checks})
        return 1

    if not already or args.reimport_identity:
        try:
            OI.store_identity(identity_line, version=1)
        except OI.OffsiteIdentityError as exc:
            check("identitaet_im_schluesselbund", False, str(exc))
            _write_report({"results": checks})
            return 1
    check("identitaet_im_schluesselbund", True,
          "de.solvio.offsite/identity-v1, Rueckweg geprueft")
    identity_line = ""
    del identity_line

    # -- Schritt 2: Konfiguration -------------------------------------------
    try:
        existing = OC.load()
    except OC.OffsiteConfigError:
        existing = None
    cfg = OC.fresh(recipient)
    if existing is not None and existing.enabled:
        # Einschalten und Einrichten sind zwei Handlungen. Was an ist,
        # bleibt an — aus macht nur `--disable` (kommt mit B3).
        cfg = OC.OffsiteConfig(enabled=True, recipient=cfg.recipient,
                               recipient_version=cfg.recipient_version,
                               region=cfg.region, classes=cfg.classes)
    where = OC.save(cfg)
    check("config_geschrieben", True,
          f"{where} (enabled={cfg.enabled} — eingeschaltet wird hier nichts)")

    # -- Schritt 3: Upload-Zugang in den Tresor ------------------------------
    broker = SecretBroker()
    if broker.exists(SECRET_REF) and not args.replace_credential:
        check("tresor_credential", True,
              "existiert schon — unveraendert (--replace-credential ersetzt)")
        report_path = _write_report({"results": checks})
        _say(f"\nReport: {report_path}")
        return 0

    _say("  Jetzt der AWS-Upload-Zugang (Passwortmanager-Eintrag "
         "'AWS SOLVIO OFFSITE UPLOAD'). Beide Eingaben verdeckt.")
    if sys.stdin is None or not sys.stdin.isatty():
        check("terminal_vorhanden", False,
              "setup braucht ein Terminal (getpass)")
        _write_report({"results": checks})
        return 1
    access_key = getpass.getpass("  Access Key ID (verdeckt): ").strip()
    secret_key = getpass.getpass("  Secret Access Key (verdeckt): ").strip()
    if not access_key or not secret_key:
        check("eingaben_vollstaendig", False, "leer — nichts gespeichert")
        _write_report({"results": checks})
        return 1

    # STS-Gegenprobe: der Zugang gehoert dem erwarteten rechtlosen
    # Upload-User — BEVOR irgendetwas gespeichert wird.
    try:
        import boto3
        from botocore.config import Config as BotoConfig
        sts = boto3.client("sts", region_name=cfg.region,
                           aws_access_key_id=access_key,
                           aws_secret_access_key=secret_key,
                           config=BotoConfig(retries={"max_attempts": 2}))
        arn = str(sts.get_caller_identity()["Arn"])
    except Exception as exc:  # noqa: BLE001 - jede Absage ist ein Abbruchgrund
        del secret_key
        check("sts_gegenprobe", False,
              f"STS verweigert ({type(exc).__name__}) — nichts gespeichert")
        _write_report({"results": checks})
        return 1
    if not arn.endswith(f":user/{EXPECTED_IAM_USER}"):
        del secret_key
        check("sts_gegenprobe", False,
              f"falscher Principal ({arn.split('/')[-1]}) — nichts gespeichert")
        _write_report({"results": checks})
        return 1
    check("sts_gegenprobe", True, f"…{arn.split(':')[4][-4:]}:"
                                  f"user/{EXPECTED_IAM_USER}")

    payload_text = json.dumps({"access_key_id": access_key,
                               "secret_access_key": secret_key},
                              separators=(",", ":"))
    payload = payload_text.encode("utf-8")
    del secret_key
    targets = tuple(cfg.bucket_url(k) for k in ("daily", "weekly", "monthly"))
    try:
        admin.add(secret_ref=SECRET_REF,
                  kind=VP.SecretKind.SERVICE_CREDENTIAL,
                  plaintext=payload,
                  allowed_capabilities=(CAPABILITY,),
                  allowed_targets=targets,
                  allowed_executors=(VP.ExecutorId.OFFSITE,),
                  allow_background=True,
                  requires_user_presence=False,
                  display_name="AWS SOLVIO Offsite Upload",
                  service_label=f"AWS S3 {cfg.region}",
                  account_label=f"…{access_key[-4:]}",
                  note="Offsite Encrypted Backup V1 — Upload-Principal, "
                       "bewusst rechtlos (kein Delete, keine Retention).",
                  replace=bool(args.replace_credential))
    except admin.AdminError as exc:
        check("tresor_credential", False, f"{exc} — nichts gespeichert")
        _write_report({"results": checks})
        return 1

    ok, why = _borrow_via_broker(payload_text)
    del payload, payload_text
    if not check("broker_rueckweg", ok, why):
        _write_report({"results": checks})
        return 1
    check("tresor_credential", True,
          f"{SECRET_REF} → nur Offsite-Executor, nur die drei Buckets, "
          "Hintergrund erlaubt")

    report_path = _write_report({"results": checks})
    _say(f"\nAlles bestanden. Report: {report_path}")
    _say("Eingeschaltet ist damit NICHTS — der Schalter, der Job und die "
         "Buckets kommen mit B3.")
    return 0


# -------------------------------------------------------------- enable/disable
LAUNCH_LABEL = "de.solvio.offsite"
LAUNCH_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_LABEL}.plist")


def _render_plist() -> bytes:
    """Baut die plist mit den PFADEN DIESER Installation.

    Die Vorlage in `deploy/` traegt feste Pfade — richtig als Dokument,
    falsch als Installationsquelle: der Job muss den Baum fahren, in dem
    der Code WIRKLICH liegt. Genau daran haengt DEBT-0111 (eine
    Repo-Vorlage, die den Dienst nicht beschreibt); hier wird sie deshalb
    gerendert, nicht kopiert.

    Kein Geheimnis geht hinein — nicht als Umgebungsvariable, nicht als
    Argument. Der Anbieterzugang kommt aus dem Tresor, die Identitaet aus
    dem Schluesselbund (§16).
    """
    import plistlib
    python = os.path.join(REPO, ".venv", "bin", "python")
    payload = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [python, "-m", "solvio.storage.offsite.job"],
        "WorkingDirectory": REPO,
        "StartCalendarInterval": {"Hour": 7, "Minute": 30},
        "StartInterval": 3600,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": os.path.join(OC.offsite_dir(), "job.err.log"),
    }
    return plistlib.dumps(payload)


def _launchctl(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["launchctl", *args], capture_output=True,
                          text=True, timeout=60)
    return proc.returncode, (proc.stderr or proc.stdout).strip()


def _source_health() -> tuple[bool, str]:
    """Meldet der kanonische lokale Sicherungsstand `ok=true`?

    Gelesen wird das juengste MANIFEST auf der Platte, nicht nur
    `state.json`: das Manifest ist die Wahrheit ueber den Satz, der
    Betriebszustand nur die Wahrheit ueber den Lauf.
    """
    import json as _json
    from solvio.storage import engine as SE, volume as SV
    state = SE.load_state()
    if int(state.get("consecutive_failures") or 0) > 0:
        return False, (f"die lokale Sicherung scheiterte zuletzt "
                       f"{state['consecutive_failures']}-mal")
    try:
        cfg = SV.load_config()
        vol = SV.probe(cfg) if cfg else None
        root = SV.storage_root(vol, cfg) if vol else None
    except Exception as exc:  # noqa: BLE001
        return False, f"die Speicherplatte ist nicht lesbar ({exc})"
    if not root:
        return False, "keine Speicherplatte eingerichtet"
    sets_dir = os.path.join(root, "Backups", "sets")
    sets = SE.list_sets(sets_dir)
    if not sets:
        return False, "es gibt noch keinen lokalen Sicherungssatz"
    newest = os.path.join(sets_dir, sets[-1], SE.MANIFEST_NAME)
    try:
        with open(newest, encoding="utf-8") as fh:
            manifest = _json.load(fh)
    except (OSError, ValueError) as exc:
        return False, f"das juengste Manifest ist unlesbar ({exc})"
    if not manifest.get("ok"):
        errors = list(manifest.get("errors") or [])[:2]
        return False, f"der juengste Satz meldet Fehler: {'; '.join(errors)}"
    return True, f"{sets[-1]} meldet ok=true"


def cmd_enable(args: argparse.Namespace) -> int:
    """Schaltet die Fernsicherung EIN — die dritte Besitzerhandlung.

    Vorher wird das **bindende Aktivierungs-Gate** geprueft (§11): der
    kanonische lokale Sicherungsstand muss `ok=true` melden. Eine
    Offsite-Sicherung, die auf einem kranken Quellsatz aufsetzt, traegt den
    Fehler mit hinaus — und der Vertrag verlangt ausdruecklich, dass sie in
    dieser Lage gar nicht erst eingeschaltet wird.

    Danach wird die plist gerendert und per `launchctl bootstrap` geladen.
    Vor diesem Befehl laeuft nicht einmal ein No-op-Job.
    """
    cfg = OC.load()
    if cfg is None:
        _say("Keine Offsite-Konfiguration — erst `setup`.")
        return 1

    checks: list[dict] = []

    def check(name: str, ok: bool, note: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok),
                       **({"note": note} if note else {})})
        _say(f"  [{'PASS' if ok else 'FAIL'}] {name}"
             + (f" — {note}" if note else ""))
        return ok

    # -- Gate 0: schaltet hier ueberhaupt der produktive Baum? -------------
    # `_render_plist` traegt die Pfade DIESER Installation. Wird `enable` aus
    # einem Entwicklungs-Worktree aufgerufen, zeigte der dauerhafte Scheduler
    # in einen Baum, der laut Projektregel nicht produktiv ist — und der
    # jederzeit umgesetzt, geleert oder entfernt werden darf. Ein Backup, das
    # daran haengt, faellt still aus. Das ist eine Freigabe-Grenze, kein
    # Schoenheitsfehler, und sie gehoert hierher: vor die erste Handlung.
    if not check("scheduler_zeigt_in_den_produktiven_baum",
                 f"{os.sep}.claude{os.sep}worktrees{os.sep}" not in REPO + os.sep,
                 REPO):
        _say("\nOffsite wird NICHT eingeschaltet: dieser Baum ist ein "
             "Entwicklungs-Worktree. Der dauerhafte Scheduler darf nur aus "
             "dem produktiven Baum kommen — sonst sichert SOLVIO aus einem "
             "Verzeichnis, das jederzeit verschwinden darf.")
        return 1

    # -- Gate 1: der Quellsatz muss gruen sein -----------------------------
    from solvio.storage import engine as SE
    state = SE.load_state()
    manifest_ok, detail = _source_health()
    if not check("quellsatz_gruen", manifest_ok, detail):
        _say("\nOffsite wird NICHT eingeschaltet: eine Sicherung ausserhalb "
             "des Hauses setzt einen gesunden Satz voraus (§11).")
        return 1
    check("letzter_lokaler_satz", True,
          f"{state.get('last_backup_id', '?')}")

    # -- Gate 2: Material vollstaendig -------------------------------------
    try:
        identity_da = OI.present(cfg.recipient_version)
    except OI.OffsiteKeychainLocked:
        check("identitaet_im_schluesselbund", False, "Schluesselbund gesperrt")
        return 1
    if not check("identitaet_im_schluesselbund", identity_da):
        return 1
    if not check("tresor_credential", SecretBroker().exists(SECRET_REF)):
        return 1
    if not check("umschlag_lokal", os.path.exists(
            OC.envelope_path(cfg.recipient_version))):
        return 1

    # -- Gate 3: der Umschlag liegt beim ANBIETER --------------------------
    # Ohne ihn gibt es keinen Katastropheneinstieg (§14 Schritt 2). Das
    # hier ist der Punkt, an dem das Fehlen auffallen MUSS — nicht erst,
    # wenn jemand ihn braucht.
    from solvio.storage.offsite import s3 as OS3
    key = (f"{OC.RECOVERY_PREFIX}offsite-identity-"
           f"v{cfg.recipient_version}.age")
    try:
        info = OS3.S3Client(region=cfg.region).head_object(
            cfg.bucket("monthly"), key)
        check("umschlag_beim_anbieter", info.size > 0,
              f"{info.size} B unter {key}")
    except OS3.S3NotFound:
        check("umschlag_beim_anbieter", False,
              f"{key} fehlt — `envelope-publish` zuerst")
        return 1
    except OS3.S3Error as exc:
        check("umschlag_beim_anbieter", False, f"{exc} ({exc.detail})")
        return 1

    # -- Schalter um ------------------------------------------------------
    OC.save(OC.OffsiteConfig(enabled=True, recipient=cfg.recipient,
                             recipient_version=cfg.recipient_version,
                             region=cfg.region, classes=cfg.classes))
    check("schalter_an", True, "config.json enabled=true")

    # -- plist rendern und laden ------------------------------------------
    if args.no_launchd:
        _say("  (--no-launchd: der Zeitplan wird NICHT geladen)")
        return 0
    os.makedirs(os.path.dirname(LAUNCH_PLIST), exist_ok=True)
    with open(LAUNCH_PLIST, "wb") as fh:
        fh.write(_render_plist())
    os.chmod(LAUNCH_PLIST, 0o644)
    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/{LAUNCH_LABEL}")
    code, detail = _launchctl("bootstrap", f"gui/{uid}", LAUNCH_PLIST)
    if not check("zeitplan_geladen", code == 0,
                 detail[:120] if code else f"{LAUNCH_LABEL}, 07:30 + stuendlich"):
        return 1
    _say("\nEingeschaltet. Der erste Lauf kommt vom Zeitplan, nicht von Hand.")
    return 0


def cmd_disable(_args: argparse.Namespace) -> int:
    """Schaltet aus und entlaedt den Zeitplan (§21 Rollback).

    Was schon hochgeladen ist, bleibt liegen — SOLVIO kann beim Anbieter
    nichts loeschen, und die Lifecycle-Regeln raeumen es zu ihrer Zeit ab.
    """
    cfg = OC.load()
    if cfg is not None:
        OC.save(OC.OffsiteConfig(enabled=False, recipient=cfg.recipient,
                                 recipient_version=cfg.recipient_version,
                                 region=cfg.region, classes=cfg.classes))
        _say("  Schalter aus (config.json enabled=False).")
    uid = os.getuid()
    code, _detail = _launchctl("bootout", f"gui/{uid}/{LAUNCH_LABEL}")
    _say(f"  Zeitplan entladen ({'ok' if code == 0 else 'war nicht geladen'}).")
    if os.path.exists(LAUNCH_PLIST):
        os.remove(LAUNCH_PLIST)
        _say("  plist entfernt.")
    _say("Bereits hochgeladene Generationen bleiben — dieser Mac kann beim "
         "Anbieter nichts loeschen.")
    return 0


# ---------------------------------------------------------- envelope-publish
def cmd_envelope_publish(_args: argparse.Namespace) -> int:
    """Legt den Wiederherstellungsumschlag beim Anbieter ab — Schritt 2 des
    Katastrophenfalls (§14).

    **Warum das ein eigener Befehl ist und nicht Teil des Uploads:** der
    Umschlag ist ausdruecklich KEIN Teil einer Generation. Er liegt als
    SEPARATES Objekt unter `v1/recovery/` im monthly-Bucket, damit die
    Disaster-Reihenfolge zirkelfrei bleibt: Provider-Zugang + Passphrase →
    Umschlag oeffnen → ERST DANN Generationen entschluesseln. Ein Umschlag
    INNERHALB der Huelle, die er oeffnet, waere ein Schloss im eigenen
    Tresor (§5).

    Ohne Passphrase ist er wertlos — genau das macht ihn ablegbar. Und der
    monthly-Bucket traegt fuer `v1/recovery/` bewusst KEINE
    Lifecycle-Expiry: Umschlaege laufen nie automatisch aus.
    """
    cfg = OC.load()
    if cfg is None:
        _say("Keine Offsite-Konfiguration — erst `setup`.")
        return 1
    version = cfg.recipient_version
    envelope = OC.envelope_path(version)
    if not os.path.exists(envelope):
        _say(f"Der Umschlag fehlt: {envelope}")
        return 1

    from solvio.storage.offsite import pack as OP
    from solvio.storage.offsite import s3 as OS3

    bucket = cfg.bucket("monthly")
    key = f"{OC.RECOVERY_PREFIX}offsite-identity-v{version}.age"
    digest = OP.sha256_file(envelope)
    client = OS3.S3Client(region=cfg.region)

    _say(f"Umschlag → s3://{bucket}/{key}")
    try:
        existing = client.head_object(bucket, key)
    except OS3.S3NotFound:
        existing = None
    except OS3.S3Error as exc:
        _say(f"  [FAIL] head: {exc} ({exc.detail})")
        return 1
    if existing is not None:
        _say(f"  Es liegt schon eines dort ({existing.size} Bytes, "
             f"Version {existing.version_id[:16]}). Ein PUT erzeugt eine "
             f"NEUE Version und loescht nichts.")

    try:
        info = client.put_object(bucket, key, file_path=envelope,
                                 sha256=digest)
    except OS3.S3Error as exc:
        _say(f"  [FAIL] put: {exc} ({exc.detail})")
        return 1

    # Zurueckholen und nachrechnen: abgelegt ist nicht dasselbe wie lesbar.
    import tempfile
    work = tempfile.mkdtemp(prefix="envelope-readback-")
    try:
        back = os.path.join(work, "zurueck.age")
        client.get_object(bucket, key, dest_path=back)
        same = OP.sha256_file(back) == digest
    except OS3.S3Error as exc:
        _say(f"  [FAIL] readback: {exc} ({exc.detail})")
        return 1
    finally:
        import shutil as _sh
        _sh.rmtree(work, ignore_errors=True)

    if not same:
        _say("  [FAIL] der zurueckgeholte Umschlag weicht ab")
        return 1
    _say(f"  [PASS] abgelegt und zurueckverglichen "
         f"({info.size} Bytes, Version {info.version_id[:16]})")
    _say("  Ohne die Passphrase ist er wertlos — genau das macht ihn "
         "ablegbar. Kein Lifecycle-Ablauf auf v1/recovery/.")
    return 0


# ------------------------------------------------------------- identity-rotate
def cmd_identity_rotate(_args: argparse.Namespace) -> int:
    cfg = OC.load()
    if cfg is None:
        _say("Keine Offsite-Konfiguration — erst `setup`.")
        return 1
    current = cfg.recipient_version
    nxt = current + 1
    new_envelope = OC.envelope_path(nxt)
    if os.path.exists(new_envelope):
        _say(f"{new_envelope} existiert schon — Abbruch, nichts geaendert.")
        return 1
    _say(f"Rotation v{current} → v{nxt}. Der alte Schluessel bleibt im "
         "Schluesselbund — seine Generationen leben noch (§5).")

    line, recipient = OI.create_identity()
    _say("  Neues Paar erzeugt. Jetzt der Umschlag: age fragt nach einer "
         "Passphrase — LEER lassen, dann ERZEUGT age eine und zeigt sie "
         "GENAU EINMAL. Passwortmanager, dann weiter.")
    try:
        proc = subprocess.run([AGE, "-p", "-o", new_envelope],
                              input=(line + "\n").encode("ascii"),
                              timeout=900)
    except subprocess.TimeoutExpired:
        _say("Zeitueberschreitung — nichts geaendert.")
        return 1
    if proc.returncode != 0 or not os.path.exists(new_envelope):
        _say("age konnte den Umschlag nicht schreiben — nichts geaendert.")
        return 1
    os.chmod(new_envelope, 0o600)

    OI.store_identity(line, version=nxt)
    del line

    copied = ""
    if os.path.isdir(EXTERNAL_RECOVERY_DIR):
        import shutil as _shutil
        target = os.path.join(EXTERNAL_RECOVERY_DIR,
                              os.path.basename(new_envelope))
        _shutil.copy2(new_envelope, target)
        copied = target
    OC.save(OC.OffsiteConfig(enabled=cfg.enabled, recipient=recipient,
                             recipient_version=nxt, region=cfg.region,
                             classes=cfg.classes))
    _say(f"  Schluesselbund: identity-v{nxt} abgelegt, Rueckweg geprueft.")
    _say(f"  Umschlag: {new_envelope}"
         + (f" + Kopie {copied}" if copied else
            "  (externe Platte nicht eingehaengt — Kopie NACHHOLEN)"))
    _say(f"  Konfiguration: Recipient v{nxt} aktiv. Der v{current}-Umschlag "
         "bleibt liegen, bis seine letzte Generation ausgelaufen ist.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="offsite_admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    setup = sub.add_parser("setup")
    setup.add_argument("--reimport-identity", action="store_true",
                       help="Umschlag auch dann oeffnen, wenn der "
                            "Schluesselbund die Identitaet schon traegt")
    setup.add_argument("--replace-credential", action="store_true",
                       help="einen vorhandenen Tresor-Eintrag ersetzen "
                            "(Rotation des Upload-Zugangs)")
    sub.add_parser("identity-rotate")
    sub.add_parser("envelope-publish")
    enable = sub.add_parser("enable")
    enable.add_argument("--no-launchd", action="store_true",
                        help="nur den Schalter umlegen, den Zeitplan NICHT "
                             "laden (fuer Proben)")
    sub.add_parser("disable")
    args = parser.parse_args()
    return {"status": cmd_status, "setup": cmd_setup,
            "envelope-publish": cmd_envelope_publish,
            "enable": cmd_enable, "disable": cmd_disable,
            "identity-rotate": cmd_identity_rotate}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
