"""Die Bruecke: Capability-Router -> bestehende iPhone-Freigabe.

Hier entsteht **kein zweites Freigabesystem**. Approval Security V1 ist eingefroren
und bleibt die einzige Autoritaetsquelle; dieses Modul uebersetzt lediglich einen
Faehigkeitsaufruf in die Sprache, die der bestehende Kontrollpfad ohnehin spricht:

    create_request(tool, mode, task, workspace, human_summary) -> approval_id
    ... iPhone signiert, App Attest belegt das Geraet, der Mac prueft ...
    execute_approved(approval_id, executor)

**Was der Nutzer autorisiert, ist `task`.** Das ist keine Entscheidung dieses Moduls,
sondern die des bestehenden Protokolls: die iOS-Seite vermerkt ausdruecklich, dass
`task` — nicht `human_summary` — die Grundlage der Freigabe ist und `human_summary`
ein unvertrauenswuerdiger, modellverfasster Hinweis bleibt.

Daraus folgt die einzige echte Entwurfsaufgabe hier: `task` muss **gleichzeitig
lesbar und ausfuehrungsbestimmend** sein. Ein JSON-Klumpen waere exakt, aber auf
einem Telefon unlesbar — und was niemand liest, bestaetigt niemand bewusst. Ein
huebscher Satz waere lesbar, aber mehrdeutig: zwei verschiedene Aktionen duerften
nie denselben Text ergeben, sonst hiesse „A freigeben" am Ende „B ausfuehren".

`render_action` loest das mit einer beschrifteten Zeile je Argument, sortiert und
JSON-kodiert: lesbar wie ein Formular, umkehrbar eindeutig wie ein Schluessel.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any

from solvio.capabilities import execution_identity as EI
from solvio.capabilities.contract import CapabilitySpec
from solvio.capabilities.router import _call as _invoke_handler
from solvio.logging_setup import get_logger
from solvio.security.approval import action_digest

log = get_logger("capabilities")

#: Ueberschrift und Feldbeschriftungen je Faehigkeit. Die Beschriftungen muessen je
#: Faehigkeit EINDEUTIG sein — zwei Argumente mit derselben Beschriftung waeren zwei
#: Aktionen mit demselben Freigabetext.
ACTION_LABELS: dict[str, tuple[str, dict[str, str]]] = {
    "native_credit_consent": ("ChatGPT-Credits für SOLVIO", {
        "konto": "Konto", "nutzung": "Credit-Nutzung", "bedingungen": "Umfang und Grenzen"}),
    "calendar_create_event": ("Kalendertermin anlegen", {
        "title": "Titel", "when": "Tag", "time": "Uhrzeit",
        "duration_minutes": "Dauer (Minuten)", "all_day": "Ganztägig",
        "location": "Ort", "description": "Notiz"}),
    "calendar_update_event": ("Kalendertermin ändern", {
        "title": "Bisheriger Titel", "when": "Bisheriger Tag",
        "new_title": "Neuer Titel", "new_when": "Neuer Tag",
        "new_time": "Neue Uhrzeit", "duration_minutes": "Dauer (Minuten)"}),
    "calendar_delete_event": ("Kalendertermin löschen", {
        "title": "Titel", "when": "Tag"}),
    # Beim Versand steht der GANZE Inhalt im autorisierenden Text. Eine
    # Zusammenfassung freizugeben, waehrend darunter ein anderer Volltext
    # gebunden ist, waere genau die Taeuschung, die das hier verhindern soll.
    "gmail_send_draft": ("E-Mail senden", {
        "to": "An", "subject": "Betreff", "body": "Nachricht",
        "attachments": "Anhänge", "in_reply_to": "Antwort auf", "draft_id": "Entwurf"}),
    "gmail_create_draft": ("E-Mail-Entwurf anlegen", {
        "to": "An", "subject": "Betreff", "body": "Nachricht",
        "reply_to_message": "Antwort auf", "forward_message": "Weiterleitung von"}),
    "portal_login": ("SOLVIO möchte sich anmelden", {
        "portal": "Portal", "adresse": "Domain", "seite": "Seite",
        "aktion": "Vorgang", "zugang": "Konto", "felder": "Eingaben",
        "pruefsumme": "Seitenprüfsumme"}),
    "portal_submit": ("Auf einer Webseite abschicken", {
        "portal": "Portal", "adresse": "Domain", "seite": "Seite",
        "aktion": "Vorgang", "zugang": "Konto", "felder": "Eingaben",
        "pruefsumme": "Seitenprüfsumme"}),
    "ha_turn_on": ("Gerät einschalten", {"name": "Gerät", "area": "Raum"}),
    "ha_turn_off": ("Gerät ausschalten", {"name": "Gerät", "area": "Raum"}),
    "ha_set_brightness": ("Helligkeit setzen", {
        "name": "Lampe", "area": "Raum", "brightness_pct": "Helligkeit (Prozent)"}),
    "background_create": ("Wiederkehrende Aufgabe anlegen", {
        "titel": "Titel", "wann": "Wann", "aktion": "Was",
        "argumente": "Angaben", "bedingung": "Bedingung", "melden": "Melden"}),
    "background_delete": ("Wiederkehrende Aufgabe löschen", {"id": "Aufgabe"}),
    "background_pause": ("Wiederkehrende Aufgabe pausieren", {"id": "Aufgabe"}),
    "background_resume": ("Wiederkehrende Aufgabe fortsetzen", {"id": "Aufgabe"}),
    "background_run_now": ("Wiederkehrende Aufgabe jetzt ausführen", {"id": "Aufgabe"}),
    "background_require_approval": ("Vorab-Freigabe zurückziehen", {"id": "Aufgabe"}),
    # Der Tresor. Die Beschriftungen stehen hier ausdruecklich, statt sich auf
    # den Rueckfall zu verlassen: der setzt die ARGUMENTNAMEN als Beschriftung
    # ein, und ein Freigabetext, in dem `allow_background: "true"` steht, ist
    # kein Text, den ein Mensch bestaetigen kann. Ein Wert steht in keiner
    # dieser Zeilen — er reist als Einlagerungskennung (`vorgang`).
    "google_connect": ("Google für SOLVIO verbinden", {
        "konto": "Google-Konto", "aktion": "Änderung", "rechte": "Bestehende Rechte",
        "versand": "Mailversand", "vorgang": "Vorgang"}),
    "secret_add": ("SOLVIO möchte einen Zugang im Tresor hinterlegen", {
        "zugang": "Zugang", "verweis": "Verweis", "art": "Art",
        "konto": "Konto", "nur_fuer": "Nur für", "nur_ueber": "Nur über",
        "faehigkeiten": "Verwendbar für", "im_hintergrund": "Im Hintergrund",
        "vorgang": "Vorgang"}),
    "secret_replace": ("SOLVIO möchte einen Zugang ersetzen", {
        "zugang": "Zugang", "verweis": "Verweis", "vorgang": "Vorgang"}),
    "secret_disable": ("Einen Zugang sperren", {
        "zugang": "Zugang", "verweis": "Verweis"}),
    "secret_enable": ("Einen gesperrten Zugang wieder freigeben", {
        "zugang": "Zugang", "verweis": "Verweis"}),
    "secret_delete": ("Einen Zugang endgültig löschen", {
        "zugang": "Zugang", "verweis": "Verweis"}),
    # Die Zahlung. Der Betrag steht als EIGENE Zeile mit eigener Beschriftung —
    # nicht im Fliesstext, nicht in einer Zusammenfassung, nicht als Teil eines
    # Satzes, den man ueberliest. Jede Angabe, die die wirtschaftliche Wirkung
    # veraendert, hat hier eine Zeile: aendert sich eine davon, aendert sich der
    # Text, der Digest und damit die Gueltigkeit der Freigabe.
    "purchase_place": ("SOLVIO möchte für dich bezahlen", {
        "zweck": "Wofür", "rechnung": "Rechnung",
        "haendler": "Händler", "adresse": "Adresse", "posten": "Artikel",
        "menge": "Stückzahl", "betrag": "Gesamtbetrag", "waehrung": "Währung",
        "zwischensumme": "Zwischensumme", "aufschlaege": "Hinzu kommen",
        "belastet": "Belastet wird", "zahlungsmittel": "Zahlungsmittel",
        "lieferung": "Lieferung", "lieferziel": "Lieferziel (Prüfsumme)",
        "gueltig_bis": "Gültig bis", "vorgang": "Vorgang",
        "pruefsumme": "Prüfsumme"}),
    "purchase_cancel": ("Eine Bestellung stornieren", {
        "vorgang": "Vorgang", "haendler": "Händler", "betrag": "Betrag"}),
    "refund_request": ("Geld zurückfordern", {
        "vorgang": "Vorgang", "haendler": "Händler", "betrag": "Betrag",
        "zurueck_auf": "Zurück auf"}),
    "payment_method_add": ("SOLVIO möchte ein Zahlungsmittel hinterlegen", {
        "zahlungsmittel": "Zahlungsmittel", "verweis": "Verweis", "art": "Art",
        "anbieter": "Anbieter", "grenze_einzeln": "Höchstbetrag je Zahlung",
        "grenze_taeglich": "Höchstbetrag je Tag", "waehrungen": "Währungen",
        "haendler": "Nur bei", "vorgang": "Vorgang"}),
    "payment_method_remove": ("Ein Zahlungsmittel endgültig entfernen", {
        "zahlungsmittel": "Zahlungsmittel", "verweis": "Verweis"}),
    "payment_method_enable": ("Ein gesperrtes Zahlungsmittel wieder freigeben", {
        "zahlungsmittel": "Zahlungsmittel", "verweis": "Verweis"}),
    "payment_method_disable": ("Ein Zahlungsmittel sperren", {
        "zahlungsmittel": "Zahlungsmittel", "verweis": "Verweis",
        "grund": "Grund"}),
    "payment_limit_raise": ("Eine Zahlungsgrenze ANHEBEN", {
        "zahlungsmittel": "Zahlungsmittel", "verweis": "Verweis",
        "bisher_einzeln": "Bisher je Zahlung", "neu_einzeln": "Neu je Zahlung",
        "bisher_taeglich": "Bisher je Tag", "neu_taeglich": "Neu je Tag"}),
    "payment_limit_lower": ("Eine Zahlungsgrenze senken", {
        "zahlungsmittel": "Zahlungsmittel", "verweis": "Verweis",
        "bisher_einzeln": "Bisher je Zahlung", "neu_einzeln": "Neu je Zahlung",
        "bisher_taeglich": "Bisher je Tag", "neu_taeglich": "Neu je Tag"}),
    "payment_method_rescope": ("Ändern, wofür ein Zahlungsmittel gilt", {
        "zahlungsmittel": "Zahlungsmittel", "verweis": "Verweis",
        "waehrungen": "Währungen", "haendler": "Nur bei",
        "bisher_haendler": "Bisher nur bei"}),
    "secret_rescope": ("Ändern, wofür ein Zugang benutzt werden darf", {
        "zugang": "Zugang", "verweis": "Verweis",
        "faehigkeiten": "Verwendbar für", "nur_fuer": "Nur für",
        "nur_ueber": "Nur über", "im_hintergrund": "Im Hintergrund",
        "bisher_nur_fuer": "Bisher nur für",
        # Die Differenz ist der Grund, warum dieser Bildschirm existiert —
        # und sie stand hier bis zur Geraeteabnahme als roher Schluesselname.
        # `describe_rescope` legt diese fuenf Felder in den Freigabetext;
        # `_labels_for` faellt fuer unbeschriftete Schluessel auf den
        # Schluesselnamen zurueck, und der Eigentuemer las „entfaellt" statt
        # „Wird entfernt" — an genau der Zeile, auf die es ankommt.
        "kommt_hinzu": "Neu erlaubt", "entfaellt": "Wird entfernt",
        "bisher": "Bisher erlaubt", "bleibt": "Bleibt erlaubt",
        "fassung": "Aktueller Stand"}),
    # Der Telefonanruf. Wie beim Mailversand steht der GANZE Wortlaut im
    # autorisierenden Text und nicht eine Zusammenfassung davon: freigegeben
    # wird genau der Satz, den ein fremder Mensch gleich zu hoeren bekommt.
    #
    # Und der Empfaenger steht mit NAMEN und RUFNUMMER da, nicht als Alias.
    #
    # Bis DEBT-0206 zeigte diese Zeile das Modellargument, also „Anrufen: 'papa'".
    # Wen SOLVIO daraufhin anwaehlte, entschied die Kontaktbindung — nach der
    # Freigabe. Der Mensch bestaetigte damit einen Namen, den er selbst vergeben
    # hatte, und nicht das Telefon, das klingelt.
    #
    # Die Schluessel hier sind deshalb NICHT die Modellargumente, sondern die
    # Ausgabe von `TelephonyCapabilities.describe_call`, die die Bindung vor der
    # Freigabe aufloest. Sie sind ausserdem so benannt, dass die Sortierung in
    # `render_action` einen lesbaren Text ergibt.
    # Die Kontaktbindung. Sie beantwortet „wer ist gemeint" — und von ihrer
    # Antwort haengt ab, wessen Telefon klingelt und wessen Postfach eine
    # Nachricht bekommt. Hier stand der rohe Faehigkeitsname mit JSON darunter;
    # das ist dieselbe Luecke wie DEBT-0206, eine Faehigkeit weiter.
    #
    # Seit Contact Binding Authority Hardening V1 (DEBT-0208) steht auch der
    # BISHERIGE Stand im Text: Name, Kontaktwege und Fassung der Bindung, die
    # ersetzt wird. Der Mensch sieht damit nicht nur, was kuenftig gilt,
    # sondern was er damit aufgibt — und eine zwischenzeitlich veraenderte
    # Bindung ergibt einen anderen Text, also einen anderen Digest. Die
    # Schluessel sind so benannt, dass die Sortierung in `render_action` einen
    # lesbaren Text ergibt; die Beschriftungen sind die Woerter des Menschen.
    "communication_confirm_binding": ("Einen Kontakt festlegen oder ändern", {
        "diese_person": "Wer",
        "gemerkt_als": "Gemerkt als",
        "hiess_bisher": "Wer bisher",
        "kontakt_bisher": "Bisher erreichbar über",
        "kontakt_neu": "Künftig erreichbar über",
        "stand": "Stand"}),
    "telephony_call": ("Anruf führen und etwas ausrichten", {
        "anrufen": "Anrufen",
        "bei_nummer": "Unter der Nummer",
        "nachricht": "Diese Nachricht wird ausgerichtet",
        "worum_es_geht": "Worum es geht",
        "zeitrahmen": "Das Gespräch endet spätestens nach"}),
}


def _labels_for(spec: CapabilitySpec) -> tuple[str, dict[str, str]]:
    headline, labels = ACTION_LABELS.get(spec.name, ("", {}))
    if not headline:
        # Unbekannte Faehigkeit: der Name selbst ist die Ueberschrift, die
        # Argumentnamen sind die Beschriftungen. Weniger schoen, aber eindeutig —
        # und eine vergessene Beschriftung darf keine Freigabe verhindern.
        headline = f"{spec.name} ausführen"
    return headline, labels


def render_action(spec: CapabilitySpec, arguments: dict[str, Any],
                  origin_label: str = "") -> str:
    """Der Text, den der Nutzer auf dem iPhone bestaetigt.

    Lesbar: eine Zeile je Angabe, mit deutscher Beschriftung.
    Eindeutig: Schluessel sortiert, Werte JSON-kodiert, Beschriftungen je Faehigkeit
    kollisionsfrei. Zwei verschiedene Argumentmengen ergeben zwei verschiedene Texte.

    Seit Approval Policy V2 steht die HERKUNFT mit im Text — und damit im
    Digest, in der signierten Challenge und auf dem Display. Der Mensch soll
    nicht nur sehen, was freigegeben werden soll, sondern auch, von wo aus
    gefragt wurde: „die Haustuer, angefragt ueber das Raum-Mikrofon" ist eine
    andere Entscheidung als dieselbe Bitte aus der App in seiner Hand.
    """
    headline, labels = _labels_for(spec)
    lines = [headline]
    for key in sorted(arguments):
        label = labels.get(key, key)
        lines.append(f"{label}: {json.dumps(arguments[key], ensure_ascii=False)}")
    if origin_label:
        lines.append(f"Angefragt über: {origin_label}")
    return "\n".join(lines)


def approval_mode(spec: CapabilitySpec) -> str:
    """Bindet die Version mit ein: eine Freigabe fuer v1 gilt nicht fuer v2."""
    return f"{spec.execution_class.value}-v{spec.version}"


def approval_digest(spec: CapabilitySpec, arguments: dict[str, Any],
                    origin_label: str = "") -> str:
    """Derselbe Digest, den die Kontrollebene beim Anlegen berechnet.

    Nicht nachgebaut: es ist die eingefrorene `action_digest` mit denselben vier
    Feldern, die auch `create_request` hasht.
    """
    return action_digest(tool_id=spec.name, mode=approval_mode(spec),
                         task=render_action(spec, arguments, origin_label),
                         workspace="")


class CapabilityApprovals:
    """Faehigkeitsaufrufe am bestehenden iPhone-Freigabeweg.

    `owner_principal` ist der Principal, auf den das iPhone registriert ist. Er ist
    ausdruecklich NICHT das Aufruf-Principal des Satelliten: wer *fragt* und wer
    *freigeben darf*, sind zwei verschiedene Rollen. Die Kontrollebene weist eine
    Entscheidung zurueck, wenn Geraet und Anfrage nicht denselben Principal tragen.
    """

    def __init__(self, coordinator: Any, *, owner_principal: str) -> None:
        self.coordinator = coordinator
        self.owner_principal = owner_principal

    @property
    def control_plane(self) -> Any:
        return self.coordinator.cp

    async def request(self, spec: CapabilitySpec, arguments: dict[str, Any], *,
                      requested_by: str = "", origin_label: str = "") -> str:
        """Legt die durable Freigabeanfrage an. Gibt NUR eine Kennung zurueck.

        Kein Token, keine Autoritaet — die Kennung allein fuehrt nichts aus.
        """
        summary = f"Angefragt von {requested_by}" if requested_by else "Sprachanfrage"
        return await self.control_plane.create_request(
            principal=self.owner_principal, tool=spec.name, mode=approval_mode(spec),
            task=render_action(spec, arguments, origin_label), workspace="",
            # Bewusst duenn: dieses Feld gilt protokollseitig als unvertrauenswuerdig
            # und traegt deshalb keine Aussage ueber die Aktion.
            human_summary=summary)

    async def pending(self) -> list[dict[str, Any]]:
        return await self.control_plane.store.list_pending()

    async def abandon(self, approval_id: str, *, reason: str = "abandoned") -> bool:
        """Nimmt eine Anfrage aus der Liste, die niemand mehr ausfuehren kann.

        Der Anlass ist ein gemessener Vorfall: nach abgebrochenen Laeufen standen
        mehrere Freigaben gleichzeitig auf dem iPhone, und bestaetigt wurde die
        aeltere — hinter der kein lebender Vorgang mehr stand. Die Regel „lass nie
        mehr als eine offen" ist dafuer die falsche Antwort; sie ist eine Bitte an
        den Menschen, wo eine Eigenschaft des Systems gehoert.

        Benutzt wird ausschliesslich der eingefrorene Zustandsgraph: `PENDING ->
        EXPIRED` ist dort eine erlaubte Kante, und `transition` ist ein
        Compare-and-Swap in einer Schreibtransaktion. Keine Zeile unter
        `security/` aendert sich, kein zweites Buch entsteht, und kein
        historischer Eintrag wird umgeschrieben.

        Ehrlich zur Wortwahl: `EXPIRED` sagt „abgelaufen", gemeint ist
        „zurueckgezogen". Es ist der einzige Ausgang aus `PENDING`, der ohne eine
        Geraeteentscheidung erlaubt ist — die Begruendung steht daneben im
        Journal, und dort unterscheidet sie sich von einem echten Fristablauf.
        """
        from solvio.security.mobile_approval import store as S
        try:
            await self.control_plane.store.transition(
                approval_id, S.EXPIRED, error=reason)
        except (S.IllegalTransition, S.ConcurrentTransition, S.ApprovalStoreError) as exc:
            # Alle drei heissen dasselbe: jemand anderes hat die Anfrage bereits
            # abgeschlossen. Das ist kein Fehlschlag, das ist der Normalfall.
            log.info("capability.abandon_noop", reason=type(exc).__name__)
            return False
        # Falls im selben Augenblick eine Bestaetigung eintraf, darf sie nicht im
        # Speicher liegenbleiben — sonst haette der Abbruch nur die Anzeige
        # geraeumt und nicht die Wirkung.
        approver = getattr(self.coordinator, "approver", None)
        if approver is not None and hasattr(approver, "revoke_confirmation"):
            approver.revoke_confirmation(approval_id)
        log.info("capability.approval_abandoned", reason=reason)
        return True

    async def abandon_orphans(self) -> int:
        """Beim Start: was offen ist, kann dieser Prozess nicht mehr ausfuehren.

        Eine freigegebene Aktion laeuft nur dort, wo die bestaetigte Entscheidung
        im Arbeitsspeicher liegt — also im selben Prozess, der die Anfrage
        gestellt hat. Ein frisch gestarteter Core hat keine gestellt. Alles, was
        er beim Hochfahren offen vorfindet, stammt folglich aus einem Prozess,
        den es nicht mehr gibt, und koennte bestenfalls noch bestaetigt werden —
        ohne dass irgendwo etwas geschieht.

        Genau diese Waisen standen im Vorfall auf dem Display.
        """
        orphans = await self.pending()
        closed = 0
        for entry in orphans:
            if await self.abandon(str(entry.get("approval_id", "")),
                                  reason="orphaned_by_core_restart"):
                closed += 1
        if closed:
            log.info("capability.orphaned_approvals_closed", count=closed)
        return closed

    async def resume(self, approval_id: str, spec: CapabilitySpec,
                     arguments: dict[str, Any], handler: Any,
                     origin_label: str = "") -> tuple[Any, str]:
        """Fuehrt die freigegebene Aktion aus — und nur genau sie.

        Vor der Ausfuehrung wird der Digest gegen die AKTUELLEN Argumente neu
        berechnet. Weicht er ab, ist es nicht mehr die Aktion, die der Nutzer auf
        dem Display gesehen hat: `approval_drift`, und es passiert nichts.
        """
        stored = await self.control_plane.store.get_request(approval_id)
        if stored is None:
            return None, "unknown_approval"
        # NEIN IST EINE ANTWORT — und muss von „noch keine Antwort" unterscheidbar
        # sein.
        #
        # Der eingefrorene Pfad kann das nicht liefern: `execute_approved` prueft
        # `state != APPROVED` und gibt fuer JEDEN anderen Zustand `not_approved`
        # zurueck (`security/mobile_approval/bridge.py`). Das ist dort richtig —
        # es ist ein Ausfuehrungstor, kein Auskunftsdienst —, und die
        # Agentenlaufzeit weiss davon und liest den Zustand deshalb ueber den
        # Kontrollweg (ADR-0028, AGENT_RUNTIME_V1_CONTRACT_DELTA).
        #
        # Der Zahlungsweg tat das nicht, und die Live-Abnahme von DEBT-0126
        # (2026-08-30) hat gezeigt, was das kostet: der Eigentuemer lehnte eine
        # Kauffreigabe ab, der Router las `not_approved`, verwarf die Anfrage und
        # legte SOFORT eine neue an — zweimal hintereinander. Punkt 2 der Schuld
        # verlangt woertlich „eine ABGELEHNTE Zahlungsfreigabe UND IHRE
        # ENDGUELTIGKEIT".
        #
        # Die Zeile steht hier und nicht im eingefrorenen Pfad: sie fragt den
        # Speicher nur, sie aendert nichts an ihm, und `src/solvio/security/`
        # bleibt unberuehrt. Nur DENIED wird unterschieden. Ein Fristablauf ist
        # KEINE Antwort — dort darf weiter neu gefragt werden.
        from solvio.security.mobile_approval import store as S
        if stored["state"] == S.DENIED:
            log.info("capability.approval_denied_is_final", capability=spec.name,
                     approval_id=approval_id)
            return None, "denied"
        expected = approval_digest(spec, arguments, origin_label)
        if stored["action_digest"] != expected:
            log.warning("capability.approval_drift", capability=spec.name)
            return None, "approval_drift"
        if stored["tool"] != spec.name:
            return None, "approval_capability_mismatch"

        async def executor(payload: dict[str, Any]):
            # Der eingefrorene Pfad reicht Ausfuehrungs- und Idempotenzidentitaet
            # mit. Die Faehigkeit selbst bekommt weiterhin nur ihre Argumente —
            # Autoritaet gehoert nicht in einen Handler.
            # Dieselbe Aufrufkonvention wie im Router: synchrone und asynchrone
            # Handler sind beide erlaubt, und es gibt dafuer genau eine Stelle.
            #
            # NEU seit Payment Capability V1: die Identitaet des Vorgangs wandert
            # als KONTEXT mit, nicht als Argument. Sie autorisiert nichts — sie
            # benennt, welcher freigegebene Vorgang gerade laeuft. Eine Zahlung
            # braucht sie, weil eine Wiederholung derselben Ausfuehrungskennung
            # beim Anbieter dieselbe Buchung treffen muss statt eine zweite
            # anzulegen. Ein Handler, der sie nicht liest, merkt nichts davon.
            #
            # Nebeneffekt und laengst faellig: `UseContext.execution_id` war seit
            # dem Tresor deklariert und wurde nie gefuellt. Ab hier traegt die
            # Zugriffsspur des Tresors sie fuer JEDE Faehigkeit.
            # Der Import liegt in der Funktion, nicht oben: der Freigabepfad
            # steht in der Abhaengigkeitsordnung UNTER dem Tresor, und ein
            # Modulimport waere ein Zyklus. Dieselbe Stelle und derselbe Grund
            # wie in `router._secret_context`.
            from solvio.secret_vault import context as SC
            identity = EI.ExecutionIdentity(
                execution_id=str(payload.get("execution_id") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                approval_id=approval_id,
                capability=spec.name,
                semantics=str(payload.get("semantics") or ""),
                action_digest=str(payload.get("action_digest") or ""))
            running = SC.current()
            updates = {"execution_id": identity.execution_id}
            if (spec.name == "background_create" and str(arguments.get("aktion", "")).strip()
                    in ("tagesueberblick", "mail_antwort_pruefen")):
                # A voice transport alias is not the owner of the task ledger.
                # Use the principal on THIS validated, consumed approval. The
                # caller/model cannot choose another owner through arguments.
                owner = stored.get("principal")
                if not isinstance(owner, str) or not owner:
                    from solvio.capabilities.contract import CapabilityRefused
                    raise CapabilityRefused("schedule_owner_unconfirmed", "Der Auftraggeber der Freigabe ist nicht bestätigt.")
                updates["principal"] = owner
            if spec.name in {"agent_task_research", "agent_task_build"}:
                # Der Claim ist schon vor der Anlage gueltig; Erfolg wird erst
                # danach ins Ausfuehrungsjournal geschrieben. Nicht verwechseln.
                from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
                from solvio.agent_runtime.task_start_service import AuthorizedTaskStart
                receipt, state = await self.control_plane.task_start_claim_receipt(
                    approval_id, identity.execution_id)
                if receipt is None:
                    from solvio.capabilities.contract import CapabilityRefused
                    raise CapabilityRefused("task_start_authorization_missing",
                                            "Der gebundene Startbeleg fehlt.")
                method = "dashboard_ok" if receipt["method"] == "dashboard_session" else "face_id"
                proof = VerifiedTaskReceipt(method, receipt["reference"], receipt["authorizer"])
                updates["principal"] = receipt["authorizer"]
                updates["task_start_receipt"] = AuthorizedTaskStart.bind(
                    receipt=proof, request_id="approval-" + approval_id,
                    capability=spec.name, arguments=arguments)
            with EI.bound(identity), SC.bound(dataclasses.replace(
                    running, **updates)):
                data = await _invoke_handler(handler, dict(arguments))
            return True, data

        return await self.coordinator.execute_approved(approval_id, executor)


def labels_are_unambiguous() -> list[str]:
    """Meldet Faehigkeiten, deren Beschriftungen kollidieren.

    Eine Kollision waere kein Schoenheitsfehler: zwei Argumente mit derselben
    Beschriftung koennten zwei verschiedene Aktionen als denselben Text anzeigen.
    """
    broken = []
    for name, (_, labels) in ACTION_LABELS.items():
        if len(set(labels.values())) != len(labels):
            broken.append(name)
    return broken
