"""Tresorverwaltung als Faehigkeiten — jede einzelne ueber den Freigabeweg.

Was hier registriert wird, sind die VERWALTUNGS-Handlungen: anlegen, ersetzen,
deaktivieren, wieder aktivieren, loeschen, Berechtigung aendern. Sie laufen
durch denselben `CapabilityRouter` wie alles andere und bekommen damit
Approval Policy V2 geschenkt (ADR-0022).

**Was hier ausdruecklich NICHT registriert ist: das Benutzen eines Geheimnisses.**
Der Wert verlaesst den Tresor nur ueber `SecretBroker.use()`, direkt in einen
vertrauenswuerdigen Executor, innerhalb eines Vorgangs, der ohnehin schon
autorisiert ist. Es gibt keine Faehigkeit „gib mir den Wert", weil es keine
geben darf.

**Der Wert reist nie als Argument.** Der Freigabeweg schreibt die Argumente
einer Faehigkeit als Text in `approval_control.sqlite3`, hasht sie in den
Autorisierungs-Digest und zeigt sie auf dem iPhone. Eine Tresor-Faehigkeit, die
ihren Wert als Argument mitgaebe, legte ihn im Klartext in genau die Datenbank,
die es am wenigsten verdient. Stattdessen steht in den Argumenten eine
Einlagerungskennung (`solvio.secret_vault.staging`) — ein Wegwerfwort.

**Die Klassen sind Geburtsrecht, keine Selbsteinschaetzung.** Anlegen,
Ersetzen, Loeschen, Wieder-Aktivieren und Umwidmen stehen in
`VERY_CRITICAL_BY_BIRTH`: aus jeder Herkunft biometrisch, aus einem Zeitplan
und aus fremdem Inhalt verweigert. Nur `secret_disable` ist `CRITICAL` — eine
Sperre ist die sichere Richtung, und einen verlorenen Zugang schnell totlegen zu
koennen ist ein Sicherheitsgewinn, kein Risiko.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.contract import (CapabilityDeclined, CapabilityRefused,
                                          CapabilitySpec, ExecutionClass)
from solvio.logging_setup import get_logger
from solvio.nodes.models import DataClass
from solvio.secret_vault import admin, policy as VP, refs
from solvio.secret_vault.staging import SecretStaging, StagingError
from solvio.secret_vault.store import VaultStore
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE, READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("capabilities")

_REF = {"type": "string"}
_TEXT = {"type": "string"}

#: Listen reisen als kommagetrennte Zeichenketten und nicht als Feld.
#: Zwei Gruende, und beide sind gemessen: der Autorisierungs-Digest laeuft ueber
#: eine kanonische JSON-Darstellung (verschachtelte Felder sind dort eine
#: zusaetzliche Fehlerquelle), und die Bindung auf dem iPhone kennt nur
#: `[String: String]`. Flach auf beiden Seiten ist eine Form weniger, die
#: auseinanderlaufen kann.
_SCOPE_PROPERTIES = {
    "secret_ref": _REF,
    "capabilities": _TEXT,
    "targets": _TEXT,
    "executors": _TEXT,
    "allow_background": _TEXT,
}

SPECS: dict[str, CapabilitySpec] = {
    "google_connect": CapabilitySpec(
        name="google_connect", version=1, execution_class=ExecutionClass.CONTROLLED, timeout=45,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "additionalProperties": False,
                      "properties": {k: _TEXT for k in ("expected_binding", "account_id", "account_email", "staging_id")},
                      "required": ["expected_binding", "account_id", "account_email", "staging_id"]},
        description="Google für Gmail und Kalender verbinden oder neu anmelden"),
    "secret_list": CapabilitySpec(
        name="secret_list", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"capability": _TEXT}},
        description="Nennt die hinterlegten Zugaenge — Verweis und Zweck, nie einen Wert."),
    "secret_add": CapabilitySpec(
        name="secret_add", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {**_SCOPE_PROPERTIES, "kind": _TEXT,
                                     "display_name": _TEXT, "service_label": _TEXT,
                                     "account_label": _TEXT, "staging_id": _TEXT},
                      "required": ["secret_ref", "kind", "capabilities",
                                   "targets", "executors", "staging_id"]},
        description="Einen neuen Zugang im Tresor hinterlegen"),
    "secret_replace": CapabilitySpec(
        name="secret_replace", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {"secret_ref": _REF, "staging_id": _TEXT},
                      "required": ["secret_ref", "staging_id"]},
        description="Den Wert eines Zugangs ersetzen; die Berechtigung bleibt"),
    "secret_disable": CapabilitySpec(
        name="secret_disable", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {"secret_ref": _REF},
                      "required": ["secret_ref"]},
        description="Einen Zugang sperren. Wirkt sofort"),
    "secret_enable": CapabilitySpec(
        name="secret_enable", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {"secret_ref": _REF},
                      "required": ["secret_ref"]},
        description="Einen gesperrten Zugang wieder freigeben"),
    "secret_delete": CapabilitySpec(
        name="secret_delete", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object", "properties": {"secret_ref": _REF},
                      "required": ["secret_ref"]},
        description="Einen Zugang samt Geheimtext entfernen. Nicht umkehrbar"),
    # `expected_version` ist PFLICHT und steht bewusst in den Argumenten, nicht
    # nur in der Beschreibung: der Autorisierungs-Digest laeuft ueber die
    # ARGUMENTE (`router.binding_digest`), nicht ueber den Anzeigetext. Ein
    # Riegel, der nur im Anzeigetext staende, waere keiner — eine Freigabe von
    # vorhin passte nach einer zwischenzeitlichen Aenderung weiterhin.
    "secret_rescope": CapabilitySpec(
        name="secret_rescope", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        data_class=DataClass.HOME_ONLY,
        input_schema={"type": "object",
                      "properties": {**_SCOPE_PROPERTIES,
                                     "expected_version": _TEXT},
                      "required": ["secret_ref", "expected_version"]},
        description="Aendert, wofuer ein Zugang benutzt werden darf"),
}

#: Die Faehigkeiten, die eine Einlagerung verbrauchen. Der Rest kommt ohne Wert aus.
NEEDS_STAGING = frozenset({"secret_add", "secret_replace", "google_connect"})


def _list(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in str(text or "").split(",") if p.strip())


def _flag(text: str) -> bool:
    return str(text or "").strip().lower() in ("true", "ja", "1", "yes")


def _executors(text: str) -> tuple[VP.ExecutorId, ...]:
    out = []
    for name in _list(text):
        try:
            out.append(VP.ExecutorId(name))
        except ValueError as exc:
            raise CapabilityDeclined(
                "unknown_executor",
                human_message=f"Den Executor {name!r} kenne ich nicht.") from exc
    return tuple(out)


def _human(ref: str, store: VaultStore) -> str:
    described = store.row(ref)
    if described is None:
        return ref
    return str(described.get("display_name") or "") or ref


class SecretVaultCapabilities:
    """Die Handlungen. Halten selbst kein Geheimnis und geben keines zurueck."""

    def __init__(self, *, store: VaultStore | None = None,
                 staging: SecretStaging | None = None, google_connection=None) -> None:
        self.store = store or VaultStore()
        self.staging = staging or SecretStaging()
        from solvio.secret_vault.google_mobile import GoogleMobileConnection
        self.google_connection = google_connection or GoogleMobileConnection(self.store)
        #: Wer die Einlagerung abholen darf. Setzt der Aufnahmeweg je Vorgang.
        self.device_for_staging: dict[str, str] = {}

    # -- Lesen ---------------------------------------------------------------
    def list_secrets(self, args: dict[str, Any]) -> dict[str, Any]:
        from solvio.secret_vault.broker import SecretBroker
        broker = SecretBroker(self.store)
        entries = broker.catalogue(capability=str(args.get("capability") or ""))
        # Was das Modell sieht: Verweis, Zweck, Zustand. Kein Wert, keine Laenge,
        # keine Pruefsumme, kein Umschlag.
        return {"zugaenge": [{
            "verweis": e["secret_ref"],
            "name": e["display_name"] or e["secret_ref"],
            "art": e["kind"],
            "zustand": e["status"],
            "faehigkeiten": e["allowed_capabilities"],
            "ziele": e["allowed_targets"],
            "zuletzt_benutzt": e["last_used_at"],
        } for e in entries]}

    # -- Schreiben -----------------------------------------------------------
    def _take_staged(self, args: dict[str, Any]) -> bytes:
        staging_id = str(args.get("staging_id") or "")
        device = self.device_for_staging.get(staging_id, "")
        if not staging_id or not device:
            raise CapabilityRefused(
                "staging_missing",
                human_message="Der Wert ist nicht mehr da — bitte neu eingeben.")
        try:
            return self.staging.take(staging_id, device_id=device)
        except StagingError as exc:
            raise CapabilityRefused(
                "staging_missing", human_message=(
                    "Der Wert ist abgelaufen oder schon verbraucht — "
                    "bitte neu eingeben.")) from exc
        finally:
            self.device_for_staging.pop(staging_id, None)

    async def google_connect(self, args: dict[str, Any]) -> dict[str, Any]:
        plaintext = self._take_staged(args)
        try:
            return await self.google_connection.activate(args, plaintext)
        except Exception:
            # Provider/SDK/database errors may carry credentials; closed text only.
            raise CapabilityRefused("google_connection_unconfirmed", human_message=(
                "Der Google-Zugang konnte nicht bestätigt werden. Bitte prüfe den Verbindungsstand, "
                "bevor du dich erneut anmeldest.")) from None
        finally:
            del plaintext

    def describe_google_connect(self, args: dict[str, Any]) -> dict[str, Any]:
        from solvio.secret_vault.google_mobile import GoogleConnectionError
        try:
            preview = self.google_connection.preview()
            if args.get("expected_binding") != preview["expected_binding"]:
                raise GoogleConnectionError("google_connection_changed")
        except Exception:
            raise CapabilityRefused("google_connection_changed", human_message=(
                "Der Google-Zugang hat sich geändert oder ist nicht bereit. Bitte öffne Verbindungen erneut.")) from None
        return {"konto": str(args.get("account_email") or ""),
                "aktion": "Google-Zugang für Gmail und Kalender ersetzen und aktivieren",
                "rechte": ", ".join(preview["capabilities"]),
                "versand": "Mailversand benötigt weiterhin deine auftragsbezogene Freigabe.",
                "vorgang": str(args.get("staging_id") or "")[:16]}

    def add(self, args: dict[str, Any]) -> dict[str, Any]:
        plaintext = self._take_staged(args)
        try:
            policy = admin.add(
                secret_ref=str(args["secret_ref"]), kind=str(args["kind"]),
                plaintext=plaintext,
                allowed_capabilities=_list(args.get("capabilities")),
                allowed_targets=_list(args.get("targets")),
                allowed_executors=_executors(args.get("executors")),
                display_name=str(args.get("display_name") or ""),
                service_label=str(args.get("service_label") or ""),
                account_label=str(args.get("account_label") or ""),
                allow_background=_flag(args.get("allow_background")),
                store=self.store)
        except admin.AdminError as exc:
            raise CapabilityRefused(str(exc), human_message=_message(str(exc))) from exc
        except refs.InvalidSecretRef as exc:
            raise CapabilityDeclined(
                "invalid_secret_ref",
                human_message="Der Verweis passt nicht.") from exc
        finally:
            del plaintext
        return {"verweis": policy.secret_ref, "fassung": policy.version,
                "info": f"{policy.display_name or policy.secret_ref} liegt im Tresor."}

    def replace(self, args: dict[str, Any]) -> dict[str, Any]:
        plaintext = self._take_staged(args)
        try:
            policy = admin.replace_value(secret_ref=str(args["secret_ref"]),
                                         plaintext=plaintext, store=self.store)
        except admin.AdminError as exc:
            raise CapabilityRefused(str(exc), human_message=_message(str(exc))) from exc
        finally:
            del plaintext
        return {"verweis": policy.secret_ref, "fassung": policy.version,
                "info": f"{policy.display_name or policy.secret_ref} ist ersetzt."}

    def _status(self, args: dict[str, Any], status: VP.Status,
                word: str) -> dict[str, Any]:
        try:
            policy = admin.set_status(secret_ref=str(args["secret_ref"]),
                                      status=status, store=self.store)
        except admin.AdminError as exc:
            raise CapabilityRefused(str(exc), human_message=_message(str(exc))) from exc
        return {"verweis": policy.secret_ref, "zustand": policy.status.value,
                "info": f"{policy.display_name or policy.secret_ref} ist {word}."}

    def disable(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._status(args, VP.Status.DISABLED, "gesperrt")

    def enable(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._status(args, VP.Status.ACTIVE, "wieder freigegeben")

    def delete(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args["secret_ref"])
        name = _human(ref, self.store)
        try:
            removed = admin.delete(secret_ref=ref, store=self.store)
        except admin.AdminError as exc:
            raise CapabilityRefused(str(exc), human_message=_message(str(exc))) from exc
        if not removed:
            raise CapabilityDeclined(
                "unknown_secret", human_message="Diesen Zugang gibt es nicht.")
        return {"verweis": ref, "info": f"{name} ist geloescht."}

    def rescope(self, args: dict[str, Any]) -> dict[str, Any]:
        """Aendert den Umfang — aber nur den, den der Mensch gesehen hat.

        Die Fassungspruefung ist der Riegel gegen das Rennen zwischen
        Beschreibung und Freigabe: zwischen „das steht auf dem iPhone" und
        „Face ID war eben" koennen Sekunden bis Minuten liegen. Aendert
        jemand den Umfang in dieser Zeit, gilt die Zustimmung nicht mehr fuer
        das, was jetzt da ist.
        """
        ref = str(args.get("secret_ref") or "")
        current = self.store.policy(ref)
        if current is None:
            raise CapabilityRefused("unknown_secret",
                                    human_message=_message("unknown_secret"))
        erwartet = str(args.get("expected_version") or "").strip()
        if erwartet != str(current.version):
            raise CapabilityRefused(
                "scope_changed_meanwhile",
                human_message=_message("scope_changed_meanwhile"))
        try:
            policy, widened = admin.rescope(
                secret_ref=str(args["secret_ref"]),
                allowed_capabilities=(_list(args["capabilities"])
                                      if "capabilities" in args else None),
                allowed_targets=(_list(args["targets"])
                                 if "targets" in args else None),
                allowed_executors=(_executors(args["executors"])
                                   if "executors" in args else None),
                allow_background=(_flag(args["allow_background"])
                                  if "allow_background" in args else None),
                store=self.store)
        except admin.AdminError as exc:
            raise CapabilityRefused(str(exc), human_message=_message(str(exc))) from exc
        return {"verweis": policy.secret_ref, "erweitert": widened,
                "info": ("Berechtigung erweitert." if widened
                         else "Berechtigung eingeschraenkt.")}

    # -- Was auf dem iPhone steht -------------------------------------------
    def describe_add(self, args: dict[str, Any]) -> dict[str, Any]:
        """Der Freigabetext fuers Anlegen. Enthaelt NIE den Wert.

        Die Einlagerungskennung steht mit drin, und das ist kein Versehen: der
        Autorisierungs-Digest laeuft ueber genau diesen Text, und beim
        Fortsetzen wird er erneut gebildet. Ohne die Kennung truege eine
        Freigabe fuer den einen Wert auch den naechsten, der zufaellig unter
        demselben Namen eingelagert wurde.
        """
        return {
            "zugang": str(args.get("display_name") or args.get("secret_ref") or ""),
            "verweis": str(args.get("secret_ref") or ""),
            "art": str(args.get("kind") or ""),
            "konto": str(args.get("account_label") or ""),
            "nur_fuer": str(args.get("targets") or ""),
            "nur_ueber": str(args.get("executors") or ""),
            "faehigkeiten": str(args.get("capabilities") or ""),
            "im_hintergrund": "ja" if _flag(args.get("allow_background")) else "nein",
            "vorgang": str(args.get("staging_id") or "")[:16],
        }

    def describe_replace(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("secret_ref") or "")
        return {"zugang": _human(ref, self.store), "verweis": ref,
                "vorgang": str(args.get("staging_id") or "")[:16]}

    def describe_ref(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("secret_ref") or "")
        return {"zugang": _human(ref, self.store), "verweis": ref}

    def describe_rescope(self, args: dict[str, Any]) -> dict[str, Any]:
        """Der Freigabetext fuers Umskopen. Enthaelt NIE einen Wert.

        `capabilities` ERSETZT die Liste, es ergaenzt sie nicht. Wer nur die
        neue Liste sieht, kann nicht erkennen, was dabei verschwindet — und
        genau das ist der teure Fehler: zwei Namen hinzufuegen und dabei zehn
        verlieren, ohne dass es jemand merkt.

        Deshalb steht hier die Differenz und nicht nur das Ergebnis: was
        hinzukommt, was WEGFAELLT, was bleibt. Wer „nichts entfaellt" liest,
        soll sich darauf verlassen koennen.
        """
        ref = str(args.get("secret_ref") or "")
        current = self.store.policy(ref)
        out = {"zugang": _human(ref, self.store), "verweis": ref}

        bisher = tuple(current.allowed_capabilities) if current is not None else ()
        out["bisher"] = ", ".join(bisher)
        #: Die Fassung reist im Freigabetext MIT, damit ein Mensch sieht,
        #: worauf sich seine Zustimmung bezieht. Der eigentliche Riegel ist das
        #: gleichnamige ARGUMENT — der Digest laeuft ueber Argumente, nicht
        #: ueber diesen Text.
        if current is not None:
            out["fassung"] = str(current.version)

        if "capabilities" in args:
            neu_liste = _list(args["capabilities"])
            out["faehigkeiten"] = ", ".join(neu_liste)
            hinzu = [c for c in neu_liste if c not in bisher]
            weg = [c for c in bisher if c not in neu_liste]
            bleibt = [c for c in bisher if c in neu_liste]
            out["kommt_hinzu"] = ", ".join(hinzu) if hinzu else "nichts"
            out["entfaellt"] = ", ".join(weg) if weg else "nichts"
            out["bleibt"] = ", ".join(bleibt)
        if "targets" in args:
            out["nur_fuer"] = str(args["targets"])
        if "executors" in args:
            out["nur_ueber"] = str(args["executors"])
        if "allow_background" in args:
            out["im_hintergrund"] = "ja" if _flag(args["allow_background"]) else "nein"
        if current is not None:
            out["bisher_nur_fuer"] = ", ".join(current.allowed_targets)
        return out


_MESSAGES = {
    "secret_already_exists": "Diesen Zugang gibt es schon — ersetzen statt anlegen.",
    "unknown_secret": "Diesen Zugang gibt es nicht.",
    "scope_changed_meanwhile": ("Die Rechte dieses Zugangs haben sich inzwischen "
                                "geaendert. Sieh sie dir noch einmal an."),
    "scope_must_not_be_empty": "Ein Zugang braucht Zweck, Ziel und Executor.",
    "target_is_not_an_origin": "Ein Ziel muss vollstaendig sein, z. B. https://www.amazon.de",
    "kind_belongs_to_payment_capability": "Zahlungsmittel gehoeren nicht hierher.",
    "vault_unavailable": "Der Tresor ist gerade gesperrt.",
    "vault_not_initialised": "Der Tresor ist noch nicht eingerichtet.",
    "secret_is_too_large": "Das ist zu lang fuer einen Zugang.",
    "secret_is_empty": "Da war nichts.",
    "unknown_kind": "Diese Art von Zugang kenne ich nicht.",
    "envelope_rejected": "Der Eintrag laesst sich nicht oeffnen.",
}


def _message(reason: str) -> str:
    return _MESSAGES.get(reason, "Das hat nicht geklappt.")


def register(router: Any, capabilities: SecretVaultCapabilities) -> list[str]:
    handlers = {
        "google_connect": capabilities.google_connect,
        "secret_list": capabilities.list_secrets,
        "secret_add": capabilities.add,
        "secret_replace": capabilities.replace,
        "secret_disable": capabilities.disable,
        "secret_enable": capabilities.enable,
        "secret_delete": capabilities.delete,
        "secret_rescope": capabilities.rescope,
    }
    describers = {
        "google_connect": capabilities.describe_google_connect,
        "secret_add": capabilities.describe_add,
        "secret_replace": capabilities.describe_replace,
        "secret_disable": capabilities.describe_ref,
        "secret_enable": capabilities.describe_ref,
        "secret_delete": capabilities.describe_ref,
        "secret_rescope": capabilities.describe_rescope,
    }
    for name, handler in handlers.items():
        router.register(SPECS[name], handler, describe=describers.get(name))
    return sorted(handlers)
