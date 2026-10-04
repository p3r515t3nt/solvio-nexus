"""Warum ein Weg blockiert ist — und ob das ueberhaupt eine Luecke ist.

Die wichtigste Unterscheidung dieses Moduls ist nicht technisch, sondern
inhaltlich: **eine verlangte Freigabe ist kein Mangel.** Wer beides in einen Topf
wirft, baut sich unweigerlich eine Maschine, die nach einem Weg an Face ID vorbei
sucht — nicht aus boesem Willen, sondern weil ihr Ziel „Hindernis beseitigen"
heisst und ein Hindernis genau so aussieht.

Deshalb steht die Trennung hier ganz vorne und nicht im Planer: `AUTHORITY_REQUIRED`
und `HUMAN_ACTION_REQUIRED` sind **Ergebnisse**, keine Probleme. Der Planer bekommt
sie gar nicht erst zum Loesen vorgelegt; er bekommt den Auftrag, den kuerzesten
zulaessigen Weg **zu** dieser menschlichen Grenze zu finden.

Ebenso `POLICY_HARD_STOP`. Eine Policy ist kein Defekt, den man wegforscht. Was
erlaubt ist: einen anderen, regelkonformen Weg zum selben Ziel suchen. Was nicht
erlaubt ist: einen Weg um die Regel herum. Der Unterschied ist im Code als
`may_seek_alternative` / `may_seek_bypass` abgebildet, damit er nicht bloss in
einem Kommentar steht.

Die Taxonomie ist bewusst klein. Zehn Arten, keine Ontologie.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from solvio.capabilities.envelope import CapabilityOutcome


class GapKind(str, Enum):
    """Zehn Arten, blockiert zu sein. Mehr braucht V1 nicht."""

    #: Es gibt keinen Ausfuehrenden fuer diese Operation. Die echte Luecke.
    CAPABILITY_MISSING = "capability_missing"

    #: Die Faehigkeit gibt es — sie braucht die Zustimmung des Nutzers.
    #: KEIN Mangel. Der bestehende Freigabeweg ist die Antwort.
    AUTHORITY_REQUIRED = "authority_required"

    #: Ein Mensch muss etwas tun: Passwort, Face ID, 2FA, Admin-Dialog, Hand ans
    #: Geraet. Ebenfalls kein Mangel.
    HUMAN_ACTION_REQUIRED = "human_action_required"

    #: Software, Paket oder Laufzeit fehlt auf dem Zielsystem.
    DEPENDENCY_MISSING = "dependency_missing"

    #: Anbieter oder Geraet existiert, ist aber nicht verbunden/eingerichtet.
    CREDENTIAL_OR_CONNECTION_MISSING = "credential_or_connection_missing"

    #: Bekanntes Ziel, gerade nicht erreichbar. Voruebergehend.
    DEVICE_OR_SERVICE_UNAVAILABLE = "device_or_service_unavailable"

    #: Externer Dienst gerade nicht verfuegbar oder Kontingent erschoepft.
    PROVIDER_OR_QUOTA_UNAVAILABLE = "provider_or_quota_unavailable"

    #: Die Faehigkeit gibt es, diese Spielart beherrscht sie noch nicht.
    UNSUPPORTED_VARIANT = "unsupported_variant"

    #: Eine echte Sicherheits- oder Policy-Grenze. Kein Umweg wird gesucht.
    POLICY_HARD_STOP = "policy_hard_stop"

    #: Erst NACH der Untersuchung: kein praktikabler unterstuetzter Weg.
    TECHNICALLY_IMPOSSIBLE = "technically_impossible"


@dataclass(frozen=True)
class GapRules:
    """Was bei dieser Art gesucht werden darf — und was ausdruecklich nicht."""

    #: Darf nach einem anderen, zulaessigen Weg zum selben Ziel gesucht werden?
    may_seek_alternative: bool
    #: Ist das ein Mangel an Faehigkeit (statt einer offenen Zustimmung)?
    is_true_gap: bool
    #: Loest sich das voraussichtlich von selbst — Wiederholen statt Bauen?
    is_transient: bool
    #: Ist die einzige fehlende Zutat ein Mensch?
    needs_human: bool
    #: Wuerde neue Faehigkeit hier ueberhaupt helfen?
    #:
    #: Ausdruecklich ein eigenes Feld statt einer Ableitung. Abgeleitet ergaebe
    #: `TECHNICALLY_IMPOSSIBLE` einen Aenderungsvorschlag — fuer etwas, das per
    #: Definition keinen Weg hat. Ein Vorschlag fuer das Unmoegliche ist die
    #: gefaehrlichste Ausgabe dieses Systems, weil er beschaeftigt aussieht.
    proposal_helps: bool

    @property
    def may_propose_capability(self) -> bool:
        """Nur eine echte Luecke rechtfertigt einen Aenderungsvorschlag.

        Eine Stoerung ist kein Grund, Code zu entwerfen, und eine ausstehende
        Freigabe erst recht nicht. Ohne diese Regel produziert das System
        Entwicklungsvorschlaege fuer einen Serverausfall.
        """
        return (self.is_true_gap and self.proposal_helps
                and not self.is_transient and not self.needs_human)


#: Es gibt bewusst KEINE Art mit `may_seek_bypass`. Das Feld existiert nicht,
#: damit es niemand versehentlich auf True setzt.
RULES: dict[GapKind, GapRules] = {
    #                                            alternativ  echte  vor-    braucht  Vorschlag
    #                                            suchen?     Luecke uebergehend Mensch  hilft?
    GapKind.CAPABILITY_MISSING:               GapRules(True,  True,  False, False, True),
    GapKind.AUTHORITY_REQUIRED:               GapRules(False, False, False, True,  False),
    GapKind.HUMAN_ACTION_REQUIRED:            GapRules(False, False, False, True,  False),
    GapKind.DEPENDENCY_MISSING:               GapRules(True,  True,  False, False, True),
    GapKind.CREDENTIAL_OR_CONNECTION_MISSING: GapRules(True,  False, False, True,  False),
    GapKind.DEVICE_OR_SERVICE_UNAVAILABLE:    GapRules(True,  False, True,  False, False),
    GapKind.PROVIDER_OR_QUOTA_UNAVAILABLE:    GapRules(True,  False, True,  False, False),
    GapKind.UNSUPPORTED_VARIANT:              GapRules(True,  True,  False, False, True),
    GapKind.POLICY_HARD_STOP:                 GapRules(True,  False, False, False, False),
    GapKind.TECHNICALLY_IMPOSSIBLE:           GapRules(False, True,  False, False, False),
}


def rules_for(kind: GapKind) -> GapRules:
    return RULES[kind]


#: Ausgang -> Art. Die Zuordnung ist absichtlich stur: sie liest den Umschlag,
#: nicht die Absicht. Ein Modell darf die Art NICHT waehlen — sonst waere die
#: bequemste Einstufung immer die, die den meisten Spielraum laesst.
_BY_OUTCOME: dict[CapabilityOutcome, GapKind] = {
    CapabilityOutcome.APPROVAL_REQUIRED: GapKind.AUTHORITY_REQUIRED,
    CapabilityOutcome.REJECTED_BY_POLICY: GapKind.POLICY_HARD_STOP,
    CapabilityOutcome.EXECUTOR_UNAVAILABLE: GapKind.DEVICE_OR_SERVICE_UNAVAILABLE,
    CapabilityOutcome.TIMEOUT: GapKind.DEVICE_OR_SERVICE_UNAVAILABLE,
    CapabilityOutcome.INVALID_INPUT: GapKind.UNSUPPORTED_VARIANT,
    CapabilityOutcome.CAPABILITY_FAILED: GapKind.PROVIDER_OR_QUOTA_UNAVAILABLE,
}

#: Fehler, die das Modell selbst beheben kann und soll.
#:
#: Ein fehlendes Pflichtargument ist keine Luecke im System — das Schema stand
#: dem Modell die ganze Zeit zur Verfuegung. Eine Untersuchung waere hier nicht
#: nur Verschwendung, sondern schaedlich: im Abnahmelauf schlug sie fuer ein
#: falsch aufgerufenes `ha_turn_on` doch tatsaechlich `ha_turn_off` vor. Beide
#: Beschreibungen enthalten „schaltet", der Satz enthielt „schalte" — und aus
#: einem Tippfehler im Aufruf wurde ein Vorschlag, das Licht auszuschalten.
#:
#: Wer den falschen Schluessel benutzt hat, braucht den richtigen Schluessel,
#: keinen anderen Weg ins Haus.
SELF_CORRECTABLE = ("missing_argument", "unknown_argument", "wrong_type",
                    "invalid_arguments")


#: Ein ausdrueckliches NEIN. Hier wird ueberhaupt nicht mehr gesucht.
#:
#: Das ist der wichtigste Eintrag dieser Datei. `REJECTED_BY_POLICY` ist im
#: Router ein weiter Sammelbegriff — darunter liegt auch der Fall, dass der
#: Mensch auf dem iPhone abgelehnt hat. Wer daraufhin „einen anderen Weg" sucht,
#: sucht den Weg an einer Ablehnung vorbei. Das ist kein Grenzfall, sondern
#: genau das Verhalten, das dieses ganze System nicht haben darf.
#:
#: Ebenso `digest drift`: die Welt hat sich seit der Zustimmung geaendert. Die
#: Antwort darauf ist erneut fragen, nicht anders versuchen.
DECLINED_REASONS = frozenset({
    "not_approved", "denied", "user_denied", "approval_denied",
    "device_revoked", "boundary_lost",
})


#: Gruende, die eine feinere Einstufung erlauben als der blosse Ausgang. Der
#: Grund kommt aus dem Core, nicht aus dem Modell.
_BY_REASON: dict[str, GapKind] = {
    "unknown_capability": GapKind.CAPABILITY_MISSING,
    "unknown_tool": GapKind.CAPABILITY_MISSING,
    "not_exposed_to_llm": GapKind.CAPABILITY_MISSING,
    "credentials_missing": GapKind.CREDENTIAL_OR_CONNECTION_MISSING,
    "not_configured": GapKind.CREDENTIAL_OR_CONNECTION_MISSING,
    "provider_unavailable": GapKind.PROVIDER_OR_QUOTA_UNAVAILABLE,
    "portal_unavailable": GapKind.DEVICE_OR_SERVICE_UNAVAILABLE,
    "executor_unavailable": GapKind.DEVICE_OR_SERVICE_UNAVAILABLE,
    "quota_exceeded": GapKind.PROVIDER_OR_QUOTA_UNAVAILABLE,
    "rate_limited": GapKind.PROVIDER_OR_QUOTA_UNAVAILABLE,
    "awaiting_user_approval": GapKind.AUTHORITY_REQUIRED,
    "user_authority_required": GapKind.AUTHORITY_REQUIRED,
    "session_expired": GapKind.HUMAN_ACTION_REQUIRED,
    "login_required": GapKind.HUMAN_ACTION_REQUIRED,
    # Der Freigabeweg selbst fehlt oder ist ueberlastet. Beides ist keine
    # fehlende Faehigkeit — und schon gar kein Grund, an ihm vorbeizuplanen.
    "no_approval_channel": GapKind.CREDENTIAL_OR_CONNECTION_MISSING,
    "approval_unavailable": GapKind.CREDENTIAL_OR_CONNECTION_MISSING,
    "too_many_pending_approvals": GapKind.DEVICE_OR_SERVICE_UNAVAILABLE,
    "approval_drift": GapKind.HUMAN_ACTION_REQUIRED,
    "unknown_approval": GapKind.HUMAN_ACTION_REQUIRED,
    "approval_capability_mismatch": GapKind.HUMAN_ACTION_REQUIRED,
    # Echte Policy-Grenzen des Trust-Boundary-Vertrags.
    "untrusted_origin": GapKind.POLICY_HARD_STOP,
    "user_authority_missing": GapKind.AUTHORITY_REQUIRED,
    # Die Aktion laesst sich nicht mehr beschreiben — die Seite ist eine andere.
    "action_not_describable": GapKind.UNSUPPORTED_VARIANT,
    "unknown_argument": GapKind.UNSUPPORTED_VARIANT,
}


def is_self_correctable(reason: str) -> bool:
    """Ob der Aufrufer das selbst richten kann — dann wird nicht untersucht."""
    key = (reason or "").strip().lower()
    return any(key == prefix or key.startswith(prefix + ":")
               for prefix in SELF_CORRECTABLE)


def was_declined(reason: str) -> bool:
    """Ob hier ein Mensch (oder eine Sperre) ausdruecklich Nein gesagt hat.

    Wird VOR der Einstufung gefragt. Ein Nein ist kein Problem, das man loest.
    """
    key = (reason or "").strip().lower()
    return key in DECLINED_REASONS or any(
        key.startswith(d + ":") for d in DECLINED_REASONS)


def classify(outcome: CapabilityOutcome | None, reason: str = "") -> GapKind:
    """Die Art einer Blockade — aus dem Umschlag, nicht aus einer Vermutung.

    Der Grund schlaegt den Ausgang, weil er spezifischer ist: `capability_failed`
    allein sagt wenig, `credentials_missing` sagt alles. Passt nichts, bleibt es
    bei der groebsten wahren Aussage statt bei einer erfundenen genauen.
    """
    key = (reason or "").strip().lower()
    if key in _BY_REASON:
        return _BY_REASON[key]
    for prefix, kind in _BY_REASON.items():
        if key.startswith(prefix + ":") or key.startswith(prefix + "_"):
            return kind
    if outcome is not None and outcome in _BY_OUTCOME:
        return _BY_OUTCOME[outcome]
    return GapKind.CAPABILITY_MISSING


#: Ausgaenge, bei denen ueberhaupt nichts zu loesen ist.
SETTLED = (CapabilityOutcome.SUCCESS, CapabilityOutcome.CANCELLED,
           CapabilityOutcome.RECOVERY_REQUIRED)


def is_blocked(outcome: CapabilityOutcome | None) -> bool:
    """Ob dieser Ausgang ueberhaupt nach einem Weg fragt.

    `RECOVERY_REQUIRED` steht bewusst NICHT hier drin: ein unklarer Ausgang ist
    kein blockierter Weg, sondern ein offener. Dort noch einen Alternativweg zu
    suchen hiesse, eine womoeglich ausgefuehrte Aktion ein zweites Mal zu
    versuchen — genau das, was die eingefrorene Wiederherstellung verbietet.
    """
    return outcome is not None and outcome not in SETTLED
