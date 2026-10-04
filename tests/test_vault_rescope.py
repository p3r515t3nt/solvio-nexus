"""Secret Vault Rescope V1 — der Umfang aendert sich nur so, wie ein Mensch ihn sah.

`capabilities` ERSETZT die Liste. Das ist die gefaehrliche Eigenschaft dieser
Operation: zwei Namen hinzufuegen heisst, alle anderen mitzuschicken, sonst
sind sie fort. Ein Freigabetext, der nur das Ergebnis zeigt, macht diesen
Fehler unsichtbar — deshalb zeigt er hier die Differenz.

Und zwischen „das steht auf dem iPhone" und „Face ID war eben" liegen
Sekunden bis Minuten. Aendert jemand den Umfang in dieser Zeit, gilt die
Zustimmung nicht mehr fuer das, was jetzt da ist.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.

Direkt: python tests/test_vault_rescope.py
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-rescope-")
atexit.register(shutil.rmtree, _SANDBOX, True)

from solvio.capabilities.contract import CapabilityRefused      # noqa: E402
from solvio.capabilities.secret_vault import (                  # noqa: E402
    SPECS, SecretVaultCapabilities)
from solvio.secret_vault import admin, keyring as K             # noqa: E402
from solvio.secret_vault import policy as VP                    # noqa: E402
from solvio.secret_vault.store import VaultStore                # noqa: E402

REF = "secret://google/refresh"
ZIEL = "https://oauth2.googleapis.com"

#: Die zehn, die der Google-Zugang heute wirklich fuehrt.
BESTAND = ("calendar_create_event", "calendar_delete_event", "calendar_list_events",
           "calendar_update_event", "gmail_create_draft", "gmail_list_recent",
           "gmail_read_message", "gmail_read_thread", "gmail_search",
           "gmail_send_draft")
NEU = ("document_find", "document_ask")


def _tresor():
    """Ein Tresor je Fall. Eigene Datei, eigener Schluessel, eigenes Nichts."""
    wurzel = tempfile.mkdtemp(dir=_SANDBOX)
    os.environ["SOLVIO_VAULT_DIR"] = wurzel
    os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(wurzel, "keys")
    K.forget_kek()
    store = VaultStore()
    admin.initialize(store)
    admin.add(secret_ref=REF, kind=VP.SecretKind.OAUTH_REFRESH_TOKEN,
              plaintext=b"synthetisch-nie-echt",
              allowed_capabilities=BESTAND, allowed_targets=(ZIEL,),
              allowed_executors=(VP.ExecutorId.HTTP,),
              display_name="Google — Anmeldung", service_label="Google",
              account_label="Kalender und Mail", allow_background=True, store=store)
    return store, SecretVaultCapabilities(store=store)


def _fassung(store) -> str:
    return str(store.policy(REF).version)


def _run(coro):
    return asyncio.run(coro) if asyncio.iscoroutine(coro) else coro


# ------------------------------------------------------------------ R2
def t_the_description_shows_what_actually_changes():
    """Hinzu, weg, bleibt — nicht nur das Ergebnis.

    Ohne die Differenz kann ein Mensch nicht sehen, dass eine „Ergaenzung"
    zehn Berechtigungen mitnimmt.
    """
    store, caps = _tresor()
    text = caps.describe_rescope({
        "secret_ref": REF,
        "capabilities": ", ".join(BESTAND + NEU),
        "expected_version": _fassung(store)})

    require_equal(text["kommt_hinzu"], "document_find, document_ask")
    require_equal(text["entfaellt"], "nichts")
    require_equal(text["bleibt"], ", ".join(BESTAND))
    require_equal(text["bisher"], ", ".join(BESTAND))
    require_equal(text["fassung"], _fassung(store))


def t_the_description_names_what_would_be_lost():
    """Der Fall, fuer den die Differenz ueberhaupt da ist."""
    store, caps = _tresor()
    text = caps.describe_rescope({"secret_ref": REF,
                                  "capabilities": ", ".join(NEU),
                                  "expected_version": _fassung(store)})
    require_equal(text["kommt_hinzu"], "document_find, document_ask")
    require_equal(text["entfaellt"], ", ".join(BESTAND),
                  "der Verlust von zehn Rechten blieb unsichtbar")
    require_equal(text["bleibt"], "")


def t_the_description_never_carries_a_value():
    store, caps = _tresor()
    text = caps.describe_rescope({"secret_ref": REF,
                                  "capabilities": ", ".join(BESTAND + NEU),
                                  "expected_version": _fassung(store)})
    gerendert = str(text)
    require("synthetisch" not in gerendert, "ein Wert stand im Freigabetext")
    require("nie-echt" not in gerendert, "ein Wert stand im Freigabetext")


# ------------------------------------------------------------------ R3
def t_an_additive_change_keeps_every_existing_capability():
    """Die eigentliche Sache: zwoelf danach, nicht zwei."""
    store, caps = _tresor()
    caps.rescope({"secret_ref": REF,
                  "capabilities": ", ".join(BESTAND + NEU),
                  "expected_version": _fassung(store)})
    danach = store.policy(REF).allowed_capabilities
    for name in BESTAND:
        require(name in danach, f"{name} ging verloren")
    for name in NEU:
        require(name in danach, f"{name} kam nicht an")
    require_equal(len(danach), 12, f"unerwartete Anzahl: {danach}")


def t_removing_stays_possible_and_is_visible():
    """Entfernen bleibt moeglich — es soll nur nicht aus Versehen passieren."""
    store, caps = _tresor()
    kleiner = tuple(c for c in BESTAND if c != "gmail_send_draft")
    text = caps.describe_rescope({"secret_ref": REF,
                                  "capabilities": ", ".join(kleiner),
                                  "expected_version": _fassung(store)})
    require_equal(text["entfaellt"], "gmail_send_draft")
    caps.rescope({"secret_ref": REF, "capabilities": ", ".join(kleiner),
                  "expected_version": _fassung(store)})
    require("gmail_send_draft" not in store.policy(REF).allowed_capabilities,
            "das Entfernen wirkte nicht")


def t_a_scope_that_moved_meanwhile_is_refused():
    """Das Rennen zwischen Beschreibung und Freigabe.

    Der Mensch sah Fassung N. Bis seine Zustimmung ankommt, steht dort N+1 —
    also gilt sie nicht mehr fuer das, was jetzt da ist.
    """
    store, caps = _tresor()
    gesehen = _fassung(store)

    # Jemand anderes aendert zwischendurch.
    caps.rescope({"secret_ref": REF, "capabilities": ", ".join(BESTAND[:5]),
                  "expected_version": gesehen})

    grund = ""
    try:
        caps.rescope({"secret_ref": REF,
                      "capabilities": ", ".join(BESTAND + NEU),
                      "expected_version": gesehen})       # die ALTE Fassung
    except CapabilityRefused as exc:
        grund = exc.reason
    require_equal(grund, "scope_changed_meanwhile",
                  "eine veraltete Zustimmung wurde angenommen")


def t_a_replayed_approval_cannot_widen_a_changed_scope():
    """Derselbe Riegel aus der Angreiferrichtung: die alte Freigabe kommt
    zurueck, nachdem der Umfang sich bewegt hat."""
    store, caps = _tresor()
    alt = _fassung(store)
    caps.rescope({"secret_ref": REF, "capabilities": ", ".join(BESTAND),
                  "expected_version": alt})
    vorher = store.policy(REF).allowed_capabilities

    grund = ""
    try:
        caps.rescope({"secret_ref": REF,
                      "capabilities": ", ".join(BESTAND + NEU),
                      "expected_version": alt})
    except CapabilityRefused as exc:
        grund = exc.reason
    require_equal(grund, "scope_changed_meanwhile")
    require_equal(store.policy(REF).allowed_capabilities, vorher,
                  "der Wiederholungsversuch hat trotzdem etwas veraendert")


# ------------------------------------------------------------------ R5
def t_the_version_is_a_required_argument_not_an_optional_courtesy():
    """Pflicht, und in den ARGUMENTEN.

    Der Autorisierungs-Digest laeuft ueber die Argumente, nicht ueber den
    Anzeigetext. Stuende die Fassung nur im Text, waere sie nicht an die
    Freigabe gebunden — und der Riegel waere Zierde.
    """
    schema = SPECS["secret_rescope"].input_schema
    require("expected_version" in schema.get("required", ()),
            "die Fassung ist nicht verpflichtend")
    require("expected_version" in schema.get("properties", {}),
            "die Fassung ist gar kein Argument")


def t_a_missing_version_is_refused():
    store, caps = _tresor()
    grund = ""
    try:
        caps.rescope({"secret_ref": REF, "capabilities": ", ".join(BESTAND + NEU)})
    except CapabilityRefused as exc:
        grund = exc.reason
    require_equal(grund, "scope_changed_meanwhile",
                  "ohne Fassung ging es trotzdem durch")


def t_rescope_still_demands_face_id():
    """Die Freigabeklasse bleibt, was sie war."""
    from solvio.capabilities import policy as AP
    spec = SPECS["secret_rescope"]
    require_equal(spec.base_risk.name, "CRITICAL")
    require(not spec.is_read_only(), "Umskopen gilt als lesend")
    require_equal(AP.base_class("secret_rescope", read_only=False),
                  AP.ActionClass.VERY_CRITICAL,
                  "Umskopen ist nicht mehr die strengste Klasse")


# ------------------------------------------------------------------ R4
def t_the_entries_route_names_the_capabilities_that_exist():
    """Die App darf keinen Faehigkeitsnamen raten und keinen tippen.

    Ohne diese Liste koennte ein Umfang nur um das erweitert werden, was schon
    drinsteht — und `document_find` steht eben noch nicht drin. Die Alternative
    waere ein Freitextfeld gewesen: dort ergaebe ein Tippfehler eine
    Berechtigung, die ins Leere geht, oder schlimmer eine, die jemand anderem
    gehoert.
    """
    import inspect
    from solvio.secret_vault import endpoint as EP

    quelle = inspect.getsource(EP.h_entries)
    require("bekannte_faehigkeiten" in quelle,
            "die Uebersicht nennt die bekannten Faehigkeiten nicht")
    require("router.names()" in quelle,
            "die Liste kommt nicht aus der Registratur des Cores")
    # Und sie darf den Bildschirm nicht kippen, wenn kein Router da ist.
    require("if router is not None else []" in quelle,
            "ohne Router faellt die Uebersicht aus statt leer zu bleiben")


def t_rescope_is_reachable_through_the_attested_route():
    """Der Weg bleibt der attestierte. Kein zweiter Endpunkt, kein CLI."""
    from solvio.secret_vault.endpoint import ALLOWED_CAPABILITIES

    require("secret_rescope" in ALLOWED_CAPABILITIES,
            "der attestierte Weg kennt das Umskopen nicht")

    # Und es bringt keinen Wert mit — anders als Anlegen und Ersetzen.
    from solvio.secret_vault.endpoint import NEEDS_VALUE
    require("secret_rescope" not in NEEDS_VALUE,
            "das Umskopen verlangt einen Wert, den es nicht braucht")


def t_every_shown_field_has_a_human_label():
    """Der Freigabetext zeigt keine rohen Schluesselnamen.

    Das ist keine Kosmetik. `describe_rescope` legt die DIFFERENZ in den
    Text — was hinzukommt, was wegfaellt — und genau diese Zeilen trugen bei
    der Geraeteabnahme am 03.09.2026 ihren internen Namen: der Eigentuemer las
    `entfaellt` an der Stelle, an der „Wird entfernt" stehen muss. `render_action`
    faellt fuer unbeschriftete Schluessel auf den Schluesselnamen zurueck, und
    `labels_are_unambiguous` prueft nur Kollisionen, nicht Vollstaendigkeit.
    """
    import re
    from solvio.capabilities.approval_gateway import ACTION_LABELS

    with open("src/solvio/capabilities/secret_vault.py", encoding="utf-8") as fh:
        quelle = fh.read()
    beginn = quelle.index("def describe_rescope")
    segment = quelle[beginn:quelle.index("def ", beginn + 10)]
    gezeigt = set(re.findall(r'out\["(\w+)"\]', segment))
    gezeigt |= {"zugang", "verweis"}   # stehen im Anfangswert von `out`

    _, labels = ACTION_LABELS["secret_rescope"]
    fehlend = sorted(gezeigt - set(labels))
    require(not fehlend,
            f"ohne Beschriftung, erscheint roh auf dem Freigabeschirm: {fehlend}")

    # Und die Beschriftungen bleiben unterscheidbar — zwei Zeilen mit
    # demselben Wort waeren schlimmer als eine rohe.
    from solvio.capabilities.approval_gateway import labels_are_unambiguous
    require("secret_rescope" not in labels_are_unambiguous(),
            "zwei Felder des Umskopens tragen dieselbe Beschriftung")


def t_the_difference_lines_read_as_german():
    """Die drei Zeilen, auf die es ankommt, sind lesbar benannt."""
    from solvio.capabilities.approval_gateway import ACTION_LABELS

    _, labels = ACTION_LABELS["secret_rescope"]
    for schluessel in ("kommt_hinzu", "entfaellt", "bisher", "bleibt", "fassung"):
        wort = labels.get(schluessel, "")
        require(wort and wort != schluessel,
                f"{schluessel} traegt keine eigene Beschriftung")
        require("_" not in wort,
                f"die Beschriftung von {schluessel} ist ein Schluesselname: {wort}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
