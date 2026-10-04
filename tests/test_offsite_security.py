"""Offsite Encrypted Backup V1 — was der Transportzugang NIE tut.

Der Upload-Zugang (`secret://offsite/s3`) ist das einzige Geheimnis des
Offsite-Wegs — und er gehoert an genau eine Stelle: das Modul, das
signiert (`solvio.storage.offsite.s3`). Diese Suite friert die Grenzen ein:

* nur dieses eine Modul darf ausleihen — nicht `pack`, nicht `job`, nicht
  irgendein Werkzeug, das die Kennung behauptet,
* nur an die drei Vertrags-Buckets — keine fremde Herkunft, keine fremde
  Region,
* nur aus einem gesetzten Vorgang — wer keine Herkunft hat, bekommt nichts,
* und die Identitaet der Huelle erscheint nie in einem Artefakt (§22).

Alle Werte sind synthetisch. Kein Test beruehrt den produktiven Tresor,
den produktiven Schluesselbund oder das produktive Offsite-Verzeichnis —
der erste Test weigert sich, wenn doch.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import ast
import atexit
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

# Umgelenkt wird VOR jedem Import mit Seiteneffekt — beide Tresor-Haelften
# UND das Offsite-Verzeichnis samt Offsite-Schluesselspeicher.
_SANDBOX = tempfile.mkdtemp(prefix="solvio-offsite-sec-")
os.environ["SOLVIO_VAULT_DIR"] = os.path.join(_SANDBOX, "vault")
os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "keys")
os.environ["SOLVIO_OFFSITE_DIR"] = os.path.join(_SANDBOX, "offsite")
os.environ["SOLVIO_OFFSITE_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "okeys")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.capabilities import policy as AP                 # noqa: E402
from solvio.secret_vault import admin                        # noqa: E402
from solvio.secret_vault import broker as B                  # noqa: E402
from solvio.secret_vault import context as SC                # noqa: E402
from solvio.secret_vault import keyring as K                 # noqa: E402
from solvio.secret_vault import policy as VP                 # noqa: E402
from solvio.secret_vault.store import VaultStore             # noqa: E402
from solvio.storage.offsite import config as OC              # noqa: E402
from solvio.storage.offsite import identity as OI            # noqa: E402
from solvio.storage.offsite import pack as OP                # noqa: E402

#: Synthetisch und scannerfest — kein echter Anbieterschluessel hat diese Form.
FAKE_CREDENTIAL = json.dumps({
    "access_key_id": "SYNTHETIC-DRILL-KEY-0001",
    "secret_access_key": "synthetic-drill-value-0003"}).encode("utf-8")

REF = "secret://offsite/s3"
CAPABILITY = "offsite_backup"
S3_MODULE = "solvio.storage.offsite.s3"
DAILY = "https://solvio-offsite-daily.s3.eu-central-1.amazonaws.com"
WEEKLY = "https://solvio-offsite-weekly.s3.eu-central-1.amazonaws.com"
MONTHLY = "https://solvio-offsite-monthly.s3.eu-central-1.amazonaws.com"


# --------------------------------------------------------------------- Werkzeug
def _fresh() -> VaultStore:
    root = tempfile.mkdtemp(prefix="offsite-sec-case-", dir=_SANDBOX)
    os.environ["SOLVIO_VAULT_DIR"] = root
    os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(root, "keys")
    K.forget_kek()
    store = VaultStore()
    admin.initialize(store)
    return store


def _seed(store: VaultStore, *, background: bool = True):
    """Der Eintrag, wie `offsite_admin.py setup` ihn anlegt — kanonisch."""
    return admin.add(secret_ref=REF,
                     kind=VP.SecretKind.SERVICE_CREDENTIAL,
                     plaintext=FAKE_CREDENTIAL,
                     allowed_capabilities=(CAPABILITY,),
                     allowed_targets=(DAILY, WEEKLY, MONTHLY),
                     allowed_executors=(VP.ExecutorId.OFFSITE,),
                     allow_background=background,
                     requires_user_presence=False,
                     display_name="AWS SOLVIO Offsite Upload",
                     store=store)


def _module(name: str):
    module = types.ModuleType(name)
    exec(compile(
        "def borrow(probe, ref, executor, target):\n"
        "    with probe.use(ref, executor=executor, target=target) as m:\n"
        "        return m.plaintext()\n", "<probe>", "exec"), module.__dict__)
    return module


def _borrow(store, *, module=S3_MODULE, executor=VP.ExecutorId.OFFSITE,
            target=DAILY, capability=CAPABILITY,
            origin=AP.OriginClass.BACKGROUND_AUTOMATION):
    probe = B.SecretBroker(store)
    with SC.bound(SC.UseContext(origin=origin, capability=capability,
                                automation_id="de.solvio.offsite")):
        return _module(module).borrow(probe, REF, executor, target)


# ------------------------------------------------------------ Testisolation
def t_this_suite_cannot_touch_production_state() -> None:
    from solvio.secret_vault.store import vault_dir
    require("/.solvio-vault" not in vault_dir(), "Tresor nicht umgelenkt")
    require(K.is_test_backend(), "Schluesselbund des Tresors nicht umgelenkt")
    require("/.solvio/" not in OC.offsite_dir() + "/",
            "Offsite-Verzeichnis nicht umgelenkt")
    require(OI.is_test_backend(), "Offsite-Schluesselspeicher nicht umgelenkt")


# ------------------------------------------------- die vier Grenzen des Zugangs
def t_only_the_s3_module_may_borrow_the_upload_credential() -> None:
    store = _fresh()
    _seed(store)
    value = _borrow(store)
    require_equal(value, FAKE_CREDENTIAL.decode("utf-8"),
                  "der kanonische Weg liefert nicht den Wert")
    require(json.loads(value)["access_key_id"] == "SYNTHETIC-DRILL-KEY-0001",
            "der Wert ist kein lesbares Credential-JSON")


def t_a_generic_module_claiming_offsite_is_denied() -> None:
    store = _fresh()
    _seed(store)
    exc = require_raises(B.SecretDenied, _borrow, store,
                         module="solvio.tools.exfiltrator",
                         message="ein fremdes Modul bekam den Upload-Zugang")
    require_equal(exc.reason, VP.Denied.EXECUTOR_MODULE_MISMATCH,
                  "abgewiesen, aber aus dem falschen Grund")


def t_pack_and_job_may_not_borrow() -> None:
    """`pack` verschluesselt OHNE Geheimnis, `job` orchestriert nur. Wenn
    einer von beiden den Transportzugang bekommt, ist die Grenze verrutscht."""
    store = _fresh()
    _seed(store)
    for name in ("solvio.storage.offsite.pack", "solvio.storage.offsite.job",
                 "solvio.storage.offsite"):
        exc = require_raises(B.SecretDenied, _borrow, store, module=name,
                             message=f"{name} bekam den Upload-Zugang")
        require_equal(exc.reason, VP.Denied.EXECUTOR_MODULE_MISMATCH,
                      f"{name}: falscher Absagegrund")


def t_a_module_that_merely_starts_like_s3_is_denied() -> None:
    store = _fresh()
    _seed(store)
    exc = require_raises(B.SecretDenied, _borrow, store,
                         module="solvio.storage.offsite.s3_umgehung",
                         message="der nackte Praefixvergleich lebt wieder")
    require_equal(exc.reason, VP.Denied.EXECUTOR_MODULE_MISMATCH,
                  "falscher Absagegrund")


def t_the_wrong_executor_is_denied() -> None:
    store = _fresh()
    _seed(store)
    exc = require_raises(B.SecretDenied, _borrow, store,
                         executor=VP.ExecutorId.HTTP,
                         module="solvio.integrations.generic",
                         message="ein fremder Executor bekam den Zugang")
    require_equal(exc.reason, VP.Denied.EXECUTOR_NOT_ALLOWED,
                  "falscher Absagegrund")


def t_a_foreign_target_is_denied() -> None:
    store = _fresh()
    _seed(store)
    for target in ("https://angreifer.example",
                   "https://solvio-offsite-daily.s3.us-east-1.amazonaws.com",
                   "https://solvio-offsite-daily.s3.eu-central-1.amazonaws.com.evil.example"):
        exc = require_raises(B.SecretDenied, _borrow, store, target=target,
                             message=f"fremdes Ziel {target} wurde bedient")
        require_equal(exc.reason, VP.Denied.TARGET_NOT_ALLOWED,
                      f"{target}: falscher Absagegrund")


def t_an_unset_origin_gets_nothing() -> None:
    store = _fresh()
    _seed(store)
    probe = B.SecretBroker(store)
    exc = require_raises(
        B.SecretDenied, _module(S3_MODULE).borrow, probe, REF,
        VP.ExecutorId.OFFSITE, DAILY,
        message="ohne Vorgang gab es den Upload-Zugang")
    require_equal(exc.reason, VP.Denied.ORIGIN_NOT_ALLOWED,
                  "falscher Absagegrund")


def t_background_use_requires_allow_background() -> None:
    store = _fresh()
    _seed(store, background=False)
    exc = require_raises(B.SecretDenied, _borrow, store,
                         message="Hintergrundlauf trotz allow_background=False")
    require_equal(exc.reason, VP.Denied.BACKGROUND_NOT_ALLOWED,
                  "falscher Absagegrund")


def t_the_wrong_capability_is_denied() -> None:
    store = _fresh()
    _seed(store)
    exc = require_raises(B.SecretDenied, _borrow, store,
                         capability="home_assistant_backup",
                         message="eine fremde Faehigkeit bekam den Zugang")
    require_equal(exc.reason, VP.Denied.CAPABILITY_NOT_ALLOWED,
                  "falscher Absagegrund")


# ----------------------------------------------------------------- Einfrierung
def t_the_offsite_module_binding_is_frozen() -> None:
    """Das Praefix ist EIN Modul, nicht ein Paket. Wer hier erweitert, hat
    die Beweislast — nicht der Test."""
    require_equal(VP.EXECUTOR_MODULES[VP.ExecutorId.OFFSITE],
                  ("solvio.storage.offsite.s3",),
                  "die Offsite-Modulbindung wurde aufgeweicht")


def t_no_offsite_capability_faces_the_model() -> None:
    """Vertrag §11: kein `offsite_*` im Faehigkeitsvertrag — kein Modell kann
    eine Offsite-Sicherung ausloesen, lesen oder loeschen. Der Werkzeugkasten
    des Modells (src/solvio/tools/) kennt das Wort nicht."""
    tools_dir = os.path.join(os.path.dirname(__file__), "..", "src",
                             "solvio", "tools")
    offenders = []
    for name in sorted(os.listdir(tools_dir)):
        if not name.endswith(".py"):
            continue
        if "offsite" in name.lower():
            offenders.append(name)
            continue
        with open(os.path.join(tools_dir, name), encoding="utf-8") as fh:
            if "offsite" in fh.read().lower():
                offenders.append(name)
    require_equal(offenders, [],
                  "das Modell-Werkzeug kennt Offsite — das darf es nicht: "
                  f"{offenders}")


def t_admin_setup_never_prints_key_material() -> None:
    """AST-Zusicherung ueber `scripts/offsite_admin.py`: in keinem Aufruf von
    `_say` oder `check` kommt `access_key` oder `secret_key` vor. Die letzten
    4 Zeichen der Key-ID duerfen ins `account_label` — auf den Schirm darf
    keines von beiden."""
    path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "offsite_admin.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    offenders: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            name = getattr(node.func, "id", "")
            if name in ("_say", "check", "print"):
                for arg in ast.walk(ast.Module(body=[ast.Expr(node)],
                                               type_ignores=[])):
                    if isinstance(arg, ast.Name) and arg.id in (
                            "access_key", "secret_key", "payload",
                            "payload_text", "identity_line"):
                        offenders.append(f"{name}() beruehrt {arg.id} "
                                         f"(Zeile {node.lineno})")
            self.generic_visit(node)

    Visitor().visit(tree)
    require_equal(offenders, [], f"Schluesselmaterial im Terminalpfad: {offenders}")


def t_the_admin_contract_constants_are_frozen() -> None:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    try:
        import offsite_admin as OA
    finally:
        sys.path.pop(0)
    require_equal(OA.SECRET_REF, "secret://offsite/s3", "SecretRef verschoben")
    require_equal(OA.CAPABILITY, "offsite_backup", "Faehigkeitsname verschoben")
    require_equal(OA.AUTOMATION_ID, "de.solvio.offsite", "automation_id verschoben")
    require_equal(OA.EXPECTED_IAM_USER, "solvio-offsite-upload",
                  "der erwartete Upload-Principal verschoben")


# ------------------------------ die Keychain-Write-Naht (Owner-Fund 2026-08-31)
def t_the_keychain_writer_survives_a_controlling_tty() -> None:
    """Der Owner-Lauf hat die Luecke der ersten §25.11-Messung gefunden:
    `add-generic-password -w` ohne Wert prompted mit controlling TTY auf
    /dev/tty und laesst den stdin-Feed liegen — die Messung lief TTY-los
    und sah es nicht. Diese Zusicherung fuehrt den ECHTEN Write-Pfad
    (`security -i` gegen eine Wegwerf-Keychain-DATEI) in einem Kindprozess
    MIT controlling TTY aus: kein Prompt, gedeckelte Zeit, und der
    Recipient-Beweis laeuft ueber die sichere Seam (`recipient_of`) — der
    Wert bleibt im Kind. Faellt der Writer je auf einen interaktiven
    `security`-Prompt zurueck, wird dieser Test rot."""
    import fcntl
    import pty
    import select
    import subprocess
    import termios
    import time

    work = tempfile.mkdtemp(prefix="offsite-kc-", dir=_SANDBOX)
    keychain = os.path.join(work, "wegwerf.keychain-db")
    require(" " not in keychain, "der Testpfad traegt Leerzeichen")
    subprocess.run(["/usr/bin/security", "create-keychain", "-p",
                    "wegwerf-kc-pw", keychain], check=True, timeout=30)
    subprocess.run(["/usr/bin/security", "unlock-keychain", "-p",
                    "wegwerf-kc-pw", keychain], check=True, timeout=30)
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    child_code = (
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "from solvio.storage.offsite import identity as OI\n"
        "line, recipient_a = OI.create_identity()\n"
        "OI.store_identity(line, version=1)\n"
        "back = OI.read_identity(1)\n"
        "assert back == line, 'roundtrip'\n"
        "recipient_b = OI.recipient_of(back)\n"
        "print('ERGEBNIS', recipient_a, recipient_b, flush=True)\n")
    env = {k: v for k, v in os.environ.items()
           if k != OI.TEST_BACKEND_ENV}
    env[OI.TEST_KEYCHAIN_FILE_ENV] = keychain

    master, slave = pty.openpty()

    def make_ctty() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen([sys.executable, "-c", child_code],
                            stdin=subprocess.DEVNULL, stdout=slave,
                            stderr=slave, env=env, preexec_fn=make_ctty,
                            close_fds=False)
    os.close(slave)
    output = b""
    deadline = time.time() + 30
    try:
        while proc.poll() is None and time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.3)
            if ready:
                try:
                    output += os.read(master, 4096)
                except OSError:
                    break
        code = proc.poll()
        if code is None:
            proc.kill()
            proc.wait()
    finally:
        os.close(master)
        subprocess.run(["/usr/bin/security", "delete-keychain", keychain],
                       capture_output=True)

    text = output.decode(errors="replace")
    require("password data" not in text.lower(),
            "der Writer prompted wieder interaktiv — die Owner-Falle lebt")
    require(code == 0,
            f"der Write-Pfad endete nicht sauber (exit={code}): {text[-200:]}")
    tokens = [t for line in text.splitlines() if line.startswith("ERGEBNIS")
              for t in line.split()[1:]]
    require(len(tokens) == 2 and tokens[0] == tokens[1]
            and tokens[0].startswith("age1"),
            "der Recipient-Beweis ueber die Seam fehlt oder widerspricht sich")
    require(not any(t.startswith("AGE-SECRET-KEY-1") for t in text.split()),
            "die Identitaet stand im PTY-Ausgabestrom")


def t_the_write_argv_is_static_and_value_free() -> None:
    """Der Wert reist NIE in argv: die Schreib-argv ist eine eingefrorene
    Konstante, und der Batch-Builder verweigert jedes Token ausserhalb des
    quoting-freien Zeichensatzes."""
    require_equal(OI._WRITE_ARGV, ("/usr/bin/security", "-i"),
                  "die Schreib-argv hat sich veraendert")
    command = OI._write_batch_command("de.solvio.offsite", "identity-v1",
                                      "solvio-offsite-age-identitaet",
                                      "QUJDRA==", None)
    require(command.startswith("add-generic-password ")
            and command.endswith("-w QUJDRA==\n"),
            "das Batch-Kommando hat eine unerwartete Form")
    for bad in ("hat leerzeichen", 'quote"drin', "semikolon;", "tab\tdrin", ""):
        require_raises(OI.OffsiteIdentityError, OI._write_batch_command,
                       "s", "a", "l", bad, None,
                       message=f"unsicheres Token {bad!r} passierte")
    require_raises(OI.OffsiteIdentityError, OI._write_batch_command,
                   "s", "a", "l", "QUJD", "/pfad mit space",
                   message="ein Keychain-Pfad mit Leerzeichen passierte")


# --------------------------------------------- B3: der Transport als Grenze
def t_the_s3_module_is_the_only_place_that_borrows() -> None:
    """Die Bindung lebt davon, dass `use()` woertlich im gebundenen Modul
    steht (der Broker nimmt den Aufrufer per Stack-Inspektion). Ein Helfer
    dazwischen wuerde die Bindung auf den Helfer verschieben — deshalb
    friert diese Zusicherung ein, WO der Aufruf steht."""
    from solvio.storage.offsite import s3 as OS3
    source = open(OS3.__file__, encoding="utf-8").read()
    require(source.count("_broker.use(") == 1,
            "es gibt nicht genau einen Ausleih-Aufruf im S3-Modul")
    tree = ast.parse(source)
    holders = [node.name for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)
               and "_broker.use(" in ast.get_source_segment(source, node)]
    require_equal(holders, ["_credential"],
                  f"der Ausleih-Aufruf wanderte: {holders}")
    for module in ("job", "pack", "ledger", "config", "identity"):
        path = os.path.join(os.path.dirname(OS3.__file__), f"{module}.py")
        text = open(path, encoding="utf-8").read()
        require(".use(" not in text.replace("broker.use", "X"),
                f"{module}.py leiht selbst aus — nur s3.py darf das")


def t_the_transport_never_writes_the_secret_anywhere() -> None:
    """Der Wert darf in keinem Artefakt des Transports auftauchen: nicht im
    Buch, nicht im Log, nicht im Betriebszustand, nicht in einer Ausnahme."""
    import io
    import logging

    from solvio.storage.offsite import job as OJ
    from solvio.storage.offsite import ledger as OL
    from solvio.storage.offsite import s3 as OS3

    secret = "synthetic-drill-value-0003"
    store = _fresh()
    _seed(store)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)
    try:
        # Der Client leiht, signiert und wirft — der Wert darf dabei nirgends
        # sichtbar werden. Ein unerreichbarer Host erzwingt eine Ausnahme.
        client = OS3.S3Client(region="eu-central-1", broker=B.SecretBroker(store),
                              endpoint_suffix="127.0.0.1", port=1, insecure=True,
                              timeout=2.0)
        try:
            with SC.bound(SC.UseContext(
                    origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                    capability=CAPABILITY, automation_id="de.solvio.offsite")):
                client.head_object("solvio-offsite-daily", "v1/generations/x")
        except OS3.S3Error as exc:
            require(secret not in str(exc), "der Wert steht in der Ausnahme")
            require(secret not in repr(exc), "der Wert steht im repr")
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(old_level)
    require(secret not in buf.getvalue(), "der Wert steht in einer Logzeile")

    ledger_file = os.path.join(_SANDBOX, "leak-check.sqlite3")
    book = OL.OffsiteLedger(ledger_file)
    book.start(generation_id="20260902T053000Z", classes=("daily",),
               access_key_id="SYNTHETIC-DRILL-KEY-0001")
    book.mark("20260902T053000Z", OL.FAILED, failure_category="auth_failed",
              error="offsite credential denied (secret_denied:...)")
    raw = open(ledger_file, "rb").read()
    require(secret.encode() not in raw, "der Wert steht im Buch")


def t_the_plist_carries_no_secret_and_no_environment() -> None:
    """§16: kein `.env`, keine Datei, kein launchd-Environment. Die plist
    wird gesichert — sie waere sonst ein Leck mit Zeitplan."""
    import plistlib
    path = os.path.join(os.path.dirname(__file__), "..", "deploy",
                        "de.solvio.offsite.plist")
    with open(path, "rb") as fh:
        payload = plistlib.load(fh)
    require("EnvironmentVariables" not in payload,
            "die plist setzt Umgebungsvariablen — dort landen Geheimnisse")
    text = open(path, encoding="utf-8").read().lower()
    for needle in ("secret", "aws_", "access_key", "token", "password"):
        require(needle not in text.replace("secret://", "").replace(
                "geheimnistresor", "").replace("secret-", ""),
                f"die plist nennt {needle!r}")
    require_equal(payload["Label"], "de.solvio.offsite", "falsches Label")
    require_equal(payload.get("KeepAlive"), False,
                  "KeepAlive baut hier eine Endlosschleife gegen einen "
                  "fremden Server")
    require("StartOnMount" not in payload,
            "Offsite haengt bewusst an keiner Platte")


def t_no_offsite_capability_reaches_the_model() -> None:
    """Kein Modell kann eine Offsite-Sicherung ausloesen, lesen oder
    anhalten (§11) — und diese Zusicherung sagt jetzt genauer, was das heisst.

    **Die Linie hat sich in B4 verschoben, mit Grund.** Die erste Fassung
    verbot das WORT „offsite" ueberall unter `capabilities/`. Das war zu
    breit und widersprach dem Vertrag selbst: §12 verlangt ausdruecklich,
    dass `offsite` als eigener Punkt im Kontrollzentrum erscheint und dass
    der Befund beide Zeitstempel traegt — dafuer braucht die Arzt-Faehigkeit
    einen Anzeigenamen und einen gesprochenen Namen. Ein Test, der das
    verbietet, verbietet eine Vertragsforderung.

    Verboten bleibt, worauf es ankommt, und das wird hier einzeln geprueft:

    1. kein `offsite_*` im Faehigkeitsvertrag — es gibt keine Faehigkeit,
       die eine Offsite-Sicherung ausloest oder ihren Inhalt liest;
    2. kein Werkzeug im Werkzeugkasten des Modells kennt Offsite;
    3. `system_heal` kann an der Fernsicherung strukturell nichts tun —
       kein Playbook, und das Verbot steht geschrieben.

    Was ERLAUBT ist und bleibt: der Zustand darf gelesen werden. „Wie steht
    es um meine Fernsicherung?" ist eine Frage, keine Handlung.
    """
    src_root = os.path.join(os.path.dirname(__file__), "..", "src", "solvio")

    # 1. Keine Faehigkeit mit einem Offsite-Namen — nirgends.
    from solvio.capabilities.doctor import SPECS as DOCTOR_SPECS
    require_equal([n for n in DOCTOR_SPECS if "offsite" in n], [],
                  "es gibt eine Arzt-Faehigkeit mit Offsite-Namen")
    named: list[str] = []
    for dirpath, dirs, files in os.walk(os.path.join(src_root, "capabilities")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            text = open(os.path.join(dirpath, name), encoding="utf-8").read()
            for needle in ('"offsite_', "'offsite_", "offsite_backup",
                           "offsite_run", "offsite_restore"):
                if needle in text:
                    named.append(f"capabilities/{name}:{needle}")
    require_equal(named, [],
                  f"eine Faehigkeit traegt einen Offsite-Namen: {named}")

    # 2. Der Werkzeugkasten des Modells bleibt zu — hier gilt das Wortverbot
    #    weiter, denn ein Werkzeug ist immer eine HANDLUNG.
    tools = os.path.join(src_root, "tools")
    offenders = [f"tools/{name}" for name in sorted(os.listdir(tools))
                 if name.endswith(".py")
                 and "offsite" in open(os.path.join(tools, name),
                                       encoding="utf-8").read().lower()]
    require_equal(offenders, [],
                  f"Offsite ist im Modell-Werkzeug sichtbar: {offenders}")

    # 3. Und heilen kann das Modell an der Fernsicherung nichts.
    from solvio.doctor import playbooks as PB
    require_equal(PB.for_component("offsite"), [],
                  "es gibt ein Playbook, das ein Modell ausloesen koennte")
    require("offsite" in PB.FORBIDDEN_RESTARTS,
            "das Neustart-Verbot fuer die Fernsicherung fehlt")


# ------------------------------------------------------- §22: nichts leckt
def t_the_identity_never_appears_in_pack_artifacts() -> None:
    """Grep-Zusicherung (§22): die Identitaet erscheint nie im Archiv, nie im
    Staging, nie im Offsite-Verzeichnis, nie in einer Logzeile."""
    line, recipient = OI.create_identity()
    work = tempfile.mkdtemp(prefix="offsite-leak-", dir=_SANDBOX)
    staging = os.path.join(work, "staging")
    os.makedirs(staging)
    with open(os.path.join(staging, "manifest.json"), "w") as fh:
        json.dump({"format_version": 1, "backup_id": "probe"}, fh)
    with open(os.path.join(staging, "daten.bin"), "wb") as fh:
        fh.write(os.urandom(1 << 18))

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        archive = os.path.join(work, "g.tar.zst.age")
        OP.pack(staging, recipient=recipient, out_path=archive)
        OP.unpack(archive, identity_line=line,
                  dest_dir=os.path.join(work, "zurueck"))
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    needle = line.encode("ascii")
    require(needle not in open(archive, "rb").read(),
            "die Identitaet steht im Archiv")
    for dirpath, _dirs, files in os.walk(work):
        for name in files:
            if name.endswith(".age"):
                continue
            with open(os.path.join(dirpath, name), "rb") as fh:
                require(needle not in fh.read(),
                        f"die Identitaet steht in {name}")
    require(line not in buf.getvalue(), "die Identitaet steht in einer Logzeile")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
