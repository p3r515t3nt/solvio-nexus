"""Zahlung als Faehigkeiten — und die eine, die ein Modell nicht nennen kann.

Was hier registriert wird, zerfaellt in drei Gruppen, und die Trennung ist die
eigentliche Sicherheitsaussage dieses Moduls:

**Was ein Modell darf.** `payment_list_methods` (sichere Beschreibung, nie ein
Wert), `payment_intent_prepare` (Absicht anlegen und beim Anbieter bewerten
lassen), `payment_intent_cancel` (den eigenen Entwurf verwerfen) und
`payment_reconcile` (nachsehen, ob eine unklare Zahlung durchging). Keine davon
bewegt einen Cent.

**Was nur vom Telefon kommt.** `purchase_place`, `purchase_cancel`,
`refund_request` und die ganze Zahlungsmittelverwaltung. Sie haben bewusst
**keine Sprachseite und kein Werkzeugschema** — dieselbe Haltung wie beim
Tresor: ein Modell kann sie nicht aufrufen, weil es sie nicht nennen kann. Der
Weg dorthin ist der attestierte Zahlungsendpunkt (`solvio.payment.endpoint`).

**Was es gar nicht gibt.** `bank_transfer` und `invest_order` sind als Namen in
`VERY_CRITICAL_BY_BIRTH` reserviert und in diesem Milestone ausdruecklich NICHT
gebaut. Ein reservierter Name ohne Faehigkeit ist die billigste Art, eine
Zusicherung zu geben.

**Der Betrag kommt nie vom Modell.** `payment_intent_prepare` nimmt eine
Erwartung entgegen und schreibt sie als solche hin; was bezahlt wuerde, holt der
Executor beim Anbieter. Weichen beide ab, steht die Abweichung im ERGEBNIS der
Vorbereitung — dort, wo das Modell und der Mensch sie lesen —, statt zu
verschwinden. Im Freigabetext steht ohnehin nur der wirkliche Betrag.

**Der Beschreiber rechnet nicht neu.** `describe_purchase` rendert den
GESPEICHERTEN Kostenvoranschlag und ruft keinen Anbieter. Das ist keine
Bequemlichkeit, sondern die Bedingung dafuer, dass eine Freigabe ueberhaupt
einloesbar ist: der Router bildet die Beschreibung beim Fortsetzen ERNEUT, und
eine Beschreibung, die sich von selbst aendert, erzeugt eine Endlosschleife aus
Freigaben, die der Mensch bestaetigt und die nie wirken. Aendern soll sich der
Betrag nur, wenn jemand ausdruecklich neu bewertet — und dann ist die alte
Freigabe zu Recht wertlos.
"""
from __future__ import annotations

import secrets as _secrets
import time
from typing import Any

from solvio.capabilities.contract import (CapabilityDeclined, CapabilityRefused,
                                          CapabilitySpec, ExecutionClass,
                                          ExecutorUnavailable)
from solvio.logging_setup import get_logger
from solvio.nodes.models import DataClass
from solvio.payment import merchant as PM
from solvio.payment import refs as PR
from solvio.payment import store as PS
from solvio.payment.executor import PaymentExecutor, PaymentRefused
from solvio.payment.instruments import (Instrument, InstrumentKind,
                                        InstrumentStatus, can_pay, widens)
from solvio.payment.intent import (LineItem, PaymentIntent, PaymentIntentError,
                                   PaymentState, format_amount,
                                   normalize_currency)
from solvio.security.mobile_approval.execution import (NON_IDEMPOTENT_WRITE,
                                                       READ_ONLY)
from solvio.tools.base import RiskLevel

log = get_logger("capabilities")

_TEXT = {"type": "string"}

#: Wie lange eine vorgelegte Absicht gilt.
#:
#: Die Zahl ist nicht frei gewaehlt. Der eingefrorene Freigabepfad laesst eine
#: Anfrage nach 600 s verfallen, und der Anspruch auf eine Ausfuehrung verlangt
#: beim Einloesen `expires_at > now`. Eine Absicht, die LAENGER gilt als ihre
#: Freigabe, waere
#: die Zusage, etwas ausfuehren zu koennen, was nicht mehr ausfuehrbar ist.
#: Acht Minuten lassen einem Menschen Zeit zum Lesen und liegen sicher darunter.
INTENT_TTL = 480.0

#: Hoechstbetrag, den SOLVIO ueberhaupt vorlegt — unabhaengig vom Zahlungsmittel.
#: Eine Obergrenze, die VOR der Frage greift, ist der einzige strukturelle Schutz
#: gegen einen falsch gelesenen Betrag am Daumen eines muedens Menschen. Sie
#: SENKT nur; die Grenze des Zahlungsmittels gilt zusaetzlich.
CEILING_MINOR = 50_000

SPECS: dict[str, CapabilitySpec] = {
    # -- Was ein Modell darf ------------------------------------------------
    "payment_list_methods": CapabilitySpec(
        name="payment_list_methods", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {}},
        description="Nennt die hinterlegten Zahlungsmittel — Verweis, Grenzen, "
                    "Zustand. Nie eine Kartennummer und nie einen Token."),
    "payment_intent_prepare": CapabilitySpec(
        name="payment_intent_prepare", version=1,
        execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"haendler": _TEXT, "zweck": _TEXT,
                                     "posten": _TEXT, "waehrung": _TEXT,
                                     "zahlungsmittel": _TEXT,
                                     "erwarteter_betrag": _TEXT,
                                     "lieferung": _TEXT},
                      "required": ["haendler", "zweck", "posten", "waehrung",
                                   "zahlungsmittel"]},
        description="Bereitet einen Kauf vor und laesst den Betrag beim Anbieter "
                    "bestaetigen. Bezahlt NICHTS."),
    "payment_intent_cancel": CapabilitySpec(
        name="payment_intent_cancel", version=1,
        execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {"vorgang": _TEXT},
                      "required": ["vorgang"]},
        description="Verwirft einen vorbereiteten Kauf, der noch nicht bezahlt ist."),
    "payment_reconcile": CapabilitySpec(
        name="payment_reconcile", version=1,
        execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=READ_ONLY,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {"vorgang": _TEXT},
                      "required": ["vorgang"]},
        description="Sieht beim Anbieter nach, ob eine unklar gebliebene Zahlung "
                    "durchging. Belastet nie."),

    # -- Nur vom attestierten Telefon ---------------------------------------
    "purchase_place": CapabilitySpec(
        name="purchase_place", version=1, execution_class=ExecutionClass.CONTROLLED,
        # CRITICAL und nicht MUTATING, damit auch die alte, herkunftsblinde
        # Schwelle des Schattenlaufs (`requires_approval(risk)`) eine Freigabe
        # verlangt. Die Klasse VERY_CRITICAL kommt zusaetzlich aus der Geburt.
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"vorgang": _TEXT, "pruefsumme": _TEXT},
                      "required": ["vorgang", "pruefsumme"]},
        description="Fuehrt einen freigegebenen Kauf aus. Genau einmal."),
    "purchase_cancel": CapabilitySpec(
        name="purchase_cancel", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {"vorgang": _TEXT},
                      "required": ["vorgang"]},
        description="Storniert eine Bestellung beim Haendler. Das ist NICHT "
                    "dasselbe wie eine Erstattung."),
    "refund_request": CapabilitySpec(
        name="refund_request", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"vorgang": _TEXT, "betrag": _TEXT},
                      "required": ["vorgang"]},
        description="Fordert Geld zurueck — ausschliesslich auf dasselbe "
                    "Zahlungsmittel. Es gibt kein Zielfeld."),
    "payment_method_add": CapabilitySpec(
        name="payment_method_add", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"verweis": _TEXT, "art": _TEXT,
                                     "anbieter": _TEXT, "name": _TEXT,
                                     "hinweis": _TEXT, "waehrungen": _TEXT,
                                     "haendler": _TEXT, "grenze_einzeln": _TEXT,
                                     "grenze_taeglich": _TEXT, "zugang": _TEXT,
                                     "lesezugang": _TEXT, "vorgang": _TEXT},
                      "required": ["verweis", "art", "anbieter", "waehrungen",
                                   "haendler", "grenze_einzeln", "vorgang"]},
        description="Hinterlegt ein Zahlungsmittel. Der Anbieter-Token reist "
                    "eingelagert, nie als Argument."),
    "payment_method_remove": CapabilitySpec(
        name="payment_method_remove", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {"verweis": _TEXT},
                      "required": ["verweis"]},
        description="Entfernt ein Zahlungsmittel endgueltig."),
    "payment_method_disable": CapabilitySpec(
        name="payment_method_disable", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"verweis": _TEXT, "grund": _TEXT},
                      "required": ["verweis"]},
        description="Sperrt ein Zahlungsmittel. Wirkt sofort."),
    "payment_method_enable": CapabilitySpec(
        name="payment_method_enable", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {"verweis": _TEXT},
                      "required": ["verweis"]},
        description="Gibt ein gesperrtes Zahlungsmittel wieder frei."),
    "payment_method_rescope": CapabilitySpec(
        name="payment_method_rescope", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"verweis": _TEXT, "waehrungen": _TEXT,
                                     "haendler": _TEXT},
                      "required": ["verweis"]},
        description="Aendert, wofuer ein Zahlungsmittel gilt."),
    "payment_limit_raise": CapabilitySpec(
        name="payment_limit_raise", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"verweis": _TEXT, "grenze_einzeln": _TEXT,
                                     "grenze_taeglich": _TEXT},
                      "required": ["verweis"]},
        description="Hebt eine Zahlungsgrenze an. Aus jeder Herkunft biometrisch."),
    "payment_limit_lower": CapabilitySpec(
        name="payment_limit_lower", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"verweis": _TEXT, "grenze_einzeln": _TEXT,
                                     "grenze_taeglich": _TEXT},
                      "required": ["verweis"]},
        description="Senkt eine Zahlungsgrenze. Die sichere Richtung."),
}

#: Faehigkeiten, die ein Modell aufrufen darf. Alles andere hat kein
#: Werkzeugschema und ist damit fuer ein Modell nicht benennbar.
MODEL_FACING: frozenset[str] = frozenset({
    "payment_list_methods", "payment_intent_prepare",
    "payment_intent_cancel", "payment_reconcile",
})

#: Faehigkeiten, die eine Einlagerung verbrauchen.
NEEDS_STAGING = frozenset({"payment_method_add"})


def _list(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in str(text or "").split(",") if p.strip())


def _minor(text: str, *, what: str) -> int:
    """Liest einen Betrag in Minor Units. Nie ein Fliesskomma, nie ein Komma."""
    raw = str(text or "").strip()
    if not raw:
        return 0
    if not raw.isdigit():
        raise CapabilityDeclined(
            f"{what}_not_an_integer",
            human_message="Betraege sage ich in Cent, ohne Komma.")
    value = int(raw)
    if value < 0 or value > 100_000_000:
        raise CapabilityDeclined(f"{what}_out_of_range",
                                 human_message="Dieser Betrag ist unplausibel.")
    return value


def parse_items(text: str) -> tuple[LineItem, ...]:
    """`"MagSafe Stativ|1|8499; Kabel|2|1299"` -> Positionen.

    Bewusst flach und bewusst streng. Flach, weil dieselbe Zeichenkette durch
    Router, Digest, Anzeige und die `[String: String]`-Bindung des iPhones
    laeuft — eine Form weniger, die auseinanderlaufen kann. Streng, weil eine
    tolerante Rechnung eine falsche Rechnung ist.
    """
    items: list[LineItem] = []
    for chunk in str(text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) != 3:
            raise CapabilityDeclined(
                "item_malformed",
                human_message="Ich brauche je Posten Bezeichnung, Menge und "
                              "Einzelpreis in Cent.")
        try:
            items.append(LineItem(description=parts[0], quantity=int(parts[1]),
                                  unit_amount_minor=int(parts[2])))
        except (ValueError, PaymentIntentError) as exc:
            raise CapabilityDeclined(
                "item_invalid",
                human_message="Diesen Posten kann ich nicht lesen.") from exc
    if not items:
        raise CapabilityDeclined("no_items",
                                 human_message="Ohne Posten gibt es keinen Kauf.")
    return tuple(items)


class PaymentCapabilities:
    """Der Faehigkeitsteil. Haelt Ablage und Executor, entscheidet nichts selbst."""

    def __init__(self, store: PS.PaymentStore | None = None, *,
                 executor: PaymentExecutor | None = None, staging: Any = None,
                 clock=time.time) -> None:
        self.store = store if store is not None else PS.PaymentStore()
        self.executor = executor if executor is not None else PaymentExecutor(self.store)
        self._staging = staging
        self._clock = clock
        #: Welche Einlagerung zu welchem Geraet gehoert.
        #:
        #: Das Aufnahmelager bindet jeden Wert an das Geraet, das ihn gebracht
        #: hat — `take()` verlangt dieselbe Kennung wieder. Der Handler laeuft
        #: aber im Router und kennt kein Geraet; ohne diese Bruecke uebergaebe
        #: er einen Leerstring, und der passt auf nichts.
        #:
        #: Der erste Bau tat genau das. Ergebnis: `payment_method_add` konnte
        #: NIE gelingen — der Mensch bestaetigte mit Face ID und bekam
        #: „Die Angabe ist abgelaufen." Gefunden in der kalten Abnahme, nicht
        #: von einem Test: es gab keinen, der den Weg ganz gegangen ist.
        self.device_for_staging: dict[str, str] = {}

    @property
    def staging(self):
        if self._staging is None:
            from solvio.secret_vault.staging import SecretStaging
            self._staging = SecretStaging()
        return self._staging

    # -- lesen ---------------------------------------------------------------
    async def list_methods(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"zahlungsmittel": [i.describe(for_model=True)
                                   for i in self.store.instruments()]}

    # -- vorbereiten ---------------------------------------------------------
    async def prepare(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Legt eine Absicht an und laesst den Betrag beim Anbieter bestaetigen.

        Bewegt nichts. Die Absicht traegt danach die Betragswahrheit des
        Anbieters — und daneben, ausdruecklich als solche benannt, was das Modell
        erwartet hatte.
        """
        ref = str(arguments.get("zahlungsmittel", "")).strip()
        if not PR.is_valid(ref):
            raise CapabilityDeclined(
                "unknown_payment_method",
                human_message="So heisst kein Zahlungsmittel, das ich kenne.")
        instrument = self.store.instrument(ref)
        if instrument is None:
            raise CapabilityDeclined(
                "unknown_payment_method",
                human_message="Dieses Zahlungsmittel kenne ich nicht.")
        if not can_pay(instrument.status):
            raise CapabilityRefused(
                "payment_method_not_active",
                human_message="Dieses Zahlungsmittel ist gerade gesperrt.")

        merchant_id = str(arguments.get("haendler", "")).strip().lower()
        merchant = PM.merchant_for_id(merchant_id)
        if merchant is None:
            raise CapabilityRefused(
                "unknown_merchant",
                human_message="Bei diesem Haendler kaufe ich nicht — er steht "
                              "nicht auf meiner Liste.")
        currency = str(arguments.get("waehrung", "")).strip()
        try:
            currency = normalize_currency(currency)
        except PaymentIntentError as exc:
            raise CapabilityDeclined(
                "currency_invalid",
                human_message="Diese Waehrung kenne ich nicht.") from exc

        items = parse_items(arguments.get("posten", ""))
        now = self._clock()
        # Die Herkunft ist Core-Wahrheit und kommt aus dem Vorgangskontext, den
        # der Router gesetzt hat — nie aus einem Argument. Ohne Kontext heisst
        # sie `unspecified`, und das ist die strengste Antwort, nicht die
        # bequemste.
        from solvio.secret_vault import context as SC
        origin = SC.current().origin.value or "unspecified"
        draft = PaymentIntent(
            payment_intent_id="pi-" + _secrets.token_hex(8),
            requested_by="model", origin=origin, merchant_id=merchant.merchant_id,
            merchant_origin=merchant.origin, currency=currency,
            payment_method_ref=instrument.payment_ref,
            purpose=str(arguments.get("zweck", "")).strip() or merchant.display_name,
            items=items, created_at=now, expires_at=now + INTENT_TTL,
            agent_expected_total_minor=_minor(arguments.get("erwarteter_betrag"),
                                              what="erwarteter_betrag"),
            shipping_label=str(arguments.get("lieferung", "")).strip())
        self.store.put_intent(draft)
        self.store.record(payment_intent_id=draft.payment_intent_id,
                          event=PS.EVENT_CREATED, merchant_id=draft.merchant_id,
                          merchant_origin=draft.merchant_origin,
                          description=draft.purpose,
                          instrument_ref=draft.payment_method_ref,
                          currency=draft.currency, origin=draft.origin,
                          status="draft")

        quoted = await self.executor.quote(draft)
        total = quoted.final_total_minor
        if total > CEILING_MINOR:
            # VOR der Frage, nicht danach. Ein Betrag ueber der Hausgrenze wird
            # gar nicht erst vorgelegt.
            self.store.put_intent(quoted.with_state(PaymentState.CANCELLED))
            raise CapabilityRefused(
                "above_ceiling",
                human_message=f"{format_amount(total, quoted.currency)} liegt "
                              "ueber dem, was ich dir ueberhaupt vorlege.")
        ready = quoted.with_state(PaymentState.READY_FOR_APPROVAL)
        self.store.put_intent(ready)

        view = ready.safe_view()
        view["pruefsumme"] = ready.economic_digest()
        view["betrag_lesbar"] = format_amount(total, ready.currency)
        view["erwartung_stimmt"] = ready.agent_expectation_matches
        if ready.agent_expected_total_minor and not ready.agent_expectation_matches:
            view["hinweis"] = (
                "Der Anbieter nennt einen anderen Betrag als erwartet. Es gilt "
                "der des Anbieters.")
        return view

    async def cancel_intent(self, arguments: dict[str, Any]) -> dict[str, Any]:
        intent = self._intent_or_decline(arguments.get("vorgang"))
        if intent.state not in (PaymentState.DRAFT, PaymentState.QUOTED,
                                PaymentState.READY_FOR_APPROVAL,
                                PaymentState.APPROVED):
            raise CapabilityRefused(
                "not_cancellable",
                human_message="Diesen Vorgang kann ich nicht mehr zuruecknehmen.")
        self.store.put_intent(intent.with_state(PaymentState.CANCELLED))
        self.store.record(payment_intent_id=intent.payment_intent_id,
                          event=PS.EVENT_CANCELLED, status="cancelled")
        return {"vorgang": intent.payment_intent_id, "zustand": "verworfen"}

    # -- bezahlen ------------------------------------------------------------
    def describe_purchase(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Was auf dem Telefon steht — und damit, was Face ID bindet.

        Jede Angabe hier veraendert den Digest. Deshalb steht der Betrag als
        eigene Zeile und als ZEICHENKETTE: `json.dumps(19.90)` und
        `json.dumps(19.9)` sind zwei verschiedene Texte fuer denselben Betrag,
        und ein Fliesskomma weiter oben veraenderte still, was jemand bestaetigt
        hat.

        Und nichts hier haengt an der Uhr. `gueltig_bis` ist der GESPEICHERTE
        Zeitpunkt, kein `jetzt + x` — sonst faellt die Beschreibung beim
        Fortsetzen anders aus, die Freigabe gilt nicht mehr, der Router legt
        eine neue an, und der Mensch bestaetigt in einer Schleife etwas, das nie
        wirkt.
        """
        intent = self._intent_or_decline(arguments.get("vorgang"))
        digest = str(arguments.get("pruefsumme", "")).strip()
        if intent.quote is None:
            raise CapabilityDeclined(
                "intent_not_quoted",
                human_message="Zu diesem Kauf steht noch kein Betrag fest.")
        if intent.economic_digest() != digest:
            raise CapabilityRefused(
                "intent_changed",
                human_message="Der Vorgang hat sich geaendert. Danach frage ich "
                              "nicht — ich bereite ihn neu vor.")
        if intent.state not in (PaymentState.READY_FOR_APPROVAL,
                                PaymentState.APPROVED):
            raise CapabilityRefused(
                "intent_not_ready",
                human_message="Dieser Kauf steht nicht zur Freigabe an.")
        # Ein abgelaufener Vorgang wird GAR NICHT ERST vorgelegt.
        #
        # Gefunden in der Live-Abnahme von DEBT-0126 (2026-08-30): ein Vorgang
        # vom 27. August stand noch in der Liste der App, der Mensch tippte auf
        # „Bezahlen", der Core baute einen vollstaendigen signierten Kaufauftrag
        # ueber 89,98 EUR und fragte per Face ID danach — und `purchase` wies ihn
        # danach mit `intent_expired` ab (:513). Es floss kein Cent; die
        # Weigerung sitzt vor dem Draht. Aber der Mensch hat biometrische
        # Autoritaet fuer eine Handlung ausgegeben, die nie wirken konnte, und
        # genau das nennt der Absatz zwei ueber dieser Zeile beim Namen.
        #
        # Der GERENDERTE TEXT haengt weiterhin an keiner Uhr — `gueltig_bis`
        # bleibt der gespeicherte Zeitpunkt. Von der Uhr haengt nur ab, OB
        # ueberhaupt vorgelegt wird, und das ist der Unterschied zwischen einer
        # Beschreibung, die sich unter dem Daumen aendert, und einer Frage, die
        # gar nicht gestellt wird.
        if intent.is_expired(self._clock()):
            raise CapabilityRefused(
                "intent_expired",
                human_message="Dieser Kauf ist abgelaufen. Ich frage dich nicht "
                              "danach — sag mir, wenn ich ihn neu vorbereiten soll.")
        # `_instrument_or_decline` statt eines rohen Zugriffs: ein
        # zwischenzeitlich entferntes Zahlungsmittel ergab sonst einen
        # `AttributeError`, den der Router als „nicht beschreibbar" faengt —
        # eine ehrliche Absage, aber ohne den Grund, den es zu sagen gibt.
        instrument = self._instrument_or_decline(intent.payment_method_ref)
        quote = intent.quote
        aufschlaege = "; ".join(
            f"{e.label} {format_amount(e.amount_minor, quote.currency)}"
            for e in sorted(quote.extras, key=lambda e: e.kind.value)) or "keine"
        belastet = "wie oben"
        if quote.charged_currency and quote.charged_currency != quote.currency:
            belastet = format_amount(quote.charged_total_minor,
                                     quote.charged_currency)
        return {
            "zweck": intent.purpose,
            "haendler": intent.merchant_display_name,
            "adresse": intent.merchant_origin,
            "posten": "; ".join(
                f"{i.quantity}× {i.description} zu "
                f"{format_amount(i.unit_amount_minor, quote.currency)}"
                for i in intent.items),
            "menge": str(intent.total_quantity),
            "zwischensumme": format_amount(quote.items_total_minor, quote.currency),
            "aufschlaege": aufschlaege,
            "betrag": format_amount(quote.total_minor, quote.currency),
            "waehrung": quote.currency,
            "rechnung": ("stimmt" if quote.reconciles
                         else "WEICHT AB von der Summe der Posten"),
            "belastet": belastet,
            "zahlungsmittel": (instrument.display_name or intent.payment_method_ref)
                              + (f" ({instrument.display_hint})"
                                 if instrument and instrument.display_hint else ""),
            "lieferung": intent.shipping_label or "keine",
            "lieferziel": (intent.shipping_destination_sha256[:16] or "keins"),
            "gueltig_bis": _iso(intent.expires_at),
            "vorgang": intent.payment_intent_id,
            "pruefsumme": intent.economic_digest()[:16],
        }

    async def purchase(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Fuehrt den freigegebenen Kauf aus. Ab hier zaehlt nur noch Ehrlichkeit.

        Jede Absage VOR dem Draht ist ein `SafeExecutionFailure` — der
        eingefrorene Pfad bucht jede andere Ausnahme als `UNKNOWN`, und der
        Mensch bekaeme „ich weiss nicht sicher, ob das durchging" fuer einen
        Aufruf, der den Rechner nie verlassen hat.
        """
        from solvio.capabilities import execution_identity as EI
        from solvio.security.mobile_approval.execution import SafeExecutionFailure

        identity = EI.current()
        if not identity.usable:
            # Ohne freigegebenen Vorgang gibt es keine Zahlung. Das faengt auch
            # den Schattenlauf und jeden Weg, der die Freigabe uebersprungen hat.
            raise SafeExecutionFailure("no_execution_identity")

        intent = self.store.intent(str(arguments.get("vorgang", "")).strip())
        if intent is None:
            raise SafeExecutionFailure("unknown_intent")
        if intent.quote is None:
            raise SafeExecutionFailure("intent_not_quoted")
        if intent.economic_digest() != str(arguments.get("pruefsumme", "")).strip():
            raise SafeExecutionFailure("intent_changed")
        if intent.is_expired(self._clock()):
            raise SafeExecutionFailure("intent_expired")
        if intent.quote.total_minor > CEILING_MINOR:
            raise SafeExecutionFailure("above_ceiling")

        if intent.state is PaymentState.READY_FOR_APPROVAL:
            try:
                intent = intent.with_state(PaymentState.APPROVED,
                                           approval_id=identity.approval_id)
                self.store.put_intent(intent)
                self.store.record(payment_intent_id=intent.payment_intent_id,
                                  event=PS.EVENT_APPROVED,
                                  approval_id=identity.approval_id,
                                  execution_id=identity.execution_id,
                                  amount_minor=intent.quote.total_minor,
                                  currency=intent.quote.currency,
                                  instrument_ref=intent.payment_method_ref,
                                  origin=intent.origin, status="approved")
            except Exception as exc:  # noqa: BLE001
                # Auch diese Buchhaltung steht VOR dem Draht. Scheitert sie,
                # ist beweisbar nichts passiert — und genau das muss gesagt
                # werden, sonst bucht der eingefrorene Pfad „koennte passiert
                # sein" fuer einen Aufruf, der nie abgesendet wurde.
                log.error("payment.approval_write_failed", kind=type(exc).__name__)
                raise SafeExecutionFailure("approval_write_failed") from exc
        elif intent.state is not PaymentState.APPROVED:
            raise SafeExecutionFailure(f"intent_state_{intent.state.value}")

        outcome = await self.executor.charge(intent.payment_intent_id)
        return {"vorgang": intent.payment_intent_id, "zustand": outcome.state.value,
                "betrag": format_amount(outcome.amount_minor, outcome.currency),
                "waehrung": outcome.currency,
                "bestellnummer": outcome.order_ref,
                "erneut": outcome.replayed,
                "haendler": intent.merchant_display_name}

    async def reconcile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        intent = self._intent_or_decline(arguments.get("vorgang"))
        try:
            outcome = await self.executor.reconcile(intent.payment_intent_id)
        except PaymentRefused as exc:
            raise CapabilityRefused(exc.reason,
                                    human_message=exc.human_message) from exc
        words = {
            PaymentState.SUCCEEDED: "Die Zahlung ist durchgegangen.",
            PaymentState.FAILED: "Es wurde nichts belastet.",
            PaymentState.AWAITING_SCA: "Deine Bank wartet noch auf dich.",
        }
        return {"vorgang": intent.payment_intent_id,
                "zustand": outcome.state.value,
                "antwort": words.get(outcome.state, "Noch unklar."),
                "betrag": format_amount(outcome.amount_minor, outcome.currency)
                          if outcome.amount_minor else ""}

    # -- Hilfen --------------------------------------------------------------
    def _intent_or_decline(self, value: Any) -> PaymentIntent:
        intent = self.store.intent(str(value or "").strip())
        if intent is None:
            raise CapabilityDeclined(
                "unknown_intent",
                human_message="Diesen Vorgang kenne ich nicht mehr.")
        return intent


def _iso(when: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(when), timezone.utc).replace(
        microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Verwaltung — nur ueber den attestierten Zahlungsendpunkt, nie ueber Sprache
# ---------------------------------------------------------------------------
class PaymentAdminMixin:
    """Die Verwaltungshandlungen. Getrennt, damit man sie zusammen liest.

    Jede davon steht in `VERY_CRITICAL_BY_BIRTH` oder in `ACTION_CLASS` und
    laeuft ueber denselben Router wie alles andere. Keine davon hat ein
    Werkzeugschema — ein Modell kann sie nicht nennen.
    """

    # -- beschreiben (was auf dem Telefon steht) ------------------------------
    def describe_method_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ref = str(arguments.get("verweis", "")).strip()
        return {
            "zahlungsmittel": str(arguments.get("name", "")).strip() or ref,
            "verweis": ref,
            "art": str(arguments.get("art", "")).strip(),
            "anbieter": str(arguments.get("anbieter", "")).strip(),
            "grenze_einzeln": format_amount(
                _minor(arguments.get("grenze_einzeln"), what="grenze_einzeln"),
                _first_currency(arguments.get("waehrungen"))),
            "grenze_taeglich": _limit_word(arguments.get("grenze_taeglich"),
                                           arguments.get("waehrungen")),
            "waehrungen": ", ".join(_list(arguments.get("waehrungen", ""))) or "keine",
            "haendler": ", ".join(_list(arguments.get("haendler", ""))) or "keine",
            "vorgang": str(arguments.get("vorgang", "")).strip(),
        }

    def describe_method_change(self, arguments: dict[str, Any]) -> dict[str, Any]:
        instrument = self._instrument_or_decline(arguments.get("verweis"))
        return {"zahlungsmittel": instrument.display_name or instrument.payment_ref,
                "verweis": instrument.payment_ref,
                "grund": str(arguments.get("grund", "")).strip()}

    def describe_limit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        instrument = self._instrument_or_decline(arguments.get("verweis"))
        code = (instrument.allowed_currencies or ("EUR",))[0]
        single = _minor(arguments.get("grenze_einzeln"), what="grenze_einzeln")
        daily = _minor(arguments.get("grenze_taeglich"), what="grenze_taeglich")
        return {
            "zahlungsmittel": instrument.display_name or instrument.payment_ref,
            "verweis": instrument.payment_ref,
            "bisher_einzeln": format_amount(instrument.max_single_minor, code),
            "neu_einzeln": (format_amount(single, code) if single
                            else "unveraendert"),
            "bisher_taeglich": (format_amount(instrument.daily_total_minor, code)
                                if instrument.daily_total_minor else "keine"),
            "neu_taeglich": (format_amount(daily, code) if daily else "unveraendert"),
        }

    def describe_rescope(self, arguments: dict[str, Any]) -> dict[str, Any]:
        instrument = self._instrument_or_decline(arguments.get("verweis"))
        return {
            "zahlungsmittel": instrument.display_name or instrument.payment_ref,
            "verweis": instrument.payment_ref,
            "waehrungen": ", ".join(_list(arguments.get("waehrungen", ""))) or "keine",
            "haendler": ", ".join(_list(arguments.get("haendler", ""))) or "keine",
            "bisher_haendler": ", ".join(instrument.allowed_merchant_ids) or "keine",
        }

    def describe_order_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        intent = self._intent_or_decline(arguments.get("vorgang"))
        quote = intent.quote
        code = quote.currency if quote else intent.currency
        amount = _minor(arguments.get("betrag"), what="betrag")
        return {"vorgang": intent.payment_intent_id,
                "haendler": intent.merchant_display_name,
                "betrag": format_amount(amount or (quote.total_minor if quote else 0),
                                        code),
                "zurueck_auf": intent.payment_method_ref}

    # -- handeln --------------------------------------------------------------
    async def method_add(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ref = str(arguments.get("verweis", "")).strip()
        if not PR.is_valid(ref):
            raise CapabilityDeclined("invalid_reference",
                                     human_message="So kann ein Verweis nicht heissen.")
        if self.store.instrument(ref) is not None:
            raise CapabilityRefused(
                "already_exists",
                human_message="Unter diesem Verweis liegt schon ein Zahlungsmittel.")
        try:
            kind = InstrumentKind(str(arguments.get("art", "")).strip())
        except ValueError as exc:
            raise CapabilityDeclined(
                "unknown_kind",
                human_message="Diese Art von Zahlungsmittel kenne ich nicht.") from exc

        # Der Anbieter-Token kommt EINGELAGERT herein und nie als Argument: was
        # als Argument reist, steht im Freigabetext und dauerhaft in der
        # Freigabe-Datenbank. Dieselbe Ueberlegung wie beim Tresor.
        staging_id = str(arguments.get("vorgang", "")).strip()
        device_id = self.device_for_staging.get(staging_id, "")
        if not device_id:
            # Ausdruecklich KEIN Leerstring an `take()`: das Lager wuerde ihn
            # gegen die echte Geraetekennung halten und immer ablehnen — eine
            # Absage, die wie ein Fristablauf aussieht und keiner ist.
            raise CapabilityDeclined(
                "staging_unknown",
                human_message="Die Angabe ist abgelaufen. Bitte noch einmal.")
        try:
            token = self.staging.take(staging_id, device_id=device_id).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - eine fehlende Einlagerung ist ein Nein
            raise CapabilityDeclined(
                "staging_missing",
                human_message="Die Angabe ist abgelaufen. Bitte noch einmal.") from exc
        finally:
            self.device_for_staging.pop(staging_id, None)

        merchants = _list(arguments.get("haendler", ""))
        unknown = [m for m in merchants if PM.merchant_for_id(m) is None]
        if unknown:
            raise CapabilityRefused(
                "unknown_merchant",
                human_message="Mindestens einer dieser Haendler steht nicht auf "
                              "meiner Liste.")
        try:
            instrument = Instrument(
                payment_ref=ref, kind=kind,
                provider=str(arguments.get("anbieter", "")).strip(),
                max_single_minor=_minor(arguments.get("grenze_einzeln"),
                                        what="grenze_einzeln"),
                daily_total_minor=_minor(arguments.get("grenze_taeglich"),
                                         what="grenze_taeglich"),
                allowed_currencies=_list(arguments.get("waehrungen", "")),
                allowed_merchant_ids=merchants,
                provider_secret_ref=str(arguments.get("zugang", "")).strip(),
                provider_readonly_ref=str(arguments.get("lesezugang", "")).strip(),
                provider_token=token,
                display_name=str(arguments.get("name", "")).strip(),
                display_hint=str(arguments.get("hinweis", "")).strip(),
                created_at=PS.utcnow_iso())
        except ValueError as exc:
            raise CapabilityDeclined("instrument_invalid",
                                     human_message="Diese Angaben passen nicht "
                                                   "zusammen.") from exc
        self.store.put_instrument(instrument)
        self.store.record(payment_intent_id="", event=PS.EVENT_INSTRUMENT_CHANGED,
                          instrument_ref=instrument.payment_ref, status="added")
        log.info("payment.method_added", payment_ref=instrument.payment_ref,
                 kind=instrument.kind.value)
        return {"verweis": instrument.payment_ref, "zustand": "hinterlegt"}

    async def method_remove(self, arguments: dict[str, Any]) -> dict[str, Any]:
        instrument = self._instrument_or_decline(arguments.get("verweis"))
        self.store.delete_instrument(instrument.payment_ref)
        self.store.record(payment_intent_id="", event=PS.EVENT_INSTRUMENT_CHANGED,
                          instrument_ref=instrument.payment_ref, status="removed")
        return {"verweis": instrument.payment_ref, "zustand": "entfernt"}

    async def method_disable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._set_status(arguments, InstrumentStatus.DISABLED,
                                      str(arguments.get("grund", "")).strip())

    async def method_enable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._set_status(arguments, InstrumentStatus.ACTIVE, "")

    async def _set_status(self, arguments: dict[str, Any],
                          status: InstrumentStatus, reason: str) -> dict[str, Any]:
        import dataclasses
        instrument = self._instrument_or_decline(arguments.get("verweis"))
        updated = dataclasses.replace(instrument, status=status,
                                      disabled_reason=reason,
                                      version=instrument.version + 1)
        self.store.put_instrument(updated)
        self.store.record(payment_intent_id="", event=PS.EVENT_INSTRUMENT_CHANGED,
                          instrument_ref=updated.payment_ref, status=status.value)
        log.info("payment.method_status", payment_ref=updated.payment_ref,
                 status=status.value)
        return {"verweis": updated.payment_ref, "zustand": status.value}

    async def limit_raise(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._set_limits(arguments, direction="raise")

    async def limit_lower(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._set_limits(arguments, direction="lower")

    async def _set_limits(self, arguments: dict[str, Any], *,
                          direction: str) -> dict[str, Any]:
        """Richtung wird GEPRUEFT, nicht geglaubt.

        Zwei Faehigkeiten mit zwei Klassen waeren wertlos, wenn die billigere
        auch anheben koennte. Der Core rechnet deshalb nach, was die neue Zeile
        gegenueber der alten bedeutet — und weist die falsche Richtung ab.
        """
        import dataclasses
        instrument = self._instrument_or_decline(arguments.get("verweis"))
        single = _minor(arguments.get("grenze_einzeln"), what="grenze_einzeln")
        daily = _minor(arguments.get("grenze_taeglich"), what="grenze_taeglich")
        updated = dataclasses.replace(
            instrument,
            max_single_minor=single or instrument.max_single_minor,
            daily_total_minor=(daily if arguments.get("grenze_taeglich")
                               else instrument.daily_total_minor),
            version=instrument.version + 1)
        widened = widens(instrument, updated)
        if direction == "lower" and widened:
            raise CapabilityRefused(
                "would_widen",
                human_message="Das waere eine ANHEBUNG. Dafuer brauche ich deine "
                              "ausdrueckliche Freigabe.")
        if direction == "raise" and not widened:
            raise CapabilityDeclined(
                "nothing_raised",
                human_message="Das hebt nichts an — nimm den anderen Weg.")
        self.store.put_instrument(updated)
        self.store.record(payment_intent_id="", event=PS.EVENT_INSTRUMENT_CHANGED,
                          instrument_ref=updated.payment_ref,
                          status=f"limits_{direction}")
        return {"verweis": updated.payment_ref,
                "grenze_einzeln": updated.max_single_minor,
                "grenze_taeglich": updated.daily_total_minor}

    async def method_rescope(self, arguments: dict[str, Any]) -> dict[str, Any]:
        import dataclasses
        instrument = self._instrument_or_decline(arguments.get("verweis"))
        merchants = _list(arguments.get("haendler", ""))
        unknown = [m for m in merchants if PM.merchant_for_id(m) is None]
        if unknown:
            raise CapabilityRefused(
                "unknown_merchant",
                human_message="Mindestens einer dieser Haendler steht nicht auf "
                              "meiner Liste.")
        updated = dataclasses.replace(
            instrument,
            allowed_currencies=_list(arguments.get("waehrungen", ""))
                               or instrument.allowed_currencies,
            allowed_merchant_ids=merchants or instrument.allowed_merchant_ids,
            version=instrument.version + 1)
        self.store.put_instrument(updated)
        self.store.record(payment_intent_id="", event=PS.EVENT_INSTRUMENT_CHANGED,
                          instrument_ref=updated.payment_ref, status="rescoped")
        return {"verweis": updated.payment_ref,
                "haendler": list(updated.allowed_merchant_ids)}

    # -- Bestellung zuruecknehmen --------------------------------------------
    async def order_cancel(self, arguments: dict[str, Any]) -> dict[str, Any]:
        intent = self._intent_or_decline(arguments.get("vorgang"))
        try:
            result = await self.executor.cancel_order(intent.payment_intent_id)
        except PaymentRefused as exc:
            raise CapabilityRefused(exc.reason,
                                    human_message=exc.human_message) from exc
        return result

    async def refund(self, arguments: dict[str, Any]) -> dict[str, Any]:
        intent = self._intent_or_decline(arguments.get("vorgang"))
        amount = _minor(arguments.get("betrag"), what="betrag")
        try:
            result = await self.executor.refund(intent.payment_intent_id,
                                                amount_minor=amount)
        except PaymentRefused as exc:
            raise CapabilityRefused(exc.reason,
                                    human_message=exc.human_message) from exc
        return result

    def _instrument_or_decline(self, value: Any) -> Instrument:
        instrument = self.store.instrument(str(value or "").strip())
        if instrument is None:
            raise CapabilityDeclined(
                "unknown_payment_method",
                human_message="Dieses Zahlungsmittel kenne ich nicht.")
        return instrument


class PaymentCapabilitiesFull(PaymentCapabilities, PaymentAdminMixin):
    """Lesen, Vorbereiten, Bezahlen und Verwalten in EINEM Objekt."""


def _first_currency(text: Any) -> str:
    found = _list(str(text or ""))
    return found[0].upper() if found else "EUR"


def _limit_word(value: Any, currencies: Any) -> str:
    amount = _minor(value, what="grenze_taeglich")
    return format_amount(amount, _first_currency(currencies)) if amount else "keine"


#: Faehigkeit -> (Handler-Name, Beschreiber-Name oder leer).
_WIRING: dict[str, tuple[str, str]] = {
    "payment_list_methods": ("list_methods", ""),
    "payment_intent_prepare": ("prepare", ""),
    "payment_intent_cancel": ("cancel_intent", ""),
    "payment_reconcile": ("reconcile", ""),
    "purchase_place": ("purchase", "describe_purchase"),
    "purchase_cancel": ("order_cancel", "describe_order_action"),
    "refund_request": ("refund", "describe_order_action"),
    "payment_method_add": ("method_add", "describe_method_add"),
    "payment_method_remove": ("method_remove", "describe_method_change"),
    "payment_method_disable": ("method_disable", "describe_method_change"),
    "payment_method_enable": ("method_enable", "describe_method_change"),
    "payment_method_rescope": ("method_rescope", "describe_rescope"),
    "payment_limit_raise": ("limit_raise", "describe_limit"),
    "payment_limit_lower": ("limit_lower", "describe_limit"),
}


def register(router: Any, capabilities: PaymentCapabilitiesFull) -> list[str]:
    """Meldet die Zahlungsfaehigkeiten am Router an."""
    registered: list[str] = []
    for name, (handler_name, describe_name) in _WIRING.items():
        spec = SPECS[name]
        handler = getattr(capabilities, handler_name)
        describe = getattr(capabilities, describe_name) if describe_name else None
        router.register(spec, handler, describe=describe)
        registered.append(name)
    return sorted(registered)
