"""Kanalunabhaengige Kommunikation ueber bestaetigte Empfaengerbindungen."""
from __future__ import annotations

import time
from typing import Any

from solvio.capabilities.contract import (
    AmbiguousExecution, CapabilityDeclined, CapabilityRefused, CapabilitySpec,
    ExecutionClass, ExecutorUnavailable,
)
from solvio.capabilities.router import CapabilityRouter
from solvio.communication.bindings import (
    BindingChanged, BindingStore, clean_handles, fingerprint,
    normalize_alias, same_binding,
)
from solvio.capabilities.gmail import MAX_SENDABLE_BODY, _hidden, extract_address
from solvio.security.mobile_approval.execution import IDEMPOTENT_WRITE, NON_IDEMPOTENT_WRITE, READ_ONLY
from solvio.tools.base import RiskLevel

DELIVERY_TRUTHS = frozenset({"QUEUED", "SENT_TO_PROVIDER", "DELIVERED", "FAILED", "UNKNOWN"})

SPECS: dict[str, CapabilitySpec] = {
    "communication_import_contacts": CapabilitySpec(
        name="communication_import_contacts", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.MUTATING,
        semantics=IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "source_device": {"type": "string"}, "contacts_json": {"type": "string"}},
            "required": ["source_device", "contacts_json"]},
        description="Ersetzt die lesende Kontaktquelle eines attestierten iPhones; bestätigt keine Identität."),
    "communication_resolve_recipient": CapabilitySpec(
        name="communication_resolve_recipient", version=1,
        execution_class=ExecutionClass.FAST, base_risk=RiskLevel.HARMLESS,
        semantics=READ_ONLY, input_schema={"type": "object", "properties": {
            "alias": {"type": "string"}}, "required": ["alias"]},
        description="Loest einen vom Nutzer genannten Empfaenger-Alias auf."),
    "communication_confirm_binding": CapabilitySpec(
        name="communication_confirm_binding", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.CRITICAL,
        semantics=NON_IDEMPOTENT_WRITE, input_schema={"type": "object", "properties": {
            "alias": {"type": "string"}, "display_name": {"type": "string"},
            "handles": {"type": "array", "items": {"type": "object"}},
            "source": {"type": "string"}},
            "required": ["alias", "display_name", "handles"]},
        description="Legt fest oder aendert, welche Person und welche Kontaktwege "
                    "hinter einem Alias stehen. Jede Aenderung braucht Face ID."),
    "communication_send": CapabilitySpec(
        name="communication_send", version=1,
        execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.CRITICAL,
        semantics=NON_IDEMPOTENT_WRITE, input_schema={"type": "object", "properties": {
            "alias": {"type": "string"}, "channel": {"type": "string"},
            "recipient_handle": {"type": "string"}, "content": {"type": "string"},
            "attachments": {"type": "array"}},
            "required": ["alias", "channel", "recipient_handle", "content"]},
        description="Sendet eine Nachricht an einen bestaetigt gebundenen Empfaenger."),
}


class CommunicationCapabilities:
    def __init__(self, gmail: Any, store: BindingStore | None = None) -> None:
        self.gmail = gmail
        self.store = store or BindingStore()
        from solvio.communication.sources import ContactSources
        self.sources = ContactSources(self.store)

    async def import_contacts(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"imported": self.sources.replace(arguments['source_device'], arguments['contacts_json']),
                "confirmed": False}

    async def resolve_recipient(self, arguments: dict[str, Any]) -> dict[str, Any]:
        alias = str(arguments.get("alias", "") or "").strip()
        if not alias:
            raise CapabilityDeclined("missing_alias")
        binding = self.store.get(alias)
        if binding is not None:
            return {"confirmed": True, "binding": binding}
        matches = self.store.matching_name(alias)
        if len(matches) == 1:
            return {"confirmed": True, "binding": matches[0]}
        if matches:
            return {"confirmed": False, "ambiguous": True,
                    "candidates": [{**item, "confirmed": True} for item in matches[:10]]}
        imported = self.sources.search(alias)
        if imported:
            return {"confirmed": False, "candidates": imported}
        found = await self.gmail.search({"query": alias})
        candidates = []
        seen = set()
        for message in (found or {}).get("messages", []):
            sender = str(message.get("from", "") or "").strip()
            address = extract_address(sender)
            if address and address not in seen:
                seen.add(address)
                candidates.append({"display_name": sender, "handles": [
                    {"channel": "gmail", "value": address}], "confirmed": False,
                    "content_trust": "untrusted_email"})
        return {"confirmed": False, "candidates": candidates}

    # -- Die Bindung: was der Mensch bestaetigt, und was danach geschrieben wird --
    @staticmethod
    def _requested(arguments: dict[str, Any]) -> dict[str, Any]:
        """Die gewuenschte Bindung, normalisiert wie der Speicher sie schreibt."""
        alias = " ".join(str(arguments.get("alias") or "").strip().split())
        name = str(arguments.get("display_name") or "").strip()
        wege = [w for w in clean_handles(arguments.get("handles"))
                if w["channel"] and w["value"]]
        if (not alias or not name or not wege
                or len(wege) != len(clean_handles(arguments.get("handles")))):
            raise CapabilityDeclined(
                "incomplete_binding",
                human_message="Dazu fehlt mir noch, wer gemeint ist und wie er erreichbar ist.")
        return {"alias": alias, "alias_norm": normalize_alias(alias),
                "display_name": name, "handles": wege,
                "source": str(arguments.get("source") or "user_confirmed").strip()}

    @staticmethod
    def _shown(gewollt: dict[str, Any], bisher: dict[str, Any] | None) -> dict[str, Any]:
        """Der Text aus Wunsch und Stand — dieselbe Funktion fuer Anzeige und Schreiben.

        Kein Zustand dazwischen. Der Beschreiber rendert sie fuer das Geraet,
        der Handler rendert sie noch einmal gegen den Stand, den er gleich
        ersetzt, und vergleicht mit dem Digest, den der Mensch signiert hat.
        """
        gezeigt = {
            "diese_person": gewollt["display_name"],
            "gemerkt_als": gewollt["alias"],
            "kontakt_bisher": (_kontaktwege(bisher.get("handles"))
                               if bisher is not None else "noch nicht gemerkt"),
            "kontakt_neu": _kontaktwege(gewollt["handles"]),
        }
        if bisher is not None:
            gezeigt["stand"] = str(int(bisher.get("version", 0)))
            if str(bisher.get("display_name", "")) != gewollt["display_name"]:
                gezeigt["hiess_bisher"] = str(bisher.get("display_name", ""))
        return gezeigt

    def describe_confirm_binding(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Was auf dem iPhone steht, wenn ein Kontakt festgelegt oder geaendert wird.

        Der Mensch entscheidet genau hier, WEN ein Alias kuenftig meint — davon
        haengt ab, wessen Telefon bei einem Anruf klingelt und wessen Postfach
        eine Nachricht bekommt. Deshalb steht hier nicht nur, was NEU gelten
        soll, sondern auch, was BISHER galt: Name, Kontaktwege und Fassung der
        bestehenden Bindung. Alles davon ist Teil des Textes, den Face ID
        bindet. Wer zwischen Freigabe und Ausfuehrung die Bindung veraendert,
        veraendert damit den Text, den Digest und die Gueltigkeit der Freigabe
        (`approval_drift`) — dieselbe Schranke, die DEBT-0206 fuer den Anruf
        eingezogen hat, jetzt fuer die Bindung selbst (DEBT-0208).

        Die Werte stehen UNVERAENDERT da. Keine Maske, keine Kuerzung: jede
        Umformung ist eine Stelle, an der zwei verschiedene Nummern zu einem
        Text zusammenfallen koennen.

        Steht die gewuenschte Bindung bereits genau so, gibt es nichts zu
        bestaetigen: dann wird gar nicht erst gefragt (`binding_unchanged`), und
        es wird auch nichts geschrieben.
        """
        gewollt = self._requested(arguments)
        bisher = self.store.get(gewollt["alias"])
        if same_binding(bisher, gewollt["display_name"], gewollt["handles"]):
            raise CapabilityDeclined(
                "binding_unchanged",
                human_message="Das ist schon genau so gemerkt.",
                data={"confirmed": True, "changed": False, "binding": bisher})
        return self._shown(gewollt, bisher)

    async def confirm_binding(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Schreibt die Bindung — und nur die, die der Mensch gesehen und bestaetigt hat.

        Der Router uebergibt dem Handler die ECHTEN Argumente, nicht die
        Anzeige, und zwischen dem letzten Beschreiben und diesem Punkt liegt
        der Freigabepfad mit echten Wartepunkten. Ein Fach je Alias, in dem der
        Beschreiber seinen Stand hinterlegt, ist dort keine Schranke: ein
        zweiter Schreiber plus ein dritter Beschreiber desselben Alias
        ueberschreiben es, und der Handler schriebe gegen einen Stand, den der
        Mensch nie gesehen hat (Befund des Technical Lead zu diesem Milestone,
        reproduziert). Deshalb liegt hier KEIN Zustand.

        Stattdessen: der Handler bildet den Text aus Wunsch und JETZIGEM Stand
        noch einmal — mit derselben Funktion wie der Beschreiber — und
        vergleicht ihn mit dem Digest, den das Geraet signiert hat und den der
        eingefrorene Freigabepfad als Ausfuehrungsidentitaet mitreicht. Passt
        er, wird in einer Transaktion gegen genau diesen Stand geschrieben
        (Compare-and-Swap auf Inhalt und Fassung). Text, Stand und Freigabe
        sind damit ohne Zwischenzustand aneinander gebunden.

        Ohne freigegebenen Vorgang wird nicht geschrieben. Die
        Ausfuehrungsidentitaet setzt ausschliesslich `CapabilityApprovals.resume`;
        ein Handler kann sie nicht waehlen. Sie autorisiert nichts — sie belegt,
        dass der Weg hierher ueber das Geraet fuehrte, und sie benennt, WAS
        dort bestaetigt wurde. Dieselbe Wache wie „ohne Ausfuehrungsidentitaet
        wird nicht gewaehlt" in der Telefonie.

        Jede Absage VOR dem Schreiben ist `SafeExecutionFailure`: der
        eingefrorene Freigabepfad buchte jede andere Ausnahme als UNKNOWN, und
        der Mensch hoerte „ich weiss nicht, ob das durchging" fuer etwas, das
        nachweislich nicht passiert ist.
        """
        # Die Importe liegen in der Funktion: der Freigabepfad und der Tresor
        # stehen in der Abhaengigkeitsordnung ueber dieser Faehigkeit.
        from solvio.capabilities import execution_identity as EI
        from solvio.capabilities import policy as P
        from solvio.capabilities.approval_gateway import approval_digest
        from solvio.secret_vault import context as SC
        from solvio.security.mobile_approval.execution import SafeExecutionFailure
        try:
            gewollt = self._requested(arguments)
            vorgang = EI.current()
            if not vorgang.approval_id or not vorgang.action_digest:
                raise CapabilityRefused("binding_without_approval")
            bisher = self.store.get(gewollt["alias"])
            jetzt = approval_digest(SPECS["communication_confirm_binding"],
                                    self._shown(gewollt, bisher),
                                    P.origin_label(SC.current().origin))
            if jetzt != vorgang.action_digest:
                raise CapabilityRefused("binding_changed_after_approval")
            try:
                binding = self.store.confirm(
                    gewollt["alias"], gewollt["display_name"], gewollt["handles"],
                    gewollt["source"], expected=fingerprint(bisher))
            except BindingChanged:
                raise CapabilityRefused("binding_changed_after_approval") from None
            except (TypeError, ValueError):
                raise CapabilityDeclined("invalid_binding") from None
        except (CapabilityDeclined, CapabilityRefused) as exc:
            raise SafeExecutionFailure(exc.reason) from None
        return {"confirmed": True, "changed": True, "binding": binding}

    def describe_send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Vor der Freigabe: steht der Empfaenger bestaetigt hinter dem Alias?

        Seit ADR-0041 fragt jeder Versand Face ID. Eine Nachricht an jemanden, den
        niemand bestaetigt hat, soll dann nicht erst gefragt und danach abgelehnt
        werden — sie endet VOR der Frage an der Kontaktgrenze. Die Beschreibung
        selbst bleibt dieselbe wie die Argumente.
        """
        self._checked_binding(arguments)
        content = str(arguments.get("content", "") or "")
        if _hidden(content) or len(content) > MAX_SENDABLE_BODY:
            # Hinaus geht dieser Wortlaut Byte fuer Byte — also muss er vollstaendig
            # sichtbar sein (Review S2, Befund 1).
            raise CapabilityRefused("content_not_showable")
        permits = getattr(self.gmail, "permits", None)
        if permits is not None and not permits("communication_send"):
            # Der Tresor wuerde den Versand ohnehin abweisen (DEBT-0341) — dann nicht
            # erst um Face ID bitten.
            raise CapabilityRefused("contact_send_not_permitted_by_vault")
        return dict(arguments)

    def _checked_binding(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        alias = str(arguments.get("alias", "") or "").strip()
        channel = str(arguments.get("channel", "") or "").strip().lower()
        handle = str(arguments.get("recipient_handle", "") or "").strip()
        binding = self.store.get(alias)
        if binding is None:
            raise CapabilityDeclined("recipient_unknown")
        handles = binding["handles"]
        if not any(str(item.get("value", "")) == handle for item in handles):
            raise CapabilityRefused("recipient_not_bound")
        if not any(str(item.get("channel", "")).lower() == channel for item in handles):
            raise CapabilityRefused("channel_unavailable")
        if not any(str(item.get("channel", "")).lower() == channel
                   and str(item.get("value", "")) == handle for item in handles):
            raise CapabilityRefused("recipient_not_bound")
        if arguments.get("attachments"):
            raise CapabilityRefused("attachments_not_supported")
        if channel != "gmail":
            raise CapabilityRefused("channel_unavailable")
        return binding, channel, handle

    async def send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        content = str(arguments.get("content", "") or "")
        binding, channel, handle = self._checked_binding(arguments)
        identity = {"alias": binding["alias"], "display_name": binding["display_name"],
                    "recipient_handle": handle}
        try:
            # Der Inhalt ist durch die Freigabe DIESER Faehigkeit gebunden (Alias,
            # Kanal, Adresse, Text). Hinaus gehen genau diese Bytes (ADR-0041).
            provider_result = await self.gmail.send_composed(
                to=handle, subject="(ohne Betreff)", body=content)
            truth = "SENT_TO_PROVIDER" if provider_result.get("message_id") else "UNKNOWN"
        except AmbiguousExecution as exc:
            provider_result, truth = {"error": type(exc).__name__}, "UNKNOWN"
        except (CapabilityDeclined, CapabilityRefused, ExecutorUnavailable) as exc:
            provider_result, truth = {"error": getattr(exc, "reason", type(exc).__name__)}, "FAILED"
        result = {"recipient_identity": identity, "channel": channel,
                  "requested_content": content, "provider_result": provider_result,
                  "sent_at": time.time(), "delivery_truth": truth}
        if result["delivery_truth"] not in DELIVERY_TRUTHS:
            # Kein `assert`. Die Zusicherungspolitik dieses Hauses ist
            # ausdruecklich: was unter `python -O` verschwindet, ist keine
            # Wache. Und genau diese hier haelt die Ergebnismenge geschlossen —
            # ein erfundener Wert waere eine Aussage ueber Zustellung, die
            # niemand geprueft hat.
            raise ExecutorUnavailable(
                f"unknown delivery truth: {result['delivery_truth']!r}")
        return result


#: Wie ein Kanal auf dem iPhone heisst. Der Rohwert ist ein Bezeichner, kein
#: Wort, das jemand bestaetigen will.
_KANAL_WORT = {
    "phone": "Telefon",
    "gmail": "E-Mail",
    "email": "E-Mail",
    "sms": "SMS",
}


def _kontaktwege(handles: Any) -> Any:
    """„Telefon: +…" — in Worten, unveraendert im Wert, und strukturell eindeutig.

    Ein Weg ist eine Zeichenkette, mehrere sind eine LISTE. `render_action`
    kodiert beides als JSON, und dort kann eine Liste nie mit einer
    Zeichenkette zusammenfallen — anders als bei einer selbstgebauten
    Verkettung: zwei Wege `[Telefon X, E-Mail Y]` und EIN Weg mit dem Wert
    „X · E-Mail: Y" ergaben denselben Text und damit denselben Digest
    (Befund des Technical Lead). Jetzt nicht mehr.
    """
    wege = [f"{_KANAL_WORT.get(w['channel'], w['channel'])}: {w['value']}"
            for w in clean_handles(handles) if w["channel"] and w["value"]]
    return wege[0] if len(wege) == 1 else wege


def register(router: CapabilityRouter, capabilities: CommunicationCapabilities) -> list[str]:
    handlers = {
        "communication_import_contacts": capabilities.import_contacts,
        "communication_resolve_recipient": capabilities.resolve_recipient,
        "communication_confirm_binding": capabilities.confirm_binding,
        "communication_send": capabilities.send,
    }
    describers = {"communication_confirm_binding": capabilities.describe_confirm_binding,
                  "communication_send": capabilities.describe_send}
    for name, handler in handlers.items():
        router.register(SPECS[name], handler, describe=describers.get(name))
    return sorted(handlers)
