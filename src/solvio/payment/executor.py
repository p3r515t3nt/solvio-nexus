"""Der Zahlungs-Executor — die EINZIGE Stelle, die Anbieterbefugnis anfassen darf.

Es gibt hier absichtlich keine Funktion, die ein Modell aufrufen koennte. Kein
`execute_payment_raw`, kein `http_request_with_payment_secret`, kein Verb, das
einen Betrag als Argument nimmt und ihn irgendwohin schickt. Was es gibt, sind
Vorgaenge auf einer bereits im Core gepruefen `PaymentIntent`.

Vier Schranken vor jeder Belastung, jede fuer sich ausreichend zum Nein:

1. **Ausfuehrungsidentitaet.** Ohne `execution_id` und `idempotency_key` aus dem
   eingefrorenen Freigabepfad wird nicht belastet. Es gibt keinen Weg, sie sich
   selbst auszudenken — sie sind reine Funktionen aus Core-Instanz und
   Freigabekennung.
2. **Zustand.** Die Absicht muss `APPROVED` und unverfallen sein.
3. **Zahlungsmittel.** Aktiv, Waehrung erlaubt, Haendler erlaubt, Einzel- und
   Tagesgrenze eingehalten — ERNEUT geprueft, nicht auf die Vorpruefung
   vertraut.
4. **Doppelbuchung.** Wurde unter dieser Ausfuehrungskennung schon belastet,
   wird das ERGEBNIS zurueckgegeben und nichts Neues angelegt.

**Der Wert reist als Kopfzeile und lebt einen Vorgang lang.** Der Anbieterzugang
kommt ueber `SecretBroker.use()` und ist nach dem `with`-Block weg. Er steht in
keinem Argument, keinem Freigabetext, keiner Protokollzeile und keinem Ergebnis.

**Drei Ausgaenge, nicht zwei.** Gelungen, sicher fehlgeschlagen, unbekannt. Der
dritte wird als `AmbiguousExecution` gemeldet — der eingefrorene Pfad macht
daraus `UNKNOWN` und niemals einen stillen zweiten Versuch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from solvio.capabilities.contract import AmbiguousExecution, ExecutorUnavailable
from solvio.logging_setup import get_logger
from solvio.payment import config as PC
from solvio.capabilities import execution_identity as PX
from solvio.payment import store as PS
from solvio.payment.instruments import Denied, Instrument, evaluate
from solvio.payment.intent import (Extra, ExtraKind, PaymentIntent, PaymentQuote,
                                   PaymentState, format_amount)
from solvio.payment.providers import (ChargeStatus, FailureCategory,
                                      ProviderAmbiguous, ProviderCharge,
                                      ProviderError, ProviderUnavailable)
from solvio.secret_vault import policy as VP
from solvio.secret_vault.broker import SecretDenied, SecretUnavailable

log = get_logger("payment")


class PaymentRefused(RuntimeError):
    """Der Core sagt nein — und zwar BEVOR irgendetwas abgesendet wurde.

    Traegt einen kategorischen Grund aus einer geschlossenen Liste. Dieser Text
    kann bis ins Modell laufen; ein Betrag, ein Verweis oder ein Anbietertext
    haben darin nichts zu suchen.
    """

    def __init__(self, reason: str, human_message: str = "") -> None:
        super().__init__(f"payment_refused:{reason}")
        self.reason = reason
        self.human_message = human_message or "Das mache ich so nicht."


@dataclass(frozen=True)
class ChargeOutcome:
    """Was aus einer Belastung wurde. Sicher, strukturiert, ohne Anbietertext."""

    state: PaymentState
    amount_minor: int = 0
    currency: str = ""
    provider_ref: str = ""
    order_ref: str = ""
    failure_category: str = FailureCategory.NONE.value
    replayed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.state is PaymentState.SUCCEEDED


class PaymentExecutor:
    """Haelt Ablage, Makler und Anbieteradressen zusammen — und sonst nichts."""

    def __init__(self, store: PS.PaymentStore | None = None, *,
                 broker: Any = None, clock=time.time) -> None:
        self._store = store if store is not None else PS.PaymentStore()
        self._broker = broker
        self._clock = clock

    @property
    def store(self) -> PS.PaymentStore:
        return self._store

    # -- Anbieter ------------------------------------------------------------
    def _broker_or_fail(self):
        if self._broker is None:
            from solvio.secret_vault.broker import SecretBroker
            self._broker = SecretBroker()
        return self._broker

    def _provider_config(self, instrument: Instrument) -> PC.ProviderConfig:
        found = PC.provider(instrument.provider)
        if found is None:
            raise PaymentRefused(
                "provider_not_configured",
                "Fuer dieses Zahlungsmittel ist kein Anbieter eingerichtet.")
        return found

    def _build_provider(self, config: PC.ProviderConfig, secret: str):
        if config.kind == "sandbox":
            from solvio.payment.providers.sandbox import SandboxProvider
            return SandboxProvider(config.base_url, secret=secret,
                                   allow_plaintext_loopback=True)
        raise PaymentRefused("provider_not_configured",
                             "Diesen Anbieter kenne ich nicht.")

    def _borrow(self, secret_ref: str, config: PC.ProviderConfig, *,
                capability: str, execution_id: str = "", approval_id: str = ""):
        """Leiht einen Anbieterzugang fuer GENAU EINEN Vorgang.

        **Dieser Aufruf steht bewusst genau hier und nirgends sonst.**
        `SecretBroker.use()` liest `sys._getframe(1).f_globals["__name__"]` — den
        Modulnamen des tatsaechlichen Aufrufers. Ein Dekorator, eine
        Basisklassenmethode oder ein geteilter Kontextmanager in einer anderen
        Datei machte daraus den Modulnamen des Wrappers, und der Tresor
        antwortete mit `EXECUTOR_MODULE_MISMATCH`. Das saehe aus wie ein
        Politikfehler und verleitete dazu, `EXECUTOR_MODULES` zu erweitern —
        also genau die Tuer aufzumachen, die hier zugehen soll.

        **Weder `origin=` noch `user_present=` werden uebergeben.** Beide sind
        gueltige Schluesselwoerter des Maklers, und beide wuerden ausgerechnet
        die zwei Schranken aushebeln, auf die es ankommt. Sie kommen aus dem
        Vorgangskontext, den der Router gesetzt hat. Eine Zusicherung liest
        diese Datei im Quelltext und haelt das fest.
        """
        if not secret_ref:
            raise PaymentRefused(
                "provider_not_configured",
                "Fuer dieses Zahlungsmittel liegt kein Anbieterzugang im Tresor.")
        broker = self._broker_or_fail()
        return broker.use(secret_ref,
                          executor=VP.ExecutorId.PAYMENT,
                          target=config.base_url, capability=capability,
                          execution_id=execution_id, approval_id=approval_id)

    # -- Betragswahrheit -----------------------------------------------------
    async def quote(self, intent: PaymentIntent) -> PaymentIntent:
        """Holt den verbindlichen Endbetrag und haengt ihn an die Absicht.

        Der Betrag kommt vom Anbieter und wird NICHT aus den Positionen
        uebernommen. Rechnet der Anbieter anders als seine eigenen Zeilen, gilt
        sein Endbetrag — und die Abweichung steht danach sichtbar im
        Freigabetext, statt still zu verschwinden.
        """
        instrument = self._instrument_or_fail(intent.payment_method_ref)
        config = self._provider_config(instrument)
        items = [item.as_dict() for item in intent.items]
        try:
            with self._borrow(instrument.provider_readonly_ref, config,
                              capability="payment_intent_prepare") as material:
                provider = self._build_provider(config, material.plaintext())
                quoted = await provider.quote(
                    merchant_id=intent.merchant_id,
                    merchant_origin=intent.merchant_origin, items=items,
                    currency=intent.currency, shipping_label=intent.shipping_label)
        except ProviderUnavailable as exc:
            raise ExecutorUnavailable("payment_provider_unavailable") from exc
        except SecretDenied as exc:
            raise PaymentRefused(
                exc.reason.value,
                "Fuer dieses Zahlungsmittel darf ich den Anbieterzugang hier "
                "nicht benutzen.") from exc
        except SecretUnavailable as exc:
            raise ExecutorUnavailable("payment_credential_unavailable") from exc
        except ProviderError as exc:
            raise PaymentRefused(
                exc.category.value,
                "Der Zahlungsanbieter hat den Betrag nicht bestaetigt.") from exc

        if quoted.recurring:
            # §30: ein Abonnement ist eine ANDERE wirtschaftliche Entscheidung.
            raise PaymentRefused(
                "recurring_not_supported",
                "An dieser Kasse entstuende ein Abonnement. Das mache ich nicht "
                "ohne dich — bitte schliess es selbst ab.")

        extras = []
        for kind, amount, label in quoted.extras:
            try:
                extra_kind = ExtraKind(kind)
            except ValueError:
                # Ein Aufschlag, den wir nicht benennen koennen, ist kein
                # Sammelposten — er ist ein Nein.
                raise PaymentRefused(
                    "unknown_surcharge",
                    "Auf der Rechnung steht ein Posten, den ich dir nicht "
                    "erklaeren kann. Deshalb frage ich gar nicht erst.") from None
            extras.append(Extra(kind=extra_kind, amount_minor=int(amount),
                                label=label or ""))

        quote = PaymentQuote(
            source="executor", currency=quoted.currency,
            items_total_minor=quoted.items_total_minor, extras=tuple(extras),
            total_minor=quoted.total_minor, quoted_at=self._clock(),
            quote_ref=quoted.quote_ref, charged_currency=quoted.charged_currency,
            charged_total_minor=quoted.charged_total_minor)
        fresh = intent.with_quote(quote)
        self._store.put_intent(fresh)
        # Die Kennung des Kostenvoranschlags ist ANBIETERTEXT und laeuft damit
        # durch denselben Zaun wie die Belastungskennung — der gelegentlich
        # anschlaegt. Derselbe zweistufige Weg wie beim `charged`-Eintrag: erst
        # mit Kennung, sonst ohne. Eine Zeile ohne Kennung ist unbequem; ein
        # Vorbereiten, das an einer zufaelligen Ziffernfolge zerbricht, ist eine
        # gesunde Zahlung weniger.
        felder = dict(payment_intent_id=fresh.payment_intent_id,
                      event=PS.EVENT_QUOTED, merchant_id=fresh.merchant_id,
                      merchant_origin=fresh.merchant_origin,
                      amount_minor=quote.total_minor, currency=quote.currency,
                      instrument_ref=fresh.payment_method_ref,
                      origin=fresh.origin, provider=instrument.provider,
                      status="quoted")
        try:
            self._store.record(provider_ref=quote.quote_ref, **felder)
        except Exception as exc:  # noqa: BLE001
            log.warning("payment.quote_ref_rejected", kind=type(exc).__name__,
                        payment_intent_id=fresh.payment_intent_id)
            self._store.record(provider_ref="", **felder)
        return fresh

    # -- Die Geldbewegung ----------------------------------------------------
    async def charge(self, payment_intent_id: str) -> ChargeOutcome:
        """Belastet GENAU EINMAL — oder sagt ehrlich, dass es unbekannt ist.

        Der eingefrorene Ausfuehrungspfad hat den durablen Anspruch bereits
        gesetzt, bevor diese Funktion laeuft. Was hier passiert, ist die
        Ueberschreitung der aeusseren Grenze und nichts sonst.
        """
        identity = PX.current()
        if not identity.usable:
            # Ohne freigegebenen Vorgang gibt es keine Zahlung. Sicher
            # fehlgeschlagen: es wurde nichts abgesendet.
            raise self._safe_failure("no_execution_identity")

        intent = self._store.intent(payment_intent_id)
        if intent is None:
            raise self._safe_failure("unknown_intent")
        if intent.state is not PaymentState.APPROVED:
            raise self._safe_failure(f"intent_state_{intent.state.value}")
        if intent.is_expired(self._clock()):
            raise self._safe_failure("intent_expired")
        if intent.quote is None:
            raise self._safe_failure("no_quote")

        # DIE DOPPELBUCHUNGSSPERRE, Core-Seite. Der Anbieter hat seine eigene
        # (die Idempotenzkennung); dies ist die davor.
        already = self._existing_charge(identity.execution_id)
        if already is not None:
            log.info("payment.replayed_execution", execution_id=identity.execution_id)
            return already

        # AB HIER IST JEDE ABSAGE EIN `SafeExecutionFailure`, und das ist keine
        # Stilfrage. Der eingefrorene Pfad hat die durable Grenze bereits
        # ueberschritten; jede ANDERE Ausnahme wird dort als `UNKNOWN` gebucht,
        # und der Mensch bekaeme „ich weiss nicht sicher, ob das durchging" fuer
        # einen Aufruf, der den Rechner nie verlassen hat. Das waere eine Luege
        # in die vorsichtige Richtung — und die kostet Vertrauen genauso.
        try:
            instrument = self._instrument_or_fail(intent.payment_method_ref)
            config = self._provider_config(instrument)
        except PaymentRefused as exc:
            raise self._safe_failure(exc.reason) from exc
        amount = intent.quote.total_minor
        verdict = evaluate(
            instrument, currency=intent.quote.currency, merchant_id=intent.merchant_id,
            amount_minor=amount,
            day_total_minor=self._store.day_total_minor(
                instrument.payment_ref, currency=intent.quote.currency))
        if not verdict.allowed:
            self._store.record(payment_intent_id=intent.payment_intent_id,
                               event=PS.EVENT_DENIED,
                               instrument_ref=instrument.payment_ref,
                               amount_minor=amount, currency=intent.quote.currency,
                               origin=intent.origin, approval_id=identity.approval_id,
                               execution_id=identity.execution_id,
                               failure_category=(verdict.reason or Denied.NOT_ACTIVE).value)
            self._store.put_intent(intent.with_state(PaymentState.FAILED))
            raise self._safe_failure((verdict.reason or Denied.NOT_ACTIVE).value)

        running = intent.with_state(PaymentState.EXECUTING,
                                    execution_id=identity.execution_id,
                                    approval_id=identity.approval_id)
        try:
            self._store.put_intent(running)
            self._store.record(payment_intent_id=intent.payment_intent_id,
                               event=PS.EVENT_CLAIMED,
                               merchant_id=intent.merchant_id,
                               merchant_origin=intent.merchant_origin,
                               description=intent.purpose, amount_minor=amount,
                               currency=intent.quote.currency,
                               instrument_ref=instrument.payment_ref,
                               origin=intent.origin,
                               approval_id=identity.approval_id,
                               execution_id=identity.execution_id,
                               provider=instrument.provider,
                               # NUR ZWOELF ZEICHEN, und das ist kein Geschmack.
                               # Der Zaun gegen Zahlungsmaterial sucht Ziffernfolgen
                               # ab dreizehn Stellen mit gueltiger Luhn-Pruefung.
                               # Ein 32-stelliger Hex-Praefix trifft das
                               # gelegentlich — und haette dann eine Zahlung
                               # zerlegt, die voellig in Ordnung war. Zwoelf
                               # Zeichen koennen die Regel gar nicht ausloesen.
                               provider_ref=identity.idempotency_key[:12],
                               status="claimed", agent_run_id=intent.agent_run_id,
                               task_id=intent.task_id)
        except Exception as exc:  # noqa: BLE001
            # Der Anspruch steht VOR dem Draht. Scheitert er — volle Platte,
            # gesperrte Datei, ein Zaun, der zuschlaegt —, ist beweisbar nichts
            # passiert. Ohne diesen Fang liefe es als gewoehnliche Ausnahme
            # weiter und der eingefrorene Pfad buchte „koennte passiert sein"
            # fuer einen Aufruf, der nie abgesendet wurde.
            log.error("payment.claim_write_failed", kind=type(exc).__name__,
                      execution_id=identity.execution_id)
            raise self._safe_failure("claim_write_failed") from exc

        try:
            with self._borrow(instrument.provider_secret_ref, config,
                              capability="purchase_place",
                              execution_id=identity.execution_id,
                              approval_id=identity.approval_id) as material:
                provider = self._build_provider(config, material.plaintext())
                # Ab der naechsten Zeile ist der Draht im Spiel. Alles davor
                # ist beweisbar folgenlos, alles danach nicht mehr.
                charged = await provider.charge(
                    idempotency_key=identity.idempotency_key,
                    instrument_token=instrument.provider_token,
                    merchant_id=intent.merchant_id, amount_minor=amount,
                    currency=intent.quote.currency,
                    quote_ref=intent.quote.quote_ref, description=intent.purpose)
        except (SecretDenied, SecretUnavailable, PaymentRefused) as exc:
            # Der Tresor oder die Anbieterwahl hat nein gesagt — noch vor dem
            # ersten Byte. Beweisbar folgenlos.
            #
            # `SecretDenied` steht hier ausdruecklich mit dabei, und der Grund
            # ist eine Zusicherung, die genau das gefunden hat: der Makler wirft
            # sie beim BETRETEN des `with`-Blocks, also bevor irgendein Byte
            # gesendet wurde. Liefe sie als gewoehnliche Ausnahme weiter, buchte
            # der eingefrorene Pfad sie als `UNKNOWN` — und der Mensch bekaeme
            # „ich weiss nicht sicher, ob das durchging" fuer einen Aufruf, der
            # den Rechner nie verlassen hat.
            self._store.put_intent(running.with_state(PaymentState.FAILED))
            self._store.record(payment_intent_id=intent.payment_intent_id,
                               event=PS.EVENT_DENIED,
                               execution_id=identity.execution_id,
                               instrument_ref=instrument.payment_ref,
                               failure_category=FailureCategory.POLICY_DENIED.value)
            reason = getattr(exc, "reason", None)
            raise self._safe_failure(
                getattr(reason, "value", reason) or "secret_denied") from exc
        except ProviderUnavailable as exc:
            # Verbindung kam nie zustande — nichts abgesendet.
            self._store.put_intent(running.with_state(PaymentState.FAILED))
            self._store.record(payment_intent_id=intent.payment_intent_id,
                               event=PS.EVENT_FAILED,
                               execution_id=identity.execution_id,
                               failure_category=FailureCategory.PROVIDER_UNAVAILABLE.value,
                               instrument_ref=instrument.payment_ref)
            raise self._safe_failure("provider_unavailable") from exc
        except ProviderAmbiguous as exc:
            return self._record_ambiguous(running, instrument, identity, amount, exc)
        except ProviderError as exc:
            # Ein Anbieterfehler, den der Client NICHT als „nichts passiert"
            # belegen kann, ist mehrdeutig. Alles andere waere geraten.
            return self._record_ambiguous(running, instrument, identity, amount, exc)

        return self._settle(running, instrument, identity, charged)

    # -- innere Wege ---------------------------------------------------------
    def _settle(self, intent: PaymentIntent, instrument: Instrument,
                identity: PX.ExecutionIdentity,
                charged: ProviderCharge) -> ChargeOutcome:
        amount = charged.amount_minor or (intent.quote.total_minor if intent.quote else 0)
        currency = charged.currency or (intent.quote.currency if intent.quote else "")
        if charged.status is ChargeStatus.SUCCEEDED:
            booked = self._book_charge(intent, instrument, identity, charged,
                                       amount, currency)
            if booked is not None:
                return booked
            self._store.touch_instrument(instrument.payment_ref)
            self._store.put_intent(intent.with_state(PaymentState.SUCCEEDED))
            log.info("payment.succeeded", merchant=intent.merchant_id,
                     amount=format_amount(amount, currency),
                     execution_id=identity.execution_id)
            return ChargeOutcome(PaymentState.SUCCEEDED, amount_minor=amount,
                                 currency=currency, provider_ref=charged.charge_ref,
                                 order_ref=charged.order_ref)

        if charged.status is ChargeStatus.SCA_REQUIRED:
            # Eine legitime menschliche Grenze. Sie wird nicht umgangen und nicht
            # als Fehler verkauft — SOLVIO haelt an und sagt die Wahrheit.
            self._store.put_intent(intent.with_state(PaymentState.AWAITING_SCA))
            self._store.record(payment_intent_id=intent.payment_intent_id,
                               event=PS.EVENT_AMBIGUOUS,
                               execution_id=identity.execution_id,
                               instrument_ref=instrument.payment_ref,
                               amount_minor=amount, currency=currency,
                               provider=instrument.provider,
                               # Zwoelf Zeichen, nicht zweiunddreissig — die
                               # Begruendung steht oben am `claimed`-Eintrag. Hier
                               # war sie beim ersten Bau nicht mitgezogen worden,
                               # und ausgerechnet hier waere sie am teuersten:
                               # diese Zeile IST die Aussage „ich weiss nicht, ob
                               # das durchging". Zerbricht ihr Schreiben am Zaun,
                               # gibt es sie gar nicht. Als Gegenprobe reicht der
                               # kuerzere Praefix — `reconcile` rechnet den
                               # Schluessel ohnehin neu und vergleicht nur.
                               provider_ref=identity.idempotency_key[:12],
                               failure_category=FailureCategory.SCA_REQUIRED.value,
                               status="awaiting_sca")
            raise AmbiguousExecution("purchase_place_sca_required")

        # Abgelehnt oder vom Haendler abgebrochen: der Anbieter SAGT, dass nichts
        # belastet wurde. Nur er kann das wissen, und er hat es gesagt.
        event = (PS.EVENT_DECLINED if charged.status is ChargeStatus.DECLINED
                 else PS.EVENT_FAILED)
        self._store.record(payment_intent_id=intent.payment_intent_id, event=event,
                           merchant_id=intent.merchant_id,
                           instrument_ref=instrument.payment_ref,
                           amount_minor=amount, currency=currency,
                           origin=intent.origin, approval_id=identity.approval_id,
                           execution_id=identity.execution_id,
                           provider=instrument.provider,
                           failure_category=charged.failure_category.value,
                           status=charged.status.value)
        self._store.put_intent(intent.with_state(PaymentState.FAILED))
        raise self._safe_failure(charged.failure_category.value)

    def _book_charge(self, intent: PaymentIntent, instrument: Instrument,
                     identity: PX.ExecutionIdentity, charged: ProviderCharge,
                     amount: int, currency: str) -> ChargeOutcome | None:
        """Schreibt die Belastungszeile. NACH dem Draht — sie MUSS entstehen.

        Das Geld ist weg. Ab hier ist die Zeile im Buch das Einzige, was SOLVIO
        noch davon weiss: sie traegt die Tagesgrenze, sie traegt die Erstattung,
        und sie ist der Zaun gegen die zweite Belastung. Sie an einem
        Anbietertext scheitern zu lassen, waere der teuerste denkbare Fehlschlag.

        Deshalb zwei Stufen: erst mit der Anbieterkennung, und wenn ein Zaun
        gegen die anschlaegt, ohne sie. Eine Zeile ohne Kennung ist unbequem;
        keine Zeile ist ein verlorener Kauf.
        """
        felder = dict(payment_intent_id=intent.payment_intent_id,
                      event=PS.EVENT_CHARGED, merchant_id=intent.merchant_id,
                      merchant_origin=intent.merchant_origin,
                      description=intent.purpose, amount_minor=amount,
                      currency=currency, instrument_ref=instrument.payment_ref,
                      origin=intent.origin, approval_id=identity.approval_id,
                      execution_id=identity.execution_id,
                      provider=instrument.provider, status="succeeded",
                      agent_run_id=intent.agent_run_id, task_id=intent.task_id)
        try:
            self._store.record(provider_ref=charged.charge_ref, **felder)
            return None
        except PS.DuplicateCharge:
            # Der Anbieter hat denselben Vorgang wiedergegeben. Genau dafuer ist
            # die Idempotenzkennung da — es entsteht keine zweite Zeile.
            log.info("payment.charge_already_booked",
                     execution_id=identity.execution_id)
            return self._existing_charge(identity.execution_id)
        except Exception as exc:  # noqa: BLE001
            log.error("payment.charge_ref_rejected", kind=type(exc).__name__,
                      execution_id=identity.execution_id)
        try:
            self._store.record(provider_ref="", **felder)
            log.warning("payment.charge_booked_without_reference",
                        execution_id=identity.execution_id)
            return None
        except PS.DuplicateCharge:
            return self._existing_charge(identity.execution_id)
        except Exception as exc:  # noqa: BLE001
            # Jetzt ist es wirklich unklar: das Geld ist bewegt und SOLVIO kann
            # es nicht aufschreiben. Das ist der einzige ehrliche Ausgang.
            log.error("payment.charge_unbookable", kind=type(exc).__name__,
                      execution_id=identity.execution_id)
            self._store.put_intent(
                intent.with_state(PaymentState.RECONCILIATION_REQUIRED))
            raise AmbiguousExecution("purchase_place_unbookable") from exc

    def _record_ambiguous(self, intent: PaymentIntent, instrument: Instrument,
                          identity: PX.ExecutionIdentity, amount: int,
                          exc: Exception) -> ChargeOutcome:
        """Unbekannt ist ein Ergebnis. Es wird gebucht, nicht ueberschrieben."""
        self._store.put_intent(intent.with_state(PaymentState.RECONCILIATION_REQUIRED))
        self._store.record(payment_intent_id=intent.payment_intent_id,
                           event=PS.EVENT_AMBIGUOUS,
                           merchant_id=intent.merchant_id,
                           description=intent.purpose, amount_minor=amount,
                           currency=intent.quote.currency if intent.quote else "",
                           instrument_ref=instrument.payment_ref,
                           origin=intent.origin, approval_id=identity.approval_id,
                           execution_id=identity.execution_id,
                           provider=instrument.provider,
                           # Zwoelf statt zweiunddreissig, siehe oben. Dieselbe
                           # Zeile, dieselbe Gefahr: ohne sie ist ein moeglicher
                           # Kauf unsichtbar.
                           provider_ref=identity.idempotency_key[:12],
                           failure_category=FailureCategory.UNKNOWN_RESULT.value,
                           status="unknown", agent_run_id=intent.agent_run_id,
                           task_id=intent.task_id)
        log.warning("payment.ambiguous", execution_id=identity.execution_id,
                    kind=type(exc).__name__)
        # Der eingefrorene Pfad macht daraus `UNKNOWN` und niemals einen stillen
        # zweiten Versuch. Aufgeloest wird ueber `reconcile()`.
        raise AmbiguousExecution("purchase_place")

    def _existing_charge(self, execution_id: str) -> ChargeOutcome | None:
        for row in self._store.ledger(limit=1000):
            if row["event"] == PS.EVENT_CHARGED and row["execution_id"] == execution_id:
                return ChargeOutcome(PaymentState.SUCCEEDED,
                                     amount_minor=int(row["amount_minor"]),
                                     currency=row["currency"],
                                     provider_ref=row["provider_ref"], replayed=True)
        return None

    def _instrument_or_fail(self, payment_ref: str) -> Instrument:
        instrument = self._store.instrument(payment_ref)
        if instrument is None:
            raise PaymentRefused("unknown_instrument",
                                 "Dieses Zahlungsmittel kenne ich nicht.")
        return instrument

    @staticmethod
    def _safe_failure(reason: str):
        """Eine Ausnahme, die AUSDRUECKLICH sagt: es ist nichts passiert.

        Der eingefrorene Pfad kennt genau diese eine Zusage
        (`SafeExecutionFailure`) und macht daraus `FAILED_SAFE` statt `UNKNOWN`.
        Nur wer es weiss, darf sie geben.
        """
        from solvio.security.mobile_approval.execution import SafeExecutionFailure
        return SafeExecutionFailure(reason)

    # -- Nachschlagen statt raten --------------------------------------------
    async def reconcile(self, payment_intent_id: str) -> ChargeOutcome:
        """Loest einen mehrdeutigen Ausgang auf — durch NACHSEHEN, nie durch Wiederholen.

        Gefragt wird an der Idempotenzkennung. Kennt der Anbieter sie, ist die
        Belastung passiert; kennt er sie nicht, ist sie es nicht. Beides ist
        Wissen. Eine zweite Belastung entsteht dabei nie: `lookup` ist lesend.
        """
        intent = self._store.intent(payment_intent_id)
        if intent is None:
            raise PaymentRefused("unknown_intent", "Diesen Vorgang kenne ich nicht.")
        # `EXECUTING` steht ausdruecklich mit dabei.
        #
        # Ein Vorgang, der zwischen dem durablen Anspruch und der Antwort des
        # Anbieters stehen blieb — Absturz, Stromausfall, `kill -9` —, ist der
        # gefaehrlichste Zustand ueberhaupt: die Belastung KANN stattgefunden
        # haben, und niemand hat es aufgeschrieben. Der erste Bau liess ihn
        # nicht nachschlagen und antwortete „Dieser Vorgang ist bereits
        # geklaert." — eine Aussage ueber eine Zahlung, die SOLVIO nie
        # bestaetigt hat. Gefunden in der kalten Abnahme.
        if intent.state not in (PaymentState.RECONCILIATION_REQUIRED,
                                PaymentState.AWAITING_SCA,
                                PaymentState.EXECUTING):
            raise PaymentRefused("nothing_to_reconcile",
                                 "Dieser Vorgang ist bereits geklaert.")
        execution_id = intent.execution_id
        key = ""
        for row in self._store.ledger(payment_intent_id=payment_intent_id, limit=50):
            if row["event"] == PS.EVENT_AMBIGUOUS and row["provider_ref"]:
                key = row["provider_ref"]
                break
        if not execution_id:
            raise PaymentRefused("no_execution_identity",
                                 "Zu diesem Vorgang fehlt die Ausfuehrungskennung.")
        from solvio.security.mobile_approval.execution import idempotency_key_for
        full_key = idempotency_key_for(execution_id, "purchase_place")
        if key and not full_key.startswith(key):
            log.warning("payment.reconcile_key_mismatch", execution_id=execution_id)

        instrument = self._instrument_or_fail(intent.payment_method_ref)
        config = self._provider_config(instrument)
        try:
            with self._borrow(instrument.provider_readonly_ref, config,
                              capability="payment_reconcile",
                              execution_id=execution_id,
                              approval_id=intent.approval_id) as material:
                provider = self._build_provider(config, material.plaintext())
                found = await provider.lookup(idempotency_key=full_key)
        except (ProviderError, SecretDenied, SecretUnavailable) as exc:
            # Nachschlagen ist lesend: ein Fehler dabei aendert nichts und
            # entscheidet nichts. Der Vorgang bleibt offen.
            raise ExecutorUnavailable("payment_provider_unavailable") from exc

        if found is None:
            # Der Anbieter kennt die Kennung nicht — es wurde nie belastet.
            self._store.put_intent(intent.with_state(PaymentState.FAILED))
            self._store.record(payment_intent_id=payment_intent_id,
                               event=PS.EVENT_RECONCILED,
                               execution_id=execution_id,
                               instrument_ref=instrument.payment_ref,
                               provider=instrument.provider, status="no_charge",
                               failure_category=FailureCategory.NETWORK_FAILURE.value)
            return ChargeOutcome(PaymentState.FAILED,
                                 failure_category=FailureCategory.NETWORK_FAILURE.value)
        if found.status is ChargeStatus.SUCCEEDED:
            try:
                self._store.record(
                    payment_intent_id=payment_intent_id, event=PS.EVENT_CHARGED,
                    merchant_id=intent.merchant_id,
                    merchant_origin=intent.merchant_origin,
                    description=intent.purpose, amount_minor=found.amount_minor,
                    currency=found.currency, instrument_ref=instrument.payment_ref,
                    origin=intent.origin, approval_id=intent.approval_id,
                    execution_id=execution_id, provider=instrument.provider,
                    provider_ref=found.charge_ref, status="succeeded",
                    agent_run_id=intent.agent_run_id, task_id=intent.task_id)
            except PS.DuplicateCharge:
                pass
            self._store.record(payment_intent_id=payment_intent_id,
                               event=PS.EVENT_RECONCILED, execution_id=execution_id,
                               provider_ref=found.charge_ref, status="succeeded")
            self._store.put_intent(intent.with_state(PaymentState.SUCCEEDED))
            return ChargeOutcome(PaymentState.SUCCEEDED,
                                 amount_minor=found.amount_minor,
                                 currency=found.currency,
                                 provider_ref=found.charge_ref,
                                 order_ref=found.order_ref)
        if found.status is ChargeStatus.SCA_REQUIRED:
            # Noch offen: der Mensch hat in seiner Bank-App nichts bestaetigt.
            return ChargeOutcome(PaymentState.AWAITING_SCA,
                                 failure_category=FailureCategory.SCA_REQUIRED.value)
        self._store.put_intent(intent.with_state(PaymentState.FAILED))
        self._store.record(payment_intent_id=payment_intent_id,
                           event=PS.EVENT_RECONCILED, execution_id=execution_id,
                           status=found.status.value,
                           failure_category=found.failure_category.value)
        return ChargeOutcome(PaymentState.FAILED,
                             failure_category=found.failure_category.value)

    # -- zurueck ---------------------------------------------------------------
    async def refund(self, payment_intent_id: str, *,
                     amount_minor: int = 0) -> dict[str, Any]:
        """Fordert Geld zurueck — ausschliesslich auf DASSELBE Zahlungsmittel.

        Es gibt hier kein Zielfeld, und das ist der ganze Unterschied zu einer
        Ueberweisung. Eine Erstattung kann strukturell kein Geld irgendwohin
        schicken; sie kann nur einen bereits gebuchten Betrag ruecknehmen.
        Deshalb ist sie `CRITICAL` und nicht `VERY_CRITICAL`.
        """
        intent, instrument, config = self._settled_or_fail(payment_intent_id)
        charged = self._charged_row(payment_intent_id)
        if charged is None:
            raise PaymentRefused("nothing_charged",
                                 "Zu diesem Vorgang wurde nichts belastet.")
        booked = int(charged["amount_minor"])
        already = self._refunded_minor(payment_intent_id)
        wanted = int(amount_minor) or (booked - already)
        if wanted <= 0 or already + wanted > booked:
            raise PaymentRefused("amount_out_of_range",
                                 "So viel ist da nicht mehr offen.")
        # DIE KENNUNG GEHOERT DIESER ERSTATTUNG, nicht dem Kauf.
        #
        # Der erste Bau leitete sie aus der Ausfuehrungskennung des KAUFS ab.
        # Folge: jede weitere Erstattung desselben Kaufs trug dieselbe Kennung,
        # der Anbieter gab brav die erste zurueck — und SOLVIO buchte sie als
        # neues Geld. Aus 40,00 + 49,98 wurden im Buch 120,00, waehrend beim
        # Anbieter genau 40,00 zurueckgingen. Gefunden in der kalten Abnahme.
        #
        # Die Erstattung hat eine eigene Freigabe und damit eine eigene
        # Ausfuehrungskennung; die gilt. Fehlt sie, wird der Betrag und der
        # Stand des bereits Erstatteten mit eingerechnet, damit zwei
        # verschiedene Erstattungen nie dieselbe Kennung ergeben.
        own = PX.current()
        if own.usable and own.capability == "refund_request":
            key = own.idempotency_key
        else:
            key = self._derived_key(
                intent.execution_id, f"refund_request|{already}|{wanted}")
        try:
            with self._borrow(instrument.provider_refund_ref
                              or instrument.provider_secret_ref, config,
                              capability="refund_request",
                              execution_id=own.execution_id or intent.execution_id,
                              approval_id=own.approval_id or intent.approval_id) as material:
                provider = self._build_provider(config, material.plaintext())
                refunded = await provider.refund(
                    idempotency_key=key,
                    charge_ref=str(charged["provider_ref"]),
                    amount_minor=wanted)
        except ProviderUnavailable as exc:
            raise ExecutorUnavailable("payment_provider_unavailable") from exc
        except SecretUnavailable as exc:
            raise ExecutorUnavailable("payment_credential_unavailable") from exc
        except SecretDenied as exc:
            raise PaymentRefused(
                exc.reason.value,
                "Fuer diese Erstattung darf ich den Anbieterzugang nicht "
                "benutzen.") from exc
        except ProviderError as exc:
            raise PaymentRefused(exc.category.value,
                                 "Der Anbieter hat die Erstattung nicht "
                                 "angenommen.") from exc

        # An opaque provider receipt can accidentally look like card material.
        # Preserve its identity for replay comparison without persisting that
        # material. Colon-separated digest groups cannot form a PAN digit run;
        # ordinary and empty references retain their existing representation.
        refund_ref = refunded.refund_ref
        from solvio.payment.firewall import reason_for
        if reason_for(refund_ref):
            import hashlib
            digest = hashlib.sha256(b"SOLVIO_REFUND_REFERENCE_V1\0" + refund_ref.encode("utf-8")).hexdigest()
            refund_ref = "refund-sha256:" + ":".join(digest[i:i + 8] for i in range(0, len(digest), 8))

        # Dieselbe Erstattung zweimal zu buchen waere derselbe Fehler noch
        # einmal, nur eine Ebene tiefer. Der Anbieter sagt mit `refund_ref`,
        # welchen Vorgang er meint; kennt das Buch ihn schon, ist es eine
        # Wiedergabe und kein zurueckgeflossenes Geld.
        if refund_ref and self._refund_already_booked(
                payment_intent_id, refund_ref):
            log.info("payment.refund_replayed", payment_intent_id=payment_intent_id)
            return {"vorgang": payment_intent_id, "zustand": intent.state.value,
                    "erstattet": format_amount(0, refunded.currency),
                    "hinweis": "Das war dieselbe Erstattung noch einmal — es ist "
                               "kein weiteres Geld zurueckgegangen.",
                    "zurueck_auf": instrument.display_name or instrument.payment_ref}

        total_back = already + refunded.amount_minor
        state = (PaymentState.REFUNDED if total_back >= booked
                 else PaymentState.PARTIALLY_REFUNDED)
        self._store.put_intent(intent.with_state(state))
        self._store.record(payment_intent_id=payment_intent_id,
                           event=PS.EVENT_REFUNDED,
                           merchant_id=intent.merchant_id,
                           amount_minor=refunded.amount_minor,
                           currency=refunded.currency,
                           instrument_ref=instrument.payment_ref,
                           origin=intent.origin, approval_id=intent.approval_id,
                           execution_id=intent.execution_id,
                           provider=instrument.provider,
                           provider_ref=refund_ref,
                           refund_status=refunded.status,
                           refunded_minor=total_back, status=state.value)
        return {"vorgang": payment_intent_id, "zustand": state.value,
                "erstattet": format_amount(refunded.amount_minor,
                                           refunded.currency),
                "zurueck_auf": instrument.display_name or instrument.payment_ref}

    async def cancel_order(self, payment_intent_id: str) -> dict[str, Any]:
        """Storniert die BESTELLUNG. Ausdruecklich nicht dasselbe wie erstatten.

        Ob und wann Geld zurueckkommt, entscheidet der Haendler. Diese Funktion
        behauptet es deshalb nicht — sie sagt, dass storniert wurde, und nichts
        darueber hinaus.
        """
        intent, instrument, config = self._settled_or_fail(payment_intent_id)
        charged = self._charged_row(payment_intent_id)
        if charged is None:
            raise PaymentRefused("nothing_charged",
                                 "Zu diesem Vorgang gibt es keine Bestellung.")
        own = PX.current()
        key = (own.idempotency_key
               if own.usable and own.capability == "purchase_cancel"
               else self._derived_key(intent.execution_id, "purchase_cancel"))
        try:
            with self._borrow(instrument.provider_refund_ref
                              or instrument.provider_secret_ref, config,
                              capability="purchase_cancel",
                              execution_id=own.execution_id or intent.execution_id,
                              approval_id=own.approval_id or intent.approval_id) as material:
                provider = self._build_provider(config, material.plaintext())
                await provider.cancel(idempotency_key=key,
                                      charge_ref=str(charged["provider_ref"]))
        except ProviderUnavailable as exc:
            raise ExecutorUnavailable("payment_provider_unavailable") from exc
        except SecretUnavailable as exc:
            raise ExecutorUnavailable("payment_credential_unavailable") from exc
        except SecretDenied as exc:
            raise PaymentRefused(
                exc.reason.value,
                "Fuer diese Stornierung darf ich den Anbieterzugang nicht "
                "benutzen.") from exc
        except ProviderError as exc:
            raise PaymentRefused(exc.category.value,
                                 "Der Haendler hat die Stornierung nicht "
                                 "angenommen.") from exc
        self._store.record(payment_intent_id=payment_intent_id,
                           event=PS.EVENT_CANCELLED,
                           merchant_id=intent.merchant_id,
                           instrument_ref=instrument.payment_ref,
                           execution_id=intent.execution_id,
                           provider=instrument.provider, status="order_cancelled")
        return {"vorgang": payment_intent_id, "zustand": "storniert",
                "hinweis": "Ob und wann Geld zurueckkommt, sagt dir der Haendler."}

    def _settled_or_fail(self, payment_intent_id: str):
        intent = self._store.intent(payment_intent_id)
        if intent is None:
            raise PaymentRefused("unknown_intent", "Diesen Vorgang kenne ich nicht.")
        if intent.state not in (PaymentState.SUCCEEDED,
                                PaymentState.PARTIALLY_REFUNDED):
            raise PaymentRefused("not_settled",
                                 "Zu diesem Vorgang ist noch nichts bezahlt.")
        instrument = self._instrument_or_fail(intent.payment_method_ref)
        return intent, instrument, self._provider_config(instrument)

    def _charged_row(self, payment_intent_id: str):
        for row in self._store.ledger(payment_intent_id=payment_intent_id, limit=200):
            if row["event"] == PS.EVENT_CHARGED:
                return row
        return None

    def _refund_already_booked(self, payment_intent_id: str, refund_ref: str) -> bool:
        return any(row["event"] == PS.EVENT_REFUNDED
                   and row["provider_ref"] == refund_ref
                   for row in self._store.ledger(
                       payment_intent_id=payment_intent_id, limit=200))

    def _refunded_minor(self, payment_intent_id: str) -> int:
        return sum(int(row["amount_minor"]) for row
                   in self._store.ledger(payment_intent_id=payment_intent_id, limit=200)
                   if row["event"] == PS.EVENT_REFUNDED)

    @staticmethod
    def _derived_key(execution_id: str, capability: str) -> str:
        """Die Idempotenzkennung fuer eine FOLGEHANDLUNG derselben Zahlung.

        Abgeleitet, nie gewuerfelt — und mit der Faehigkeit im Hash, damit eine
        Stornierung und eine Erstattung nie unter derselben Kennung laufen.
        Genau dieselbe Funktion, die auch der eingefrorene Pfad benutzt.
        """
        from solvio.security.mobile_approval.execution import idempotency_key_for
        if not execution_id:
            raise PaymentRefused("no_execution_identity",
                                 "Zu diesem Vorgang fehlt die Ausfuehrungskennung.")
        return idempotency_key_for(execution_id, capability)

    # -- Gesundheit ----------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        """Was gefahrlos ueber die Zahlungsschicht gesagt werden kann.

        Ausdruecklich OHNE Testbuchung. Eine Gesundheitspruefung, die Geld
        bewegt, ist keine.
        """
        out: dict[str, Any] = {"instruments": [], "providers": {},
                               "reconciliation_required": 0}
        for instrument in self._store.instruments():
            out["instruments"].append({
                "verweis": instrument.payment_ref, "status": instrument.status.value,
                "anbieter": instrument.provider})
            config = PC.provider(instrument.provider)
            if config is None:
                out["providers"][instrument.provider] = {
                    "reachable": False, "authorized": False,
                    "detail": "not_configured"}
                continue
            if instrument.provider in out["providers"]:
                continue
            try:
                with self._borrow(instrument.provider_readonly_ref, config,
                                  capability="payment_health") as material:
                    provider = self._build_provider(config, material.plaintext())
                    health = await provider.health()
                out["providers"][instrument.provider] = {
                    "reachable": health.reachable, "authorized": health.authorized,
                    "detail": health.detail}
            except Exception as exc:  # noqa: BLE001 - eine Pruefung stoert nie
                out["providers"][instrument.provider] = {
                    "reachable": False, "authorized": False,
                    "detail": type(exc).__name__}
        out["reconciliation_required"] = len(self._store.open_reconciliations())
        return out
