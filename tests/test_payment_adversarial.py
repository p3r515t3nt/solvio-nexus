"""Was Zahlung NICHT kann — und warum das strukturell ist, nicht bloss verboten.

Der Satz, den diese Datei nachweist:

    Ein Agent kann eine Zahlung VORSCHLAGEN. Er kann sie nicht genehmigen,
    nicht auslösen, nicht umleiten, nicht wiederholen und nicht lesen.

Jeder Fall hier ist ein Angriff, kein Randfall. Die meisten scheitern nicht an
einer Pruefung in einer Zahlungsdatei, sondern daran, dass es den Weg nicht
gibt: kein Werkzeugschema, kein Argument, keine Zelle in der Matrix.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_payment_adversarial.py
"""
import ast
import os
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions  # noqa: E402

enforce_assertions()

from _guard import require, require_equal  # noqa: E402

import payment_harness as H                                          # noqa: E402
from solvio.capabilities import execution_identity as EI             # noqa: E402
from solvio.capabilities import policy as AP                         # noqa: E402
from solvio.capabilities.contract import ArgumentSource              # noqa: E402
from solvio.capabilities.envelope import CapabilityOutcome           # noqa: E402
from solvio.contracts.trust import TrustContext, TrustLevel          # noqa: E402
from solvio.payment import merchant as PM                            # noqa: E402
from solvio.payment.intent import PaymentState                       # noqa: E402
from solvio.secret_vault import policy as VP                         # noqa: E402
from solvio.secret_vault.broker import SecretDenied                  # noqa: E402
from solvio.security.mobile_approval.execution import (               # noqa: E402
    SafeExecutionFailure)

REPO = pathlib.Path(__file__).resolve().parent.parent


# ============================================================ die Werkzeugflaeche
def t_kein_werkzeug_kann_geld_bewegen():
    """Ein Modell kann `purchase_place` nicht aufrufen, weil es ihn nicht nennen kann.

    Das ist die wichtigste Zusicherung dieser Datei. Der Router setzt eine
    Anfrage fort, wenn er zu einer Faehigkeit eine bereits freigegebene offene
    findet — gebaut fuer „ich hab's doch gerade bestaetigt". Gaebe es ein
    Werkzeug fuer den Kauf, waere „sag es zweimal" ein Kauf.
    """
    from solvio.tools.payment_capability_tools import (NEVER_EXPOSED, _SCHEMAS,
                                                       payment_capability_tools)
    require_equal(set(_SCHEMAS) & NEVER_EXPOSED, set())
    tools = payment_capability_tools(router=None, gate=None)
    namen = {t.name for t in tools if getattr(t, "expose_to_llm", False)}
    require_equal(namen & NEVER_EXPOSED, set(),
                  "eine geldbewegende Faehigkeit hat ein Werkzeugschema bekommen")
    require_equal(namen, {"payment_list_methods", "payment_intent_prepare",
                          "payment_intent_cancel", "payment_reconcile"})


def t_jedes_zahlungswerkzeug_erreicht_den_router_wirklich():
    """Ein Werkzeug, das die Faehigkeit nie anfragt, ist kein Werkzeug.

    Der Dispatcher traegt noch die V1-Schranke: bei `risk_level >= MUTATING`
    verlangt er ein Argument `confirmed`, das ein Sprachaufruf nie mitbringt.
    Der erste Bau setzte auf den Zahlungswerkzeugen die Stufe der FAEHIGKEIT
    ein — und machte damit drei von vier strukturell tot: `needs_confirmation`,
    danach `ok=False` in null Millisekunden, und die Faehigkeit wurde nie
    angefragt.

    Gefunden in der ersten Minute der Live-Abnahme, von keinem der 2355 gruenen
    Tests. Geprueft wird deshalb der ECHTE Dispatcher, nicht das Werkzeug
    allein: die Frage ist nicht „wie ist es deklariert", sondern „kommt der
    Aufruf an".
    """
    async def case():
        from solvio.tools.dispatcher import ToolDispatcher
        from solvio.tools.payment_capability_tools import payment_capability_tools

        angefragt: list[str] = []

        class _Router:
            async def execute(self, name, arguments, **kwargs):
                angefragt.append(name)
                from solvio.capabilities.envelope import CapabilityResult
                return CapabilityResult(CapabilityOutcome.SUCCESS, "c-1", name,
                                        data={"ok": True})

        class _Gate:
            def context(self):
                return SimpleNamespace(
                    has_principal=True, principal="test",
                    trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                                       user_authorized=True, note="test"),
                    origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP, commanded=True)

            def provenance_for(self, args):
                return {k: ArgumentSource.USER_DIRECT for k in args}

        dispatcher = ToolDispatcher()
        dispatcher.capability_gate = _Gate()
        werkzeuge = payment_capability_tools(_Router(), _Gate())
        for tool in werkzeuge:
            dispatcher.register(tool)

        for tool in werkzeuge:
            angefragt.clear()
            antwort = await dispatcher.dispatch(tool.name, {"vorgang": "pi-1"})
            require(not antwort.get("needs_confirmation"),
                    f"{tool.name} haengt in der alten Bestaetigungsschranke — "
                    f"die Faehigkeit wird nie angefragt")
            require_equal(angefragt, [tool.name],
                          f"{tool.name} hat den Router nicht erreicht")
    H.run(case())


def t_ein_faehigkeitswerkzeug_urteilt_nicht_ueber_die_faehigkeit():
    """Die Hausregel, an der der Fehler oben haengt — fuer den GANZEN Baum.

    Ein Faehigkeits-Werkzeug reicht weiter und entscheidet nichts; welche
    Reibung eine Handlung kostet, sagt Approval Policy V2 am Router. Wer hier
    die Stufe der Faehigkeit einsetzt, baut eine zweite Schranke vor die erste
    — und die zweite kennt die Herkunft nicht.
    """
    from solvio.tools.base import RiskLevel
    module = REPO / "src" / "solvio" / "tools"
    verdaechtig = []
    for path in sorted(module.glob("*_capability_tools.py")):
        baum = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(baum):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                ziel = ""
                if isinstance(stmt, ast.Assign) and stmt.targets:
                    ziel = getattr(stmt.targets[0], "id", "")
                if ziel != "risk_level":
                    continue
                wert = getattr(stmt.value, "attr", "")
                if wert != "HARMLESS":
                    verdaechtig.append(f"{path.name}::{node.name} = {wert}")
    require_equal(verdaechtig, [],
                  f"ein Faehigkeits-Werkzeug urteilt selbst: {verdaechtig}")
    from solvio.tools.payment_capability_tools import PaymentTool
    require_equal(PaymentTool.risk_level, RiskLevel.HARMLESS)


def t_es_gibt_kein_werkzeug_das_zahlungsmaterial_herausgibt():
    """Kein `get_card_number`, kein `reveal_payment_token`, kein `execute_payment_raw`.

    Gezaehlt wird der ganze Baum, nicht nur die Zahlungsdatei: ein solcher Weg
    waere ueberall gleich schlimm.
    """
    verboten = ("get_card", "card_number", "reveal_payment", "reveal_card",
                "execute_payment_raw", "dump_payment", "export_payment",
                "payment_secret", "get_pan", "read_cvv")
    treffer = []
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in verboten:
            if f"def {needle}" in text or f'"{needle}"' in text:
                treffer.append(f"{path.name}:{needle}")
    require_equal(treffer, [], f"ein Weg zum Zahlungsmaterial existiert: {treffer}")


def t_die_zahlungsablage_hat_keine_spalte_fuer_kartendaten():
    from solvio.payment.store import SCHEMA
    for verboten in ("pan", "cvv", "cvc", "card_number", "iban", "expiry",
                     "security_code", "magstripe"):
        require(verboten not in SCHEMA.lower(),
                f"die Zahlungsablage hat eine Spalte fuer {verboten}")


def t_das_modell_sieht_weder_token_noch_tresorverweis():
    """Was ein Modell ueber ein Zahlungsmittel erfaehrt, ist eine Beschreibung."""
    instrument = H.default_instrument()
    sicht = str(instrument.describe(for_model=True))
    for geheim in (instrument.provider_token, instrument.provider_secret_ref,
                   instrument.provider_readonly_ref, instrument.display_hint):
        require(geheim not in sicht,
                f"die Modellsicht traegt {geheim[:24]!r}")


def t_kein_ergebnis_traegt_je_einen_anbieterzugang():
    """§45 O: was aus einer Zahlung herauskommt, ist sichere Wahrheit.

    Geprueft wird nicht der Vorsatz, sondern das ERGEBNIS: jedes Feld, das die
    Faehigkeit zurueckgibt, jede Zeile des Zahlungsbuchs und jede Modellsicht
    wird gegen den Zaun fuer Zahlungsmaterial UND gegen den fuer Zugangsdaten
    gehalten. Eine Mutation, die einen Anbieterschluessel ins Ergebnis stellte,
    ueberlebte den ersten Durchgang — das hier ist die Antwort darauf.
    """
    async def case():
        from solvio.payment.firewall import is_payment_material
        from solvio.secret_vault.firewall import is_credential
        from solvio.capabilities import payment as payment_capabilities
        from unittest.mock import patch
        rig = await H.Rig().start()
        try:
            # Reproduzierter Fehlalarm: ein echter wirtschaftlicher SHA-256
            # enthaelt zufaellig eine Luhn-gueltige Ziffernfolge. Die Frist und
            # Vorgangskennung dieser lokalen Fixture machen den Fall stabil;
            # Anbieterzugang, Tresor und Zahlungsweg bleiben echt verdrahtet.
            now = 1789146822.615 - payment_capabilities.INTENT_TTL
            rig.caps._clock = rig.caps.executor._clock = lambda: now
            with patch.object(payment_capabilities._secrets, "token_hex",
                              return_value="9f95d20026a67d8c"):
                view = await H.prepare_intent(rig)
            digest = rig.store.intent(view["payment_intent_id"]).economic_digest()
            require_equal(digest, "34093155f3f47104963926543efcdb6e9f3e48059e4515259ff109410d52dca0")
            require(is_payment_material(digest), "der reproduzierte Pruefsummen-Fehlalarm fehlt")
            beschreibung = rig.caps.describe_purchase(
                {"vorgang": view["payment_intent_id"],
                 "pruefsumme": view["pruefsumme"]})
            ergebnis = await H.pay(rig, view)
            liste = await rig.caps.list_methods({})
            buch = rig.store.ledger(limit=100)
            geheim = rig.provider_secret
            for name, ding in (("Vorbereitung", view), ("Beschreibung", beschreibung),
                               ("Kaufergebnis", ergebnis), ("Zahlungsmittel", liste),
                               ("Zahlungsbuch", buch)):
                text = str(ding)
                require(geheim not in text, f"{name} traegt den Anbieterzugang")
                # NUR das benannte, gegen den gespeicherten Vorgang gepruefte
                # Digestfeld ist kein Freitext. Ein Geheimnis/Kartentext in
                # irgendeinem anderen Feld bleibt unveraendert sichtbar.
                payment_text = text
                if name in ("Vorbereitung", "Beschreibung"):
                    require_equal(ding["pruefsumme"], digest if name == "Vorbereitung" else digest[:16])
                    payment_text = str({**ding, "pruefsumme": "gepruefter Vorgangsdigest"})
                require(not is_payment_material(payment_text),
                        f"{name} sieht wie Zahlungsmaterial aus")
                require(not is_credential(text),
                        f"{name} sieht wie ein Zugangsdatum aus")
                for wort in ("secret://", "sk_live", "sk_test", "whsec_",
                             "pm_sandbox"):
                    require(wort not in text, f"{name} nennt {wort}")
        finally:
            await rig.stop()
    H.run(case())


def t_eine_kartennummer_kommt_nicht_ins_zahlungsbuch():
    """Der Zaun steht VOR dem Schreibvorgang, nicht daneben.

    Eine Mutation, die ihn entfernte, ueberlebte den ersten Durchgang: kein
    Test hatte je versucht, eine Kartennummer hineinzuschreiben.
    """
    async def case():
        from solvio.payment.firewall import PaymentMaterialRefused
        rig = await H.Rig().start()
        try:
            for feld, wert in (("description", "4242 4242 4242 4242"),
                               ("description", "CVV 123"),
                               ("provider_ref", "sk_live_abcdefgh12345"),
                               ("description", "DE89 3704 0044 0532 0130 00")):
                raised = None
                try:
                    rig.store.record(payment_intent_id="pi-probe", event="charged",
                                     execution_id=f"x-{feld}-{len(wert)}",
                                     **{feld: wert})
                except PaymentMaterialRefused as exc:
                    raised = exc
                require(raised is not None,
                        f"{wert[:12]}… kam ins Zahlungsbuch")
                require(wert not in str(raised),
                        "die Ausnahme traegt den Wert, den sie abweisen soll")
            # Und ein Zahlungsmittel mit einer Kartennummer im Namen ebenso.
            raised = None
            try:
                rig.store.put_instrument(
                    H.default_instrument(display_name="4242424242424242"))
            except PaymentMaterialRefused as exc:
                raised = exc
            require(raised is not None,
                    "eine Kartennummer kam als Anzeigename durch")
        finally:
            await rig.stop()
    H.run(case())


# ============================================================ der Tresor
def t_ein_zahlungsverweis_wird_nie_zu_einem_tresorverweis():
    """Zwei Schemata, die sich nicht ineinander uebersetzen lassen."""
    from solvio.payment import refs as PR
    from solvio.secret_vault import refs as SR
    require(not PR.is_valid("secret://payment-provider/sandbox"))
    require(not SR.is_valid("payment://shopping/default"))


def t_ein_fremdes_modul_bekommt_den_anbieterzugang_nicht():
    """Die zweite Schranke des Maklers: der Modulname des ECHTEN Aufrufers.

    Selbst wer die richtige Executor-Kennung BEHAUPTET, kommt nicht durch —
    und das ist der Unterschied zwischen einem Tresor und einer Variablen.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            from solvio.secret_vault.broker import SecretBroker
            broker = SecretBroker(rig.vault)
            with H.use_context(capability="purchase_place", user_present=True):
                raised = None
                try:
                    # Dieser Aufruf steht in einer TESTDATEI, nicht in
                    # `solvio.payment.executor`. Genau daran scheitert er.
                    with broker.use(H.CHARGE_REF,
                                    executor=VP.ExecutorId.PAYMENT,
                                    target=rig.base_url) as material:
                        material.plaintext()
                except SecretDenied as exc:
                    raised = exc
            require(raised is not None, "ein fremdes Modul bekam den Zugang")
            require_equal(raised.reason, VP.Denied.EXECUTOR_MODULE_MISMATCH)
        finally:
            await rig.stop()
    H.run(case())


def t_der_belastungszugang_verlangt_zusaetzlich_anwesenheit():
    """Ein zweiter, vom Router unabhaengiger Face-ID-Zaun.

    Ein Kauf, der irgendwie am Freigabeweg vorbeikaeme, bekommt vom Tresor
    trotzdem nichts: `requires_user_presence` ist nur auf dem Fortsetzungsweg
    erfuellt, und den betritt man nur mit einer bestaetigten Entscheidung.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            # Ohne `user_present` — also NICHT auf dem Fortsetzungsweg.
            with H.use_context(capability="purchase_place", user_present=False,
                               approval_id="ap-1"), EI.bound(H.identity("ap-1")):
                raised = None
                try:
                    await rig.caps.purchase({"vorgang": view["payment_intent_id"],
                                             "pruefsumme": view["pruefsumme"]})
                except SafeExecutionFailure as exc:
                    raised = exc
            require(raised is not None,
                    "ohne bewiesene Anwesenheit wurde belastet")
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


def t_ein_hintergrundlauf_bekommt_den_anbieterzugang_nicht():
    async def case():
        rig = await H.Rig().start()
        try:
            from solvio.secret_vault import policy as VPX
            row = rig.vault.row(H.READ_REF)
            policy = VPX.from_row(row)
            verdict = VPX.evaluate(policy, VPX.UseRequest(
                capability="payment_intent_prepare",
                executor=VPX.ExecutorId.PAYMENT, target=rig.base_url,
                origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                caller_module="solvio.payment.executor"))
            require(not verdict.allowed)
            require_equal(verdict.reason, VPX.Denied.BACKGROUND_NOT_ALLOWED)
        finally:
            await rig.stop()
    H.run(case())


# ============================================================ die Matrix
def t_aus_dem_hintergrund_wird_nicht_bezahlt():
    """§35: eine Zahlung aus einem Zeitplan ist DENY, nicht Face ID.

    Nicht durch eine Pruefung im Zahlungsteil — durch die Zelle der Matrix.
    Eine Hintergrundaufgabe kann kein Gesicht vorlegen; ein `REQUIRE_FACE_ID`
    waere eine Anfrage, die ewig steht. `DENY` ist die ehrliche Antwort.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig)
            view = await H.prepare_intent(rig)
            result = await stack.execute(
                "purchase_place",
                {"vorgang": view["payment_intent_id"],
                 "pruefsumme": view["pruefsumme"]},
                origin=AP.OriginClass.BACKGROUND_AUTOMATION)
            require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY)
            require_equal(result.reason, "policy_denied")
            require_equal(stack.requests, [],
                          "eine Hintergrundzahlung landete als Freigabe auf dem iPhone")
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


def t_fremder_inhalt_kann_keine_zahlung_ausloesen():
    """§10: „Kauf das sofort" in einer E-Mail erzeugt keine Nutzerautoritaet."""
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig)
            view = await H.prepare_intent(rig)
            result = await stack.execute(
                "purchase_place",
                {"vorgang": view["payment_intent_id"],
                 "pruefsumme": view["pruefsumme"]},
                origin=AP.OriginClass.EXTERNAL_UNTRUSTED)
            require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY)
            require_equal(stack.requests, [])
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


def t_ein_argument_aus_fremdem_inhalt_verschaerft_statt_zu_lockern():
    """Auch vom bewusst benutzten iPhone: fremder Inhalt in den Argumenten
    schliesst eine Direktausfuehrung aus. Bei VERY_CRITICAL aendert das nichts —
    und genau das soll gezeigt werden: es wird nie lockerer."""
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig)
            view = await H.prepare_intent(rig)
            result = await stack.execute(
                "purchase_place",
                {"vorgang": view["payment_intent_id"],
                 "pruefsumme": view["pruefsumme"]},
                provenance={"vorgang": ArgumentSource.UNTRUSTED_CONTENT,
                            "pruefsumme": ArgumentSource.UNTRUSTED_CONTENT})
            require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED)
        finally:
            await rig.stop()
    H.run(case())


def t_auch_vom_vertrauten_telefon_kostet_eine_zahlung_face_id():
    """§8: VERY_CRITICAL bleibt aus JEDER Herkunft biometrisch."""
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig)
            view = await H.prepare_intent(rig)
            result = await stack.execute(
                "purchase_place",
                {"vorgang": view["payment_intent_id"],
                 "pruefsumme": view["pruefsumme"]})
            require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED)
            require_equal(len(stack.requests), 1)
            aufgabe = stack.requests[0]["task"]
            require("89,98 EUR" in aufgabe, "der Betrag steht nicht im Freigabetext")
            require("SOLVIO Testladen" in aufgabe)
            require("https://sandbox.solvio.invalid" in aufgabe,
                    "die Haendlerherkunft steht nicht im Freigabetext")
            require("Angefragt über: iPhone-App" in aufgabe)
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0,
                          "es wurde vor der Freigabe belastet")
        finally:
            await rig.stop()
    H.run(case())


def t_im_schattenlauf_bewegt_sich_ebenfalls_kein_geld():
    """Der Schattenlauf faellt auf die alte, herkunftsblinde Schwelle zurueck.

    `base_risk=CRITICAL` sorgt dafuer, dass auch sie eine Freigabe verlangt —
    und der Identitaetszaun im Handler faengt jeden Weg, der trotzdem
    durchkaeme.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig, policy_mode="shadow")
            view = await H.prepare_intent(rig)
            result = await stack.execute(
                "purchase_place",
                {"vorgang": view["payment_intent_id"],
                 "pruefsumme": view["pruefsumme"]})
            require_equal(result.outcome, CapabilityOutcome.APPROVAL_REQUIRED)
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


# ============================================================ der ganze Weg
def t_eine_abgelehnte_freigabe_ist_endgueltig():
    """§45 K: einmal abgelehnt heisst abgelehnt — und es wird NICHT neu gefragt.

    **Dieser Fall war gruen und hat trotzdem nichts bewiesen.** Er pruefte, dass
    der zweite Aufruf kein Erfolg ist und nichts belastet wurde. Beides stimmte.
    Was er nicht pruefte: WAS der zweite Aufruf statt dessen tat. Er erzeugte
    eine NEUE Freigabeanfrage — `not_approved` verschluckte die Ablehnung, der
    Router verwarf die alte Anfrage und fragte erneut.

    Gefunden nicht hier, sondern in der Live-Abnahme von DEBT-0126 am
    2026-08-30: der Eigentuemer lehnte zweimal ab und bekam zweimal sofort
    dieselbe Karte zurueck. Der Vertrag verlangt woertlich „eine ABGELEHNTE
    Zahlungsfreigabe UND IHRE ENDGUELTIGKEIT" — die Endgueltigkeit stand in
    keiner Zusicherung.

    Ab jetzt zaehlt dieser Fall die Anfragen.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig)
            view = await H.prepare_intent(rig)
            args = {"vorgang": view["payment_intent_id"],
                    "pruefsumme": view["pruefsumme"]}
            first = await stack.execute("purchase_place", args)
            require_equal(first.outcome, CapabilityOutcome.APPROVAL_REQUIRED)
            require_equal(len(stack.requests), 1)
            approval_id = stack.requests[0]["approval_id"]
            stack.cp.reject(approval_id)

            second = await stack.execute("purchase_place", args)
            require(second.outcome is not CapabilityOutcome.SUCCESS,
                    "eine abgelehnte Freigabe hat den Kauf getragen")
            # DAS ist der Satz, der vorher fehlte.
            require_equal(second.outcome, CapabilityOutcome.REJECTED_BY_POLICY,
                          f"nach einer Ablehnung kam {second.outcome}")
            require_equal(second.reason, "denied", str(second.reason))
            require_equal(len(stack.requests), 1,
                          "die Ablehnung hat eine neue Freigabeanfrage erzeugt — "
                          "der Mensch wird gefragt, bis er ja sagt")

            # Ehrlich zur Reichweite: ein Nein beendet DIESE Frage, es sperrt die
            # Faehigkeit nicht. Sagt ein Mensch von sich aus noch einmal, was er
            # will, darf daraus eine neue Anfrage werden — sonst waere eine
            # versehentliche Ablehnung eine Sperre bis zum Neustart. Was NICHT
            # mehr passiert, ist die Wiederholung OHNE neuen menschlichen Anlass:
            # die Wiedervorlage derselben Handlung laeuft ins Leere, und genau
            # die schickt die App nach einer Entscheidung ab.
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
            intent = rig.store.intent(view["payment_intent_id"])
            require(intent.state is not PaymentState.SUCCEEDED)
        finally:
            await rig.stop()
    H.run(case())


def t_der_ganze_weg_endet_in_genau_einer_buchung():
    """Vorbereiten, Freigabe, Face ID, Fortsetzen — und EINE Belastung.

    Der Papier-Freigabepfad reicht dieselbe Nutzlast wie der echte, samt
    `execution_id` und `idempotency_key`. Damit prueft dieser Fall auch die
    Naht, ueber die der Handler seine Ausfuehrungsidentitaet bekommt.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig)
            view = await H.prepare_intent(rig)
            args = {"vorgang": view["payment_intent_id"],
                    "pruefsumme": view["pruefsumme"]}
            first = await stack.execute("purchase_place", args)
            require_equal(first.outcome, CapabilityOutcome.APPROVAL_REQUIRED)
            stack.cp.approve(stack.requests[0]["approval_id"])
            second = await stack.execute("purchase_place", args)
            require_equal(second.outcome, CapabilityOutcome.SUCCESS,
                          f"der freigegebene Kauf lief nicht: {second.reason}")
            stats = await rig.stats()
            require_equal(stats["created_charges"], 1)
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 1)
            # Und ein dritter Anlauf unter derselben Freigabe laeuft ins Leere.
            third = await stack.execute("purchase_place", args)
            require(third.outcome is not CapabilityOutcome.SUCCESS)
            stats = await rig.stats()
            require_equal(stats["created_charges"], 1,
                          "eine verbrauchte Freigabe hat ein zweites Mal bezahlt")
        finally:
            await rig.stop()
    H.run(case())


def t_eine_freigabe_traegt_keine_andere_menge():
    """§7: geaenderte Stueckzahl macht die Freigabe wertlos.

    Zwei Stueck zu 42,50 und ein Stueck zu 85,00 ergeben denselben Betrag und
    sind nicht derselbe Kauf. Deshalb steht die Menge einzeln im Digest.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            eins = await H.prepare_intent(rig, posten="Ding|1|8500")
            zwei = await H.prepare_intent(rig, posten="Ding|2|4250")
            require(eins["pruefsumme"] != zwei["pruefsumme"],
                    "zwei verschiedene Bestellungen ergaben dieselbe Pruefsumme")
            beschreibung_a = rig.caps.describe_purchase(
                {"vorgang": eins["payment_intent_id"],
                 "pruefsumme": eins["pruefsumme"]})
            beschreibung_b = rig.caps.describe_purchase(
                {"vorgang": zwei["payment_intent_id"],
                 "pruefsumme": zwei["pruefsumme"]})
            require(beschreibung_a["menge"] != beschreibung_b["menge"])
        finally:
            await rig.stop()
    H.run(case())


def t_eine_abgelaufene_absicht_wird_nicht_bezahlt():
    """§36: was am Morgen vorbereitet wurde, laeuft am Abend nicht mehr."""
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            import dataclasses
            intent = rig.store.intent(view["payment_intent_id"])
            rig.store.put_intent(dataclasses.replace(
                intent, state=PaymentState.APPROVED))
            # Die Uhr weiterstellen, statt zu warten.
            rig.caps._clock = lambda: intent.expires_at + 1.0
            with H.use_context(capability="purchase_place", user_present=True,
                               approval_id="ap-1"), EI.bound(H.identity("ap-1")):
                raised = None
                try:
                    await rig.caps.purchase({"vorgang": view["payment_intent_id"],
                                             "pruefsumme": view["pruefsumme"]})
                except SafeExecutionFailure as exc:
                    raised = exc
            require(raised is not None, "eine abgelaufene Absicht wurde bezahlt")
            require("intent_expired" in str(raised))
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


# ============================================================ Haendleridentitaet
def t_derselbe_anzeigename_an_fremder_herkunft_ist_ein_fremder_haendler():
    """§11: „Amazon" ist ein Wort, `https://www.amazon.de` ist ein Ort."""
    require(PM.merchant_for_origin("https://sandbox.solvio.invalid.angreifer.example")
            is None)
    require(PM.merchant_for_origin("https://sandbox.solvio.invalid") is not None)
    raised = None
    try:
        PM.resolve("sandbox-shop", "https://sandbox.solvio.invalid.angreifer.example")
    except PM.InvalidMerchant as exc:
        raised = exc
    require(raised is not None, "eine fremde Herkunft kam unter bekanntem Namen durch")


def t_ein_homograph_ist_ein_anderer_host():
    """Kyrillisches „а" sieht aus wie lateinisches „a" und ist es nicht."""
    echt = PM.normalize_origin("https://amazon.de")
    falsch = PM.normalize_origin("https://аmazon.de")
    require(echt != falsch, "ein Homograph wurde zum selben Host normalisiert")
    require(falsch.startswith("https://xn--"),
            f"der Homograph wurde nicht punycodiert: {falsch}")


def t_eine_unverschluesselte_herkunft_ist_kein_haendler():
    for bad in ("http://sandbox.solvio.invalid",
                "https://sandbox.solvio.invalid/checkout",
                "https://sandbox.solvio.invalid@angreifer.example"):
        raised = None
        try:
            PM.normalize_origin(bad)
        except PM.InvalidMerchant as exc:
            raised = exc
        require(raised is not None, f"{bad} wurde als Herkunft akzeptiert")


# ============================================================ Quelltextzusagen
def t_der_executor_leiht_genau_einmal_und_ohne_selbstauskunft():
    """Eine Zusicherung am Quelltext, weil ein Kommentar keine ist.

    `origin=` und `user_present=` sind gueltige Schluesselwoerter des Maklers,
    und beide wuerden ausgerechnet die zwei Schranken aushebeln, auf die es
    ankommt. Sie duerfen an dieser Stelle nicht vorkommen.
    """
    quelle = (REPO / "src" / "solvio" / "payment" / "executor.py").read_text(
        encoding="utf-8")
    baum = ast.parse(quelle)
    stellen = [node for node in ast.walk(baum)
               if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == "use"]
    require_equal(len(stellen), 1,
                  f"der Executor leiht an {len(stellen)} Stellen statt an einer")
    schluessel = {k.arg for k in stellen[0].keywords if k.arg}
    require("origin" not in schluessel,
            "der Executor sucht sich seine Herkunft selbst aus")
    require("user_present" not in schluessel,
            "der Executor behauptet die Anwesenheit eines Menschen")


def t_nur_der_zahlungs_executor_darf_die_zahlungskennung_benutzen():
    """Das Praefix ist EIN Modul, nicht ein Paket.

    `solvio.capabilities.` waere die bequeme Wahl gewesen — und haette jeder
    Faehigkeit im Baum den Belastungszugang geoeffnet, `router.py`
    eingeschlossen.
    """
    praefixe = VP.EXECUTOR_MODULES[VP.ExecutorId.PAYMENT]
    require_equal(praefixe, ("solvio.payment.executor",))


def t_die_geldnamen_sind_als_geburtsrecht_eingetragen():
    """Sie stehen dort, seit es sie noch gar nicht gab. Jetzt gibt es sie."""
    for name in ("purchase_place", "payment_send", "bank_transfer", "invest_order",
                 "payment_method_add", "payment_method_remove",
                 "payment_limit_raise"):
        require(name in AP.VERY_CRITICAL_BY_BIRTH, f"{name} fehlt im Geburtsrecht")
        require_equal(AP.base_class(name, read_only=False),
                      AP.ActionClass.VERY_CRITICAL)
        # Auch eine Faehigkeit, die sich selbst als lesend deklariert, bleibt es.
        require_equal(AP.base_class(name, read_only=True),
                      AP.ActionClass.VERY_CRITICAL,
                      f"{name} konnte sich als lesend tarnen")


def t_ueberweisung_und_wertpapier_sind_reserviert_aber_nicht_gebaut():
    """§29: V1 baut sie NICHT. Der Name steht da, die Faehigkeit nicht."""
    from solvio.capabilities.payment import SPECS
    from solvio.payment.endpoint import ALLOWED_CAPABILITIES
    for name in ("bank_transfer", "invest_order", "payment_send"):
        require(name not in SPECS, f"{name} wurde gebaut")
        require(name not in ALLOWED_CAPABILITIES,
                f"{name} hat eine Route am Zahlungsendpunkt")


def t_der_zahlungsendpunkt_hat_einen_eigenen_domaenentrenner():
    """Eine Wissens- oder Tresor-Assertion kann keine Zahlung eroeffnen."""
    from solvio.payment.endpoint import DOMAIN_PAYMENT_MUTATION, client_data_hash
    from solvio.secret_vault.endpoint import (DOMAIN_VAULT_MUTATION,
                                              client_data_hash as vault_hash)
    require(DOMAIN_PAYMENT_MUTATION != DOMAIN_VAULT_MUTATION)
    gleich = b"dieselben bytes"
    require(client_data_hash(gleich) != vault_hash(gleich),
            "zwei Domaenen ergaben denselben clientDataHash")


def t_der_zahlungsendpunkt_kennt_nur_eine_geschlossene_liste():
    from solvio.payment.endpoint import ALLOWED_CAPABILITIES
    from solvio.capabilities.payment import MODEL_FACING
    require_equal(ALLOWED_CAPABILITIES & MODEL_FACING, set(),
                  "eine Sprachfaehigkeit haengt zusaetzlich am Zahlungsendpunkt")
    require("purchase_place" in ALLOWED_CAPABILITIES)


def t_kein_playbook_startet_die_zahlung_neu():
    """§24: ein Neustart ist genau die Handlung, die aus einem unklaren Ausgang
    zwei Belastungen machen kann."""
    from solvio.doctor.playbooks import FORBIDDEN_RESTARTS, PLAYBOOKS, for_component
    require("payment" in FORBIDDEN_RESTARTS)
    require_equal([k for k, p in PLAYBOOKS.items() if p.component == "payment"], [])
    require_equal(for_component("payment"), [])


def t_keine_hintergrundaufgabe_kann_eine_zahlung_benennen():
    """Ein zweiter, struktureller Zaun hinter der DENY-Zelle."""
    from solvio.capabilities.proactive import ALLOWED_ACTIONS
    from solvio.capabilities.payment import SPECS
    require_equal(set(ALLOWED_ACTIONS) & set(SPECS), set(),
                  "eine Zahlungsfaehigkeit steht auf der Zeitplanliste")


def t_die_gesundheitspruefung_bucht_nichts():
    """Eine Pruefung, die alle fuenfzehn Sekunden Geld bewegt, ist keine."""
    quelle = (REPO / "src" / "solvio" / "payment" / "health.py").read_text(
        encoding="utf-8")
    for verboten in ("charge(", "broker", "use(", "aiohttp", "provider."):
        require(verboten not in quelle,
                f"die Gesundheitspruefung benutzt {verboten}")


def t_eine_meldung_traegt_keinen_betrag_und_keinen_verweis():
    """§39 und die Falle darunter: der proaktive Eingang VERWEIGERT bei
    zugangsdatenfoermigem Text, statt zu schwaerzen. Eine Meldung, die ihn zum
    Werfen bringt, ist keine Meldung."""
    from solvio.payment.notify import _MESSAGES
    from solvio.secret_vault.firewall import is_credential
    from solvio.payment.firewall import is_payment_material
    for kind, (summary, label) in _MESSAGES.items():
        require(not is_credential(summary), f"{kind}: Meldung sieht wie ein Zugang aus")
        require(not is_payment_material(summary),
                f"{kind}: Meldung sieht wie Zahlungsmaterial aus")
        for wort in ("EUR", "€", "payment://", "secret://", "pm_"):
            require(wort not in summary, f"{kind}: die Meldung nennt {wort}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
