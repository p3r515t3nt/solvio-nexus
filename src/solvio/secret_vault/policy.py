"""Wofuer ein Geheimnis benutzt werden darf — deterministisch, Vorgabe VERWEIGERN.

Der Tresor genehmigt nichts. Ob eine HANDLUNG erlaubt ist, entscheidet weiterhin
Approval Policy V2 an Herkunft und Aktionsklasse (ADR-0022). Dieses Modul
beantwortet die andere, engere Frage: **darf ausgerechnet DIESES Geheimnis in
DIESEN Vorgang?**

Vier Bindungen, und alle vier muessen stimmen:

* **Faehigkeit.** Ein Zugang fuer `browser_login` wird nicht zu einem Zugang fuer
  `gmail_send_draft`, nur weil beide Zugangsdaten brauchen.
* **Executor.** Nur Core-eigener Code, der den Wert wirklich braucht. Ein
  generischer Aufrufer bekommt nichts — das ist der Unterschied zwischen einem
  Tresor und einer Variablen.
* **Ziel.** Ein Passwort fuer `https://www.amazon.de` gehoert nicht nach
  `https://angreifer.example`, und zwar auch dann nicht, wenn eine Seite darum
  bittet. Verglichen wird die aufgeloeste Herkunft, exakt, ohne Platzhalter.
* **Zustand.** Ein deaktivierter oder widerrufener Zugang beantwortet gar nichts
  mehr — sofort, nicht beim naechsten Neustart.

**Der Name bindet nichts.** `display_name` und `account_label` sind fuer Menschen
da. Wer Zugriff an einem lesbaren Namen festmacht, hat eine Zugriffskontrolle
gebaut, die man umbenennen kann.

**Vorgabe ist VERWEIGERN.** Eine leere Liste erlaubt nichts. Ein unbekanntes
Feld erlaubt nichts. Ein vergessener Eintrag erlaubt nichts. Das ist dieselbe
Haltung wie bei `ActionClass.UNCLASSIFIED`: vergessen kostet Reibung, nie
Sicherheit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping
from urllib.parse import urlsplit

from solvio.capabilities import policy as AP
from solvio.secret_vault.envelope import policy_digest


class SecretKind(str, Enum):
    """Was fuer ein Geheimnis es ist. Beeinflusst Anzeige und Verwendung, nie Befugnis."""

    PASSWORD = "password"
    API_TOKEN = "api_token"
    API_KEY = "api_key"
    OAUTH_CLIENT_SECRET = "oauth_client_secret"
    OAUTH_REFRESH_TOKEN = "oauth_refresh_token"
    SERVICE_CREDENTIAL = "service_credential"
    MACHINE_CREDENTIAL = "machine_credential"


#: Was ausdruecklich NICHT hierher gehoert. Zahlungsmittel sind ein eigener
#: Milestone (Payment Capability V1) mit eigener Sorgfaltspflicht; sie hier
#: „auch noch" abzulegen waere der bequemste Weg, beide Themen halb zu machen.
FORBIDDEN_KINDS = frozenset({
    "card_pan", "credit_card", "cvv", "cvc", "iban", "bank_account",
    "payment_instrument", "sepa_mandate",
})


class ExecutorId(str, Enum):
    """Die vertrauenswuerdigen Executoren. Abschliessend, im Code, nicht konfigurierbar."""

    #: Core-eigener HTTP-Client fuer Anbieter-APIs. Setzt Kopfzeilen selbst.
    HTTP = "http"
    #: Der Anbieter-Client des Sprachmodells.
    PROVIDER = "provider"
    #: Der Home-Assistant-Client des Cores.
    HOME_ASSISTANT = "home_assistant"
    #: Der Browser-Executor. Fuellt in genau eine Herkunft.
    BROWSER = "browser"
    #: Der Satelliten-/Maschinenzugang des Cores.
    MACHINE = "machine"
    #: Der Zahlungs-Executor. Der EINZIGE, der Anbieterbefugnis fuer eine
    #: Geldbewegung ausleihen darf — und er wohnt in genau einem Paket.
    PAYMENT = "payment"
    #: Der Offsite-Transport (Offsite Encrypted Backup V1). Der EINZIGE, der
    #: den S3-Zugang ausleihen darf — und er wohnt in genau einem Modul, dem
    #: SigV4-Client. Verschluesselt wird OHNE Geheimnis (nur Recipient);
    #: dieses hier oeffnet ausschliesslich den Transportweg.
    OFFSITE = "offsite"
    #: Die Anthropic-Flaeche des Provider Brokers (Development Autopilot V0.6).
    #: Der EINZIGE, der die Anthropic-Anmeldung ausleihen darf — Abo-OAuth oder
    #: API-Schluessel. Er wohnt in genau EINEM Modul, und dort wird der Wert
    #: erst am Ausgang zum Anbieter eingesetzt. Der schreibende Claude-Builder
    #: traegt ihn nie: er sieht ausschliesslich ein leasegebundenes
    #: Broker-Token.
    ANTHROPIC_BROKER = "anthropic_broker"
    #: Der Telefonie-Ausgang (Telephony Capability V1). Der EINZIGE, der den
    #: Zugang zum Sprachanbieter ausleihen darf. Er wohnt in genau EINEM Modul,
    #: und dort wird der Wert erst am Ausgang in den Kopfsatz gesetzt. Weder
    #: die Faehigkeit, die den Anruf anstoesst, noch der Ledger, der ihn
    #: nachhaelt, tragen ihn je.
    TELEPHONY = "telephony"


#: Welcher Executor aus welchem Modul kommen darf. Zweite Schranke neben der
#: Policy: selbst ein Aufrufer, der die richtige Kennung BEHAUPTET, kommt nicht
#: durch, wenn sein Modul nicht dazu passt. Praefixvergleich auf `__name__`.
EXECUTOR_MODULES: dict[ExecutorId, tuple[str, ...]] = {
    ExecutorId.HTTP: ("solvio.integrations.", "solvio.capabilities."),
    ExecutorId.PROVIDER: ("solvio.realtime.", "solvio.conversation.",
                          "solvio.research.", "solvio.deep."),
    ExecutorId.HOME_ASSISTANT: ("solvio.integrations.home_assistant",
                                "solvio.capabilities.home_assistant",
                                "solvio.storage.ha_backup"),
    ExecutorId.BROWSER: ("solvio.browser.", "solvio.portal."),
    ExecutorId.MACHINE: ("solvio.realtime.",),
    # Bewusst EIN Praefix, und ein enges. `solvio.capabilities.` steht hier
    # ausdruecklich NICHT: eine Faehigkeit darf eine Zahlung anstossen, aber die
    # Anbieterbefugnis nimmt der Executor auf, nicht sie.
    ExecutorId.PAYMENT: ("solvio.payment.executor",),
    # Dasselbe Muster fuer den Offsite-Transport: genau EIN Modul. Weder
    # `pack` (verschluesselt ohne Geheimnis) noch `job` (orchestriert nur)
    # stehen hier — der Wert gehoert dahin, wo signiert wird, und nirgendwo
    # sonst hin.
    ExecutorId.OFFSITE: ("solvio.storage.offsite.s3",),
    # Wieder genau EIN Modul. `service` (leitet weiter), `proxy` (prueft
    # Modell und Pfad) und `session` (haelt die Kappen) stehen hier
    # ausdruecklich NICHT — der Wert gehoert dahin, wo der ausgehende Kopfsatz
    # gebaut wird, und nirgendwo sonst hin.
    ExecutorId.ANTHROPIC_BROKER: ("solvio.provider_broker.anthropic",),
    # Und noch einmal dasselbe Muster, aus demselben Grund. `capabilities.
    # telephony` (entscheidet und bindet), `telephony.ledger` (haelt den
    # Ausgang fest) und `telephony.elevenlabs` (kennt nur Pfade) stehen hier
    # ausdruecklich NICHT — der Wert gehoert dahin, wo der ausgehende Kopfsatz
    # gebaut wird, und nirgendwo sonst hin.
    ExecutorId.TELEPHONY: ("solvio.telephony.upstream",),
}


class Status(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"


class Denied(str, Enum):
    """Warum nicht. Genau ein Grund je Absage, und alle sind protokollierbar."""

    UNKNOWN_SECRET = "unknown_secret"
    NOT_ACTIVE = "not_active"
    CAPABILITY_NOT_ALLOWED = "capability_not_allowed"
    EXECUTOR_NOT_ALLOWED = "executor_not_allowed"
    EXECUTOR_MODULE_MISMATCH = "executor_module_mismatch"
    TARGET_NOT_ALLOWED = "target_not_allowed"
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    BACKGROUND_NOT_ALLOWED = "background_not_allowed"
    VAULT_UNAVAILABLE = "vault_unavailable"
    ENVELOPE_REJECTED = "envelope_rejected"


#: Herkunftsklassen, aus denen ein Geheimnis NIE benutzt werden darf.
#:
#: `EXTERNAL_UNTRUSTED` ist der Kernsatz des Hauses: fremder Inhalt informiert,
#: er autorisiert nicht. `UNSPECIFIED` steht daneben, weil ein Core-Pfad, der
#: seine Herkunft nicht gesetzt hat, keine Vermutung verdient — hier kostet
#: Vergessen eine Absage, nicht ein Geheimnis.
FORBIDDEN_ORIGINS = frozenset({
    AP.OriginClass.EXTERNAL_UNTRUSTED,
    AP.OriginClass.UNSPECIFIED,
})


def normalize_target(value: str) -> str:
    """Die aufgeloeste Herkunft: Schema, Host, Port. Sonst nichts.

    Gleiche Rechnung wie `portal.permit.origin_of` — bewusst dieselbe Form, damit
    ein Ziel im Tresor und eine Schreiberlaubnis im Browser nicht zwei
    Vorstellungen von „dieselbe Seite" haben. Pfad, Abfrage und Fragment fallen
    weg: sie gehoeren nicht zur Herkunft, und wer sie mitvergleicht, verweigert
    irgendwann eine legitime Unterseite oder erlaubt eine fremde.
    """
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        # Ein blosser Hostname ist kein Ziel: `amazon.de` sagt nicht, ob
        # verschluesselt. Wir ergaenzen NICHT stillschweigend `https` — wer ein
        # Ziel eintraegt, soll es vollstaendig eintragen.
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


@dataclass(frozen=True)
class SecretPolicy:
    """Die Befugnis EINES Geheimnisses. Kern-eigen, deterministisch, Vorgabe DENY."""

    secret_ref: str
    kind: SecretKind
    version: int
    status: Status
    allowed_capabilities: tuple[str, ...] = ()
    allowed_targets: tuple[str, ...] = ()
    allowed_executors: tuple[ExecutorId, ...] = ()
    #: Darf ein Zeitplan oder ein proaktiver Lauf dieses Geheimnis benutzen?
    #: Vorgabe nein: eine Hintergrundaufgabe erbt keine anwesende Person.
    allow_background: bool = False
    #: Verlangt dieses Geheimnis zusaetzlich eine frische Nutzerentscheidung,
    #: auch wenn die Faehigkeit selbst sie nicht braeuchte?
    requires_user_presence: bool = False

    # -- Anzeige. Fuer Menschen, nie fuer eine Entscheidung. ------------------
    display_name: str = ""
    service_label: str = ""
    account_label: str = ""

    # -- Buchhaltung. Aendert sich laufend und geht deshalb NICHT in den Digest.
    created_at: str = ""
    rotated_at: str = ""
    last_used_at: str = ""
    note: str = ""

    def authority_fields(self) -> dict[str, Any]:
        """Genau die Felder, die Befugnis bedeuten — die Grundlage des Digests.

        Was hier fehlt, kann geaendert werden, ohne den Umschlag zu brechen.
        Deshalb steht hier NICHTS, was jemand aendern koennte, um mehr zu duerfen,
        und ALLES, was er dafuer aendern muesste.
        """
        return {
            "secret_ref": self.secret_ref,
            "kind": self.kind.value,
            "version": int(self.version),
            "status": self.status.value,
            "allowed_capabilities": sorted(set(self.allowed_capabilities)),
            "allowed_targets": sorted({normalize_target(t) for t in self.allowed_targets}),
            "allowed_executors": sorted({e.value for e in self.allowed_executors}),
            "allow_background": bool(self.allow_background),
            "requires_user_presence": bool(self.requires_user_presence),
        }

    def digest(self) -> str:
        return policy_digest(self.authority_fields())

    def __repr__(self) -> str:
        return (f"SecretPolicy({self.secret_ref} v{self.version} "
                f"{self.status.value} caps={len(self.allowed_capabilities)} "
                f"targets={len(self.allowed_targets)})")


@dataclass(frozen=True)
class UseRequest:
    """Wer will was womit wohin. Alles daran ist Core-Wahrheit, nichts Modelltext."""

    capability: str
    executor: ExecutorId
    target: str
    origin: AP.OriginClass
    #: Der Modulname des tatsaechlichen Aufrufers. Setzt der Broker, nicht der Aufrufer.
    caller_module: str = ""
    #: Freigabe-/Ausfuehrungskennung, wenn es eine gibt. Nur fuer die Spur.
    approval_id: str = ""
    execution_id: str = ""
    #: Hat ein Mensch fuer genau diesen Vorgang frisch entschieden?
    user_present: bool = False
    automation_id: str = ""

    def __repr__(self) -> str:
        return (f"UseRequest({self.capability} via {self.executor.value} "
                f"-> {self.target} from {self.origin.value})")


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: Denied | None = None
    #: Nur fuers Protokoll, nie fuer eine Entscheidung.
    detail: str = ""


ALLOW = Verdict(True)


def evaluate(policy: SecretPolicy | None, request: UseRequest) -> Verdict:
    """Darf dieses Geheimnis in diesen Vorgang? Eine Kette von Nein-Gruenden.

    Die Reihenfolge ist nicht beliebig: geprueft wird von der billigsten und
    aussagekraeftigsten Absage zur teuersten. Wer nicht existiert, braucht keine
    Zielpruefung.
    """
    if policy is None:
        return Verdict(False, Denied.UNKNOWN_SECRET)
    if policy.status is not Status.ACTIVE:
        return Verdict(False, Denied.NOT_ACTIVE, policy.status.value)
    if request.origin in FORBIDDEN_ORIGINS:
        return Verdict(False, Denied.ORIGIN_NOT_ALLOWED, request.origin.value)
    if (request.origin is AP.OriginClass.BACKGROUND_AUTOMATION
            and not policy.allow_background):
        return Verdict(False, Denied.BACKGROUND_NOT_ALLOWED)
    if request.capability not in set(policy.allowed_capabilities):
        return Verdict(False, Denied.CAPABILITY_NOT_ALLOWED, request.capability[:64])
    if request.executor not in set(policy.allowed_executors):
        return Verdict(False, Denied.EXECUTOR_NOT_ALLOWED, request.executor.value)
    prefixes = EXECUTOR_MODULES.get(request.executor, ())
    # Ein NACKTER Praefixvergleich haette `solvio.payment.executor_umgehung`
    # durchgelassen. Erlaubt ist das Modul selbst oder etwas darunter — nicht
    # etwas, das nur so anfaengt.
    if not any(request.caller_module == p or request.caller_module.startswith(
            p if p.endswith(".") else p + ".") for p in prefixes):
        return Verdict(False, Denied.EXECUTOR_MODULE_MISMATCH,
                       request.caller_module[:64])
    wanted = normalize_target(request.target)
    allowed = {normalize_target(t) for t in policy.allowed_targets} - {""}
    if not wanted or wanted not in allowed:
        return Verdict(False, Denied.TARGET_NOT_ALLOWED, wanted[:64] or "unparsable")
    if policy.requires_user_presence and not request.user_present:
        return Verdict(False, Denied.ORIGIN_NOT_ALLOWED, "user_presence_required")
    return ALLOW


def widens(previous: SecretPolicy, following: SecretPolicy) -> bool:
    """Erweitert die neue Policy die Befugnis der alten?

    Gebraucht an genau einer Stelle: eine Erweiterung ist eine staerkere
    Handlung als eine Einschraenkung und muss auch dann biometrisch bleiben,
    wenn das Produkt sonst Reibung sparen wuerde. Verglichen werden Mengen, nicht
    Texte — eine Umsortierung ist keine Erweiterung.
    """
    def _wider(new: set[Any], old: set[Any]) -> bool:
        return bool(new - old)

    if _wider(set(following.allowed_capabilities), set(previous.allowed_capabilities)):
        return True
    if _wider({normalize_target(t) for t in following.allowed_targets},
              {normalize_target(t) for t in previous.allowed_targets}):
        return True
    if _wider(set(following.allowed_executors), set(previous.allowed_executors)):
        return True
    if following.allow_background and not previous.allow_background:
        return True
    if previous.requires_user_presence and not following.requires_user_presence:
        return True
    if previous.status is not Status.ACTIVE and following.status is Status.ACTIVE:
        return True
    return False


def from_row(row: Mapping[str, Any]) -> SecretPolicy:
    """Baut eine Policy aus einer Datenbankzeile. Unbekanntes wird nicht geraten."""
    def _tuple(value: Any) -> tuple[str, ...]:
        if not value:
            return ()
        if isinstance(value, str):
            return tuple(p for p in value.split("\n") if p)
        return tuple(str(p) for p in value if p)

    return SecretPolicy(
        secret_ref=str(row["secret_ref"]),
        kind=SecretKind(str(row["kind"])),
        version=int(row["version"]),
        status=Status(str(row["status"])),
        allowed_capabilities=_tuple(row.get("allowed_capabilities")),
        allowed_targets=_tuple(row.get("allowed_targets")),
        allowed_executors=tuple(ExecutorId(e)
                                for e in _tuple(row.get("allowed_executors"))),
        allow_background=bool(row.get("allow_background") or 0),
        requires_user_presence=bool(row.get("requires_user_presence") or 0),
        display_name=str(row.get("display_name") or ""),
        service_label=str(row.get("service_label") or ""),
        account_label=str(row.get("account_label") or ""),
        created_at=str(row.get("created_at") or ""),
        rotated_at=str(row.get("rotated_at") or ""),
        last_used_at=str(row.get("last_used_at") or ""),
        note=str(row.get("note") or ""),
    )
