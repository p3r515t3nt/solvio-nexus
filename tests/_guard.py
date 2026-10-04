"""Assertion integrity guard — imported for its side effect.

P1A.4/C2. Most suites in this repo use bare `assert`, which `python -O` / `PYTHONOPTIMIZE`
strips. That is not a theoretical concern: with a security guard deleted from production,
`python -O tests/test_f5_1_atomic_begin.py` printed `=== 6/6 bestanden ===` and exited 0,
where normal Python printed 5/6. A security suite that cannot fail is worse than no suite —
it manufactures confidence.

Two defences, because neither alone is sufficient:

1. This module refuses to load under optimized Python. It is imported by the shared test
   helper, so every suite that uses it inherits the guard however it is invoked.
2. `require()` below is a real function call. It survives `-O`, so tests written with it
   keep their teeth even if this guard were somehow bypassed.

The guard deliberately does NOT use `assert` — that would be self-defeating.
"""
import os
import sys
import tempfile

# KEIN TEST OEFFNET JE DEN PRODUKTIVEN KONTAKTSPEICHER.
#
# `BindingStore()` ohne Pfad faellt auf `~/.solvio/contacts.sqlite3` zurueck —
# die Datei, aus der der laufende Core liest, wen „mich" und „mein Sohn"
# meinen. Eine Zusicherung tat genau das (`CommunicationCapabilities(gmail=None)`
# ohne Speicher), jahrelang folgenlos, weil das Oeffnen nur `CREATE TABLE IF
# NOT EXISTS` ausfuehrte. Mit der additiven Schema-Nachziehung von Contact
# Binding Authority Hardening V1 wurde daraus beim ersten Suitenlauf eine
# Migration des produktiven Bestands — gemessen am 2026-09-05 an der WAL-Datei,
# rollback-sicher und ohne veraenderte Zeile, aber ein Schreibzugriff eines
# Tests auf Production. Dieser Guard liegt hier, weil ihn jede Suite laedt:
# der Standardpfad zeigt unter Tests auf ein leeres Verzeichnis, und nur eine
# ausdruecklich gesetzte Umgebung kann das aendern.
#
# DEBT-0223: dieselbe Klasse gilt fuer JEDEN Speicher mit Standardpfad im
# Heimverzeichnis. Gemessen am 2026-09-27 in einer leeren Cloud-VM: ein Full
# Gate legte `~/.solvio/{proactive,payments,autopilot}.sqlite3` und
# `~/.solvio-vault/vault.sqlite3` an (`build_dispatcher` ohne eigene Speicher,
# `test_core_shutdown`). Deshalb zeigt unter Tests jede bekannte Pfadvariable
# auf einen leeren Ordner. Gesetzt werden nur PFADE — keine Datei, kein
# Schluesselbund-Schalter, und `HOME` bleibt, wie es ist: Suiten, die auf dem
# Mac bewusst echte Installationsdaten lesen (Hermes, Codex-/Claude-CLI,
# Schluesselbund), tun das ueber ausdrueckliche Heim-Pfade, nie ueber diese
# Standards. Eine ausdruecklich gesetzte Umgebung gewinnt (der Runner setzt
# `SOLVIO_STATE_DIR` und `SOLVIO_STORAGE_STATE_DIR` selbst);
# `tests/test_no_production_state_under_tests.py` sichert zu, dass keiner
# dieser Wege unter Tests ins echte Heimverzeichnis zeigt.
TEST_PATH_VARIABLES = (
    ("SOLVIO_STATE_DIR", "state"),                   # conversations, memory, agent_runs, notes, cognition
    ("SOLVIO_CONTACTS_DB", "contacts.sqlite3"),      # ~/.solvio/contacts.sqlite3
    ("SOLVIO_VAULT_DIR", "vault"),                   # ~/.solvio-vault
    ("SOLVIO_PAYMENT_DB", "payments.sqlite3"),       # ~/.solvio/payments.sqlite3
    ("SOLVIO_PAYMENT_CONFIG", "payment.json"),       # ~/.solvio/payment.json
    ("SOLVIO_PROACTIVE_DB", "proactive.sqlite3"),    # ~/.solvio/proactive.sqlite3
    ("SOLVIO_DOCTOR_DB", "doctor.sqlite3"),          # ~/.solvio/doctor.sqlite3
    ("SOLVIO_AUTOPILOT_DB", "autopilot.sqlite3"),    # ~/.solvio/autopilot.sqlite3 (+ independent/)
    ("SOLVIO_AUTOPILOT_LOCK", "autopilot.lock"),     # ~/.solvio/autopilot.lock
    ("SOLVIO_BROKER_DB", "broker.sqlite3"),          # ~/.solvio/broker.sqlite3
    ("SOLVIO_TELEPHONY_DB", "telephony.sqlite3"),    # ~/.solvio/telephony.sqlite3
    ("SOLVIO_OFFSITE_LEDGER", "offsite.sqlite3"),    # ~/.solvio/offsite.sqlite3
    ("SOLVIO_OFFSITE_DIR", "offsite"),               # ~/.solvio/offsite
    ("SOLVIO_STORAGE_STATE_DIR", "storage"),         # ~/.solvio/storage
    ("SOLVIO_STORAGE_CONFIG", "storage.json"),       # ~/.solvio/storage.json
    ("SOLVIO_APPROVAL_STATE_DIR", "approvals"),      # ~/.solvio-approvals
    ("SOLVIO_PORTAL_VAULT_DIR", "portal"),           # ~/.solvio-portal
    ("SOLVIO_CONTROL_SOCKET_PATH", "control.sock"),  # ~/.solvio/control.sock
    ("SOLVIO_SATELLITE_AUTH_FILE", "satellite_auth.json"),  # ~/.solvio/satellite_auth.json
    ("SOLVIO_VAULT", "knowledge"),                   # ~/SOLVIO Knowledge
)

#: Was dieser Guard selbst gesetzt hat — Name -> Pfad.
REDIRECTED: dict[str, str] = {}

_TEST_ROOT = ""
for _name, _leaf in TEST_PATH_VARIABLES:
    if _name in os.environ:
        continue
    if not _TEST_ROOT:
        # Kurz, weil `control.sock` darunter liegt: AF_UNIX-Pfade haben 104 Bytes.
        _TEST_ROOT = tempfile.mkdtemp(prefix="solvio-th-")
    os.environ[_name] = REDIRECTED[_name] = os.path.join(_TEST_ROOT, _leaf)
if "SOLVIO_STATE_DIR" in REDIRECTED:
    # Ein Zustandsverzeichnis gibt es immer (der Runner legt seines selbst an);
    # ein leerer Ordner, keine Datei.
    os.mkdir(REDIRECTED["SOLVIO_STATE_DIR"], 0o700)


def without_test_paths(environment=None) -> dict:
    """Die Umgebung ohne jede Pfadvariable aus `TEST_PATH_VARIABLES`.

    Nur fuer Pruefungen, die BEWUSST die Installation ansehen, lesend und mit
    Namen (etwa die Klassifikation der echten Eintraege von `~/.solvio`):
    ohne diese Ausnahme saehen sie unter Tests die Testorte statt der
    Installation und wuerden auf dem Mac still etwas anderes pruefen.
    """
    source = os.environ if environment is None else environment
    names = {name for name, _leaf in TEST_PATH_VARIABLES}
    return {key: value for key, value in source.items() if key not in names}

_MESSAGE = (
    "Security regression must not run under optimized Python.\n"
    "  `assert` statements are stripped by -O / PYTHONOPTIMIZE, so these suites would\n"
    "  report success without checking anything.\n"
    f"  sys.flags.optimize = {sys.flags.optimize}\n"
    "  Re-run without -O, e.g.:  python3 scripts/run_tests.py"
)

if sys.flags.optimize != 0:  # pragma: no cover - the whole point is that it exits
    print(_MESSAGE, file=sys.stderr)
    raise SystemExit(2)


def enforce_assertions() -> None:
    """Explicit bootstrap every standalone test entry point calls.

    P1A.5/H2: the previous guard only fired if a suite happened to import
    `mobile_attest_helper`, which covered 11 of 25 suites. Optimize-safety must not be an
    accident of an unrelated import — with the S1 broker's digest binding deleted,
    `python -O tests/test_approval.py` printed 17/17 where normal Python printed 16/17.
    Importing this module already exits; this function makes the dependency explicit and
    survives an import reordering.
    """
    if sys.flags.optimize != 0:  # pragma: no cover
        print(_MESSAGE, file=sys.stderr)
        raise SystemExit(2)


class RequirementFailed(AssertionError):
    """A checked security requirement did not hold."""


def require(condition, message: str = "") -> None:
    """Assert that survives `-O`. Use this in security-critical tests."""
    if not condition:
        raise RequirementFailed(message or "required condition was false")


def require_equal(actual, expected, message: str = "") -> None:
    if actual != expected:
        raise RequirementFailed(
            f"{message or 'values differ'}: expected {expected!r}, got {actual!r}")


def require_raises(exc_types, fn, *args, message: str = "", **kwargs):
    """Run `fn` and require it to raise. Returns the exception for further inspection."""
    try:
        result = fn(*args, **kwargs)
    except exc_types as caught:
        return caught
    raise RequirementFailed(
        f"{message or 'expected an exception'}: {fn!r} returned {result!r} instead")
