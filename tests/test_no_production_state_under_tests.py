"""Kein Test oeffnet je einen produktiven Speicher ueber seinen Standardpfad (DEBT-0223).

Gemessen am 2026-09-27 in einer leeren Cloud-VM: vor diesem Nachzug legte ein Full
Gate `~/.solvio/proactive.sqlite3`, `~/.solvio/payments.sqlite3`,
`~/.solvio/autopilot.sqlite3` und `~/.solvio-vault/vault.sqlite3` an — acht Suiten
bauten `build_dispatcher` ohne eigene Speicher, `test_core_shutdown` das
Autopilot-Buch. Auf dem Mac waeren das die Dateien des laufenden Cores gewesen.

Seither setzt `tests/_guard.py` jede Pfadvariable aus `TEST_PATH_VARIABLES` auf
einen leeren Testordner, bevor eine Suite einen Speicher anfasst. Diese Suite
sichert es je Speicher zu, nach dem Muster von
`t_no_test_can_open_the_production_contact_store`: erst wird der Pfad aufgeloest
und geprueft, erst danach geoeffnet — wer eine Umlenkung entfernt, macht die
zugehoerige Zusicherung rot, BEVOR etwas im Heimverzeichnis entsteht.

`HOME` wird bewusst nicht umgelenkt (Suiten mit echten Installationsdaten auf dem
Mac lesen ueber ausdrueckliche Heim-Pfade); das echte Heimverzeichnis kommt
deshalb aus der Passwortdatenbank, nicht aus `HOME`.
"""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import TEST_PATH_VARIABLES, enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()


def _real_home() -> str:
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


def _require_outside_home(label: str, path: str) -> None:
    """Der Pfad liegt nicht im echten Heimverzeichnis — auch nicht ueber `~` oder Symlinks."""
    require(path, f"{label}: kein Pfad aufgeloest")
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    for home in {_real_home(), os.path.realpath(os.path.expanduser("~"))}:
        require(resolved != home and not resolved.startswith(home + os.sep),
                f"ein Test kann den produktiven Speicher {label} oeffnen: {path}")


def _require_redirected(variable: str, label: str, path: str) -> None:
    """Die Variable ist gesetzt, zeigt nicht ins Heimverzeichnis, und der Speicher folgt ihr."""
    value = os.environ.get(variable, "")
    require(value, f"{variable} ist unter Tests nicht gesetzt — {label} faellt auf den Standard")
    _require_outside_home(label, value)
    require_equal(os.path.realpath(os.path.expanduser(str(path))),
                  os.path.realpath(os.path.expanduser(value)),
                  f"{label} folgt {variable} nicht")


# ------------------------------------------------------------------ je Speicher
def t_no_test_can_open_the_production_secret_vault():
    from solvio.secret_vault.store import vault_dir
    _require_redirected("SOLVIO_VAULT_DIR", "~/.solvio-vault", vault_dir())


def t_no_test_can_open_the_production_payment_store():
    from solvio.payment import config as payment_config
    from solvio.payment.store import db_path
    _require_redirected("SOLVIO_PAYMENT_DB", "payments.sqlite3", db_path())
    _require_redirected("SOLVIO_PAYMENT_CONFIG", "payment.json", payment_config.config_path())


def t_no_test_can_open_the_production_proactive_store():
    from solvio.proactive.store import ProactiveStore, default_path
    _require_redirected("SOLVIO_PROACTIVE_DB", "proactive.sqlite3", default_path())
    store = ProactiveStore()
    _require_redirected("SOLVIO_PROACTIVE_DB", "proactive.sqlite3", store.path)


def t_no_test_can_open_the_production_doctor_store():
    from solvio.doctor.store import DoctorStore, default_path
    _require_redirected("SOLVIO_DOCTOR_DB", "doctor.sqlite3", default_path())
    store = DoctorStore()
    _require_redirected("SOLVIO_DOCTOR_DB", "doctor.sqlite3", store.path)


def t_no_test_can_open_the_production_autopilot_ledger():
    from solvio.autopilot import driver
    from solvio.autopilot.independent import _scratch_root
    from solvio.autopilot.store import resolve_path
    _require_redirected("SOLVIO_AUTOPILOT_DB", "autopilot.sqlite3", resolve_path())
    _require_redirected("SOLVIO_AUTOPILOT_LOCK", "autopilot.lock", driver.lock_path())
    # Der Pruefbaum der unabhaengigen Abnahme folgt dem Buch (DEBT-0234).
    _require_outside_home("~/.solvio/independent",
                          _scratch_root(SimpleNamespace(path=resolve_path())))


def t_no_test_can_open_the_production_broker_ledger():
    from solvio.provider_broker.ledger import resolve_path
    _require_redirected("SOLVIO_BROKER_DB", "broker.sqlite3", resolve_path())


def t_no_test_can_open_the_production_call_ledger():
    from solvio.telephony.ledger import PATH_ENV
    require_equal(PATH_ENV, "SOLVIO_TELEPHONY_DB", "der Anrufbuch-Schalter heisst anders")
    _require_redirected("SOLVIO_TELEPHONY_DB", "telephony.sqlite3",
                        os.environ.get(PATH_ENV, ""))
    from solvio.telephony.ledger import CallLedger
    ledger = CallLedger()
    _require_redirected("SOLVIO_TELEPHONY_DB", "telephony.sqlite3", ledger.path)


def t_no_test_can_open_the_production_offsite_state():
    from solvio.storage.offsite.config import offsite_dir
    from solvio.storage.offsite.ledger import ledger_path
    _require_redirected("SOLVIO_OFFSITE_LEDGER", "offsite.sqlite3", ledger_path())
    _require_redirected("SOLVIO_OFFSITE_DIR", "~/.solvio/offsite", offsite_dir())


def t_no_test_can_open_the_production_backup_state():
    from solvio.storage import engine, volume
    _require_redirected("SOLVIO_STORAGE_STATE_DIR", "~/.solvio/storage", engine.state_dir())
    _require_redirected("SOLVIO_STORAGE_CONFIG", "storage.json", volume.config_path())


def t_no_test_can_open_the_production_approval_state():
    # Lesend importiert: `security/` ist eingefroren; sein Standard wird beim Import
    # aus `SOLVIO_APPROVAL_STATE_DIR` gelesen, deshalb setzt `_guard` ihn vorher.
    from solvio.security.mobile_approval import identity
    _require_redirected("SOLVIO_APPROVAL_STATE_DIR", "~/.solvio-approvals",
                        identity.DEFAULT_STATE_DIR)


def t_no_test_can_open_the_production_portal_vault():
    from solvio.portal.vault import PortalVault, default_dir
    _require_redirected("SOLVIO_PORTAL_VAULT_DIR", "~/.solvio-portal", default_dir())
    vault = PortalVault()
    _require_redirected("SOLVIO_PORTAL_VAULT_DIR", "~/.solvio-portal", vault.base_dir)


def t_no_test_can_reach_the_production_control_socket():
    from solvio.realtime.control import ControlClient, default_socket
    _require_redirected("SOLVIO_CONTROL_SOCKET_PATH", "control.sock", default_socket())
    _require_redirected("SOLVIO_CONTROL_SOCKET_PATH", "control.sock",
                        ControlClient().socket_path)
    require(not ControlClient().available(), "unter dem Testpfad lauscht ein Steuer-Socket")


def t_no_test_can_read_the_production_satellite_credential():
    from solvio.realtime.satellite_auth import credential_path
    _require_redirected("SOLVIO_SATELLITE_AUTH_FILE", "satellite_auth.json", credential_path())
    require(not os.path.exists(credential_path()),
            "unter dem Testpfad liegt eine Satelliten-Anmeldung")


def t_no_test_can_open_the_production_knowledge_vault():
    from solvio.knowledge.service import vault_dir
    _require_redirected("SOLVIO_VAULT", "~/SOLVIO Knowledge", vault_dir())


def t_no_test_can_open_the_production_state_dir():
    """Gespraeche, Gedaechtnis, Agentenlaeufe, Notizen, Kognition — alles unter `state_dir()`."""
    from solvio.agent_runtime import store as agent_store
    from solvio.capabilities import notes
    from solvio.cognition import ledger as cognition
    from solvio.conversation import store as conversation
    from solvio.memory.service import memory_base_dir
    state = os.environ.get("SOLVIO_STATE_DIR", "")
    _require_redirected("SOLVIO_STATE_DIR", "~/.solvio", agent_store.state_dir())
    for label, path in (("agent_runs.sqlite3", agent_store.resolve_path()),
                        ("conversations.sqlite3", conversation.default_db_path()),
                        ("cognition.sqlite3", cognition.resolve_path()),
                        ("memory/", memory_base_dir()),
                        ("notes", notes.notes_root())):
        _require_outside_home(label, path)
        require(os.path.realpath(path).startswith(os.path.realpath(state) + os.sep),
                f"{label} liegt nicht unter dem Zustandsverzeichnis der Tests: {path}")


def t_no_test_can_create_the_production_task_roots():
    """`~/.solvio-tasks` wird in einer isolierten Welt verweigert, BEVOR ein Ordner entsteht."""
    from solvio.agent_runtime.native_tasks import _private_root
    _require_outside_home("SOLVIO_STATE_DIR", os.environ.get("SOLVIO_STATE_DIR", ""))
    home_root = Path(_real_home()) / ".solvio-tasks"
    existed = home_root.exists()
    for setting, name in (("~/.solvio-tasks/workspaces", "native_workspace_root"),
                          ("~/.solvio-tasks/claude-jails", "native_jail_root")):
        try:
            _private_root(setting, name)
            require(False, f"{setting} wurde unter Tests angelegt")
        except ValueError as exc:
            require_equal(str(exc), name + "_not_isolated", f"{setting}: falscher Grund")
    require(home_root.exists() == existed, "~/.solvio-tasks ist unter Tests entstanden")


# ------------------------------------------------------------------ uebergreifend
def _known_default_paths() -> list[tuple[str, str]]:
    """Jeder bekannte Standardpfad eines SOLVIO-Speichers, so aufgeloest, wie ihn ein Test sieht."""
    from solvio.agent_runtime import store as agent_store
    from solvio.autopilot import driver
    from solvio.autopilot import store as autopilot
    from solvio.capabilities import notes
    from solvio.cognition import ledger as cognition
    from solvio.communication import bindings
    from solvio.conversation import store as conversation
    from solvio.doctor import store as doctor
    from solvio.knowledge import service as knowledge
    from solvio.memory.service import memory_base_dir
    from solvio.payment import config as payment_config
    from solvio.payment import store as payment
    from solvio.portal import vault as portal
    from solvio.proactive import store as proactive
    from solvio.provider_broker import ledger as broker
    from solvio.realtime import control, satellite_auth
    from solvio.secret_vault import store as secret_vault
    from solvio.security.mobile_approval import identity
    from solvio.storage import engine, volume
    from solvio.storage.offsite import config as offsite_config
    from solvio.storage.offsite import ledger as offsite_ledger
    from solvio.telephony import ledger as telephony
    return [
        # Nur aufgeloest, nicht geoeffnet: BindingStore() oeffnet im Konstruktor.
        # Der oeffnende Beweis ist t_no_test_can_open_the_production_contact_store.
        ("contacts", os.environ.get(bindings.PATH_ENV, "") or bindings.DEFAULT_PATH),
        ("secret-vault", secret_vault.vault_dir()),
        ("payments", payment.db_path()),
        ("payment-config", payment_config.config_path()),
        ("proactive", proactive.default_path()),
        ("doctor", doctor.default_path()),
        ("autopilot", autopilot.resolve_path()),
        ("autopilot-lock", driver.lock_path()),
        ("broker", broker.resolve_path()),
        ("telephony", os.environ.get(telephony.PATH_ENV, "") or telephony.DEFAULT_PATH),
        ("offsite-ledger", offsite_ledger.ledger_path()),
        ("offsite-dir", offsite_config.offsite_dir()),
        ("backup-state", engine.state_dir()),
        ("backup-config", volume.config_path()),
        ("approvals", identity.DEFAULT_STATE_DIR),
        ("portal-vault", portal.default_dir()),
        ("control-socket", control.default_socket()),
        ("satellite-credential", satellite_auth.credential_path()),
        ("knowledge", knowledge.vault_dir()),
        ("state-dir", agent_store.state_dir()),
        ("agent-runs", agent_store.resolve_path()),
        ("conversations", conversation.default_db_path()),
        ("cognition", cognition.resolve_path()),
        ("memory", memory_base_dir()),
        ("notes", notes.notes_root()),
    ]


def t_no_known_default_path_points_into_the_real_home_under_tests():
    """Die uebergreifende Pruefung: KEIN bekannter Standardpfad zeigt unter Tests nach `~`."""
    paths = _known_default_paths()
    require(len(paths) >= len(TEST_PATH_VARIABLES), "die Liste der Standardpfade ist geschrumpft")
    leaked = []
    for label, path in paths:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
        home = _real_home()
        if resolved == home or resolved.startswith(home + os.sep):
            leaked.append(f"{label}={path}")
    require_equal(leaked, [], "Standardpfade, die unter Tests ins echte Heimverzeichnis zeigen")


def t_every_guard_variable_is_honoured_by_a_store():
    """Keine Variable im Guard ist Dekoration: jede lenkt einen Speicher dieser Liste um."""
    resolved = {os.path.realpath(os.path.expanduser(str(p))) for _l, p in _known_default_paths()}
    for name, _leaf in TEST_PATH_VARIABLES:
        value = os.path.realpath(os.path.expanduser(os.environ.get(name, "")))
        require(value in resolved, f"{name} lenkt keinen bekannten Speicher um: {value}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
