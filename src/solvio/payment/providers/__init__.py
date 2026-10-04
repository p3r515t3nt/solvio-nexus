"""Der Anbietervertrag — was ein Zahlungsanbieter koennen muss, und was er nie darf.

SOLVIO besitzt seine Abhaengigkeiten. Ein Zahlungsanbieter ist ein austauschbares
Teil: er fuehrt aus, er entscheidet nichts, und er bekommt nie mehr als den
konkreten Vorgang. Dieses Modul beschreibt die schmale Naht dorthin.

**Was ein Anbieter zurueckgeben darf.** Strukturierte, sichere Ergebnisse — die
Datenklassen unten und nichts sonst. Kein Rohkoerper, keine Kopfzeilen, kein
Fehlertext des Anbieters. Was von dort kaeme, waere fremder Inhalt; er darf
informieren, nie autorisieren, und er hat auf dem Weg ins Modell nichts zu
suchen.

**Die drei Ausgaenge, die zaehlen.** Nicht „ging" und „ging nicht", sondern:

    gelungen        der Anbieter hat belastet und sagt das
    abgelehnt       der Anbieter hat NICHT belastet und sagt das
    unbekannt       niemand weiss es — `ProviderAmbiguous`

Der dritte ist der wichtige. Ein Zeitablauf nach dem Absenden ist KEIN
Fehlschlag; die Belastung kann stattgefunden haben. Wer daraus „fehlgeschlagen"
macht und wiederholt, kauft zweimal. Deshalb ist `ProviderAmbiguous` eine eigene
Ausnahme, und deshalb ist jede unerwartete Ausnahme ebenfalls mehrdeutig — die
Asymmetrie ist Absicht, wortgleich zu `SafeExecutionFailure` im eingefrorenen
Ausfuehrungsjournal.

**Idempotenz ist Pflicht, nicht Kuer.** Jeder Anbieter bekommt eine
Idempotenzkennung und muss unter derselben Kennung denselben Vorgang
zurueckgeben statt einen zweiten anzulegen. Ein Anbieter ohne diese Zusage ist
fuer SOLVIO nicht benutzbar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence, runtime_checkable


class ChargeStatus(str, Enum):
    """Was der Anbieter ueber DIESE Belastung sagt."""

    SUCCEEDED = "succeeded"
    #: Die Bank oder der Anbieter hat abgelehnt. NICHTS wurde belastet.
    DECLINED = "declined"
    #: Der Nutzer muss beim Anbieter oder in seiner Bank-App bestaetigen.
    #: Eine legitime menschliche Grenze — nie ein Hindernis, das man umgeht.
    SCA_REQUIRED = "sca_required"
    #: Der Haendler hat abgebrochen (Vorrat, Preis, Konto).
    MERCHANT_FAILED = "merchant_failed"
    #: Der Anbieter kennt den Vorgang, kann aber (noch) nichts sagen.
    PENDING = "pending"


class FailureCategory(str, Enum):
    """Warum nicht. Abschliessend — der Fehlertext des Anbieters ist kein Grund."""

    NONE = "none"
    DECLINED = "declined"
    SCA_REQUIRED = "sca_required"
    NETWORK_FAILURE = "network_failure"
    MERCHANT_FAILURE = "merchant_failure"
    UNKNOWN_RESULT = "unknown_result"
    EXPIRED_APPROVAL = "expired_approval"
    POLICY_DENIED = "policy_denied"
    LIMIT_EXCEEDED = "limit_exceeded"
    INSTRUMENT_UNUSABLE = "instrument_unusable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_UNAUTHORIZED = "provider_unauthorized"


class ProviderError(RuntimeError):
    """Ein Anbieterfehler. Traegt nie einen Rohkoerper und nie Zahlungsmaterial."""

    def __init__(self, category: FailureCategory, detail: str = "") -> None:
        super().__init__(f"provider_error:{category.value}")
        self.category = category
        #: Kurz, kategorisch, protokollierbar. NIE der Anbietertext.
        self.detail = detail[:120]


class ProviderUnavailable(ProviderError):
    """Der Anbieter war nicht erreichbar — BEVOR irgendetwas abgesendet wurde.

    Nur wer das WEISS, darf es sagen. Ein Verbindungsfehler beim Aufbau weiss
    es; ein Zeitablauf beim Lesen der Antwort weiss es nicht.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(FailureCategory.PROVIDER_UNAVAILABLE, detail)


class ProviderAmbiguous(ProviderError):
    """Der Ausgang ist unbekannt. Die Belastung KANN stattgefunden haben.

    Es gibt keinen automatischen Wiederholungsversuch. Aufgeloest wird ueber
    `lookup()` an der Idempotenzkennung — nachschlagen, nicht raten.
    """

    def __init__(self, detail: str = "", idempotency_key: str = "") -> None:
        super().__init__(FailureCategory.UNKNOWN_RESULT, detail)
        self.idempotency_key = idempotency_key


@dataclass(frozen=True)
class ProviderQuote:
    """Die Betragswahrheit des Anbieters. Der EINZIGE zulaessige Ursprung."""

    quote_ref: str
    currency: str
    items_total_minor: int
    #: Aufschlaege als `(kind, amount_minor, label)`. `kind` ist ein Wert aus
    #: `intent.ExtraKind` — ein unbekannter Aufschlag ist ein Fehler, kein
    #: Sammelposten.
    extras: tuple[tuple[str, int, str], ...] = ()
    total_minor: int = 0
    charged_currency: str = ""
    charged_total_minor: int = 0
    #: Sagt der Haendler selbst, dass hier ein Abonnement entsteht?
    recurring: bool = False


@dataclass(frozen=True)
class ProviderCharge:
    """Was der Anbieter ueber eine Belastung sagt. Sicher, strukturiert, knapp."""

    status: ChargeStatus
    #: Die Kennung, unter der der Anbieter den Vorgang wiederfindet.
    charge_ref: str = ""
    amount_minor: int = 0
    currency: str = ""
    charged_currency: str = ""
    charged_total_minor: int = 0
    failure_category: FailureCategory = FailureCategory.NONE
    #: Verlangt der Anbieter eine Handlung des Menschen? DASS, nicht WO — eine
    #: Adresse aus einer Anbieterantwort ist fremder Inhalt und wird dem Modell
    #: nicht vorgelegt.
    sca_pending: bool = False
    #: Bestellnummer beim Haendler, falls vorhanden.
    order_ref: str = ""
    refunded_minor: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status is ChargeStatus.SUCCEEDED


@dataclass(frozen=True)
class ProviderRefund:
    refund_ref: str
    amount_minor: int
    currency: str
    #: `succeeded`, `pending` oder `failed`.
    status: str = "succeeded"


@dataclass(frozen=True)
class ProviderHealth:
    """Was eine GEFAHRLOSE Pruefung ueber den Anbieter sagen kann.

    Ausdruecklich ohne Testbuchung: eine Gesundheitspruefung, die Geld bewegt,
    ist keine.
    """

    reachable: bool
    authorized: bool
    detail: str = ""
    instruments: tuple[str, ...] = ()


@runtime_checkable
class PaymentProvider(Protocol):
    """Die Naht. Fuenf Verben, alle mit Idempotenzkennung, keines mit Ermessen."""

    name: str

    async def quote(self, *, merchant_id: str, merchant_origin: str,
                    items: Sequence[dict[str, Any]], currency: str,
                    shipping_label: str = "") -> ProviderQuote: ...

    async def charge(self, *, idempotency_key: str, instrument_token: str,
                     merchant_id: str, amount_minor: int, currency: str,
                     quote_ref: str = "", description: str = "") -> ProviderCharge: ...

    async def lookup(self, *, idempotency_key: str) -> ProviderCharge | None: ...

    async def refund(self, *, idempotency_key: str, charge_ref: str,
                     amount_minor: int) -> ProviderRefund: ...

    async def cancel(self, *, idempotency_key: str, charge_ref: str) -> ProviderCharge: ...

    async def health(self) -> ProviderHealth: ...
