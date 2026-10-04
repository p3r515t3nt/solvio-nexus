"""Das Zahlungsmittel — was ein Modell darueber wissen darf, und was nie.

Ein Zahlungsmittel ist hier eine BESCHREIBUNG mit GRENZEN. Es ist kein Wert,
kein Token und keine Karte. Das Material selbst liegt beim Anbieter, im
Betriebssystem-Wallet oder beim Haendler; SOLVIO haelt einen `PaymentRef`, eine
Handvoll sicherer Anzeigedaten und eine Politik, die Befugnis ausschliesslich
EINSCHRAENKT.

**Grenzen reduzieren, nie erweitern.** Die Grenzen des Anbieters gelten ohnehin.
Was hier steht, kommt zusaetzlich obendrauf und kann nur strenger sein. Ein
Agent kann keine davon anheben — anheben ist eine eigene, biometrische Handlung
(`payment_limit_raise`, VERY_CRITICAL). Senken und Sperren sind die sichere
Richtung und deshalb billiger.

**Leere Menge heisst NEIN.** Ein Zahlungsmittel ohne erlaubte Haendler kauft
nirgends, eines ohne erlaubte Waehrung zahlt nichts. Wortgleich zur
Tresor-Politik, und aus demselben Grund: eine vergessene Zeile muss strenger
machen, nie lockerer.

**Was NIE in dieses Modul kommt:** PAN, Pruefziffer, Magnetstreifendaten,
Bankzugang, Anbieter-Hauptschluessel, ein wiederverwendbarer Token, der allein
Geld bewegen kann. Wenn ein Anbieter das verlangte, waere er der falsche
Anbieter — nicht dieses Modul die falsche Stelle.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from solvio.payment import refs as PR
from solvio.payment.intent import normalize_currency

_POLICY_DOMAIN = b"SOLVIO_PAYMENT_INSTRUMENT_POLICY_V1"


def _looks_like_card(text: str) -> bool:
    from solvio.payment.firewall import is_payment_material
    return is_payment_material(text)

#: Ein Anzeigehinweis wie „•••• 4242". Vier Ziffern sind keine Kartennummer,
#: aber sie sind auch keine Information, die ein Modell braucht — deshalb
#: stehen sie NUR auf dem Telefon und im Freigabetext, nie im Modellkatalog.
_HINT = re.compile(r"^[••\s·*x–-]{0,20}[0-9]{0,4}$")


class InstrumentKind(str, Enum):
    """Wo das Material wirklich liegt. Nie in SOLVIO — die Frage ist nur, wo sonst."""

    #: Eine eigene, separat widerrufbare virtuelle Karte beim Anbieter. Die
    #: empfohlene Produktivhaltung: eigenes Limit, eigene Benachrichtigungen,
    #: totlegbar ohne die Hauptkarte des Nutzers anzufassen.
    VIRTUAL_CARD = "virtual_card"
    #: Ein beim Anbieter hinterlegtes, tokenisiertes Zahlungsmittel. SOLVIO
    #: kennt die Kennung, nie das Material.
    PROVIDER_TOKEN = "provider_token"
    #: Beim Haendler gespeichert (der Nutzer hat es dort selbst hinterlegt).
    #: SOLVIO waehlt es an der Kasse aus und sieht es nie.
    MERCHANT_SAVED = "merchant_saved"
    #: Ein Wallet des Betriebssystems oder des Browsers. Die Bestaetigung
    #: passiert ausserhalb von SOLVIO — das ist ein Merkmal, kein Mangel.
    WALLET = "wallet"


class InstrumentStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"
    #: Nach einer Wiederherstellung: der Zustand beim Anbieter ist unbekannt.
    #: Es wird nicht bezahlt, bis er nachgesehen wurde — eine Sicherung darf
    #: keine Befugnis zurueckbringen, die inzwischen widerrufen ist.
    REVALIDATION_REQUIRED = "revalidation_required"


#: Nur dieser eine Zustand zahlt.
def can_pay(status: InstrumentStatus) -> bool:
    return status is InstrumentStatus.ACTIVE


class Denied(str, Enum):
    """Warum nicht. Genau ein Grund je Absage, alle protokollierbar."""

    UNKNOWN_INSTRUMENT = "unknown_instrument"
    NOT_ACTIVE = "not_active"
    CURRENCY_NOT_ALLOWED = "currency_not_allowed"
    MERCHANT_NOT_ALLOWED = "merchant_not_allowed"
    SINGLE_LIMIT_EXCEEDED = "single_limit_exceeded"
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    POLICY_TAMPERED = "policy_tampered"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: Denied | None = None
    detail: str = ""


ALLOW = Verdict(True)


@dataclass(frozen=True)
class Instrument:
    """Ein Zahlungsmittel, wie SOLVIO es kennt: Verweis, Grenzen, Anzeige."""

    payment_ref: str
    kind: InstrumentKind
    #: Welcher Anbieter das Mittel haelt (`sandbox`, spaeter ein echter Name).
    provider: str
    status: InstrumentStatus = InstrumentStatus.ACTIVE

    # -- GRENZEN. Gehen in den Digest, weil sie Befugnis beschreiben. ---------
    #: Hoechstbetrag EINER Zahlung, in Minor Units. Pflicht: ein Zahlungsmittel
    #: ohne Obergrenze ist eine offene Vollmacht.
    max_single_minor: int = 0
    #: Hoechstsumme je Kalendertag (UTC). 0 heisst: keine zusaetzliche
    #: Tagesgrenze — die Einzelgrenze gilt weiter.
    daily_total_minor: int = 0
    allowed_currencies: tuple[str, ...] = ()
    allowed_merchant_ids: tuple[str, ...] = ()
    #: Der Tresor-Verweis auf den Zugang, mit dem BELASTET wird. Er traegt
    #: `requires_user_presence=True` und ist damit ein zweiter, vom Router
    #: unabhaengiger Face-ID-Zaun: ein Kauf, der irgendwie am Freigabeweg
    #: vorbeikaeme, bekommt vom Tresor trotzdem nichts.
    provider_secret_ref: str = ""
    #: Der Tresor-Verweis auf den LESENDEN Zugang — Betrag holen, nachschlagen,
    #: Gesundheit. Ein eigener Zugang, weil `requires_user_presence` sonst jedes
    #: Nachschlagen biometrisch machen wuerde; beim Anbieter ist es ein
    #: eingeschraenkter Schluessel, der beim Anbieter gar nicht belasten darf.
    #: Ob er das WIRKLICH nicht kann, entscheidet der Anbieter und nicht diese
    #: Zeile — beim Pruefanbieter ist es durchgesetzt und nachgesehen, bei
    #: einem echten Anbieter ist es eine Frage an dessen Schluesselverwaltung.
    provider_readonly_ref: str = ""
    #: Der Tresor-Verweis auf den Zugang, der Geld ZURUECKGEHEN laesst —
    #: erstatten und stornieren. Ohne Anwesenheitspflicht, und das ist kein
    #: Nachlassen: dieser Zugang kann strukturell kein Geld vom Eigentuemer
    #: wegbewegen.
    #:
    #: Er existiert, weil der erste Bau ohne ihn eine stille Unmoeglichkeit
    #: erzeugte: Erstatten und Stornieren sind `CRITICAL` und laufen vom iPhone
    #: DIREKT — also ohne bewiesene Anwesenheit —, griffen aber auf den
    #: Belastungszugang zu, der genau die verlangt. Beide Handlungen waren damit
    #: zu hundert Prozent tot, und ausgerechnet sie sind die, mit denen ein
    #: Mensch einen Fehlkauf begrenzt. Gefunden in der kalten Abnahme.
    provider_refund_ref: str = ""
    #: Wie der Anbieter dieses Mittel NENNT (`pm_...`). Eine Kennung, kein
    #: Geheimnis: sie oeffnet nichts. Ohne den Anbieterzugang aus dem Tresor
    #: bewegt sie keinen Cent — ein Test haelt das fest, statt es zu behaupten.
    #: Sie steht trotzdem in keiner Modellsicht: was ein Modell nicht braucht,
    #: bekommt es nicht.
    provider_token: str = ""
    version: int = 1

    # -- ANZEIGE. Fuer Menschen, nie fuer eine Entscheidung. ------------------
    display_name: str = ""
    display_hint: str = ""

    # -- BUCHHALTUNG. Aendert sich laufend, deshalb NICHT im Digest. ----------
    created_at: str = ""
    last_used_at: str = ""
    disabled_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payment_ref", str(PR.parse(self.payment_ref)))
        if not isinstance(self.kind, InstrumentKind):
            raise ValueError("instrument kind is unknown")
        if not isinstance(self.status, InstrumentStatus):
            raise ValueError("instrument status is unknown")
        if not isinstance(self.provider, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9._-]{0,62}", self.provider or ""):
            raise ValueError("provider is not a valid name")
        if isinstance(self.max_single_minor, bool) \
                or not isinstance(self.max_single_minor, int) \
                or self.max_single_minor <= 0:
            raise ValueError("an instrument needs a positive single-payment limit")
        if isinstance(self.daily_total_minor, bool) \
                or not isinstance(self.daily_total_minor, int) \
                or self.daily_total_minor < 0:
            raise ValueError("daily limit must not be negative")
        object.__setattr__(self, "allowed_currencies",
                           tuple(sorted({normalize_currency(c)
                                         for c in self.allowed_currencies})))
        object.__setattr__(self, "allowed_merchant_ids",
                           tuple(sorted({str(m).strip().lower()
                                         for m in self.allowed_merchant_ids
                                         if str(m).strip()})))
        if (self.provider_secret_ref or self.provider_readonly_ref
                or self.provider_refund_ref):
            from solvio.secret_vault import refs as SR
            if self.provider_secret_ref:
                object.__setattr__(self, "provider_secret_ref",
                                   str(SR.parse(self.provider_secret_ref)))
            if self.provider_readonly_ref:
                object.__setattr__(self, "provider_readonly_ref",
                                   str(SR.parse(self.provider_readonly_ref)))
            if self.provider_refund_ref:
                object.__setattr__(self, "provider_refund_ref",
                                   str(SR.parse(self.provider_refund_ref)))
            if self.provider_secret_ref == self.provider_readonly_ref \
                    and self.provider_secret_ref:
                raise ValueError("charge and read-only credential must not be the same")
        if self.provider_token and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.provider_token):
            raise ValueError("provider token is not a plausible identifier")
        if self.provider_token and _looks_like_card(self.provider_token):
            # Ein „Token", das eine Kartennummer ist, ist keines. Der Zaun steht
            # hier und nicht nur in der Ablage, damit ein Objekt gar nicht erst
            # entsteht.
            raise ValueError("provider token must not be payment material")
        if self.display_hint and not _HINT.match(self.display_hint):
            # Ein „Hinweis", der laenger ist als vier Ziffern, ist kein Hinweis.
            raise ValueError("display hint does not look like a masked hint")
        if len(self.display_name) > 80:
            raise ValueError("display name is too long")

    # -- Befugnis ------------------------------------------------------------
    def authority_fields(self) -> dict[str, Any]:
        """Alles, was BEFUGNIS beschreibt — und nichts, was sich laufend aendert."""
        return {
            "payment_ref": self.payment_ref,
            "kind": self.kind.value,
            "provider": self.provider,
            "status": self.status.value,
            "version": int(self.version),
            "max_single_minor": int(self.max_single_minor),
            "daily_total_minor": int(self.daily_total_minor),
            "allowed_currencies": list(self.allowed_currencies),
            "allowed_merchant_ids": list(self.allowed_merchant_ids),
            "provider_secret_ref": self.provider_secret_ref,
            "provider_readonly_ref": self.provider_readonly_ref,
            "provider_refund_ref": self.provider_refund_ref,
            "provider_token": self.provider_token,
        }

    def digest(self) -> str:
        payload = json.dumps(self.authority_fields(), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(_POLICY_DOMAIN + b"\x00"
                              + payload.encode("utf-8")).hexdigest()

    # -- Anzeige -------------------------------------------------------------
    def describe(self, *, for_model: bool) -> dict[str, Any]:
        """Die sichere Beschreibung. `for_model` laesst den Anzeigehinweis weg.

        Der Unterschied ist klein und beabsichtigt: der Mensch soll erkennen,
        WELCHE Karte gemeint ist; das Modell muss das nicht wissen, um einen
        Kauf vorzuschlagen.
        """
        out: dict[str, Any] = {
            "verweis": self.payment_ref,
            "name": self.display_name or self.payment_ref,
            "art": self.kind.value,
            "anbieter": self.provider,
            "status": self.status.value,
            "verfuegbar": can_pay(self.status),
            "waehrungen": list(self.allowed_currencies),
            "haendler": list(self.allowed_merchant_ids),
            "grenze_einzeln_minor": self.max_single_minor,
            "grenze_taeglich_minor": self.daily_total_minor,
            "zuletzt_benutzt": self.last_used_at,
        }
        if not for_model:
            out["hinweis"] = self.display_hint
            out["gesperrt_weil"] = self.disabled_reason
        return out

    def __repr__(self) -> str:
        return (f"Instrument({self.payment_ref} {self.kind.value} "
                f"{self.status.value} v{self.version})")


def evaluate(instrument: Instrument, *, currency: str, merchant_id: str,
             amount_minor: int, day_total_minor: int = 0) -> Verdict:
    """Darf DIESE Zahlung ueber DIESES Mittel laufen? Vier Schranken, fail-closed.

    Die Reihenfolge ist die der Aussagekraft: erst der Zustand des Mittels, dann
    die Waehrung, dann der Haendler, dann die Betraege. Jede fuer sich reicht
    zum Nein.

    `day_total_minor` ist die bereits an diesem Tag ueber dieses Mittel
    ERFOLGREICH bewegte Summe — sie kommt aus dem Zahlungsbuch, nicht aus einem
    Zaehler im Arbeitsspeicher, damit ein Neustart keine Grenze zurueckdreht.
    """
    if not can_pay(instrument.status):
        return Verdict(False, Denied.NOT_ACTIVE, instrument.status.value)
    try:
        code = normalize_currency(currency)
    except Exception:  # noqa: BLE001 - eine unlesbare Waehrung ist ein Nein
        return Verdict(False, Denied.CURRENCY_NOT_ALLOWED, "unparsable")
    if code not in set(instrument.allowed_currencies):
        return Verdict(False, Denied.CURRENCY_NOT_ALLOWED, code)
    wanted = str(merchant_id or "").strip().lower()
    if not wanted or wanted not in set(instrument.allowed_merchant_ids):
        return Verdict(False, Denied.MERCHANT_NOT_ALLOWED, wanted[:64] or "unnamed")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) \
            or amount_minor <= 0:
        return Verdict(False, Denied.SINGLE_LIMIT_EXCEEDED, "implausible")
    if amount_minor > instrument.max_single_minor:
        return Verdict(False, Denied.SINGLE_LIMIT_EXCEEDED, str(amount_minor))
    if instrument.daily_total_minor:
        if max(0, int(day_total_minor)) + amount_minor > instrument.daily_total_minor:
            return Verdict(False, Denied.DAILY_LIMIT_EXCEEDED, str(amount_minor))
    return ALLOW


def widens(previous: Instrument, following: Instrument) -> bool:
    """Erweitert die neue Fassung die Befugnis der alten?

    Genau eine Verwendung: eine Erweiterung bleibt biometrisch, auch wenn die
    Handlung sonst billiger waere. Wortgleich zur Tresor-Politik.
    """
    def _wider(new: set, old: set) -> bool:
        return bool(new - old)

    if following.max_single_minor > previous.max_single_minor:
        return True
    # 0 heisst „keine Tagesgrenze" — von einer Grenze zu keiner ist die
    # groesste Erweiterung von allen und darf nicht als 0 < n durchrutschen.
    if previous.daily_total_minor and not following.daily_total_minor:
        return True
    if following.daily_total_minor > previous.daily_total_minor:
        return True
    if _wider(set(following.allowed_currencies), set(previous.allowed_currencies)):
        return True
    if _wider(set(following.allowed_merchant_ids), set(previous.allowed_merchant_ids)):
        return True
    if not can_pay(previous.status) and can_pay(following.status):
        return True
    return False
