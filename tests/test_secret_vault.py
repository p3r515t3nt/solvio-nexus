"""Der Geheimnistresor — die Zusicherungen, auf denen er beruht.

Der Satz, den diese Datei nachweist:

    Ein Agent darf WISSEN, dass ein Geheimnis existiert, und er darf eine
    legitime Faehigkeit anfordern, die es benutzt. Den Wert bekommt er nie.

Was hier geprueft wird, ist nicht „laeuft der Code", sondern ob die vier
Bindungen halten, die diesen Satz tragen: Faehigkeit, Executor, Ziel und
Zustand — und ob der Umschlag zubleibt, wenn jemand an den Metadaten dreht.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_secret_vault.py
"""
import atexit
import base64
import os
import shutil
import sqlite3
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402

enforce_assertions()

from _guard import require, require_equal, require_raises  # noqa: E402

# DER TRESOR WIRD UMGELENKT, BEVOR EIN TEST LAEUFT — und zwar BEIDE Haelften.
#
# Der Portaltresor hat gezeigt, warum das nicht reicht, wenn man nur die Datei
# umlenkt: seine Tests bauen `PortalVault(mkdtemp())`, aber der Hauptschluessel
# kommt aus einer MODULWEITEN Funktion, die den PRODUKTIVEN Schluesselbund
# befragt — und ihn anlegt, wenn er fehlt. Eine Testsuite, die den produktiven
# Schluesselbund beschreibt, ist kein Test, sondern ein Eingriff.
_SANDBOX = tempfile.mkdtemp(prefix="solvio-vault-suite-")
os.environ["SOLVIO_VAULT_DIR"] = os.path.join(_SANDBOX, "vault")
os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "keys")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.capabilities import policy as AP                       # noqa: E402
from solvio.secret_vault import admin, envelope as E, health       # noqa: E402
from solvio.secret_vault import broker as B                        # noqa: E402
from solvio.secret_vault import context as SC                      # noqa: E402
from solvio.secret_vault import keyring as K                       # noqa: E402
from solvio.secret_vault import migration as M                     # noqa: E402
from solvio.secret_vault import policy as VP                       # noqa: E402
from solvio.secret_vault import recovery as RC                     # noqa: E402
from solvio.secret_vault import refs as R                          # noqa: E402
from solvio.secret_vault import staging as ST                      # noqa: E402
from solvio.secret_vault.firewall import (CredentialRefused,       # noqa: E402
                                          is_credential,
                                          redact_if_credential,
                                          refuse_if_credential)
from solvio.secret_vault.store import VaultStore, describe_row     # noqa: E402

# Synthetische Werte. Keiner davon ist irgendwo echt, keiner traegt eine Form,
# die ein Geheimnisscanner als Anbieterschluessel liest.
FAKE_PASSWORD = b"synthetic-drill-value-0001"
FAKE_TOKEN = b"synthetic-drill-value-0002"
REF = "secret://amazon/gregor"
TARGET = "https://www.amazon.de"


# --------------------------------------------------------------------- Werkzeug
def _fresh() -> VaultStore:
    """Ein Tresor je Test. Eigene Datei, eigener Schluessel, eigenes Nichts."""
    root = tempfile.mkdtemp(prefix="solvio-vault-case-", dir=_SANDBOX)
    os.environ["SOLVIO_VAULT_DIR"] = root
    os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(root, "keys")
    K.forget_kek()
    store = VaultStore()
    admin.initialize(store)
    return store


def _seed(store: VaultStore, *, ref: str = REF, targets=(TARGET,),
          capabilities=("portal_login",), executors=(VP.ExecutorId.BROWSER,),
          value: bytes = FAKE_PASSWORD, background: bool = False):
    return admin.add(secret_ref=ref, kind=VP.SecretKind.PASSWORD, plaintext=value,
                     allowed_capabilities=capabilities, allowed_targets=targets,
                     allowed_executors=executors, display_name="Amazon",
                     service_label="Amazon", account_label="Gregor",
                     allow_background=background, store=store)


def _trusted_executor(name: str = "solvio.browser.probe"):
    """Ein Modul, dessen NAME zum Executor passt — wie in der Produktion.

    Der Tresor prueft den Modulnamen des Aufrufers. Ein Test, der aus
    `__main__` heraus ausleiht, prueft deshalb etwas anderes als die
    Wirklichkeit — er prueft, dass die Bindung greift, nicht dass sie traegt.
    """
    module = types.ModuleType(name)
    source = ("def borrow(probe, ref, executor, target):\n"
              "    with probe.use(ref, executor=executor, target=target) as m:\n"
              "        return m.plaintext()\n"
              "\n"
              "def lend(probe, ref, executor, target):\n"
              "    return probe.use(ref, executor=executor, target=target)\n")
    exec(compile(source, "<probe>", "exec"), module.__dict__)
    return module


def _use(store, *, ref=REF, target=TARGET, capability="portal_login",
         executor=VP.ExecutorId.BROWSER,
         origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP,
         module="solvio.browser.probe"):
    probe = B.SecretBroker(store)
    with SC.bound(SC.UseContext(origin=origin, capability=capability)):
        return _trusted_executor(module).borrow(probe, ref, executor, target)


# ------------------------------------------------------------ Testisolation
def t_this_suite_cannot_touch_the_production_vault():
    """Die schaerfste Zusicherung dieser Datei, und sie gilt fuer sie selbst.

    Faellt die Umlenkung weg, schreibt der naechste Lauf in den echten Tresor
    und legt einen echten Schluesselbund-Eintrag an. Diese Pruefung faellt
    dann um, bevor irgendein anderer Test etwas anfassen kann."""
    from solvio.secret_vault.store import db_path, vault_dir
    real = os.path.realpath(os.path.expanduser("~/.solvio-vault"))
    require(os.environ.get("SOLVIO_VAULT_DIR"), "SOLVIO_VAULT_DIR ist nicht gesetzt")
    require(os.environ.get("SOLVIO_VAULT_TEST_KEYSTORE"),
            "SOLVIO_VAULT_TEST_KEYSTORE ist nicht gesetzt")
    for path in (vault_dir(), db_path()):
        actual = os.path.realpath(os.path.expanduser(path))
        require(not (actual == real or actual.startswith(real + os.sep)),
                f"Testtresor liegt im produktiven Bereich: {actual}")
    require(K.is_test_backend(),
            "Der Schluesselspeicher ist NICHT der Testspeicher — der produktive "
            "Schluesselbund waere in Reichweite")


def t_the_keychain_backend_is_never_the_real_one_in_tests():
    """Ein Dateispeicher darf sich nie als Schluesselbund ausgeben."""
    detail = health.detail()
    require_equal(detail["schluesselspeicher"], "datei (Test)",
                  "Die Gesundheit gibt den Testspeicher als Schluesselbund aus")


# -------------------------------------------------------------------- Verweise
def t_a_reference_is_canonical_and_opaque():
    require_equal(str(R.parse("secret://Amazon/Gregor")), "secret://amazon/gregor")
    require_equal(str(R.parse("  secret://amazon/gregor  ")), "secret://amazon/gregor")
    require_equal(R.parse("secret://a/b"), R.SecretRef("a", "b"))


def t_a_malformed_reference_is_refused():
    for text in ("amazon/gregor", "secret://amazon", "secret://a/b/c", "secret://",
                 "secret://AMAZON/", "secret:///gregor", "https://amazon.de",
                 "secret://amazon/greg or", "secret://amazon/greg\nor", "",
                 "secret://" + "a" * 200 + "/b"):
        require(not R.is_valid(text), f"{text!r} haette abgelehnt werden muessen")


def t_a_reference_never_shows_more_than_itself():
    ref = R.parse(REF)
    require_equal(repr(ref), f"SecretRef({REF!r})")


# -------------------------------------------------------------------- Umschlag
def t_the_envelope_round_trips():
    kek = os.urandom(32)
    sealed = E.seal(kek=kek, ref=REF, version=1, policy_sha256="a" * 64,
                    plaintext=FAKE_PASSWORD)
    require_equal(E.unseal(kek=kek, ref=REF, version=1, policy_sha256="a" * 64,
                           sealed=sealed), FAKE_PASSWORD)


def t_the_envelope_binds_reference_version_and_policy():
    """Jedes der drei Felder allein reicht, um den Umschlag zubleiben zu lassen."""
    kek = os.urandom(32)
    sealed = E.seal(kek=kek, ref=REF, version=1, policy_sha256="a" * 64,
                    plaintext=FAKE_PASSWORD)
    for kwargs in ({"ref": "secret://amazon/andere"}, {"version": 2},
                   {"policy_sha256": "b" * 64}):
        base = {"kek": kek, "ref": REF, "version": 1, "policy_sha256": "a" * 64,
                "sealed": sealed}
        base.update(kwargs)
        require_raises(E.EnvelopeError, lambda b=base: E.unseal(**b))


def t_a_foreign_key_does_not_open_the_envelope():
    sealed = E.seal(kek=os.urandom(32), ref=REF, version=1, policy_sha256="a" * 64,
                    plaintext=FAKE_PASSWORD)
    require_raises(E.EnvelopeError, lambda: E.unseal(
        kek=os.urandom(32), ref=REF, version=1, policy_sha256="a" * 64, sealed=sealed))


def t_a_malformed_ciphertext_fails_closed():
    kek = os.urandom(32)
    sealed = E.seal(kek=kek, ref=REF, version=1, policy_sha256="a" * 64,
                    plaintext=FAKE_PASSWORD)
    broken = E.Sealed(sealed.envelope_version, sealed.wrapped_dek,
                      sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 0xFF]))
    require_raises(E.EnvelopeError, lambda: E.unseal(
        kek=kek, ref=REF, version=1, policy_sha256="a" * 64, sealed=broken))


def t_every_sealing_uses_a_fresh_nonce():
    """Zweimal derselbe Klartext ergibt nie denselben Geheimtext."""
    kek = os.urandom(32)
    seen = {E.seal(kek=kek, ref=REF, version=1, policy_sha256="a" * 64,
                   plaintext=FAKE_PASSWORD).ciphertext for _ in range(8)}
    require_equal(len(seen), 8, "Ein Nonce wurde wiederverwendet")


def t_an_envelope_never_shows_its_ciphertext():
    sealed = E.seal(kek=os.urandom(32), ref=REF, version=1, policy_sha256="a" * 64,
                    plaintext=FAKE_PASSWORD)
    require("redacted" in repr(sealed), "Der Umschlag zeigt sich im repr")
    require(FAKE_PASSWORD.decode() not in repr(sealed))


def t_an_unknown_envelope_version_is_refused():
    kek = os.urandom(32)
    sealed = E.seal(kek=kek, ref=REF, version=1, policy_sha256="a" * 64,
                    plaintext=FAKE_PASSWORD)
    future = E.Sealed(99, sealed.wrapped_dek, sealed.ciphertext)
    require_raises(E.EnvelopeError, lambda: E.unseal(
        kek=kek, ref=REF, version=1, policy_sha256="a" * 64, sealed=future))


# ---------------------------------------------------------------------- Policy
def t_the_policy_default_is_deny():
    """Eine leere Berechtigung erlaubt nichts — auch nicht mit gueltiger Anfrage."""
    empty = VP.SecretPolicy(secret_ref=REF, kind=VP.SecretKind.PASSWORD, version=1,
                            status=VP.Status.ACTIVE)
    verdict = VP.evaluate(empty, VP.UseRequest(
        capability="portal_login", executor=VP.ExecutorId.BROWSER, target=TARGET,
        origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP,
        caller_module="solvio.browser.x"))
    require(not verdict.allowed)
    require_equal(verdict.reason, VP.Denied.CAPABILITY_NOT_ALLOWED)


def t_a_target_must_be_a_full_origin():
    require_equal(VP.normalize_target("amazon.de"), "")
    require_equal(VP.normalize_target("HTTPS://WWW.Amazon.DE/anmelden?x=1"),
                  "https://www.amazon.de")
    require_equal(VP.normalize_target("http://192.168.178.136:8123/api/"),
                  "http://192.168.178.136:8123")


def t_widening_is_recognised_as_widening():
    base = VP.SecretPolicy(secret_ref=REF, kind=VP.SecretKind.PASSWORD, version=1,
                           status=VP.Status.ACTIVE,
                           allowed_capabilities=("portal_login",),
                           allowed_targets=(TARGET,),
                           allowed_executors=(VP.ExecutorId.BROWSER,))
    wider = [
        {"allowed_capabilities": ("portal_login", "gmail_send_draft")},
        {"allowed_targets": (TARGET, "https://angreifer.example")},
        {"allowed_executors": (VP.ExecutorId.BROWSER, VP.ExecutorId.HTTP)},
        {"allow_background": True},
    ]
    for change in wider:
        following = VP.SecretPolicy(**{**base.__dict__, **change})
        require(VP.widens(base, following), f"{change} gilt nicht als Erweiterung")
    narrower = VP.SecretPolicy(**{**base.__dict__, "allowed_targets": ()})
    require(not VP.widens(base, narrower), "Einschraenkung gilt als Erweiterung")


def t_reactivating_a_revoked_secret_counts_as_widening():
    revoked = VP.SecretPolicy(secret_ref=REF, kind=VP.SecretKind.PASSWORD, version=1,
                              status=VP.Status.REVOKED)
    active = VP.SecretPolicy(**{**revoked.__dict__, "status": VP.Status.ACTIVE})
    require(VP.widens(revoked, active))


def t_payment_kinds_are_refused_by_name():
    store = _fresh()
    for kind in ("card_pan", "cvv", "iban", "bank_account"):
        require_raises(admin.AdminError, lambda k=kind: admin.add(
            secret_ref="secret://bank/karte", kind=k, plaintext=b"x" * 12,
            allowed_capabilities=("payment_send",), allowed_targets=(TARGET,),
            allowed_executors=(VP.ExecutorId.HTTP,), store=store))


# ---------------------------------------------------------------------- Makler
def t_a_trusted_executor_gets_the_value():
    store = _fresh()
    _seed(store)
    require_equal(_use(store), FAKE_PASSWORD.decode())


def t_a_generic_caller_gets_nothing():
    """Der Modulname des Aufrufers muss zum behaupteten Executor passen."""
    store = _fresh()
    _seed(store)
    require_raises(B.SecretDenied,
                   lambda: _use(store, module="solvio.tools.something"))


def t_the_wrong_target_is_denied():
    store = _fresh()
    _seed(store)
    for target in ("https://angreifer.example", "http://www.amazon.de",
                   "https://amazon.de", "https://www.amazon.de.angreifer.example"):
        require_raises(B.SecretDenied, lambda t=target: _use(store, target=t))


def t_the_wrong_capability_is_denied():
    store = _fresh()
    _seed(store)
    require_raises(B.SecretDenied, lambda: _use(store, capability="gmail_send_draft"))


def t_the_wrong_executor_is_denied():
    store = _fresh()
    _seed(store)
    require_raises(B.SecretDenied, lambda: _use(store, executor=VP.ExecutorId.HTTP))


def t_external_content_never_gets_a_value():
    store = _fresh()
    _seed(store)
    require_raises(B.SecretDenied, lambda: _use(
        store, origin=AP.OriginClass.EXTERNAL_UNTRUSTED))


def t_an_unset_origin_gets_nothing():
    """Ein Executor ausserhalb eines Router-Vorgangs bekommt keine Vermutung."""
    store = _fresh()
    _seed(store)
    probe = B.SecretBroker(store)
    module = _trusted_executor()
    require_raises(B.SecretDenied, lambda: module.borrow(
        probe, REF, VP.ExecutorId.BROWSER, TARGET))


def t_background_is_denied_unless_the_policy_allows_it():
    store = _fresh()
    _seed(store)
    require_raises(B.SecretDenied, lambda: _use(
        store, origin=AP.OriginClass.BACKGROUND_AUTOMATION))
    store2 = _fresh()
    _seed(store2, background=True)
    require_equal(_use(store2, origin=AP.OriginClass.BACKGROUND_AUTOMATION),
                  FAKE_PASSWORD.decode())


def t_a_disabled_secret_stops_answering_at_once():
    store = _fresh()
    _seed(store)
    require_equal(_use(store), FAKE_PASSWORD.decode())
    admin.set_status(secret_ref=REF, status=VP.Status.DISABLED, store=store)
    require_raises(B.SecretDenied, lambda: _use(store))


def t_a_revoked_secret_is_not_served_from_a_cache():
    """Es gibt keinen Handle, der einen Widerruf ueberlebt."""
    store = _fresh()
    _seed(store)
    probe = B.SecretBroker(store)
    module = _trusted_executor()
    with SC.bound(SC.UseContext(origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP,
                                capability="portal_login")):
        module.borrow(probe, REF, VP.ExecutorId.BROWSER, TARGET)
        admin.set_status(secret_ref=REF, status=VP.Status.REVOKED, store=store)
        require_raises(B.SecretDenied, lambda: module.borrow(
            probe, REF, VP.ExecutorId.BROWSER, TARGET))


def t_borrowed_material_is_released_when_the_block_ends():
    store = _fresh()
    _seed(store)
    probe = B.SecretBroker(store)
    module = _trusted_executor()
    with SC.bound(SC.UseContext(origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP,
                                capability="portal_login")):
        borrowed = module.lend(probe, REF, VP.ExecutorId.BROWSER, TARGET)
        with borrowed as material:
            require(material.plaintext())
        require_raises(B.SecretUnavailable, material.plaintext)


def t_borrowed_material_redacts_itself_in_every_representation():
    store = _fresh()
    _seed(store)
    probe = B.SecretBroker(store)
    module = _trusted_executor()
    with SC.bound(SC.UseContext(origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP,
                                capability="portal_login")):
        with module.lend(probe, REF, VP.ExecutorId.BROWSER, TARGET) as material:
            for rendered in (repr(material), str(material), f"{material}",
                             "{}".format(material)):
                require(FAKE_PASSWORD.decode() not in rendered,
                        "Der Wert erscheint in einer Darstellung")
                require("redacted" in rendered)


def t_modified_metadata_cannot_silently_unlock_a_secret():
    """Wer die Zielliste in der Datenbank erweitert, bekommt einen Fehlschlag."""
    store = _fresh()
    _seed(store)
    connection = sqlite3.connect(store.path)
    connection.execute(
        "UPDATE secrets SET allowed_targets = allowed_targets || char(10) || ?",
        ("https://angreifer.example",))
    connection.commit()
    connection.close()
    try:
        _use(store, target="https://angreifer.example")
        require(False, "Manipulierte Metadaten haben den Tresor geoeffnet")
    except B.SecretDenied as exc:
        require_equal(exc.reason, VP.Denied.ENVELOPE_REJECTED)


def t_an_unknown_reference_is_denied_not_leaked():
    store = _fresh()
    _seed(store)
    try:
        _use(store, ref="secret://gibt-es/nicht")
        require(False, "Ein unbekannter Verweis lieferte etwas")
    except B.SecretDenied as exc:
        require_equal(exc.reason, VP.Denied.UNKNOWN_SECRET)


def t_the_catalogue_never_carries_a_value():
    store = _fresh()
    _seed(store)
    entries = B.SecretBroker(store).catalogue()
    require_equal(len(entries), 1)
    blob = repr(entries)
    require(FAKE_PASSWORD.decode() not in blob)
    for forbidden in ("ciphertext", "wrapped_dek", "policy_sha256"):
        require(forbidden not in entries[0], f"{forbidden} steht im Katalog")


def t_the_described_row_is_a_closed_list():
    store = _fresh()
    _seed(store)
    row = store.row(REF)
    described = describe_row(row)
    require("ciphertext" in row and "wrapped_dek" in row,
            "Der Test prueft die falsche Zeile")
    require(set(described) < set(row), "describe_row gibt mehr heraus als die Zeile hat")
    for forbidden in ("ciphertext", "wrapped_dek", "policy_sha256"):
        require(forbidden not in described)


# ------------------------------------------------------------------ Verwaltung
def t_a_secret_without_scope_is_refused():
    store = _fresh()
    for kwargs in ({"allowed_capabilities": ()}, {"allowed_targets": ()},
                   {"allowed_executors": ()}):
        base = {"secret_ref": REF, "kind": VP.SecretKind.PASSWORD,
                "plaintext": FAKE_PASSWORD, "allowed_capabilities": ("portal_login",),
                "allowed_targets": (TARGET,),
                "allowed_executors": (VP.ExecutorId.BROWSER,), "store": store}
        base.update(kwargs)
        require_raises(admin.AdminError, lambda b=base: admin.add(**b))


def t_a_target_without_scheme_is_refused():
    store = _fresh()
    require_raises(admin.AdminError, lambda: admin.add(
        secret_ref=REF, kind=VP.SecretKind.PASSWORD, plaintext=FAKE_PASSWORD,
        allowed_capabilities=("portal_login",), allowed_targets=("amazon.de",),
        allowed_executors=(VP.ExecutorId.BROWSER,), store=store))


def t_adding_twice_is_refused_unless_asked_to_replace():
    store = _fresh()
    _seed(store)
    require_raises(admin.AdminError, lambda: _seed(store))


def t_rotation_is_atomic_and_leaves_no_second_value():
    store = _fresh()
    _seed(store)
    before = store.sealed(REF)[0]
    policy = admin.replace_value(secret_ref=REF, plaintext=FAKE_TOKEN, store=store)
    require_equal(policy.version, 2)
    after = store.sealed(REF)[0]
    require(after.ciphertext != before.ciphertext, "Der Geheimtext blieb derselbe")
    require_equal(_use(store), FAKE_TOKEN.decode())
    require_equal(store.count(), 1, "Rotation hat einen zweiten Eintrag hinterlassen")


def t_rotation_keeps_the_scope():
    store = _fresh()
    _seed(store)
    admin.replace_value(secret_ref=REF, plaintext=FAKE_TOKEN, store=store)
    policy = store.policy(REF)
    require_equal(policy.allowed_targets, (TARGET,))
    require_equal(policy.allowed_capabilities, ("portal_login",))


def t_rescope_reports_whether_it_widened():
    store = _fresh()
    _seed(store)
    _policy, widened = admin.rescope(
        secret_ref=REF, allowed_targets=(TARGET, "https://www.amazon.at"), store=store)
    require(widened, "Ein zusaetzliches Ziel gilt nicht als Erweiterung")
    _policy, widened = admin.rescope(secret_ref=REF, allowed_targets=(TARGET,),
                                     store=store)
    require(not widened, "Eine Einschraenkung gilt als Erweiterung")


def t_rescope_keeps_the_value_usable():
    store = _fresh()
    _seed(store)
    admin.rescope(secret_ref=REF, allowed_targets=(TARGET, "https://www.amazon.at"),
                  store=store)
    require_equal(_use(store, target="https://www.amazon.at"), FAKE_PASSWORD.decode())


def t_deleting_removes_the_ciphertext():
    store = _fresh()
    _seed(store)
    require(admin.delete(secret_ref=REF, store=store))
    require_equal(store.row(REF), None)
    require_raises(B.SecretDenied, lambda: _use(store))


def t_a_new_master_key_is_refused_while_entries_exist():
    """Der Fehler, der alles kostet: neuer Schluessel neben altem Geheimtext."""
    store = _fresh()
    _seed(store)
    K.forget_kek()
    require_raises(admin.AdminError, lambda: admin.initialize(store))


def t_an_empty_secret_is_refused():
    store = _fresh()
    require_raises(admin.AdminError, lambda: admin.add(
        secret_ref=REF, kind=VP.SecretKind.PASSWORD, plaintext=b"",
        allowed_capabilities=("portal_login",), allowed_targets=(TARGET,),
        allowed_executors=(VP.ExecutorId.BROWSER,), store=store))


def t_an_oversized_secret_is_refused():
    store = _fresh()
    require_raises(admin.AdminError, lambda: admin.add(
        secret_ref=REF, kind=VP.SecretKind.PASSWORD,
        plaintext=b"x" * (admin.MAX_SECRET_BYTES + 1),
        allowed_capabilities=("portal_login",), allowed_targets=(TARGET,),
        allowed_executors=(VP.ExecutorId.BROWSER,), store=store))


# --------------------------------------------------------------- Zugriffsspur
def t_the_ledger_records_use_and_denial_without_a_value():
    store = _fresh()
    _seed(store)
    _use(store)
    try:
        _use(store, target="https://angreifer.example")
    except B.SecretDenied:
        pass
    rows = store.ledger(limit=10)
    outcomes = [r["outcome"] for r in rows]
    require("used" in outcomes and "denied" in outcomes, f"Spur unvollstaendig: {rows}")
    blob = repr(rows)
    require(FAKE_PASSWORD.decode() not in blob, "Die Spur traegt einen Wert")
    used = next(r for r in rows if r["outcome"] == "used")
    require_equal(used["target"], TARGET)
    require_equal(used["executor"], "browser")
    require_equal(used["origin"], "trusted_interactive_app")
    denied = next(r for r in rows if r["outcome"] == "denied")
    require_equal(denied["denied_reason"], "target_not_allowed")


def t_the_ledger_has_no_free_text_field():
    """Ein `**kwargs`-Protokoll waere die Stelle, an der `password=` mitgeht."""
    import inspect
    signature = inspect.signature(VaultStore.record)
    require(all(p.kind is not inspect.Parameter.VAR_KEYWORD
                for p in signature.parameters.values()),
            "Die Spur nimmt beliebige Felder entgegen")


def t_using_a_secret_updates_only_the_audit_field():
    """`last_used_at` steht bewusst NICHT im Digest — sonst waere jede Benutzung
    eine Neuversiegelung."""
    store = _fresh()
    _seed(store)
    before = store.sealed(REF)[0].ciphertext
    _use(store)
    require(store.policy(REF).last_used_at, "last_used_at wurde nicht gesetzt")
    require_equal(store.sealed(REF)[0].ciphertext, before,
                  "Eine Benutzung hat den Umschlag neu gebildet")


# ------------------------------------------------------------------ Einlagerung
def t_staged_material_is_single_use_and_device_bound():
    staging = ST.SecretStaging()
    handle = staging.stage(FAKE_PASSWORD, device_id="dev-1", payload_sha256="a" * 64)
    require_raises(ST.StagingError,
                   lambda: staging.take(handle, device_id="dev-2"))
    require_equal(staging.take(handle, device_id="dev-1"), FAKE_PASSWORD)
    require_raises(ST.StagingError, lambda: staging.take(handle, device_id="dev-1"))


def t_staged_material_expires():
    staging = ST.SecretStaging(ttl=-1.0)
    handle = staging.stage(FAKE_PASSWORD, device_id="dev-1", payload_sha256="a" * 64)
    require_raises(ST.StagingError, lambda: staging.take(handle, device_id="dev-1"))


def t_staging_is_bounded():
    staging = ST.SecretStaging()
    for index in range(ST.MAX_PENDING):
        staging.stage(FAKE_PASSWORD, device_id="dev-1", payload_sha256=str(index))
    require_raises(ST.StagingError, lambda: staging.stage(
        FAKE_PASSWORD, device_id="dev-1", payload_sha256="over"))


def t_staged_material_is_not_kept_in_the_clear():
    staging = ST.SecretStaging()
    handle = staging.stage(FAKE_PASSWORD, device_id="dev-1", payload_sha256="a" * 64)
    blob = repr(staging._slots[handle])
    require(FAKE_PASSWORD.decode() not in blob)
    require(FAKE_PASSWORD not in staging._slots[handle].blob,
            "Die Einlagerung haelt den Klartext")


# ------------------------------------------------------------ Wiederherstellung
def t_the_recovery_envelope_round_trips():
    kek = os.urandom(32)
    envelope = RC.build("eine-lange-testpassphrase", kek)
    require_equal(RC.unwrap(envelope, "eine-lange-testpassphrase"), kek)


def t_a_wrong_passphrase_is_one_refusal():
    kek = os.urandom(32)
    envelope = RC.build("eine-lange-testpassphrase", kek)
    require_raises(RC.RecoveryError, lambda: RC.unwrap(envelope, "falsche-passphrase"))


def t_a_short_passphrase_is_refused():
    require_raises(RC.RecoveryError, lambda: RC.build("kurz", os.urandom(32)))


def t_the_recovery_envelope_carries_no_passphrase_and_no_key():
    kek = os.urandom(32)
    passphrase = "eine-lange-testpassphrase"
    text = RC.build(passphrase, kek).to_json()
    require(passphrase not in text, "Die Passphrase steht im Umschlag")
    require(base64.b64encode(kek).decode() not in text, "Der Schluessel steht im Umschlag")
    require(kek.hex() not in text)


def t_the_recovery_envelope_notices_a_key_change():
    kek = os.urandom(32)
    envelope = RC.build("eine-lange-testpassphrase", kek)
    require_equal(envelope.kek_fingerprint, RC.key_fingerprint(kek))
    require(envelope.kek_fingerprint != RC.key_fingerprint(os.urandom(32)))


def t_a_recovery_envelope_from_another_key_is_refused():
    """Der Fingerabdruck hat eine eigene Aufgabe, und sie ist nicht die des AEAD.

    Eine falsche Passphrase faengt der Umschlag selbst ab. Was er allein NICHT
    faengt: ein Umschlag, der sich richtig oeffnet, dessen Inhalt aber zu einem
    ANDEREN Tresor gehoert — etwa weil zwei Saetze durcheinandergeraten sind.
    Dann kaeme ein gueltiger 32-Byte-Schluessel heraus, der nichts aufschliesst,
    und die Wiederherstellung meldete Erfolg. Der Fingerabdruck ist die Stelle,
    die das bemerkt."""
    kek = os.urandom(32)
    envelope = RC.build("eine-lange-testpassphrase", kek)
    fremder = RC.RecoveryEnvelope(**{**envelope.__dict__,
                                     "kek_fingerprint": RC.key_fingerprint(os.urandom(32))})
    require_raises(RC.RecoveryError,
                   lambda: RC.unwrap(fremder, "eine-lange-testpassphrase"),
                   message="Ein Umschlag mit fremdem Fingerabdruck wurde akzeptiert")


def t_a_tampered_recovery_envelope_does_not_open():
    kek = os.urandom(32)
    envelope = RC.build("eine-lange-testpassphrase", kek)
    broken = RC.RecoveryEnvelope(**{**envelope.__dict__,
                                    "salt_b64": base64.b64encode(os.urandom(16)).decode()})
    require_raises(RC.RecoveryError,
                   lambda: RC.unwrap(broken, "eine-lange-testpassphrase"))


def t_the_recovery_file_is_owner_only():
    store = _fresh()
    envelope = RC.build("eine-lange-testpassphrase", K.read_kek())
    path = RC.write(envelope)
    require_equal(os.stat(path).st_mode & 0o077, 0,
                  "Der Wiederherstellungsumschlag steht fuer andere offen")


# ------------------------------------------------------------------ Gesundheit
def t_health_says_ready_before_anything_is_stored():
    _fresh()
    word, _reason = health.assess()
    require(word in (health.HEALTHY, health.DEGRADED), f"unerwartet: {word}")


def t_health_notices_a_missing_recovery_envelope():
    store = _fresh()
    _seed(store)
    word, reason = health.assess()
    require_equal(word, health.DEGRADED)
    require("Wiederherstellungsumschlag" in reason, reason)


def t_health_notices_a_stale_recovery_envelope():
    store = _fresh()
    _seed(store)
    RC.write(RC.build("eine-lange-testpassphrase", os.urandom(32)))
    word, reason = health.assess()
    require_equal(word, health.DEGRADED)
    require("passt nicht" in reason, reason)


def t_health_is_healthy_with_a_current_envelope():
    store = _fresh()
    _seed(store)
    RC.write(RC.build("eine-lange-testpassphrase", K.read_kek()))
    original = M.leftover_plaintext
    M.leftover_plaintext = lambda path=None, refs=None: []
    try:
        word, reason = health.assess()
    finally:
        M.leftover_plaintext = original
    require_equal(word, health.HEALTHY, reason)


def t_health_notices_a_broken_database():
    store = _fresh()
    _seed(store)
    with open(store.path, "r+b") as handle:
        handle.seek(0)
        handle.write(b"not a database at all")
    word, _reason = health.assess()
    require_equal(word, health.UNAVAILABLE)


def t_health_never_decrypts_anything():
    """Eine Gesundheitspruefung darf keinen Zugang oeffnen — sie laeuft staendig."""
    store = _fresh()
    _seed(store)
    opened = []
    original = E.unseal
    E.unseal = lambda **kw: opened.append(kw) or original(**kw)
    try:
        health.assess()
        health.detail()
    finally:
        E.unseal = original
    require_equal(opened, [], "Die Gesundheit hat einen Zugang entschluesselt")


# -------------------------------------------------------------------- Wanderung
def t_the_migration_plan_carries_no_values():
    text = repr(M.ENV_PLANS) + repr(M.STAYS_ELSEWHERE) + repr(M.STAYS_IN_ENV)
    for shape in ("sk-", "ghp_", "Bearer "):
        require(shape not in text, f"{shape} steht im Wanderungsplan")


def t_authority_keys_are_never_planned_for_the_vault():
    """Der Tresor darf nie eine Stelle werden, an der sich Autoritaet erzeugen laesst."""
    never = [e for e in M.STAYS_ELSEWHERE if e.secret_class == M.CLASS_NEVER]
    require(any("core_signing_key" in e.what for e in never),
            "Der Signierschluessel des Cores fehlt in der Gegenliste")
    require(any("gateway_key" in e.what for e in never),
            "Der Gateway-Schluessel fehlt in der Gegenliste")
    planned = {p.secret_ref for p in M.ENV_PLANS}
    for name in ("core_signing_key", "gateway_key", "app_attest"):
        require(not any(name in ref for ref in planned))


def t_configuration_is_not_migrated():
    planned = {p.env_name for p in M.ENV_PLANS}
    require(not (planned & set(M.CONFIG_KEYS)),
            "Konfiguration steht im Wanderungsplan")


def t_what_stays_in_env_says_why():
    require(M.STAYS_IN_ENV, "Die Gegenliste ist leer — dann fehlt eine Begruendung")
    for name, why in M.STAYS_IN_ENV:
        require(len(why) > 60, f"{name} hat keine ausgeschriebene Begruendung")


def t_leftover_plaintext_needs_both_places():
    """Eine Dublette ist ein Zugang an ZWEI Orten — nicht ein geplanter Umzug.

    Die erste Fassung meldete jeden geplanten Zugang als Dublette, sobald er in
    `.env` stand. Damit haette die Gesundheit dauerhaft `degraded` gemeldet fuer
    etwas, das noch gar nicht gewandert ist — und eine Warnung, die immer
    leuchtet, liest niemand mehr."""
    path = os.path.join(_SANDBOX, "leftover.env")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("HOME_ASSISTANT_TOKEN=irrelevant\nSOLVIO_PORT=8765\n")
    # Geplant, aber noch nicht im Tresor: keine Dublette.
    require_equal(M.leftover_plaintext(path, refs=set()), [])
    # Im Tresor UND in .env: Dublette.
    require_equal(M.leftover_plaintext(path, refs={"secret://home-assistant/core"}),
                  ["HOME_ASSISTANT_TOKEN"])
    # Im Tresor, aber aus .env entfernt: erledigt.
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("SOLVIO_PORT=8765\n")
    require_equal(M.leftover_plaintext(path, refs={"secret://home-assistant/core"}), [])


def t_env_key_names_never_returns_a_value():
    path = os.path.join(_SANDBOX, "names.env")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("A=geheimer-wert\n# ein Kommentar\nB=noch-einer\n")
    require_equal(M.env_key_names(path), ["A", "B"])


# ---------------------------------------------------------------------- Zaun
def t_the_firewall_recognises_a_spoken_credential():
    for text in ("Mein Amazon-Passwort ist Hund1234",
                 "Merk dir: die PIN ist 4711",
                 "Das WLAN-Passwort lautet Sommer2024",
                 "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX"):
        require(is_credential(text), f"nicht erkannt: {text!r}")


def t_the_firewall_leaves_ordinary_sentences_alone():
    for text in ("Ich mag Kaffee", "Der Termin ist am Montag",
                 "Ein Passwort-Manager ist praktisch",
                 "Wir reden ueber Sicherheit"):
        require(not is_credential(text), f"faelschlich erkannt: {text!r}")


def t_the_firewall_refuses_and_names_the_tresor():
    try:
        refuse_if_credential("Mein Passwort ist Hund1234", where="probe")
        require(False, "Der Zaun hat nicht verweigert")
    except CredentialRefused as exc:
        require("Tresor" in exc.human_message)
        require("Hund1234" not in str(exc), "Die Ausnahme traegt den Wert")


def t_the_firewall_redacts_a_transcript_instead_of_dropping_it():
    out = redact_if_credential("Mein Passwort ist Hund1234", where="probe")
    require("Hund1234" not in out)
    require(out, "Der Verlauf wurde ganz verworfen statt redigiert")


def t_memory_refuses_a_credential_at_its_only_insert():
    from solvio.memory.sqlite_backend import _refuse_credentials
    require_raises(CredentialRefused,
                   lambda: _refuse_credentials("Mein Passwort ist Hund1234", "probe"))
    _refuse_credentials("Ich mag Kaffee", "probe")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
