"""Die Zahlung, gegen einen ECHTEN Anbieter — genau einmal, oder ehrlich unklar.

Der Satz, den diese Datei nachweist:

    SOLVIO darf beauftragt werden zu bezahlen. Ein Agent besitzt dabei nie das
    Zahlungsmittel, und eine Wiederholung kauft nie ein zweites Mal.

Geprueft wird nicht gegen ein Attrappenobjekt, sondern gegen einen laufenden
Dienst auf der Rueckschleife. Die interessanten Faelle entstehen zwischen zwei
Prozessen: eine Buchung, deren Antwort nie ankommt; eine Idempotenzkennung, die
den zweiten Versuch auf den ersten Vorgang zeigen laesst; eine Bank, die eine
Bestaetigung verlangt. Eine Attrappe kann keinen davon beweisen.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_payment_execution.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions  # noqa: E402

enforce_assertions()

from _guard import require, require_equal  # noqa: E402

import payment_harness as H                                          # noqa: E402
from solvio.capabilities import execution_identity as EI             # noqa: E402
from solvio.capabilities import policy as AP                         # noqa: E402
from solvio.capabilities.contract import (AmbiguousExecution,         # noqa: E402
                                          CapabilityRefused)
from solvio.capabilities.envelope import CapabilityOutcome           # noqa: E402
from solvio.payment.intent import PaymentState                       # noqa: E402
from solvio.payment.store import DuplicateCharge                     # noqa: E402
from solvio.security.mobile_approval.execution import (               # noqa: E402
    SafeExecutionFailure)


# ------------------------------------------------------------------ der Normalfall
def t_ein_freigegebener_kauf_belastet_genau_einmal():
    """Der Grundfall: vorbereiten, freigeben, bezahlen — und EINE Buchung.

    „Genau einmal" wird nicht am Ergebnis abgelesen, sondern am Anbieter: der
    Pruefdienst zaehlt selbst mit, wie viele Buchungen er angelegt hat.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            require_equal(view["zustand"], "ready_for_approval")
            result = await H.pay(rig, view)
            require_equal(result["zustand"], "succeeded")
            require_equal(result["betrag"], "89,98 EUR")
            stats = await rig.stats()
            require_equal(stats["created_charges"], 1,
                          "der Anbieter hat mehr als eine Buchung angelegt")
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 1)
        finally:
            await rig.stop()
    H.run(case())


def t_der_betrag_kommt_vom_anbieter_nicht_vom_modell():
    """Das Modell erwartet 84,99 — bezahlt werden 89,98, und das steht dran.

    §12: der Endbetrag stammt aus dem vertrauenswuerdigen Executor. Die
    Erwartung des Modells wird VERGLICHEN, nie uebernommen.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig, erwartet="8499")
            require_equal(view["betrag_lesbar"], "89,98 EUR")
            require(not view["erwartung_stimmt"],
                    "die Abweichung wurde nicht bemerkt")
            require("Anbieter" in view.get("hinweis", ""),
                    "die Abweichung steht nicht im Klartext da")
            beschreibung = rig.caps.describe_purchase(
                {"vorgang": view["payment_intent_id"],
                 "pruefsumme": view["pruefsumme"]})
            require_equal(beschreibung["betrag"], "89,98 EUR")
        finally:
            await rig.stop()
    H.run(case())


def t_die_beschreibung_haengt_nicht_an_der_uhr():
    """Zweimal beschreiben ergibt zweimal denselben Text.

    Sonst faellt der Digest beim Fortsetzen anders aus, die Freigabe gilt nicht
    mehr, der Router legt eine neue an — und der Mensch bestaetigt in einer
    Schleife etwas, das nie wirkt.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            args = {"vorgang": view["payment_intent_id"],
                    "pruefsumme": view["pruefsumme"]}
            first = rig.caps.describe_purchase(args)
            second = rig.caps.describe_purchase(args)
            require_equal(first, second)
            floats = [k for k, v in first.items() if isinstance(v, float)]
            require_equal(floats, [], "ein Fliesskomma im Freigabetext")
        finally:
            await rig.stop()
    H.run(case())


def t_die_beschreibung_bleibt_gleich_auch_wenn_zeit_vergeht():
    """Dieselbe Beschreibung nach einer VERSCHOBENEN Uhr, nicht nach 0 Sekunden.

    Die erste Fassung dieses Falls rief zweimal hintereinander auf — und das
    haette eine `jetzt + x`-Frist gar nicht bemerkt, weil beide Aufrufe in
    dieselbe Sekunde fielen. Eine Mutation hat genau das gezeigt: sie ueberlebte.
    Jetzt wird die Uhr dazwischen weitergestellt.

    **Warum 300 s und nicht mehr eine Stunde.** Seit der Live-Abnahme von
    DEBT-0126 legt `describe_purchase` einen ABGELAUFENEN Vorgang gar nicht mehr
    vor (`intent_expired`, siehe den Fall darunter). Eine Stunde liegt jenseits
    der Frist von `INTENT_TTL` = 480 s; der Fall haette danach die Absage
    gemessen statt der Gleichheit — und damit aufgehoert, seine Mutation zu
    fangen. 300 s liegen INNERHALB der Gueltigkeit und bewegen eine
    `jetzt + x`-Frist um volle fuenf Minuten: die Mutation stirbt weiterhin.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            args = {"vorgang": view["payment_intent_id"],
                    "pruefsumme": view["pruefsumme"]}
            first = rig.caps.describe_purchase(args)
            jetzt = rig.caps._clock()
            rig.caps._clock = lambda: jetzt + 300.0
            second = rig.caps.describe_purchase(args)
            require_equal(first, second,
                          "die Beschreibung haengt an der Uhr — eine Freigabe "
                          "waere damit nie einloesbar")
        finally:
            await rig.stop()
    H.run(case())


def t_eine_zufaellige_anbieterkennung_zerlegt_keine_gesunde_zahlung():
    """Der Zaun gegen Kartennummern darf keine gesunde Zahlung kosten.

    **Gefunden, weil das Gate flackerte** — etwa jeder fuenfte volle Lauf fiel mit
    `PaymentMaterialRefused: primary_account_number` in einem Fall, der mit
    Kartennummern nichts zu tun hat. Der Zaun sucht Ziffernfolgen ab dreizehn
    Stellen mit gueltiger Luhn-Pruefung; eine 16-stellige Hex-Kennung des
    Anbieters trifft das gelegentlich, und ein voller Lauf schreibt Dutzende davon.

    Die kalte Abnahme hatte das schon einmal gefunden und am `claimed`-Eintrag
    auf zwoelf Zeichen gekuerzt — an ZWEI weiteren Stellen blieb der
    32-stellige Praefix stehen, und ausgerechnet an den beiden, die „ich weiss
    nicht, ob das durchging" sagen. Dazu die Kennung des Kostenvoranschlags, die
    Anbietertext ist und gar keine Absicherung hatte.

    Dieser Fall wartet nicht auf den Zufall: er stellt dem Pruefanbieter
    Kennungen, die den Zaun mit Sicherheit ausloesen.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            # `ch_4539578763621486` und `q_4539578763621486` loesen den Zaun
            # SICHER aus — nachgerechnet an `firewall.reason_for`. Beide Wege
            # muessen sie ueberstehen: das Vorbereiten und die Belastung.
            import dataclasses

            from solvio.payment.providers import sandbox as SBX_P
            echte_quote = SBX_P.SandboxProvider.quote
            echte_charge = SBX_P.SandboxProvider.charge

            async def boese_quote(self, *a, **kw):
                r = await echte_quote(self, *a, **kw)
                return dataclasses.replace(r, quote_ref="q_4539578763621486")

            async def boese_charge(self, *a, **kw):
                r = await echte_charge(self, *a, **kw)
                return dataclasses.replace(r, charge_ref="ch_4539578763621486")

            SBX_P.SandboxProvider.quote = boese_quote
            SBX_P.SandboxProvider.charge = boese_charge
            try:
                view = await H.prepare_intent(rig)
                await H.pay(rig, view)
            finally:
                SBX_P.SandboxProvider.quote = echte_quote
                SBX_P.SandboxProvider.charge = echte_charge

            # Auch das Vorbereiten hat ueberlebt und steht im Buch.
            quoted = [r for r in rig.store.ledger(
                payment_intent_id=view["payment_intent_id"], limit=50)
                if r["event"] == "quoted"]
            require_equal(len(quoted), 1,
                          "der Kostenvoranschlag ging an seiner eigenen Kennung "
                          "verloren")
            require_equal(quoted[0]["provider_ref"], "",
                          "die gefaehrliche Voranschlagskennung steht im Buch")

            # Die Zeile MUSS da sein — notfalls ohne Kennung.
            gebucht = rig.charged_rows(view["payment_intent_id"])
            require_equal(len(gebucht), 1,
                          "die Belastung ging verloren, weil die Anbieterkennung "
                          "wie eine Kartennummer aussah")
            require_equal(gebucht[0]["provider_ref"], "",
                          "die gefaehrliche Kennung steht im Buch")

            # Und keine der geschriebenen Zeilen traegt Zahlungsmaterial.
            from solvio.payment.firewall import reason_for
            for row in rig.store.ledger(limit=200):
                require_equal(reason_for(str(row["provider_ref"] or "")), "",
                              f"Zahlungsmaterial im Buch: {row['event']}")
        finally:
            await rig.stop()
    H.run(case())


def t_die_unklaren_zeilen_koennen_den_zaun_nicht_ausloesen():
    """Was „unbekannt" sagt, muss geschrieben werden koennen — immer.

    Die beiden Zeilen, die einen unklaren Ausgang festhalten, tragen einen
    Praefix der Idempotenzkennung. Bei zweiunddreissig Zeichen kann er den Zaun
    ausloesen; dann gaebe es die Zeile nicht, und ein moeglicher Kauf waere
    unsichtbar. Zwoelf Zeichen koennen die Regel nicht ausloesen — nachgewiesen
    an der Regel selbst, nicht an einer Stichprobe.
    """
    import re

    from solvio.payment.firewall import _DIGIT_RUN
    quelle = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                               "payment", "executor.py"), encoding="utf-8").read()
    lang = re.findall(r"idempotency_key\[:(\d+)\]", quelle)
    require(lang, "kein Praefix der Idempotenzkennung mehr im Executor gefunden")
    for n in lang:
        require(int(n) <= 12,
                f"ein {n}-stelliger Praefix kann den Zaun ausloesen — bei einer "
                f"Zeile, die einen moeglichen Kauf festhaelt")
    # Und die Regel selbst: unter dreizehn Ziffern greift sie nicht.
    require_equal(_DIGIT_RUN.search("4" * 12), None,
                  "der Zaun greift schon unter dreizehn Stellen")


def t_ein_abgelaufener_vorgang_wird_gar_nicht_erst_vorgelegt():
    """Ein toter Kauf kostet keine Biometrie.

    **Gefunden in der Live-Abnahme von DEBT-0126 am 2026-08-30, nicht hier.**
    Ein Vorgang vom 27. August stand drei Tage spaeter noch in der Liste der
    App. Der Eigentuemer tippte auf „Bezahlen", der Core baute einen
    vollstaendigen signierten Kaufauftrag ueber 89,98 EUR, und die Matrix
    verlangte Face ID. Er bestaetigte — und erst `purchase` sagte
    `intent_expired`. Kein Cent floss; die Weigerung sitzt vor dem Draht.

    Aber biometrische Autoritaet ist ausgegeben worden fuer eine Handlung, die
    nie wirken konnte. Der Kommentar an `INTENT_TTL` nennt genau das den Grund
    fuer die Frist: „der Mensch bestaetigt in einer Schleife etwas, das nie
    wirkt". Die Frist war da; nur gefragt hat sie niemand, bevor der Mensch
    gefragt wurde.

    Gemessen wird beides: die Absage VOR der Frage, und dass beim Anbieter
    nichts ankommt.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            args = {"vorgang": view["payment_intent_id"],
                    "pruefsumme": view["pruefsumme"]}
            # Solange er gilt, wird er vorgelegt.
            rig.caps.describe_purchase(args)

            jetzt = rig.caps._clock()
            rig.caps._clock = lambda: jetzt + 481.0        # INTENT_TTL + 1 s
            try:
                rig.caps.describe_purchase(args)
                raise AssertionError("ein abgelaufener Vorgang wurde vorgelegt")
            except CapabilityRefused as exc:
                require_equal(exc.reason, "intent_expired", str(exc.reason))

            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


def t_ein_abgelaufener_vorgang_kommt_auch_nicht_durch_den_router():
    """Dieselbe Absage einen Stock hoeher — und OHNE Freigabeanfrage.

    Der Punkt ist nicht die Absage, sondern dass gar nicht gefragt wird: eine
    Freigabeanfrage, die entsteht, ist eine Karte auf dem Display eines
    Menschen. Sie zu erzeugen und danach abzulehnen waere derselbe Fehler mit
    besserem Gewissen.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig)
            view = await H.prepare_intent(rig)
            args = {"vorgang": view["payment_intent_id"],
                    "pruefsumme": view["pruefsumme"]}
            jetzt = rig.caps._clock()
            rig.caps._clock = lambda: jetzt + 481.0

            result = await stack.execute("purchase_place", args)
            require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY,
                          str(result.outcome))
            require_equal(result.reason, "intent_expired", str(result.reason))
            require_equal(len(stack.requests), 0,
                          "ein toter Vorgang hat eine Freigabekarte erzeugt")
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


def t_ein_betrag_ueber_der_hausgrenze_wird_gar_nicht_erst_vorgelegt():
    """§20/§7: die Obergrenze greift VOR der Frage, nicht danach.

    Ein Betrag, den ein muedes Auge nicht mehr prueft, soll dem Daumen gar
    nicht erst unterkommen.
    """
    async def case():
        from solvio.capabilities.payment import CEILING_MINOR
        rig = await H.Rig().start(
            instrument=H.default_instrument(max_single_minor=CEILING_MINOR * 4))
        try:
            raised = None
            try:
                await H.prepare_intent(rig,
                                       posten=f"Teures Ding|1|{CEILING_MINOR + 1}",
                                       erwartet=str(CEILING_MINOR + 1))
            except CapabilityRefused as exc:
                raised = exc
            require(raised is not None,
                    "ein Betrag ueber der Hausgrenze wurde vorgelegt")
            require_equal(raised.reason, "above_ceiling")
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


def t_eine_bereits_bezahlte_absicht_wird_nicht_noch_einmal_bezahlt():
    """Der Zustand ist ein Zaun, kein Etikett.

    Auch wenn jemand die Zustandspruefung der Faehigkeit entfernte, haelt die
    des Executors — und umgekehrt. Zwei Zaeune hintereinander, beide gemessen.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await H.pay(rig, view)
            with H.use_context(capability="purchase_place", user_present=True,
                               approval_id="ap-2"), EI.bound(H.identity("ap-2")):
                raised = None
                try:
                    await rig.caps.purchase({"vorgang": view["payment_intent_id"],
                                             "pruefsumme": view["pruefsumme"]})
                except SafeExecutionFailure as exc:
                    raised = exc
            require(raised is not None,
                    "eine bereits bezahlte Absicht wurde erneut bezahlt")
            stats = await rig.stats()
            require_equal(stats["created_charges"], 1)
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 1)
        finally:
            await rig.stop()
    H.run(case())


def t_der_betrag_der_bezahlt_wird_ist_der_des_anbieters():
    """§12 an der Stelle, an der es zaehlt: bei der Buchung selbst.

    Das Modell erwartet 84,99; der Anbieter nennt 89,98. Belastet werden 89,98
    — und der Anbieter selbst weist eine abweichende Summe zurueck.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig, erwartet="8499")
            result = await H.pay(rig, view)
            require_equal(result["betrag"], "89,98 EUR")
            gebucht = rig.charged_rows(view["payment_intent_id"])[0]
            require_equal(int(gebucht["amount_minor"]), 8998,
                          "gebucht wurde nicht der Betrag des Anbieters")
            intent = rig.store.intent(view["payment_intent_id"])
            require_equal(intent.agent_expected_total_minor, 8499)
            require(not intent.agent_expectation_matches)
        finally:
            await rig.stop()
    H.run(case())


# ------------------------------------------------------------------ Wiederholung
def t_dieselbe_ausfuehrungskennung_kauft_kein_zweites_mal():
    """§23: eine wiederholte Ausfuehrung erzeugt NIE eine zweite Zahlung.

    Geprueft wird der Executor direkt, weil der Weg darueber die Absicht
    ohnehin schon auf `succeeded` gesetzt haette. Hier steht der Zaun, der auch
    dann haelt, wenn jemand ihn umgeht.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await H.pay(rig, view)
            pid = view["payment_intent_id"]
            # Die Absicht kuenstlich zurueck auf APPROVED — als haette ein
            # Wiederholungsversuch sie dort vorgefunden.
            intent = rig.store.intent(pid)
            import dataclasses
            rig.store.put_intent(dataclasses.replace(
                intent, state=PaymentState.APPROVED))
            with H.use_context(capability="purchase_place", user_present=True,
                               approval_id="ap-1"), EI.bound(H.identity("ap-1")):
                again = await rig.caps.executor.charge(pid)
            require(again.replayed, "der zweite Lauf hat neu belastet")
            stats = await rig.stats()
            require_equal(stats["created_charges"], 1)
            require_equal(len(rig.charged_rows(pid)), 1)
        finally:
            await rig.stop()
    H.run(case())


def t_das_buch_laesst_keine_zweite_belastung_zu():
    """Der Zaun in der Datenbank selbst — nicht in einer if-Abfrage."""
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await H.pay(rig, view)
            raised = False
            try:
                rig.store.record(payment_intent_id=view["payment_intent_id"],
                                 event="charged",
                                 execution_id=H.identity("ap-1").execution_id,
                                 amount_minor=8998, currency="EUR")
            except DuplicateCharge:
                raised = True
            require(raised, "das Buch hat eine zweite Belastung angenommen")
        finally:
            await rig.stop()
    H.run(case())


# ------------------------------------------------------------------ Mehrdeutigkeit
def t_ein_abgerissener_aufruf_wird_unbekannt_und_nicht_wiederholt():
    """§24/§25: belastet und dann die Verbindung verloren.

    Der Anbieter hat gebucht, die Antwort kam nie an. Ehrlich ist genau ein
    Wort: unbekannt. Ein blinder zweiter Versuch waere ein zweiter Kauf.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await rig.scenario("ambiguous_after_charge")
            raised = None
            try:
                await H.pay(rig, view)
            except AmbiguousExecution as exc:
                raised = exc
            require(raised is not None,
                    "ein abgerissener Aufruf wurde als Ergebnis verkauft")
            intent = rig.store.intent(view["payment_intent_id"])
            require_equal(intent.state, PaymentState.RECONCILIATION_REQUIRED)
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 0,
                          "eine unklare Zahlung wurde als Buchung gefuehrt")
            stats = await rig.stats()
            require_equal(stats["created_charges"], 1,
                          "der Anbieter hat NICHT gebucht — dann prueft dieser "
                          "Fall nicht, was er soll")
        finally:
            await rig.stop()
    H.run(case())


def t_nachsehen_loest_die_mehrdeutigkeit_ohne_zweite_buchung():
    """Nachschlagen an der Idempotenzkennung — lesend, nie belastend."""
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await rig.scenario("ambiguous_after_charge")
            try:
                await H.pay(rig, view)
            except AmbiguousExecution:
                pass
            await rig.scenario("normal")
            with H.use_context(capability="payment_reconcile"):
                answer = await rig.caps.reconcile(
                    {"vorgang": view["payment_intent_id"]})
            require_equal(answer["zustand"], "succeeded")
            require("durchgegangen" in answer["antwort"])
            stats = await rig.stats()
            require_equal(stats["created_charges"], 1,
                          "das Nachschlagen hat belastet")
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 1)
        finally:
            await rig.stop()
    H.run(case())


def t_ein_anbieter_der_nie_antwortet_belastet_nicht_und_heisst_trotzdem_unbekannt():
    """Auch „gar keine Antwort" ist nicht dasselbe wie „nichts passiert"."""
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await rig.scenario("unreachable")
            raised = None
            try:
                await H.pay(rig, view)
            except (AmbiguousExecution, SafeExecutionFailure) as exc:
                raised = exc
            require(raised is not None, "ein toter Anbieter meldete Erfolg")
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 0)
        finally:
            await rig.stop()
    H.run(case())


# ------------------------------------------------------------------ Absagen
def t_eine_ablehnung_der_bank_heisst_ausdruecklich_nichts_ist_passiert():
    """Nur wer es WEISS, darf „nichts ist passiert" sagen. Der Anbieter weiss es."""
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await rig.scenario("declined")
            raised = None
            try:
                await H.pay(rig, view)
            except SafeExecutionFailure as exc:
                raised = exc
            require(raised is not None,
                    "eine Ablehnung wurde nicht als folgenlos gemeldet")
            require("declined" in str(raised))
            intent = rig.store.intent(view["payment_intent_id"])
            require_equal(intent.state, PaymentState.FAILED)
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 0)
        finally:
            await rig.stop()
    H.run(case())


def t_starke_kundenauthentifizierung_wird_nicht_umgangen_sondern_abgewartet():
    """§26: 3-D Secure ist eine legitime menschliche Grenze.

    SOLVIO haelt an, sagt die Wahrheit und macht danach weiter — nachdem der
    Mensch in seiner Bank-App bestaetigt hat, nicht statt dessen.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await rig.scenario("sca_required")
            try:
                await H.pay(rig, view)
            except AmbiguousExecution:
                pass
            intent = rig.store.intent(view["payment_intent_id"])
            require_equal(intent.state, PaymentState.AWAITING_SCA)
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 0)

            # Solange der Mensch nicht bestaetigt hat, bleibt es dabei.
            await rig.scenario("normal")
            with H.use_context(capability="payment_reconcile"):
                waiting = await rig.caps.reconcile(
                    {"vorgang": view["payment_intent_id"]})
            require_equal(waiting["zustand"], "awaiting_sca")

            # Jetzt bestaetigt er.
            await rig.complete_sca(H.identity("ap-1").idempotency_key)
            with H.use_context(capability="payment_reconcile"):
                done = await rig.caps.reconcile(
                    {"vorgang": view["payment_intent_id"]})
            require_equal(done["zustand"], "succeeded")
            require_equal(len(rig.charged_rows(view["payment_intent_id"])), 1)
        finally:
            await rig.stop()
    H.run(case())


# ------------------------------------------------------------------ Grenzen
def t_ein_gesperrtes_zahlungsmittel_bezahlt_nicht():
    async def case():
        from solvio.payment.instruments import InstrumentStatus
        rig = await H.Rig().start(
            instrument=H.default_instrument(status=InstrumentStatus.DISABLED))
        try:
            raised = None
            try:
                await H.prepare_intent(rig)
            except CapabilityRefused as exc:
                raised = exc
            require(raised is not None, "ein gesperrtes Mittel wurde vorbereitet")
            require_equal(raised.reason, "payment_method_not_active")
        finally:
            await rig.stop()
    H.run(case())


def t_die_einzelgrenze_greift_vor_dem_draht():
    async def case():
        rig = await H.Rig().start(
            instrument=H.default_instrument(max_single_minor=5000))
        try:
            view = await H.prepare_intent(rig)
            raised = None
            try:
                await H.pay(rig, view)
            except SafeExecutionFailure as exc:
                raised = exc
            require(raised is not None, "die Einzelgrenze hat nicht gegriffen")
            require("single_limit_exceeded" in str(raised))
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0,
                          "es wurde trotz Grenze abgesendet")
        finally:
            await rig.stop()
    H.run(case())


def t_die_tagesgrenze_zaehlt_aus_dem_buch_und_nicht_aus_dem_speicher():
    async def case():
        rig = await H.Rig().start(
            instrument=H.default_instrument(daily_total_minor=10000))
        try:
            first = await H.prepare_intent(rig)
            await H.pay(rig, first)
            second = await H.prepare_intent(rig)
            raised = None
            try:
                await H.pay(rig, second, approval_id="ap-2")
            except SafeExecutionFailure as exc:
                raised = exc
            require(raised is not None, "die Tagesgrenze hat nicht gegriffen")
            require("daily_limit_exceeded" in str(raised))
            require_equal(rig.store.day_total_minor(H.METHOD), 8998)
        finally:
            await rig.stop()
    H.run(case())


def t_ein_fremder_haendler_wird_gar_nicht_erst_vorbereitet():
    async def case():
        rig = await H.Rig().start()
        try:
            raised = None
            try:
                await H.prepare_intent(rig, haendler="amazon-de")
            except CapabilityRefused as exc:
                raised = exc
            require(raised is not None, "ein unbekannter Haendler kam durch")
            require_equal(raised.reason, "unknown_merchant")
        finally:
            await rig.stop()
    H.run(case())


def t_eine_getauschte_waehrung_macht_die_absicht_unlesbar_statt_zahlbar():
    """§13: 100 EUR freigegeben heisst nie 100 USD bezahlt.

    Der Versuch, die Waehrung nach der Bewertung umzudrehen, scheitert an zwei
    Stellen hintereinander, und beide sind Struktur statt Vorsatz:

    1. `put_intent` schreibt die wirtschaftliche Identitaet nicht fort — die
       Waehrung der Zeile bleibt, was sie war.
    2. Die Zeile passt danach nicht mehr zusammen, und `intent()` gibt `None`
       zurueck statt eines halben Objekts.

    Ergebnis: ein `SafeExecutionFailure`, also die ehrliche Aussage „es ist
    nichts passiert" — und kein einziges Byte an den Anbieter.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            intent = rig.store.intent(view["payment_intent_id"])
            import dataclasses
            fremd = dataclasses.replace(
                intent, currency="USD",
                quote=dataclasses.replace(intent.quote, currency="USD"))
            rig.store.put_intent(fremd)
            reloaded = rig.store.intent(view["payment_intent_id"])
            require(reloaded is None,
                    "eine in sich widerspruechliche Absicht kam zurueck")
            with H.use_context(capability="purchase_place", user_present=True,
                               approval_id="ap-1"), EI.bound(H.identity("ap-1")):
                raised = None
                try:
                    await rig.caps.purchase({"vorgang": view["payment_intent_id"],
                                             "pruefsumme": view["pruefsumme"]})
                except SafeExecutionFailure as exc:
                    raised = exc
            require(raised is not None, "eine getauschte Waehrung kam durch")
            require("unknown_intent" in str(raised))
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


def t_eine_freigabe_gilt_nicht_fuer_einen_anderen_betrag():
    """Ein Cent Unterschied, und die Pruefsumme traegt die Handlung nicht mehr.

    Das ist der Kern von §7. Geprueft wird an der Stelle, an der es zaehlt: die
    Beschreibung, die der Mensch bestaetigt hat, wird beim Bezahlen ERNEUT
    gegen die Absicht gehalten.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            alte_pruefsumme = view["pruefsumme"]
            # Neu bewerten: der Anbieter ist einen Aufschlag teurer geworden.
            await rig.scenario("price_drift")
            with H.use_context(capability="payment_intent_prepare"):
                neu = await rig.caps.executor.quote(
                    rig.store.intent(view["payment_intent_id"]))
            require(neu.economic_digest() != alte_pruefsumme,
                    "ein anderer Betrag ergab dieselbe Pruefsumme")
            with H.use_context(capability="purchase_place", user_present=True,
                               approval_id="ap-1"), EI.bound(H.identity("ap-1")):
                raised = None
                try:
                    await rig.caps.purchase({"vorgang": view["payment_intent_id"],
                                             "pruefsumme": alte_pruefsumme})
                except SafeExecutionFailure as exc:
                    raised = exc
            require(raised is not None, "die alte Freigabe hat den neuen Betrag getragen")
            require("intent_changed" in str(raised))
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


def t_eine_bestehende_ablage_bekommt_neue_spalten_nachtraeglich():
    """`CREATE TABLE IF NOT EXISTS` legt keine Spalte nach — ein Migrationsweg schon.

    Gefunden beim ersten produktiven Start: die Datei existierte bereits, die
    neue Spalte kam nie an, und der Fehler zeigte sich erst beim Hinterlegen
    eines Zahlungsmittels — also spaet und an einer Stelle, die nach etwas ganz
    anderem aussieht.
    """
    async def case():
        import sqlite3 as sq
        import tempfile
        from solvio.payment.store import PaymentStore
        pfad = os.path.join(tempfile.mkdtemp(prefix="solvio-payment-alt-"),
                            "payments.sqlite3")
        # Eine ALTE Ablage: dieselbe Tabelle, aber ohne die spaeteren Spalten.
        alt = sq.connect(pfad)
        alt.executescript("""
            CREATE TABLE instruments (
                payment_ref TEXT PRIMARY KEY, kind TEXT NOT NULL,
                provider TEXT NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                max_single_minor INTEGER NOT NULL,
                daily_total_minor INTEGER NOT NULL DEFAULT 0,
                allowed_currencies TEXT NOT NULL DEFAULT '',
                allowed_merchant_ids TEXT NOT NULL DEFAULT '',
                provider_secret_ref TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                display_hint TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL DEFAULT '',
                disabled_reason TEXT NOT NULL DEFAULT '',
                policy_sha256 TEXT NOT NULL);""")
        alt.commit()
        alt.close()

        store = PaymentStore(pfad)
        spalten = set()
        with sq.connect(pfad) as conn:
            conn.row_factory = sq.Row
            spalten = {r["name"] for r in conn.execute("PRAGMA table_info(instruments)")}
        for neu in ("provider_readonly_ref", "provider_refund_ref", "provider_token"):
            require(neu in spalten, f"{neu} wurde nicht nachgetragen")
        # Und die Ablage ist danach wirklich benutzbar.
        store.put_instrument(H.default_instrument())
        require(store.instrument(H.METHOD) is not None,
                "nach der Wanderung liess sich nichts hinterlegen")
    H.run(case())


# ------------------------------------------------------------------ stehengeblieben
def t_ein_vorgang_der_beim_ausfuehren_stehenblieb_wird_nachgeschlagen():
    """Der gefaehrlichste Zustand ueberhaupt — und er muss aufloesbar sein.

    Absturz, Stromausfall, `kill -9` zwischen dem durablen Anspruch und der
    Antwort des Anbieters. Die Belastung KANN stattgefunden haben, und niemand
    hat es aufgeschrieben. Der erste Bau liess `EXECUTING` nirgends nachsehen
    und antwortete „Dieser Vorgang ist bereits geklaert." — eine Aussage ueber
    eine Zahlung, die SOLVIO nie bestaetigt hat. Gefunden in der kalten Abnahme.
    """
    async def case():
        import dataclasses
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            pid = view["payment_intent_id"]
            # Bis zum Anspruch kommen, dann den Prozess „sterben" lassen.
            await rig.scenario("ambiguous_after_charge")
            try:
                await H.pay(rig, view)
            except AmbiguousExecution:
                pass
            # Von Hand in den Zustand zuruecksetzen, den ein Absturz hinterlaesst.
            intent = rig.store.intent(pid)
            rig.store.put_intent(dataclasses.replace(
                intent, state=PaymentState.EXECUTING))
            require_equal(rig.store.intent(pid).state, PaymentState.EXECUTING)

            require(any(r["payment_intent_id"] == pid
                        for r in rig.store.open_reconciliations()),
                    "ein stehengebliebener Vorgang gilt als erledigt")
            from solvio.payment.health import assess, reconciliation_open
            require(reconciliation_open() >= 1)
            wort, grund = assess()
            require_equal(wort, "degraded", f"Gesundheit meldet {wort}: {grund}")

            await rig.scenario("normal")
            with H.use_context(capability="payment_reconcile"):
                antwort = await rig.caps.reconcile({"vorgang": pid})
            require_equal(antwort["zustand"], "succeeded",
                          "ein stehengebliebener Vorgang liess sich nicht klaeren")
            require_equal(len(rig.charged_rows(pid)), 1)
        finally:
            await rig.stop()
    H.run(case())


def t_eine_belastung_wird_gebucht_auch_wenn_die_anbieterkennung_anstoesst():
    """Nach dem Draht ist das Geld weg. Die Zeile MUSS entstehen.

    Der Zaun gegen Zahlungsmaterial sitzt auf `provider_ref`, und eine
    Anbieterkennung ist Anbietertext: sie kann zufaellig wie eine Kartennummer
    aussehen. Faellt die Buchung daran, gibt es keine Zeile, keine Tagesgrenze,
    keinen Zaun gegen die zweite Belastung — und einen Kauf, von dem SOLVIO
    nichts mehr weiss. Gefunden in der kalten Abnahme.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            pid = view["payment_intent_id"]
            echt = rig.store.record

            def boesartig(*, payment_intent_id, event, **fields):
                if event == "charged" and fields.get("provider_ref"):
                    # Eine Kennung, die der Zaun fuer eine Kartennummer haelt.
                    fields = dict(fields, provider_ref="4242424242424242")
                return echt(payment_intent_id=payment_intent_id, event=event,
                            **fields)

            rig.store.record = boesartig
            result = await H.pay(rig, view)
            rig.store.record = echt
            require_equal(result["zustand"], "succeeded")
            zeilen = rig.charged_rows(pid)
            require_equal(len(zeilen), 1,
                          "die Belastung wurde nicht gebucht")
            require_equal(zeilen[0]["provider_ref"], "",
                          "die anstoessige Kennung landete doch im Buch")
            require_equal(rig.store.day_total_minor(H.METHOD), 8998,
                          "die Tagesgrenze weiss nichts von der Belastung")
        finally:
            await rig.stop()
    H.run(case())


def t_ein_gescheiterter_anspruch_heisst_nichts_ist_passiert():
    """Der Anspruch steht VOR dem Draht — sein Scheitern ist beweisbar folgenlos."""
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)

            echt = rig.store.record

            def kaputt(*args, **kwargs):
                raise RuntimeError("database is locked")

            # Erst die Buchhaltung der Freigabe.
            rig.store.record = kaputt
            raised = None
            try:
                await H.pay(rig, view)
            except SafeExecutionFailure as exc:
                raised = exc
            require(raised is not None,
                    "ein gescheiterter Freigabeeintrag lief als unbekannter Ausgang")
            require("approval_write_failed" in str(raised), str(raised))

            # Und JETZT der Anspruch allein — die Freigabe darf gelingen, der
            # Anspruch nicht. Ohne diese Trennung deckt der erste Zaun den
            # zweiten zu, und der zweite waere ungeprueft.
            def nur_anspruch_kaputt(*, payment_intent_id, event, **fields):
                if event == "claimed":
                    raise RuntimeError("database is locked")
                return echt(payment_intent_id=payment_intent_id, event=event,
                            **fields)

            rig.store.record = nur_anspruch_kaputt
            zweiter = await H.prepare_intent(rig)
            raised = None
            try:
                await H.pay(rig, zweiter, approval_id="ap-2")
            except SafeExecutionFailure as exc:
                raised = exc
            rig.store.record = echt
            require(raised is not None,
                    "ein gescheiterter Anspruch lief als unbekannter Ausgang")
            require("claim_write_failed" in str(raised), str(raised))
            stats = await rig.stats()
            require_equal(stats["charge_attempts"], 0)
        finally:
            await rig.stop()
    H.run(case())


# ------------------------------------------------------------------ zurueck
def t_erstattung_geht_auf_dasselbe_zahlungsmittel_und_kennt_kein_ziel():
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await H.pay(rig, view)
            with H.use_context(capability="refund_request", user_present=True,
                               approval_id="ap-1"):
                back = await rig.caps.refund({"vorgang": view["payment_intent_id"]})
            require_equal(back["zustand"], "refunded")
            require_equal(back["erstattet"], "89,98 EUR")
            require_equal(back["zurueck_auf"], "SOLVIO Shopping")
            spec = __import__("solvio.capabilities.payment", fromlist=["SPECS"]).SPECS
            felder = set(spec["refund_request"].input_schema["properties"])
            require_equal(felder & {"ziel", "empfaenger", "iban", "konto"}, set(),
                          "die Erstattung hat ein Zielfeld bekommen")
        finally:
            await rig.stop()
    H.run(case())


def t_eine_zweite_erstattung_ist_nicht_zweimal_geld():
    """§23 fuer die Gegenrichtung — und der teuerste Fehler dieser Abnahme.

    Der erste Bau leitete die Idempotenzkennung jeder Erstattung aus der
    Ausfuehrungskennung des KAUFS ab. Damit trug jede weitere Erstattung
    desselben Kaufs dieselbe Kennung, der Anbieter gab brav die erste zurueck —
    und SOLVIO buchte sie als neues Geld. Aus 40,00 + 49,98 wurden im Buch
    120,00, waehrend beim Anbieter genau 40,00 zurueckgingen.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await H.pay(rig, view)
            pid = view["payment_intent_id"]
            with H.use_context(capability="refund_request"):
                erste = await rig.caps.refund({"vorgang": pid, "betrag": "4000"})
                require_equal(erste["erstattet"], "40,00 EUR")
                zweite = await rig.caps.refund({"vorgang": pid, "betrag": "4998"})
            require_equal(zweite["erstattet"], "49,98 EUR",
                          "die zweite Erstattung war eine Wiedergabe der ersten")
            gebucht = sum(int(r["amount_minor"]) for r
                          in rig.store.ledger(payment_intent_id=pid, limit=100)
                          if r["event"] == "refunded")
            require_equal(gebucht, 8998,
                          f"im Buch stehen {gebucht} Cent statt 8998")
            intent = rig.store.intent(pid)
            require_equal(intent.state, PaymentState.REFUNDED)
        finally:
            await rig.stop()
    H.run(case())


def t_erstattungsreferenz_mit_kartenform_bleibt_buchbar_und_replay_sicher():
    """A provider reference must neither lose money nor persist card material.

    The real local provider issues two deliberately PAN-shaped identifiers.
    Repeating the first exact execution returns its original provider receipt;
    a separately authorized second partial refund remains a distinct receipt.
    """
    from types import SimpleNamespace
    from unittest.mock import patch
    from solvio.payment.firewall import reason_for

    async def case():
        rig = await H.Rig().start()
        try:
            # Public synthetic values already used by payment firewall tests.
            # The unrelated quote/charge/order references are fixed safe values
            # so only the two refund references can trigger this regression.
            tokens = iter(('abcdefabcdefabcd', 'fedcbafedcbafedcb', 'abcdefabcdef',
                           '4539578763621486', '4242424242424242'))
            raw_refs = ('re_4539578763621486', 're_4242424242424242')
            require(all(reason_for(value) == 'primary_account_number' for value in raw_refs))
            # Only the sandbox module's receipt generator changes. Vault and
            # execution identities retain their ordinary production functions.
            with patch.object(H.SBX, 'secrets', SimpleNamespace(token_hex=lambda _: next(tokens),
                    compare_digest=H.SBX.secrets.compare_digest)):
                view = await H.prepare_intent(rig)
                await H.pay(rig, view)
                pid = view['payment_intent_id']
                with H.use_context(capability='refund_request'), \
                        EI.bound(H.identity('ap-refund-first', 'refund_request')):
                    first = await rig.caps.refund({'vorgang': pid, 'betrag': '4000'})
                    replay = await rig.caps.refund({'vorgang': pid, 'betrag': '4000'})
                require_equal(first['erstattet'], '40,00 EUR')
                require_equal(replay['erstattet'], '0,00 EUR')
                with H.use_context(capability='refund_request'), \
                        EI.bound(H.identity('ap-refund-second', 'refund_request')):
                    second = await rig.caps.refund({'vorgang': pid, 'betrag': '4998'})
            require_equal(second['erstattet'], '49,98 EUR')
            rows = rig.store.ledger(payment_intent_id=pid, limit=100)
            refunds = [row for row in rows if row['event'] == 'refunded']
            require_equal(len(refunds), 2)
            require_equal(sum(row['amount_minor'] for row in refunds), 8998)
            require_equal(len({row['provider_ref'] for row in refunds}), 2)
            require(all(row['provider_ref'] and not reason_for(row['provider_ref']) for row in refunds))
            require(all(raw not in str(rows) for raw in raw_refs))
            require_equal(rig.store.intent(pid).state, PaymentState.REFUNDED)
            provider_refunds = rig.runner.app['box'].refunds
            require_equal(len(provider_refunds), 2)
            require_equal(sum(row['amount_minor'] for row in provider_refunds.values()), 8998)
        finally:
            await rig.stop()
    H.run(case())


def t_erstatten_und_stornieren_laufen_vom_telefon_ohne_face_id():
    """§28: sie sind billig, WEIL sie einen Fehlkauf begrenzen.

    Der erste Bau machte sie damit zu hundert Prozent unmoeglich: `CRITICAL`
    laeuft vom iPhone DIREKT, also ohne bewiesene Anwesenheit — und griff dann
    auf den Belastungszugang zu, der genau die verlangt. Ausgerechnet die zwei
    Handlungen, mit denen ein Mensch Schaden begrenzt, waren tot. Gefunden in
    der kalten Abnahme.
    """
    async def case():
        rig = await H.Rig().start()
        try:
            stack = H.Stack(rig)
            view = await H.prepare_intent(rig)
            await H.pay(rig, view)
            pid = view["payment_intent_id"]
            zurueck = await stack.execute("refund_request",
                                          {"vorgang": pid, "betrag": "1000"})
            require_equal(zurueck.outcome, CapabilityOutcome.SUCCESS,
                          f"die Erstattung lief nicht: {zurueck.reason}")
            require_equal(stack.requests, [],
                          "die Erstattung hat eine Face-ID-Runde verlangt")
            storno = await stack.execute("purchase_cancel", {"vorgang": pid})
            require_equal(storno.outcome, CapabilityOutcome.SUCCESS,
                          f"die Stornierung lief nicht: {storno.reason}")
        finally:
            await rig.stop()
    H.run(case())


def t_der_lesende_zugang_kann_nicht_belasten():
    """Drei Zugaenge mit drei verschiedenen Befugnissen — beim Anbieter geprueft.

    Ein Pruefanbieter, der alle drei auf denselben Wert legte, bewiese die
    Trennung nicht. Er tut es nicht, und das wird hier nachgesehen.
    """
    async def case():
        import aiohttp
        rig = await H.Rig().start()
        try:
            require(len({rig.provider_secret, rig.refund_secret,
                         rig.read_secret}) == 3,
                    "der Pruefanbieter benutzt denselben Wert mehrfach")
            async with aiohttp.ClientSession() as session:
                for wert, darf_belasten in ((rig.provider_secret, True),
                                            (rig.refund_secret, False),
                                            (rig.read_secret, False)):
                    async with session.post(
                            rig.base_url + "/v1/charge",
                            headers={"Authorization": f"Bearer {wert}",
                                     "Idempotency-Key": "probe-" + wert[:8]},
                            json={"instrument_token": "pm_sandbox_solvio_shopping",
                                  "merchant_id": H.MERCHANT, "amount_minor": 100,
                                  "currency": "EUR"}) as r:
                        if darf_belasten:
                            require_equal(r.status, 200, await r.text())
                        else:
                            require_equal(r.status, 401,
                                          "ein nicht-belastender Zugang hat belastet")
        finally:
            await rig.stop()
    H.run(case())


def t_stornieren_ist_nicht_erstatten_und_behauptet_es_auch_nicht():
    async def case():
        rig = await H.Rig().start()
        try:
            view = await H.prepare_intent(rig)
            await H.pay(rig, view)
            with H.use_context(capability="purchase_cancel", user_present=True,
                               approval_id="ap-1"):
                result = await rig.caps.order_cancel(
                    {"vorgang": view["payment_intent_id"]})
            require_equal(result["zustand"], "storniert")
            require("Haendler" in result["hinweis"],
                    "die Stornierung behauptet, Geld komme zurueck")
            intent = rig.store.intent(view["payment_intent_id"])
            require_equal(intent.state, PaymentState.SUCCEEDED,
                          "eine Stornierung hat die Zahlung wegdefiniert")
        finally:
            await rig.stop()
    H.run(case())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
