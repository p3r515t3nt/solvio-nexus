"""Die Zahlungsabsicht — was gekauft werden soll, und wer das behauptet hat.

Drei Dinge werden hier auseinandergehalten, und die Trennung ist der ganze
Sicherheitsgewinn dieses Moduls:

    ABSICHT            was jemand kaufen moechte
    BETRAGSWAHRHEIT    was es tatsaechlich kostet
    BEFUGNIS           dass es bezahlt werden darf

Ein Modell darf die ABSICHT vorschlagen. Es darf die BETRAGSWAHRHEIT nie
liefern — die kommt aus dem vertrauenswuerdigen Executor bzw. vom Anbieter, und
sie liegt deshalb nicht in der Absicht, sondern in einem eigenen Objekt
(`PaymentQuote`), das der Core anhaengt und jederzeit neu bilden kann. Die
BEFUGNIS entsteht nirgends hier, sondern ausschliesslich am Geraet mit Face ID.

**Warum der Betrag nicht im unveraenderlichen Teil steht.** Ein Preis aendert
sich zwischen Vorschlag und Kasse — Versand, Steuer, Wechselkurs, Vorrat. Wer
den Betrag in die Absicht schreibt, muss ihn entweder aendern (dann ist die
Absicht nicht mehr unveraenderlich) oder eine neue Absicht anlegen (dann
verliert er die Spur). Getrennt gilt beides: die Absicht ist ab Anlage
unveraenderlich, und jede Neubewertung ERSETZT den Kostenvoranschlag und erhoeht
`quote_version`. Weil `economic_digest()` beides abdeckt, macht genau das eine
bereits erteilte Freigabe strukturell wertlos — es braucht dafuer keine Regel,
die jemand vergessen koennte.

**Geld ist ganzzahlig.** Betraege sind Minor Units (Cent), niemals Fliesskomma.
`0.1 + 0.2 != 0.3` ist in einer Kaufbestaetigung kein akademischer Hinweis.

**Was hier nie steht:** Kartennummer, Pruefziffer, Anbieter-Token, Bankzugang.
Das Modul kennt einen `PaymentRef` und sonst nichts ueber das Zahlungsmittel.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from solvio.payment import refs as PR

#: Domain-Trenner fuer den wirtschaftlichen Digest. Ohne ihn koennte ein Hash
#: aus einem anderen Zusammenhang hier als gueltig durchgehen.
_DIGEST_DOMAIN = b"SOLVIO_PAYMENT_ECONOMIC_V1"

#: ISO 4217, drei Grossbuchstaben. Die ERLAUBTEN Waehrungen stehen nicht hier,
#: sondern am Zahlungsmittel — dies ist nur die Form.
_CURRENCY = re.compile(r"^[A-Z]{3}$")

#: Waehrungen mit ZWEI Nachkommastellen, und nur die.
#:
#: `format_amount` teilt hart durch 100. Fuer JPY (null Stellen) wuerden aus
#: 100 Yen „1,00 JPY", fuer TND (drei Stellen) aus 1000 Millimes „10,00 TND" —
#: ein falscher Betrag auf einer Kaufbestaetigung, und zwar ein leiser.
#: Solange die Rechnung den Exponenten nicht kennt, darf sie nur Waehrungen
#: sehen, fuer die sie stimmt. Eine Erweiterung kostet den Exponenten ueberall:
#: hier, in `format_amount`, in der Hausobergrenze und in den Grenzen des
#: Zahlungsmittels. Das ist der Preis, und er wird bezahlt oder nicht.
EXPONENT_TWO: frozenset[str] = frozenset({
    "EUR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "CAD",
    "AUD", "NZD", "SGD", "HKD", "ZAR", "BRL", "MXN", "TRY", "ILS", "RON",
})

#: Obergrenze fuer einen einzelnen Betrag in Minor Units. Kein Politikwert,
#: sondern ein Schutz gegen Ueberlauf und gegen die versehentliche Verwechslung
#: von Major und Minor Units (100000000 Cent = 1 Mio EUR).
MAX_AMOUNT_MINOR = 100_000_000

#: Wie viele Positionen eine Bestellung hoechstens hat. Ein Freigabetext, den
#: niemand zu Ende liest, ist keine Freigabe.
MAX_ITEMS = 50


class PaymentState(str, Enum):
    """Der Zustand einer Zahlungsabsicht. Abschliessend, uebergangsgeprueft.

    Es gibt bewusst keinen Zustand „vielleicht bezahlt". Was mehrdeutig endete,
    heisst `RECONCILIATION_REQUIRED` und wird nachgeschlagen, nie erraten und
    nie blind wiederholt.
    """

    #: Vom Modell vorgeschlagen. Traegt keine Betragswahrheit und keine Befugnis.
    DRAFT = "draft"
    #: Der Executor hat den tatsaechlichen Endbetrag geliefert.
    QUOTED = "quoted"
    #: Bewertet, innerhalb der Grenzen, Zahlungsmittel aktiv — vorlegbar.
    READY_FOR_APPROVAL = "ready_for_approval"
    #: Der Mensch hat mit Face ID genau diese wirtschaftliche Wirkung bestaetigt.
    APPROVED = "approved"
    #: Der Executor ist unterwegs. Der durable Anspruch steht bereits.
    EXECUTING = "executing"
    #: Die Bank oder der Anbieter verlangt eine Handlung des Menschen (SCA/3DS).
    AWAITING_SCA = "awaiting_sca"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Der Ausgang ist unbekannt. NICHT „fehlgeschlagen" — die Wirkung KANN
    #: eingetreten sein.
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


#: Endzustaende. Von hier fuehrt kein Weg zu einer Geldbewegung.
TERMINAL_STATES: frozenset[PaymentState] = frozenset({
    PaymentState.FAILED, PaymentState.CANCELLED, PaymentState.EXPIRED,
    PaymentState.REFUNDED,
})

#: Die einzige erlaubte Landkarte. Ein Uebergang, der hier nicht steht, ist ein
#: Fehler und keine Ermessensfrage.
TRANSITIONS: dict[PaymentState, frozenset[PaymentState]] = {
    PaymentState.DRAFT: frozenset({
        PaymentState.QUOTED, PaymentState.CANCELLED, PaymentState.EXPIRED,
        PaymentState.FAILED}),
    PaymentState.QUOTED: frozenset({
        PaymentState.QUOTED, PaymentState.READY_FOR_APPROVAL,
        PaymentState.CANCELLED, PaymentState.EXPIRED, PaymentState.FAILED}),
    PaymentState.READY_FOR_APPROVAL: frozenset({
        # Eine Neubewertung wirft die Vorlage zurueck — und macht damit jede
        # bereits erteilte Freigabe gegenstandslos.
        PaymentState.QUOTED, PaymentState.APPROVED,
        PaymentState.CANCELLED, PaymentState.EXPIRED, PaymentState.FAILED}),
    PaymentState.APPROVED: frozenset({
        PaymentState.EXECUTING, PaymentState.CANCELLED, PaymentState.EXPIRED,
        PaymentState.FAILED}),
    PaymentState.EXECUTING: frozenset({
        PaymentState.SUCCEEDED, PaymentState.FAILED, PaymentState.AWAITING_SCA,
        PaymentState.RECONCILIATION_REQUIRED}),
    PaymentState.AWAITING_SCA: frozenset({
        PaymentState.SUCCEEDED, PaymentState.FAILED,
        PaymentState.RECONCILIATION_REQUIRED, PaymentState.EXPIRED}),
    PaymentState.RECONCILIATION_REQUIRED: frozenset({
        PaymentState.SUCCEEDED, PaymentState.FAILED}),
    PaymentState.SUCCEEDED: frozenset({
        PaymentState.PARTIALLY_REFUNDED, PaymentState.REFUNDED}),
    PaymentState.PARTIALLY_REFUNDED: frozenset({
        PaymentState.PARTIALLY_REFUNDED, PaymentState.REFUNDED}),
    PaymentState.FAILED: frozenset(),
    PaymentState.CANCELLED: frozenset(),
    PaymentState.EXPIRED: frozenset(),
    PaymentState.REFUNDED: frozenset(),
}


class PaymentIntentError(ValueError):
    """Die Absicht ist keine. Traegt nie einen Wert und nie Zahlungsmaterial."""


class IllegalTransition(PaymentIntentError):
    """Dieser Uebergang steht nicht auf der Landkarte."""


def can_transition(current: PaymentState, target: PaymentState) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def require_transition(current: PaymentState, target: PaymentState) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(f"{current.value} -> {target.value}")


class ExtraKind(str, Enum):
    """Aufschlaege, die niemals stillschweigend mitlaufen duerfen.

    Trinkgeld, Spende, Garantie, Versicherung und jedes Abonnement sind eigene
    wirtschaftliche Entscheidungen. Sie stehen deshalb einzeln im Freigabetext
    und einzeln im Digest — ein Agent kann keinen davon „mitnehmen".
    """

    SHIPPING = "shipping"
    TAX = "tax"
    TIP = "tip"
    DONATION = "donation"
    WARRANTY = "warranty"
    INSURANCE = "insurance"
    SUBSCRIPTION = "subscription"
    FEE = "fee"
    DISCOUNT = "discount"


#: Aufschlaege, die V1 ueberhaupt nicht zulaesst — sie sind nicht „teuer", sie
#: sind eine ANDERE Entscheidung als ein einmaliger Kauf.
FORBIDDEN_EXTRAS: frozenset[ExtraKind] = frozenset({ExtraKind.SUBSCRIPTION})

#: Aufschlaege, die ein Modell nie selbst in einen Entwurf setzen darf. Sie
#: koennen nur aus der vom Executor gelesenen Kassenwahrheit stammen.
OPT_IN_EXTRAS: frozenset[ExtraKind] = frozenset({
    ExtraKind.TIP, ExtraKind.DONATION, ExtraKind.WARRANTY, ExtraKind.INSURANCE,
})


def _clean_text(value: Any, *, limit: int, what: str) -> str:
    """Anzeigefaehiger Text: keine Steuerzeichen, keine Richtungstricks.

    Dieselbe Haltung wie `protocol.validate_display_text` im eingefrorenen
    Freigabepfad, und aus demselben Grund: dieser Text landet auf einem Display,
    unter dem ein Mensch eine Geldbewegung bestaetigt. Ein unsichtbares
    Richtungszeichen kann `84,99 EUR` als `99,48 EUR` erscheinen lassen.
    """
    if not isinstance(value, str):
        raise PaymentIntentError(f"{what} must be a string")
    text = unicodedata.normalize("NFC", value).strip()
    if not text:
        raise PaymentIntentError(f"{what} must not be empty")
    if len(text) > limit:
        raise PaymentIntentError(f"{what} is too long")
    for ch in text:
        code = ord(ch)
        if code < 0x20 and ch not in "\t\n":
            raise PaymentIntentError(f"{what} contains a control character")
        if 0x7F <= code <= 0x9F:
            raise PaymentIntentError(f"{what} contains a control character")
        if code in (0x061C, 0x200E, 0x200F, 0xFEFF) \
                or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:
            raise PaymentIntentError(f"{what} contains a bidi control")
        if code in (0x2028, 0x2029):
            # Zeilen- und Absatztrenner ueberleben `json.dumps` als sie selbst
            # und werden auf dem Telefon als UMBRUCH gerendert. Damit liesse
            # sich in einen Freigabetext eine Zeile einziehen, die aussieht wie
            # eine eigene Angabe.
            raise PaymentIntentError(f"{what} contains a line separator")
    return text


def _amount(value: Any, *, what: str, allow_negative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaymentIntentError(f"{what} must be an integer in minor units")
    if not allow_negative and value < 0:
        raise PaymentIntentError(f"{what} must not be negative")
    if abs(value) > MAX_AMOUNT_MINOR:
        raise PaymentIntentError(f"{what} is out of range")
    return value


def normalize_currency(value: Any) -> str:
    if not isinstance(value, str):
        raise PaymentIntentError("currency must be a string")
    code = value.strip().upper()
    if not _CURRENCY.match(code):
        raise PaymentIntentError("currency must be an ISO 4217 alphabetic code")
    if code not in EXPONENT_TWO:
        raise PaymentIntentError(
            "currency is not one of the two-decimal currencies this version "
            "can render correctly")
    return code


@dataclass(frozen=True)
class LineItem:
    """Eine Bestellposition. Menge und Einzelpreis stehen einzeln im Digest.

    Warum nicht nur die Summe: §7 verlangt, dass eine geaenderte MENGE eine
    Freigabe ungueltig macht. Zwei Stueck zu 42,50 und ein Stueck zu 85,00
    ergeben denselben Betrag und sind nicht derselbe Kauf.
    """

    description: str
    quantity: int
    unit_amount_minor: int
    #: Optional: die Artikelkennung des Haendlers. Bindet die Bestellung an ein
    #: konkretes Produkt statt an einen Anzeigetext.
    item_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "description",
                           _clean_text(self.description, limit=200, what="item description"))
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) \
                or self.quantity < 1 or self.quantity > 10_000:
            raise PaymentIntentError("quantity must be a positive integer")
        object.__setattr__(self, "unit_amount_minor",
                           _amount(self.unit_amount_minor, what="unit amount"))
        if self.item_id:
            object.__setattr__(self, "item_id",
                               _clean_text(self.item_id, limit=128, what="item id"))
        if self.total_minor > MAX_AMOUNT_MINOR:
            raise PaymentIntentError("line total is out of range")

    @property
    def total_minor(self) -> int:
        return self.quantity * self.unit_amount_minor

    def as_dict(self) -> dict[str, Any]:
        return {"description": self.description, "quantity": self.quantity,
                "unit_amount_minor": self.unit_amount_minor,
                "total_minor": self.total_minor, "item_id": self.item_id}


@dataclass(frozen=True)
class Extra:
    """Ein Aufschlag oder Abzug mit eigenem Namen. Nie stillschweigend."""

    kind: ExtraKind
    amount_minor: int
    label: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExtraKind):
            raise PaymentIntentError("extra kind is unknown")
        object.__setattr__(self, "amount_minor",
                           _amount(self.amount_minor, what="extra amount",
                                   allow_negative=(self.kind is ExtraKind.DISCOUNT)))
        if self.kind is ExtraKind.DISCOUNT and self.amount_minor > 0:
            raise PaymentIntentError("a discount must not increase the total")
        label = self.label or _EXTRA_LABEL[self.kind]
        object.__setattr__(self, "label",
                           _clean_text(label, limit=120, what="extra label"))

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "amount_minor": self.amount_minor,
                "label": self.label}


_EXTRA_LABEL: dict[ExtraKind, str] = {
    ExtraKind.SHIPPING: "Versand",
    ExtraKind.TAX: "Steuer",
    ExtraKind.TIP: "Trinkgeld",
    ExtraKind.DONATION: "Spende",
    ExtraKind.WARRANTY: "Garantieverlängerung",
    ExtraKind.INSURANCE: "Versicherung",
    ExtraKind.SUBSCRIPTION: "Abonnement",
    ExtraKind.FEE: "Gebühr",
    ExtraKind.DISCOUNT: "Rabatt",
}


@dataclass(frozen=True)
class PaymentQuote:
    """Die BETRAGSWAHRHEIT. Kommt vom Executor bzw. Anbieter, nie vom Modell.

    Der Kostenvoranschlag haengt an einer Absicht und ersetzt sich beim
    Neubewerten. `version` zaehlt dabei hoch — und weil er in den Digest
    eingeht, wird jede bereits erteilte Freigabe damit strukturell wertlos.

    `source` sagt, WER den Betrag behauptet. `agent` ist als Quelle
    ausdruecklich NICHT zulaessig; die Erwartung des Modells lebt getrennt in
    `PaymentIntent.agent_expected_total_minor` und wird nur VERGLICHEN.
    """

    #: `executor` (Anbieter/Kasse ueber den vertrauenswuerdigen Executor).
    source: str
    currency: str
    items_total_minor: int
    extras: tuple[Extra, ...] = ()
    #: Der Betrag, den der Anbieter tatsaechlich abbuchen wird. Er wird NICHT
    #: aus den Positionen gerechnet, sondern vom Anbieter genannt und gegen die
    #: Rechnung GEPRUEFT — wenn beides auseinanderlaeuft, gilt der Anbieter, und
    #: die Abweichung steht sichtbar im Freigabetext.
    total_minor: int = 0
    version: int = 1
    quoted_at: float = 0.0
    #: Die Kennung, unter der der Anbieter denselben Vorgang wiederfindet.
    quote_ref: str = ""
    #: Waehrung und Betrag, die die Bank dem Nutzer belastet, falls der Anbieter
    #: umrechnet. Leer heisst: keine Umrechnung bekannt.
    charged_currency: str = ""
    charged_total_minor: int = 0

    def __post_init__(self) -> None:
        if self.source != "executor":
            raise PaymentIntentError("a quote may only come from the trusted executor")
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "items_total_minor",
                           _amount(self.items_total_minor, what="items total"))
        object.__setattr__(self, "total_minor", _amount(self.total_minor, what="total"))
        if not isinstance(self.extras, tuple) or len(self.extras) > 20:
            raise PaymentIntentError("extras must be a tuple of at most 20 entries")
        for extra in self.extras:
            if not isinstance(extra, Extra):
                raise PaymentIntentError("extras must be Extra entries")
            if extra.kind in FORBIDDEN_EXTRAS:
                raise PaymentIntentError(f"extra kind is not allowed in V1: {extra.kind.value}")
        kinds = [e.kind for e in self.extras]
        if len(set(kinds)) != len(kinds):
            raise PaymentIntentError("an extra kind must appear at most once")
        if isinstance(self.version, bool) or not isinstance(self.version, int) \
                or self.version < 1:
            raise PaymentIntentError("quote version must be a positive integer")
        if self.quote_ref:
            object.__setattr__(self, "quote_ref",
                               _clean_text(self.quote_ref, limit=128, what="quote ref"))
        if self.charged_currency:
            object.__setattr__(self, "charged_currency",
                               normalize_currency(self.charged_currency))
            object.__setattr__(self, "charged_total_minor",
                               _amount(self.charged_total_minor, what="charged total"))
            # „Dieselbe Waehrung, anderer Betrag" ist kein Umrechnungsfall,
            # sondern ein Widerspruch — und er wuerde als „Belastet wird: wie
            # oben" gerendert, also die Warnzeile auf dem Telefon gerade
            # unterdruecken. Besser gar keine Bewertung als eine, die beruhigt.
            if (self.charged_currency == self.currency
                    and self.charged_total_minor
                    and self.charged_total_minor != self.total_minor):
                raise PaymentIntentError(
                    "a charged total in the same currency must equal the total")
        elif self.charged_total_minor:
            raise PaymentIntentError("a charged total needs a charged currency")
        if self.total_minor <= 0:
            raise PaymentIntentError("a payment must move a positive amount")

    @property
    def computed_total_minor(self) -> int:
        """Was die Positionen und Aufschlaege ergeben. Nur zum VERGLEICHEN."""
        return self.items_total_minor + sum(e.amount_minor for e in self.extras)

    @property
    def reconciles(self) -> bool:
        """Stimmt die Rechnung des Anbieters mit seinen eigenen Zeilen ueberein?

        Wird in `PaymentIntent.economic_effect()` GELESEN und erscheint als
        eigene Zeile im Freigabetext. Eine Eigenschaft, die niemand aufruft,
        ist eine Zusicherung, die niemand gibt — der erste Bau hatte genau das.
        """
        return self.computed_total_minor == self.total_minor

    def extra(self, kind: ExtraKind) -> int:
        for entry in self.extras:
            if entry.kind is kind:
                return entry.amount_minor
        return 0

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "currency": self.currency,
                "items_total_minor": self.items_total_minor,
                "extras": [e.as_dict() for e in self.extras],
                "total_minor": self.total_minor, "version": self.version,
                "quoted_at": self.quoted_at, "quote_ref": self.quote_ref,
                "charged_currency": self.charged_currency,
                "charged_total_minor": self.charged_total_minor}


@dataclass(frozen=True)
class PaymentIntent:
    """Die Absicht. Ab Anlage unveraenderlich — Zustand und Voranschlag getrennt.

    Was hier steht, hat ein Mensch oder ein Modell GEWOLLT. Was es kostet, steht
    im `quote`. Ob es bezahlt werden darf, steht nirgends hier.
    """

    payment_intent_id: str
    #: Wer den Vorschlag gemacht hat: `model`, `user`, `background`.
    requested_by: str
    #: Die Herkunftsklasse des Auftrags — woertlich der Wert aus der
    #: Freigabepolitik. Kein Freitext, keine Selbstauskunft.
    origin: str
    merchant_id: str
    merchant_origin: str
    currency: str
    payment_method_ref: str
    purpose: str
    items: tuple[LineItem, ...]
    created_at: float
    expires_at: float
    #: Was das MODELL erwartet hat. Ausdruecklich unvertrauenswuerdig und
    #: ausdruecklich nicht der Betrag, der bezahlt wird — er wird nur mit der
    #: Betragswahrheit VERGLICHEN, damit eine Abweichung sichtbar wird.
    agent_expected_total_minor: int = 0
    #: Sichere Bezeichnung des Lieferziels („Standard-Lieferadresse"). Die
    #: Adresse selbst steht hier nie.
    shipping_label: str = ""
    #: SHA-256 der kanonischen Lieferadresse. Bindet das Ziel, ohne es zu
    #: zeigen: eine nach der Freigabe geaenderte Adresse aendert diesen Wert.
    shipping_destination_sha256: str = ""
    #: Wiederkehrende Abbuchung. In V1 immer False — ein Abonnement ist eine
    #: andere Entscheidung als ein Kauf und wird abgelehnt, nicht angezeigt.
    recurring: bool = False
    state: PaymentState = PaymentState.DRAFT
    quote: PaymentQuote | None = None
    #: Gesetzt, sobald der eingefrorene Freigabepfad eine Ausfuehrung beansprucht.
    execution_id: str = ""
    approval_id: str = ""
    #: Kennungen einer kuenftigen Agentenlaufzeit. Leer und dennoch da, damit
    #: das Zahlungsbuch spaeter ohne Schemawechsel darauf zeigen kann.
    agent_run_id: str = ""
    task_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payment_intent_id",
                           _clean_text(self.payment_intent_id, limit=64,
                                       what="payment intent id"))
        if self.requested_by not in ("model", "user", "background"):
            raise PaymentIntentError("requested_by is unknown")
        object.__setattr__(self, "origin",
                           _clean_text(self.origin, limit=64, what="origin"))
        object.__setattr__(self, "purpose",
                           _clean_text(self.purpose, limit=200, what="purpose"))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        PR.parse(self.payment_method_ref)
        object.__setattr__(self, "payment_method_ref",
                           str(PR.parse(self.payment_method_ref)))
        if not isinstance(self.items, tuple) or not self.items:
            raise PaymentIntentError("a payment intent needs at least one line item")
        if len(self.items) > MAX_ITEMS:
            raise PaymentIntentError("too many line items")
        for item in self.items:
            if not isinstance(item, LineItem):
                raise PaymentIntentError("items must be LineItem entries")
        if self.recurring:
            raise PaymentIntentError("recurring payments are not supported in V1")
        if self.shipping_label:
            object.__setattr__(self, "shipping_label",
                               _clean_text(self.shipping_label, limit=120,
                                           what="shipping label"))
        if self.shipping_destination_sha256:
            digest = str(self.shipping_destination_sha256).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise PaymentIntentError("shipping destination hash is malformed")
            object.__setattr__(self, "shipping_destination_sha256", digest)
        _amount(self.agent_expected_total_minor, what="agent expected total")
        if not isinstance(self.state, PaymentState):
            raise PaymentIntentError("state is unknown")
        if self.quote is not None:
            if not isinstance(self.quote, PaymentQuote):
                raise PaymentIntentError("quote must be a PaymentQuote")
            if self.quote.currency != self.currency:
                raise PaymentIntentError("quote currency differs from the intent currency")
        # Die Haendlerbindung: Kennung UND Herkunft muessen auf denselben
        # Eintrag der eigenen Liste zeigen. Der Import liegt hier, weil die
        # Liste zum Zahlungsteil gehoert und nicht zur Form einer Absicht.
        from solvio.payment.merchant import resolve as _resolve_merchant
        merchant = _resolve_merchant(self.merchant_id, self.merchant_origin)
        object.__setattr__(self, "merchant_id", merchant.merchant_id)
        object.__setattr__(self, "merchant_origin", merchant.origin)

    # -- abgeleitet ----------------------------------------------------------
    @property
    def merchant_display_name(self) -> str:
        from solvio.payment.merchant import merchant_for_id
        found = merchant_for_id(self.merchant_id)
        return found.display_name if found else self.merchant_id

    @property
    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def items_total_minor(self) -> int:
        return sum(item.total_minor for item in self.items)

    @property
    def final_total_minor(self) -> int:
        """Der Betrag, der bezahlt wuerde. OHNE Voranschlag gibt es keinen.

        Absichtlich kein Rueckfall auf die Modellerwartung: „was das Modell
        gesagt hat" ist nie der Betrag, den jemand bestaetigt.
        """
        if self.quote is None:
            raise PaymentIntentError("intent has no executor-confirmed quote")
        return self.quote.total_minor

    @property
    def agent_expectation_matches(self) -> bool:
        if self.quote is None or not self.agent_expected_total_minor:
            return False
        return self.agent_expected_total_minor == self.quote.total_minor

    def is_expired(self, now: float) -> bool:
        return bool(self.expires_at) and now >= self.expires_at

    # -- Uebergaenge ---------------------------------------------------------
    def with_state(self, target: PaymentState, **changes: Any) -> "PaymentIntent":
        require_transition(self.state, target)
        return replace(self, state=target, **changes)

    def with_quote(self, quote: PaymentQuote) -> "PaymentIntent":
        """Neu bewerten. Erhoeht die Fassung und wirft eine Vorlage zurueck.

        Nach `APPROVED` ist das kein legitimer Vorgang mehr: der Mensch hat
        einen konkreten Betrag bestaetigt, und eine Neubewertung waere der
        Versuch, unter dieser Bestaetigung etwas anderes abzubuchen.
        """
        if self.state not in (PaymentState.DRAFT, PaymentState.QUOTED,
                              PaymentState.READY_FOR_APPROVAL):
            raise IllegalTransition(f"cannot re-quote in state {self.state.value}")
        version = 1 if self.quote is None else self.quote.version + 1
        fresh = replace(quote, version=version)
        return replace(self, quote=fresh, state=PaymentState.QUOTED)

    # -- Bindung -------------------------------------------------------------
    def economic_effect(self) -> dict[str, Any]:
        """Die WIRTSCHAFTLICHE WIRKUNG, kanonisch und vollstaendig.

        Genau das geht in den Freigabetext und damit in den Digest. Aendert sich
        hier ein einziges Feld — ein Cent, ein Stueck, ein Haendler, eine
        Waehrung, das Zahlungsmittel, der Versand, das Lieferziel, die Frist —,
        traegt eine bereits erteilte Freigabe die Handlung nicht mehr.
        """
        if self.quote is None:
            raise PaymentIntentError("intent has no executor-confirmed quote")
        return {
            "payment_intent_id": self.payment_intent_id,
            # Der Zweck reist als Beschreibung MIT an den Haendler
            # (`provider.charge(description=...)`). Was beim Haendler ankommt,
            # gehoert in das, was der Mensch bestaetigt hat.
            "purpose": self.purpose,
            "merchant_id": self.merchant_id,
            "merchant_origin": self.merchant_origin,
            "merchant_display_name": self.merchant_display_name,
            "payment_method_ref": self.payment_method_ref,
            "currency": self.quote.currency,
            "total_minor": self.quote.total_minor,
            "items_total_minor": self.quote.items_total_minor,
            "extras": [e.as_dict() for e in
                       sorted(self.quote.extras, key=lambda e: e.kind.value)],
            "items": [i.as_dict() for i in self.items],
            "total_quantity": self.total_quantity,
            "shipping_label": self.shipping_label,
            "shipping_destination_sha256": self.shipping_destination_sha256,
            "charged_currency": self.quote.charged_currency,
            "charged_total_minor": self.quote.charged_total_minor,
            "recurring": self.recurring,
            "quote_version": self.quote.version,
            # Rechnet der Anbieter anders als seine eigenen Zeilen, steht das
            # SICHTBAR im Freigabetext — statt still zu verschwinden.
            "totals_agree": self.quote.reconciles,
            "expires_at": round(float(self.expires_at), 3),
        }

    def economic_digest(self) -> str:
        payload = json.dumps(self.economic_effect(), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(_DIGEST_DOMAIN + b"\x00"
                              + payload.encode("utf-8")).hexdigest()

    # -- was ein Modell sehen darf -------------------------------------------
    def safe_view(self) -> dict[str, Any]:
        """Die Beschreibung fuer das Modell. Kein Zahlungsmaterial, nie ein Wert."""
        view: dict[str, Any] = {
            "payment_intent_id": self.payment_intent_id,
            "zustand": self.state.value,
            "haendler": self.merchant_display_name,
            "haendler_herkunft": self.merchant_origin,
            "zweck": self.purpose,
            "waehrung": self.currency,
            "zahlungsmittel": self.payment_method_ref,
            "positionen": [i.as_dict() for i in self.items],
            "menge_gesamt": self.total_quantity,
            "lieferung": self.shipping_label,
            "gueltig_bis": self.expires_at,
        }
        # JEDER Vorgang traegt DIESELBEN Schluessel, auch der unbewertete.
        # Ein Feld, das mal da ist und mal nicht, laesst die Liste auf dem
        # Telefon als GANZES umfallen — samt der Vorgaenge, die ein Mensch
        # aufloesen muesste. Gefunden in der kalten Abnahme.
        if self.quote is None:
            view["betrag_minor"] = None
            view["betrag_quelle"] = "noch nicht bestaetigt"
            view["aufschlaege"] = []
        else:
            view["betrag_minor"] = self.quote.total_minor
            view["betrag_quelle"] = "executor"
            view["aufschlaege"] = [e.as_dict() for e in self.quote.extras]
        return view


def format_amount(amount_minor: int, currency: str) -> str:
    """`8499, EUR` -> `84,99 EUR`. Deutsche Schreibweise, zwei Nachkommastellen.

    Bewusst ohne `locale`: eine Umgebungsvariable darf nicht bestimmen, welcher
    Betrag auf einer Kaufbestaetigung steht.
    """
    code = normalize_currency(currency)
    sign = "-" if amount_minor < 0 else ""
    value = abs(int(amount_minor))
    major, minor = divmod(value, 100)
    grouped = f"{major:,}".replace(",", ".")
    return f"{sign}{grouped},{minor:02d} {code}"


def hash_destination(*parts: Iterable[Any]) -> str:
    """SHA-256 ueber eine kanonisierte Lieferadresse. Der Klartext bleibt draussen."""
    flat = [unicodedata.normalize("NFKC", str(p)).strip().lower()
            for p in parts if str(p).strip()]
    payload = "\x1f".join(flat).encode("utf-8")
    return hashlib.sha256(b"SOLVIO_PAYMENT_DESTINATION_V1\x00" + payload).hexdigest()
