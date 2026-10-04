"""Approval Policy V2 — die Entscheidung, WANN eine Freigabe noetig ist.

Bis V1 gab es eine einzige Schwelle: `requires_approval(risk) = risk >= MUTATING`.
Sie kannte die Herkunft eines Auftrags nicht. Derselbe Satz kostete dieselbe
Face-ID-Runde, ob ihn der Besitzer bewusst in sein entsperrtes, registriertes
Telefon sprach oder ob ihn irgendjemand — nachweislich auch ein laufender
Fernseher — ins geteilte Raummikrofon sagte.

V2 ersetzt die Schwelle durch eine deterministische Matrix aus **Herkunft** und
**Aktionsklasse**. Entwurf und Begruendung: `docs/design/approval-policy-v2/`,
Entscheidung: ADR-0022.

DREI BEGRIFFE, DIE NIE ZUSAMMENFALLEN:

    vertrauenswuerdiger Endpunkt  !=  belegter Sprecher  !=  Nutzerautoritaet

Ein Endpunkt beweist, dass DIESES GERAET spricht. Wer davor steht, beweist er
nicht. Nutzerautoritaet entsteht am Geraet mit Face ID und nirgends sonst.

WAS DAS MODELL HIER KANN: nichts. Die Herkunft kommt aus Transportfakten, die
Aktionsklasse aus einer serverseitigen Registry plus einer Klassifikation am
AUFGELOESTEN Ziel. Beides erreicht kein Modelltext. Das Modell darf eine
Handlung anfragen; welche Zelle gilt, entscheidet der Core.

V2 aendert, WANN eine Freigabe noetig ist. Es aendert NICHT, was eine gueltige
Freigabe bedeutet: Digest, Anzeige-Bindung, Einmal-Nonce, Zaehler, TTL,
Terminalitaet einer Ablehnung, Widerruf und Drift-Schutz liegen unveraendert im
eingefrorenen Sicherheitspfad.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from solvio.capabilities.contract import ArgumentSource, worst_source


class OriginClass(str, Enum):
    """Woher ein Auftrag WIRKLICH kommt — aus Transport- und Laufzeitfakten.

    Kein Freitext, keine Modellangabe, keine Selbstauskunft eines Endpunkts.
    Jede Klasse hat genau eine Ableitungsstelle im Core, und jede dieser
    Stellen liegt hinter einer bewiesenen Authentifizierung.
    """

    #: Registriertes, attestiertes, nicht gesperrtes iPhone in einer lebenden
    #: Sprachsitzung, die zusaetzlich eine App-Attest-Sitzungs-Assertion
    #: vorgelegt hat. Das statische Transportgeheimnis allein genuegt NICHT.
    TRUSTED_INTERACTIVE_APP = "trusted_interactive_app"

    #: HTTPS-Browser mit widerrufbarer Sitzung. Ein konkreter Auftragsstart
    #: braucht zusaetzlich den gebundenen Core-Eingangsbeleg im Router.
    TRUSTED_DASHBOARD = "trusted_dashboard"

    #: Raummikrofon (Satellit). Endpunkt bewiesen, Sprecher nicht — hier landet
    #: auch, was der Fernseher sagt.
    ROOM_VOICE = "room_voice"

    #: Der oertliche Control-Socket. Beweist eine uid, keinen Menschen: JEDER
    #: Prozess unter diesem Konto erreicht ihn. Erbt die iPhone-Zeile NICHT.
    LOCAL_OWNER = "local_owner"

    #: Zeitplan, Proaktivlauf, Hintergrundarbeit. Kein anwesender Mensch.
    BACKGROUND_AUTOMATION = "background_automation"

    #: Fremder Inhalt als Herkunft des Turns. Faellt in der Regel schon vorher
    #: an `authority_refusal`; die Zeile schreibt das Verhalten fest.
    EXTERNAL_UNTRUSTED = "external_untrusted"

    #: Sentinel. Ein Core-Pfad, der seine Herkunft nicht gesetzt hat, bekommt
    #: keine Vermutung, sondern die strengste menschlich aufloesbare Zeile —
    #: also genau das V1-Verhalten. Vergessen macht strenger, nie lockerer.
    UNSPECIFIED = "unspecified"


class ActionClass(str, Enum):
    """Was eine Handlung SEMANTISCH bedeutet — nicht, wie sie heisst."""

    READ_ONLY = "read_only"
    HA_NORMAL = "ha_normal"
    HA_SECURITY = "ha_security"
    NORMAL_WRITE = "normal_write"
    CRITICAL = "critical"
    VERY_CRITICAL = "very_critical"

    #: Sentinel: keine Klassifikation vorhanden. Fail-closed.
    UNCLASSIFIED = "unclassified"


class Decision(str, Enum):
    EXECUTE_DIRECTLY = "execute_directly"
    REQUIRE_FACE_ID = "require_face_id"
    DENY = "deny"
    #: Diese Faehigkeit hat einen eigenen, aelteren und engeren Freigabeweg.
    EXISTING_SPECIAL_POLICY = "existing_special_policy"


#: Strenge-Rang. Nur zum Vergleichen — ein Overlay darf nie senken.
_STRICTNESS: dict[Decision, int] = {
    Decision.EXECUTE_DIRECTLY: 0,
    Decision.REQUIRE_FACE_ID: 1,
    Decision.DENY: 2,
}


def stricter(left: Decision, right: Decision) -> Decision:
    """Die strengere der beiden Entscheidungen."""
    return left if _STRICTNESS[left] >= _STRICTNESS[right] else right


_D = Decision.EXECUTE_DIRECTLY
_F = Decision.REQUIRE_FACE_ID
_X = Decision.DENY

#: DIE MATRIX. Jede Zelle eine Konstante, kein Ermessen.
#:
#: Gelockert ist ausschliesslich, was der Nutzer ausdruecklich entschieden hat
#: (Auftrag §6/§7/§9): das Haus bleibt vom Raummikrofon aus bequem, und das
#: bewusst benutzte Telefon fragt nicht bei jeder Kleinigkeit nach. Alles
#: andere ist gleich streng wie V1 oder strenger.
MATRIX: dict[OriginClass, dict[ActionClass, Decision]] = {
    OriginClass.TRUSTED_DASHBOARD: {
        ActionClass.READ_ONLY: _D,
        ActionClass.HA_NORMAL: _F,
        ActionClass.HA_SECURITY: _F,
        ActionClass.NORMAL_WRITE: _F,
        ActionClass.CRITICAL: _F,
        ActionClass.VERY_CRITICAL: _F,
        ActionClass.UNCLASSIFIED: _F,
    },
    OriginClass.TRUSTED_INTERACTIVE_APP: {
        ActionClass.READ_ONLY: _D,
        ActionClass.HA_NORMAL: _D,
        ActionClass.HA_SECURITY: _D,
        ActionClass.NORMAL_WRITE: _D,
        ActionClass.CRITICAL: _D,
        ActionClass.VERY_CRITICAL: _F,
        ActionClass.UNCLASSIFIED: _F,
    },
    OriginClass.ROOM_VOICE: {
        ActionClass.READ_ONLY: _D,
        ActionClass.HA_NORMAL: _D,
        ActionClass.HA_SECURITY: _F,
        ActionClass.NORMAL_WRITE: _F,
        ActionClass.CRITICAL: _F,
        ActionClass.VERY_CRITICAL: _F,
        ActionClass.UNCLASSIFIED: _F,
    },
    OriginClass.LOCAL_OWNER: {
        ActionClass.READ_ONLY: _D,
        ActionClass.HA_NORMAL: _D,
        ActionClass.HA_SECURITY: _F,
        ActionClass.NORMAL_WRITE: _F,
        ActionClass.CRITICAL: _F,
        ActionClass.VERY_CRITICAL: _F,
        ActionClass.UNCLASSIFIED: _F,
    },
    OriginClass.BACKGROUND_AUTOMATION: {
        ActionClass.READ_ONLY: _D,
        # Ohne gueltige, gebundene Vorab-Autorisierung. Mit ihr wird daraus
        # EXECUTE_DIRECTLY — siehe `decide()`; die Lockerung gilt AUSSCHLIESSLICH
        # fuer diese eine Zelle.
        ActionClass.HA_NORMAL: _F,
        ActionClass.HA_SECURITY: _F,
        ActionClass.NORMAL_WRITE: _F,
        ActionClass.CRITICAL: _F,
        # Ein Zeitplan hat keinen legitimen Grund, „loesch alles" zur
        # Entsperrung vorzulegen.
        ActionClass.VERY_CRITICAL: _X,
        ActionClass.UNCLASSIFIED: _F,
    },
    OriginClass.EXTERNAL_UNTRUSTED: {
        ActionClass.READ_ONLY: _D,
        ActionClass.HA_NORMAL: _X,
        ActionClass.HA_SECURITY: _X,
        ActionClass.NORMAL_WRITE: _X,
        ActionClass.CRITICAL: _X,
        ActionClass.VERY_CRITICAL: _X,
        ActionClass.UNCLASSIFIED: _X,
    },
    OriginClass.UNSPECIFIED: {
        ActionClass.READ_ONLY: _D,
        ActionClass.HA_NORMAL: _F,
        ActionClass.HA_SECURITY: _F,
        ActionClass.NORMAL_WRITE: _F,
        ActionClass.CRITICAL: _F,
        ActionClass.VERY_CRITICAL: _F,
        ActionClass.UNCLASSIFIED: _F,
    },
}


#: Faehigkeiten mit eigenem, aelterem und ENGEREM Freigabeweg. Abschliessend,
#: nicht konfigurierbar, nicht erweiterbar ohne Codeaenderung.
#:
#: `memory_remember` traegt den turn-gebundenen Einmal-Permit der
#: MemoryIntentGate (finalisiertes Nutzertranskript derselben Runde). Es laeuft
#: heute nicht ueber den Router; steht hier, damit es nie versehentlich eine
#: Matrixzelle bekommt.
#: `system_heal` nimmt keine Argumente — seine Autoritaet ist die geschlossene
#: Playbook-Liste, nicht ein zweiter Freigabeweg.
SPECIAL_POLICY: frozenset[str] = frozenset({"memory_remember", "system_heal"})

#: Nachrichten, die das Haus verlassen. Aus JEDER Herkunft mindestens Face ID —
#: auch vom Telefon (Owner-Entscheidung 26.09.2026, ADR-0041). Die V2-Zelle
#: TRUSTED_INTERACTIVE_APP x CRITICAL bleibt fuer alles andere, wie sie ist; hier
#: wird nur verschaerft. Der Grund ist gemessen: ein Empfaenger, den das Modell
#: aus einer gelesenen Mail uebernimmt, gilt als MODEL_DERIVED, nicht als fremd
#: — das Fremdinhalts-Overlay greift dann nicht, und ohne diese Regel ginge die
#: Mail vom Telefon aus ungesehen hinaus.
OUTBOUND_MESSAGE_CAPABILITIES: frozenset[str] = frozenset({
    "gmail_send_draft", "communication_send"})


#: DIE KLASSENREGISTRY. Serverseitig, statisch, vom Modell unerreichbar.
#:
#: Wer eine Faehigkeit hinzufuegt und den Eintrag vergisst, bezahlt mit
#: Sicherheit statt mit Stille: `UNCLASSIFIED` heisst Face ID aus jeder
#: Herkunft und DENY aus fremdem Inhalt. Dasselbe Prinzip wie die eingefrorene
#: `CAPABILITY_SEMANTICS`.
#:
#: Lesende Faehigkeiten stehen hier NICHT: ihre Klasse faellt aus
#: `spec.is_read_only()` und kann so nicht auseinanderlaufen.
ACTION_CLASS: dict[str, ActionClass] = {
    # -- Home Assistant. Die endgueltige Klasse entscheidet der Verfeinerer am
    # aufgeloesten Geraet; das hier ist die Untergrenze fuer den Fall, dass
    # keine Aufloesung moeglich war.
    "ha_turn_on": ActionClass.HA_NORMAL,
    "ha_turn_off": ActionClass.HA_NORMAL,
    "ha_set_brightness": ActionClass.HA_NORMAL,
    # -- Gewoehnliches Schreiben: ein Objekt, gutartig umkehrbar.
    "calendar_create_event": ActionClass.NORMAL_WRITE,
    "calendar_update_event": ActionClass.NORMAL_WRITE,
    "gmail_create_draft": ActionClass.NORMAL_WRITE,
    "memory_confirm_candidate": ActionClass.NORMAL_WRITE,
    "memory_decline_candidate": ActionClass.NORMAL_WRITE,
    "memory_correct": ActionClass.NORMAL_WRITE,
    "memory_forget": ActionClass.NORMAL_WRITE,
    "background_create": ActionClass.NORMAL_WRITE,
    "background_pause": ActionClass.NORMAL_WRITE,
    "background_resume": ActionClass.NORMAL_WRITE,
    "background_delete": ActionClass.NORMAL_WRITE,
    "background_require_approval": ActionClass.NORMAL_WRITE,
    "background_run_now": ActionClass.NORMAL_WRITE,
    "proactive_mark_read": ActionClass.NORMAL_WRITE,
    # Address-book observations are data only; identity confirmation remains
    # VERY_CRITICAL_BY_BIRTH and is never performed by this import.
    "communication_import_contacts": ActionClass.NORMAL_WRITE,
    # -- Agentenlaufzeit. Die ERZEUGUNG autonomer Arbeit ist eine Schreibhandlung,
    # auch wenn der Lauf danach nur liest: eine READ_ONLY-Einstufung liesse
    # `authority_refusal` und selbst `EXTERNAL_UNTRUSTED × READ_ONLY` passieren.
    # Praezedenzfall ist `background_create` — dieselbe Frage, dieselbe Antwort.
    # Lesen und Abbrechen bleiben READ_ONLY (SOLVIO-interne Buchhaltung, Muster
    # `deep_cancel`); die Wiederaufnahme schreibt eigenen Zustand.
    # `agent_run_status` und `agent_run_cancel` stehen hier ABSICHTLICH NICHT:
    # Lesen wird aus der serverseitigen Spec abgeleitet, nicht gepflegt. Zwei
    # getrennte Wahrheiten ueber dieselbe Faehigkeit laufen frueher oder spaeter
    # auseinander — dieselbe Regel, aus der auch `deep_cancel` fehlt.
    "agent_task_research": ActionClass.NORMAL_WRITE,
    "agent_task_task": ActionClass.NORMAL_WRITE,
    "agent_task_action": ActionClass.NORMAL_WRITE,
    # Concrete effect stays limited by the authenticated action resource.
    # A generic call without that resource never reaches this service.
    "task_service_action": ActionClass.NORMAL_WRITE,
    "agent_task_build": ActionClass.NORMAL_WRITE,
    "agent_run_resume": ActionClass.NORMAL_WRITE,
    # -- Folgenreich oder schwer umkehrbar, aber begrenzt auf ein Objekt.
    "gmail_send_draft": ActionClass.CRITICAL,
    # Eine Nachricht verlaesst das Haus — dieselbe Klasse wie der Mailversand,
    # den sie benutzt. Ohne diese Zeile faellt sie auf `unclassified`: das ist
    # fail-closed und verlangt Face ID, nennt aber den falschen Grund. Der
    # Nutzer erfaehrt dann „nicht klassifiziert" statt „das ist folgenreich",
    # und die Zeile, die es haette sagen sollen, fehlt still.
    "communication_send": ActionClass.CRITICAL,
    # `communication_confirm_binding` steht hier NICHT mehr. Bis Contact
    # Binding Authority Hardening V1 stand es als CRITICAL an dieser Stelle —
    # und die Zeile fuer die vertraute App laesst CRITICAL direkt laufen. Wer
    # festlegt, wer „mein Sohn" ist, legt fest, wohin JEDE kuenftige Nachricht
    # und JEDER kuenftige Anruf geht; das ist Identitaetswahrheit und gehoert in
    # die Geburtsregel unten (DEBT-0208). Ein Eintrag hier waere seither tot —
    # die Geburtsregel wird zuerst gefragt — und eine zweite Wahrheit ueber
    # dieselbe Faehigkeit.
    # -- Zahlung, aber noch ohne Geldbewegung.
    #
    # Vorbereiten heisst: Betrag beim Anbieter erfragen, Absicht anlegen,
    # vorlegen. Es bewegt keinen Cent und traegt keine Befugnis. Vom bewusst
    # benutzten iPhone laeuft es deshalb direkt — sonst kostete ein Kauf zwei
    # Face-ID-Runden, und die zweite bekaeme der Mensch fuer etwas, das nichts
    # tut. Aus dem Raum, vom Rechner und aus dem Hintergrund bleibt es
    # biometrisch, aus fremdem Inhalt verweigert.
    #
    # Nachsehen, ob eine unklare Zahlung durchging, ist lesend gegenueber dem
    # Anbieter — es belastet nie. Es steht trotzdem hier und nicht bei den
    # lesenden Faehigkeiten, weil es an einen Anbieterzugang reicht.
    "payment_intent_prepare": ActionClass.CRITICAL,
    "payment_intent_cancel": ActionClass.NORMAL_WRITE,
    "payment_reconcile": ActionClass.CRITICAL,
    # Ein Zahlungsmittel zu SPERREN oder eine Grenze zu SENKEN ist die sichere
    # Richtung — wer eine verlorene Karte schnell totlegen kann, ist besser dran
    # als einer, der dafuer erst eine Face-ID-Runde braucht. Alles, was Befugnis
    # WIEDERHERSTELLT oder ERWEITERT, steht unten unter `VERY_CRITICAL_BY_BIRTH`.
    "payment_method_disable": ActionClass.CRITICAL,
    "payment_limit_lower": ActionClass.CRITICAL,
    # Stornieren und Erstatten sind AUSDRUECKLICH nicht `VERY_CRITICAL`, und das
    # ist eine Entscheidung, keine Nachlaessigkeit (§28 des Auftrags verlangt,
    # dass sie ausgesprochen wird):
    #
    # Beide koennen strukturell kein Geld VOM Eigentuemer wegbewegen. Eine
    # Erstattung geht ausschliesslich auf DASSELBE Zahlungsmittel zurueck — es
    # gibt kein Feld fuer ein Ziel, also auch keinen Missbrauch daran. Ein Storno
    # nimmt eine Bestellung zurueck.
    #
    # Und sie teuer zu machen, waere gegen den Nutzer gerichtet: das sind genau
    # die zwei Handlungen, mit denen ein Mensch einen Fehlkauf begrenzt. Wer
    # dafuer erst Face ID braucht, storniert zu spaet. Dieselbe Ueberlegung wie
    # bei `secret_disable`.
    "purchase_cancel": ActionClass.CRITICAL,
    "refund_request": ActionClass.CRITICAL,
    "calendar_delete_event": ActionClass.CRITICAL,
    "memory_purge": ActionClass.CRITICAL,
    "portal_login": ActionClass.CRITICAL,
    # Einen Zugang zu SPERREN ist die sichere Richtung. Wer einen verlorenen
    # Schluessel schnell totlegen kann, ist besser dran als einer, der dafuer
    # erst eine Face-ID-Runde braucht. Alles, was Befugnis WIEDERHERSTELLT oder
    # ERWEITERT, steht dagegen unten unter `VERY_CRITICAL_BY_BIRTH`.
    "secret_disable": ActionClass.CRITICAL,
    # -- Eng definiert. Herkunft erlaesst hier nie die Biometrie.
    #
    # `codex_modify` autorisiert eine exakte INSTRUKTION, nicht einen geprueften
    # Diff — das sagt das eingefrorene Freigabeprotokoll unter „Semantic limit"
    # selbst. Eine unueberprueft wirkende Systemmutation gehoert nicht in die
    # Direkt-Zelle eines Telefons.
    "codex_modify": ActionClass.VERY_CRITICAL,
    "codex_task": ActionClass.VERY_CRITICAL,
}

#: Kuenftige Faehigkeiten, die als VERY_CRITICAL GEBOREN werden muessen.
#:
#: Keine davon existiert heute. Der Satz steht hier, damit die Klasse schon da
#: ist, bevor die Faehigkeit es ist — und damit ein Test sie festnageln kann,
#: ohne dass irgendwo Geld bewegt werden koennte. Jede tatsaechliche
#: Geldbewegung oder vergleichbare finanzielle Bindung gehoert hierher.
VERY_CRITICAL_BY_BIRTH: frozenset[str] = frozenset({
    "memory_purge_all", "memory_wipe", "memory_factory_reset",
    "solvio_reset", "solvio_wipe",
    "approval_policy_set", "trust_policy_set", "security_disable",
    "device_revoke", "device_remove", "identity_change",
    "credentials_export", "secret_reveal",
    "payment_send", "bank_transfer", "purchase_place", "invest_order",
    # Die Zahlungsmittelverwaltung. Anlegen, Ersetzen, Wieder-Freigeben,
    # Umwidmen, Loeschen und jede ERHOEHUNG einer Grenze sind aus JEDER Herkunft
    # biometrisch und aus einem Zeitplan verweigert — ein Zeitplan hat keinen
    # legitimen Grund, eine Zahlungsbefugnis zu erweitern. Wortgleich zur
    # Tresor-Zeile darunter, und aus demselben Grund.
    "payment_method_add", "payment_method_replace", "payment_method_remove",
    "payment_method_enable", "payment_method_rescope", "payment_limit_raise",
    # Der Geheimnistresor. Anlegen, Ersetzen, Loeschen, Wieder-Freigeben und
    # Umwidmen sind aus JEDER Herkunft biometrisch und aus einem Zeitplan
    # verweigert — ein Zeitplan hat keinen legitimen Grund, Zugangsdaten zu
    # aendern. `secret_reveal` und `credentials_export` stehen weiterhin hier
    # OHNE dass es sie gibt: die Klasse ist vor der Faehigkeit da, damit ein
    # Test sie festnageln kann, und dieser Milestone hat bewusst keine davon
    # gebaut.
    "secret_add", "secret_replace", "secret_delete", "secret_enable", "google_connect", "native_credit_consent",
    "secret_rescope", "vault_reset", "vault_recovery_configure",
    # Der Telefonanruf (Telephony Capability V1). Er steht hier und NICHT neben
    # `communication_send`, obwohl beides "SOLVIO meldet sich bei jemandem" ist.
    # Der Grund steht in der Matrix oben: `TRUSTED_INTERACTIVE_APP x CRITICAL`
    # ist EXECUTE_DIRECTLY — eine als CRITICAL eingestufte Faehigkeit liefe vom
    # iPhone aus OHNE Face ID. Fuer eine E-Mail ist das die bewusst gewaehlte
    # Bequemlichkeit des Eigentuemers. Ein Anruf ist etwas anderes: er erreicht
    # einen Menschen unmittelbar, er laesst sich nicht zurueckziehen, und er
    # kostet je Minute Geld.
    #
    # Die Geburtsregel bringt drei Zusagen auf einmal, und alle drei sind
    # ausdruecklicher Auftrag: Face ID aus JEDER Herkunft; DENY aus
    # BACKGROUND_AUTOMATION, womit kein Zeitplan und kein Wiederholungslauf je
    # anrufen kann; und DENY aus EXTERNAL_UNTRUSTED, womit ein Satz aus einer
    # Mail oder einem Gespraechstranskript niemals ein Telefonat ausloest.
    "telephony_call",
    # Die Kontaktbindung (Contact Binding Authority Hardening V1, DEBT-0208).
    #
    # Sie sitzt VOR jeder Faehigkeit, die einen Menschen erreicht: wer „mich",
    # „Gregor" oder „mein Sohn" auf einen anderen Kontaktweg umhaengt, lenkt
    # damit jeden kuenftigen Anruf und jede kuenftige Nachricht um — auch die,
    # deren Freigabetext den Empfaenger NICHT zeigt. Als CRITICAL lief die
    # Umbindung vom iPhone aus ohne Face ID (gemessen: TRUSTED_INTERACTIVE_APP x
    # CRITICAL = EXECUTE_DIRECTLY); die angezeigte Rufnummer des Anrufs war die
    # einzige Schranke. Eine Identitaetsaenderung verdient dieselbe starke
    # Eigentuemer-Autoritaet wie der Anruf selbst: Face ID aus JEDER Herkunft,
    # DENY aus einem Zeitplan, DENY aus fremdem Inhalt. Und kein Modell kann
    # eine Bindung selbst bestaetigen — es darf anfragen, entscheiden tut das
    # Geraet.
    "communication_confirm_binding",
})


#: Wieviel Reibung eine Klasse ueber die ganze Matrix hinweg bedeutet. NUR zum
#: Vergleichen — die Zellen selbst stehen oben und werden hiervon nicht
#: abgeleitet.
_CLASS_RANK: dict[ActionClass, int] = {
    ActionClass.READ_ONLY: 0,
    ActionClass.HA_NORMAL: 1,
    ActionClass.HA_SECURITY: 2,
    ActionClass.NORMAL_WRITE: 2,
    ActionClass.CRITICAL: 2,
    ActionClass.UNCLASSIFIED: 3,
    ActionClass.VERY_CRITICAL: 4,
}

#: Was eine Faehigkeit ueber SICH SELBST erklaert, ist eine Untergrenze.
#:
#: Der Grund steht in einer Zusicherung, die diesen Fall gefunden hat: eine
#: Faehigkeit namens `ha_turn_off`, die sich selbst als CRITICAL deklariert,
#: darf nicht deshalb zu gewoehnlicher Haustechnik werden, weil ihr NAME in der
#: Registry so steht. Die Registry ordnet ein; sie hebt keine Selbsterklaerung
#: auf. Wer strenger ist, bleibt strenger.
_RISK_FLOOR: dict[int, ActionClass] = {
    2: ActionClass.NORMAL_WRITE,   # RiskLevel.MUTATING
    3: ActionClass.CRITICAL,       # RiskLevel.CRITICAL
}


def apply_floor(action_class: ActionClass, base_risk: int) -> ActionClass:
    """Hebt eine Klasse auf das an, was die Faehigkeit selbst erklaert hat."""
    floor = _RISK_FLOOR.get(int(base_risk))
    if floor is None:
        return action_class
    if _CLASS_RANK[action_class] >= _CLASS_RANK[floor]:
        return action_class
    return floor


@dataclass(frozen=True)
class Classification:
    """Das Ergebnis der Core-eigenen Klassifikation EINES Aufrufs."""

    action_class: ActionClass
    #: Die AUFGELOESTEN Ziele (z. B. HA-`entity_id`). Gehen in die Bindung einer
    #: Vorab-Autorisierung ein — deshalb sortiert und vollstaendig.
    targets: tuple[str, ...] = ()
    #: Warum diese Klasse. Nur fuers Journal, nie fuer eine Entscheidung.
    reason: str = ""


def base_class(name: str, *, read_only: bool) -> ActionClass:
    """Die Klasse aus Name und Spec — ohne Kenntnis der Argumente.

    Lesen wird aus der Spec abgeleitet und nicht gepflegt: eine neue lesende
    Faehigkeit ist damit automatisch richtig eingeordnet, und eine schreibende
    kann sich nicht als lesend tarnen (die Spec ist serverseitig).
    """
    # DIE GEBURTSREGEL ZUERST — und das ist kein Schoenheitsfehler in der
    # Reihenfolge, sondern die Antwort auf einen echten Fund.
    #
    # Die Ausfuehrungssemantik beschreibt die AUSSENWIRKUNG: ob eine Wiederholung
    # gefahrlos ist. Zugangsdaten herausgeben oder ein Geheimnis anzeigen ist
    # danach `READ_ONLY` — es veraendert nichts in der Welt. Es waere damit aus
    # JEDER Herkunft direkt gelaufen, auch aus fremdem Inhalt.
    #
    # Lesen ist eben nicht dasselbe wie harmlos. Was einmal draussen ist, ist
    # draussen; die Umkehrbarkeit einer Handlung sagt nichts ueber ihren Schaden.
    # Deshalb schlaegt die Geburtsregel die Selbstdeklaration.
    if name in VERY_CRITICAL_BY_BIRTH:
        return ActionClass.VERY_CRITICAL
    if read_only:
        return ActionClass.READ_ONLY
    return ACTION_CLASS.get(name, ActionClass.UNCLASSIFIED)


def has_untrusted_argument(provenance) -> bool:
    """Stammt mindestens EIN Argument aus fremdem Inhalt?

    Gemessen, nicht behauptet: die Provenienz entsteht im Core durch Abgleich
    gegen das Transkript des Turns (`CapabilityInvocationGate.provenance_for`).
    """
    if not provenance:
        return False
    return worst_source(provenance.values()) is ArgumentSource.UNTRUSTED_CONTENT


@dataclass(frozen=True)
class PolicyOutcome:
    decision: Decision
    origin: OriginClass
    action_class: ActionClass
    reason_code: str
    #: Gesetzt, wenn eine gebundene Vorab-Autorisierung die Zelle geoeffnet hat.
    preauthorization_id: str = ""

    @property
    def needs_approval(self) -> bool:
        return self.decision is Decision.REQUIRE_FACE_ID


def decide(origin: OriginClass, action_class: ActionClass, *,
           capability: str = "", provenance=None, commanded: bool = True,
           preauthorization_id: str = "") -> PolicyOutcome:
    """Die eine Entscheidung. Deterministisch, ohne Ermessen, ohne Modell.

    Reihenfolge, und sie ist nicht beliebig:

    1. Sonderweg — eine Faehigkeit mit eigenem, engerem Freigabeweg bekommt
       keine Matrixzelle.
    2. Die Zelle.
    3. Fremdinhalts-Overlay — verschaerft, nie lockernd. Greift es, ist hier
       Schluss: eine Vorab-Autorisierung kann es nicht aufheben.
    4. Die Vorab-Autorisierung — die EINZIGE Lockerung, und sie gilt nur fuer
       BACKGROUND_AUTOMATION x HA_NORMAL.

    `preauthorization_id` ist bereits GEPRUEFT, wenn es hier ankommt: der Router
    hat den Digest gegen die tatsaechlich anstehende Wirkung neu gerechnet. Die
    Kennung allein oeffnet nichts — die Bedingungen unten stehen zusaetzlich.
    """
    if capability in SPECIAL_POLICY:
        return PolicyOutcome(Decision.EXISTING_SPECIAL_POLICY, origin,
                             action_class, "special_policy")

    row = MATRIX.get(origin) or MATRIX[OriginClass.UNSPECIFIED]
    decision = row.get(action_class, _F)
    reason = "matrix_cell"

    # -- 2a. Was das Haus verlaesst, sieht der Mensch vorher (ADR-0041) -------
    #
    # Verschaerft nur: aus EXECUTE_DIRECTLY wird REQUIRE_FACE_ID, ein DENY
    # bleibt DENY. Die folgenden Overlays koennen nur noch strenger werden.
    if capability in OUTBOUND_MESSAGE_CAPABILITIES and decision is Decision.EXECUTE_DIRECTLY:
        decision = Decision.REQUIRE_FACE_ID
        reason = "outbound_message_face_id"

    # -- 2b. War es ueberhaupt ein Auftrag? ---------------------------------
    #
    # Das ist NICHT dieselbe Frage wie „hat das Modell das Ziel gewaehlt".
    # V1 hat beides in einer Regel gefuehrt, und V2 hat sie beim ersten Entwurf
    # zusammen weggenommen — womit „Ist das Flur Licht aus?" das Licht
    # geschaltet haette, und „In einer E-Mail steht: mach das Licht an" auch.
    # Die bestehenden Zusicherungen haben das gefangen; die Regel steht seither
    # getrennt, denn nur eine der beiden Fragen darf Reibung sparen.
    #
    # Wer sagt „mach es dunkel", hat beauftragt — das Modell darf die Lampe
    # dazu waehlen, und das laeuft direkt. Wer fragt, spielt durch oder zitiert
    # jemanden, hat nicht beauftragt: dann gibt es keine Direktausfuehrung.
    if action_class is not ActionClass.READ_ONLY and not commanded:
        decision = stricter(decision, row.get(ActionClass.CRITICAL, _F))
        if decision is Decision.EXECUTE_DIRECTLY:
            decision = Decision.REQUIRE_FACE_ID
        return PolicyOutcome(decision, origin, action_class, "turn_not_a_command")

    # -- 3. Fremder Inhalt kann informieren, nie ausloesen -------------------
    #
    # Lesen eskaliert nicht: ohne Aussenwirkung gibt es nichts zu bestaetigen.
    # Sonst gilt zusaetzlich die CRITICAL-Zelle derselben Herkunft, und eine
    # Direktausfuehrung ist ausgeschlossen — auch vom Telefon aus. Erst die
    # Anzeige auf dem Geraet und Face ID machen fremden Inhalt zu einem
    # Nutzerakt.
    if action_class is not ActionClass.READ_ONLY and has_untrusted_argument(provenance):
        decision = stricter(decision, row.get(ActionClass.CRITICAL, _F))
        if decision is Decision.EXECUTE_DIRECTLY:
            decision = Decision.REQUIRE_FACE_ID
        return PolicyOutcome(decision, origin, action_class,
                             "untrusted_argument_overlay")

    # -- 4. Die eine Lockerung ----------------------------------------------
    #
    # „Mach jeden Abend um 20 Uhr das Aussenlicht an" soll nicht jeden Abend
    # eine Face-ID-Runde kosten. Sie gilt ausschliesslich fuer diese Zelle:
    # nicht fuer HA_SECURITY, nicht fuer VERY_CRITICAL, nicht fuer irgendeine
    # andere Herkunft — strukturell, nicht per Konfiguration.
    if (preauthorization_id
            and origin is OriginClass.BACKGROUND_AUTOMATION
            and action_class is ActionClass.HA_NORMAL):
        return PolicyOutcome(Decision.EXECUTE_DIRECTLY, origin, action_class,
                             "bounded_preauthorization", preauthorization_id)

    if reason == "outbound_message_face_id":
        pass
    elif action_class is ActionClass.UNCLASSIFIED:
        reason = "unclassified_action"
    elif origin is OriginClass.UNSPECIFIED:
        reason = "origin_unspecified"
    return PolicyOutcome(decision, origin, action_class, reason)


#: Kanal -> Herkunft. Genau die zwei Kanaele, die der Core kennt; alles andere
#: faellt auf den Sentinel. Der Kanal selbst ist Transportwahrheit: er wird an
#: genau zwei Stellen gesetzt (Session-Default und `voice_endpoint` nach der
#: bewiesenen Geraetepruefung) und ist fuer kein Modell erreichbar.
_CHANNEL_ORIGIN: dict[str, OriginClass] = {
    "voice_iphone": OriginClass.TRUSTED_INTERACTIVE_APP,
    "voice_satellite": OriginClass.ROOM_VOICE,
}


def origin_for_session(channel: str, *, interactive_proof: bool,
                       browser_task_session=None, session_id: str = "") -> OriginClass:
    """Die Herkunft einer Sprachsitzung.

    `interactive_proof` ist die App-Attest-Sitzungs-Assertion. Ohne sie waere
    die reduzierte iPhone-Zeile nur ein statisches Bearer-Geheimnis wert — wer
    es besitzt, koennte sonst ohne Face ID die Haustuer oeffnen. Fehlt der
    Beweis, faellt die Sitzung auf die Raum-Zeile zurueck: voll benutzbar, aber
    mit genau dem Face-ID-Verhalten von V1.

    Und ausdruecklich: dieser Beweis ist NICHT Face ID. Er zeigt Geraet, echte
    App und eine bewusst geoeffnete Sitzung — keine biologische Identitaet.
    Deshalb bleibt VERY_CRITICAL auch hier biometrisch.
    """
    if channel == "voice_browser":
        from solvio.browser_voice_session import VerifiedBrowserTaskSession
        if (type(browser_task_session) is VerifiedBrowserTaskSession
                and browser_task_session.session_id == session_id and browser_task_session.live()):
            return OriginClass.TRUSTED_DASHBOARD
        return OriginClass.UNSPECIFIED
    origin = _CHANNEL_ORIGIN.get(channel or "", OriginClass.UNSPECIFIED)
    if origin is OriginClass.TRUSTED_INTERACTIVE_APP and not interactive_proof:
        return OriginClass.ROOM_VOICE
    return origin


#: Wie eine Herkunft auf dem iPhone heisst. Sie steht im gerenderten
#: Freigabetext und ist damit SIGNIERT, ANGEZEIGT und im Digest gebunden — ohne
#: ein einziges neues Protokollfeld. Der Mensch sieht nicht nur, WAS freigegeben
#: werden soll, sondern auch, von wo aus gefragt wurde.
#:
#: Nebenwirkung, und eine erwuenschte: eine Anfrage aus dem Raum und eine aus
#: dem Hintergrund tragen verschiedene Texte und koennen einander deshalb nicht
#: mehr einloesen.
ORIGIN_LABEL: dict[OriginClass, str] = {
    OriginClass.TRUSTED_DASHBOARD: "SOLVIO Dashboard",
    OriginClass.TRUSTED_INTERACTIVE_APP: "iPhone-App",
    OriginClass.ROOM_VOICE: "Raum-Mikrofon",
    OriginClass.LOCAL_OWNER: "Rechner",
    OriginClass.BACKGROUND_AUTOMATION: "Hintergrundlauf",
    OriginClass.EXTERNAL_UNTRUSTED: "fremder Inhalt",
    OriginClass.UNSPECIFIED: "unbekannt",
}


def origin_label(origin: OriginClass) -> str:
    return ORIGIN_LABEL.get(origin, ORIGIN_LABEL[OriginClass.UNSPECIFIED])
