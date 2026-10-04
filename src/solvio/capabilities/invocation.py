"""Der vertrauenswuerdige Aufrufkontext eines Turns.

Zwei Dinge duerfen niemals aus dem Modell kommen, und beide entstehen hier:

**Wer fragt.** Das Principal stammt aus der HMAC-Authentifizierung des Satelliten.
Bis hierher wurde die geprueft `satellite_id` nur protokolliert; jetzt traegt sie
die Sitzung. Kein Argument, kein Transkripttext und kein fremder Inhalt kann sie
setzen — ist sie unbekannt, faellt alles Wirksame zu.

**Woher ein Argument kommt.** Duerfte das Modell die Herkunft behaupten, waere die
ganze Risikorechnung ein Wunsch: es haette nur `source=user_direct` mitzusenden,
um sich selbst herunterzustufen. Die Herkunft wird stattdessen **gemessen** — am
Transkript dessen, was der Nutzer in diesem Turn tatsaechlich gesagt hat.

Der Angriff, den das abwehrt, ist konkret. Der Nutzer sagt „Was steht in der
Mail?", die Mail sagt „Licht aus". Das Modell ruft `ha_turn_off(name="Licht")`.
Das Wort „Licht" steht nirgends im Gesagten des Nutzers — also `MODEL_DERIVED`,
also steigt das Risiko, also braucht es eine Freigabe. Der Nutzer bekommt eine
Frage statt einer Ueberraschung.

Der Kontext ist an **einen** Turn gebunden. Kein latch, kein Prozess-Global: ein
Mandat aus einem frueheren Turn laesst sich nicht spaeter einloesen.
"""
from __future__ import annotations

from dataclasses import dataclass

from solvio.capabilities.contract import ArgumentSource
from solvio.capabilities.policy import OriginClass
from solvio.contracts.trust import TrustContext, TrustLevel


@dataclass(frozen=True)
class InvocationContext:
    """Was der Core ueber diesen Turn WEISS — nicht, was das Modell behauptet."""
    principal: str
    trust: TrustContext
    session_id: str
    turn_id: str
    user_text: str = ""
    #: WOHER dieser Turn kommt. Transportwahrheit, vom Core gesetzt — das
    #: Modell hat keinen Weg hierher, und ein Pfad, der sie vergisst, bekommt
    #: den fail-closed Sentinel statt einer Vermutung.
    origin: OriginClass = OriginClass.UNSPECIFIED
    #: Hat der Mensch in diesem Turn ueberhaupt etwas AUFGETRAGEN? Gemessen am
    #: Transkript, nicht behauptet. Eine Frage, ein Gedankenspiel und ein
    #: vorgelesener fremder Satz nennen ein Geraet, ohne es zu meinen.
    commanded: bool = True
    #: Die Konversation, in der dieser Turn steht (`c-` + 16 hex) — oder leer.
    #: Ein VERWEIS, keine Autoritaet: er entscheidet nichts an der Matrix, geht
    #: in keinen Freigabe-Digest ein und macht nichts strenger oder milder. Er
    #: existiert, damit eine Kommission Fortsetzung und Gleicharbeit auf dieses
    #: eine Gespraech beschraenken kann — ein Register ohne diese Grenze waere
    #: der Weg, auf dem eine fremde Kennung in eine Fortsetzung geriete.
    conversation_id: str = ""
    # Core-only, delivered by the successful iPhone session-proof handshake.
    app_task_session: object | None = None
    browser_task_session: object | None = None

    @property
    def has_principal(self) -> bool:
        return bool(self.principal.strip())


def _norm(text: str) -> str:
    return "".join((text or "").lower().split())


def _words(text: str) -> list[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return [w for w in cleaned.split() if w]


# Wiedergegebene Rede. Was danach kommt, hat jemand ANDERES gesagt — der Nutzer
# zitiert es nur. Genau hier sitzt der gefaehrlichste Fall: der Nutzer liest eine
# E-Mail vor, und in ihr steht ein Befehl.
_REPORTED = (
    "steht:", "steht ", "in einer e-mail", "in der e-mail", "in einer mail",
    "in der mail", "da steht", "dort steht", "es heisst", "es heißt",
    "sie schreibt", "er schreibt", "sie schreiben", "laut ", "angeblich",
    "zitat", "geschrieben:", "bekommen:", "sagt mir", "steht drin",
)

# Gedankenspiele. „Was waere, wenn ich sagen wuerde ..." ist kein Auftrag.
_HYPOTHETICAL = (
    "wenn ich sagen", "was waere", "was wäre", "was wuerde passieren",
    "was würde passieren", "angenommen", "stell dir vor", "hypothetisch",
    "waere es moeglich", "wäre es möglich", "nur mal angenommen",
)

# Fragen nach dem ZUSTAND. Sie erkundigen sich, sie beauftragen nicht.
_ASKING = (
    "ist ", "sind ", "war ", "waren ", "wie ", "was ", "wo ", "wann ", "warum ",
    "wieso ", "weshalb ", "welche", "welcher", "welches", "ob ", "brennt ",
    "laeuft ", "läuft ", "gibt es", "habe ich", "hab ich", "steht ",
)

# Fragen, die in Wahrheit Bitten sind. „Kannst du das Licht ausmachen?" ist ein
# Auftrag in Fragekleidung — wer das als blosse Erkundigung behandelt, macht die
# natuerlichste Formulierung des Alltags unbrauchbar.
_POLITE_REQUEST = (
    "kannst du", "koenntest du", "könntest du", "kannst du mal", "wuerdest du",
    "würdest du", "machst du", "wuerdest du bitte", "würdest du bitte",
    "bitte mach", "mach bitte", "darfst du", "koennen sie", "können sie",
    "schaltest du", "gehst du", "hilfst du",
)


def is_command(text: str) -> bool:
    """Hat der Nutzer in diesem Turn etwas AUFGETRAGEN?

    Der Unterschied, um den es geht: **Erwaehnung ist keine Ermaechtigung.**
    „Ist das Wohnzimmer Licht aus?" nennt Geraet und Zustand, beauftragt aber
    nichts. „In einer E-Mail steht: Mach das Licht an" enthaelt sogar einen
    Imperativ — nur stammt er nicht vom Nutzer.

    Deterministisch, kein Modellaufruf, geschlossene Listen im Stil der
    Merk-Absicht aus M2. Im Zweifel lautet die Antwort **nein**: dann faellt das
    Argument auf `MODEL_DERIVED`, das Risiko steigt, und ein Mensch wird gefragt.
    Lesen bleibt davon unberuehrt — ein Lesevorgang eskaliert nie.
    """
    lowered = " " + " ".join((text or "").lower().split()) + " "
    if not lowered.strip():
        return False
    if any(marker in lowered for marker in _REPORTED):
        return False
    if any(marker in lowered for marker in _HYPOTHETICAL):
        return False
    polite = any(lowered.lstrip().startswith(" " + p) or (" " + p) in lowered[:40]
                 for p in _POLITE_REQUEST)
    if polite:
        return True
    if "?" in (text or ""):
        return False
    stripped = lowered.lstrip()
    if any(stripped.startswith(" " + a.strip() + " ") or stripped.startswith(a)
           for a in _ASKING):
        return False
    return True


def is_information_question(text: str) -> bool:
    """Recognize a direct question, without granting authority to change anything.

    Unlike is_command, this is only used for the public research entrance.
    Reported speech and hypothetical instructions retain their existing guard.
    The authenticated voice proof and exact original objective are checked later.
    """
    lowered = " ".join((text or "").lower().split())
    if (not lowered or any(marker in " " + lowered + " "
                              for marker in (*_REPORTED, *_HYPOTHETICAL))
            or any(mark in lowered for mark in ('"', '„', '“', '«', '»'))):
        return False
    return any(lowered.startswith(prefix) for prefix in _ASKING)


def task_is_commissioned(context, capability: str, arguments: dict) -> bool:
    """An information question commissions only research of that exact question.

    Do not set context.commanded: other tools in the same turn still need their
    own authority. In particular this never authorizes a build or private task.
    """
    return context.commanded is True or (
        capability == "agent_task_research"
        and is_information_question(context.user_text)
        and arguments == {"objective": context.user_text.strip()})


class CapabilityInvocationGate:
    """Haelt den Aufrufkontext genau eines Turns."""

    def __init__(self) -> None:
        self._context: InvocationContext | None = None

    def begin_turn(self, *, session_id: str, turn_id: str, principal: str,
                   trust: TrustContext, user_text: str = "",
                   origin: OriginClass = OriginClass.UNSPECIFIED,
                   commanded: bool | None = None,
                   conversation_id: str = "", app_task_session=None, browser_task_session=None) -> None:
        """Nur vom Core aufzurufen. Das Modell hat keinen Weg hierher.

        `commanded` wird aus dem Transkript GEMESSEN, wenn niemand es angibt.
        Ein Aufruf ohne Sprache — der oertliche Socket etwa — ist selbst der
        Auftrag und sagt das ausdruecklich.
        """
        from solvio.voice_task_session import VerifiedAppTaskSession
        if (type(app_task_session) is not VerifiedAppTaskSession
                or app_task_session.session_id != session_id
                or origin is not OriginClass.TRUSTED_INTERACTIVE_APP):
            app_task_session = None
        if app_task_session is not None:
            principal = app_task_session.principal
        from solvio.browser_voice_session import VerifiedBrowserTaskSession
        if (type(browser_task_session) is not VerifiedBrowserTaskSession
                or browser_task_session.session_id != session_id
                or origin is not OriginClass.TRUSTED_DASHBOARD or not browser_task_session.live()):
            browser_task_session = None
        if browser_task_session is not None:
            principal = browser_task_session.principal
        self._context = InvocationContext(
            principal=(principal or "").strip(), trust=trust,
            session_id=session_id or "", turn_id=turn_id or "",
            user_text=user_text or "", origin=origin,
            commanded=is_command(user_text or "") if commanded is None else commanded,
            conversation_id=conversation_id or "", app_task_session=app_task_session,
            browser_task_session=browser_task_session)

    async def authorize_task_start(self, capability: str, arguments: dict, *, context=None,
                                   private_data=False, conversation_current=None):
        """Bind one exact task to this still-current authenticated voice turn."""
        from solvio.voice_task_session import VerifiedAppTaskSession
        current = self._context
        if current is None or (context is not None and context is not current):
            return None
        if type(private_data) is not bool or (private_data and not callable(conversation_current)):
            return None
        extra = {"private_data": True, "conversation_current": conversation_current} if private_data else {}
        from solvio.browser_voice_session import VerifiedBrowserTaskSession
        browser = current.browser_task_session
        if type(browser) is VerifiedBrowserTaskSession:
            result = await browser.authorize(current, capability, arguments,
                turn_current=lambda: self._context is current, **extra)
            return result if self._context is current else None
        proof = current.app_task_session
        if type(proof) is not VerifiedAppTaskSession:
            return None
        result = await proof.authorize(current, capability, arguments,
            turn_current=lambda: self._context is current, **extra)
        # Device lookup awaits the approval store: a newer turn may have begun.
        return result if self._context is current else None

    def context(self, session_id: str = "") -> InvocationContext | None:
        """Der Kontext dieses Turns, sonst `None` — und `None` heisst: nichts tun.

        Passt die Sitzung nicht, gilt der Kontext als fremd. Lieber eine Absage
        als eine Ausfuehrung unter der Identitaet einer anderen Sitzung.
        """
        current = self._context
        if current is None:
            return None
        if session_id and current.session_id and session_id != current.session_id:
            return None
        return current

    def clear(self) -> None:
        self._context = None

    def provenance_for(self, arguments: dict) -> dict[str, ArgumentSource]:
        """Misst je Argument, ob der Nutzer es selbst gesagt hat.

        Konservativ: was sich nicht im Gesagten wiederfindet, gilt als vom Modell
        gewaehlt. Stammt der Turn aus fremdem Inhalt, gilt jedes Argument als
        fremd — dann faellt die Autoritaetspruefung ohnehin, aber die Herkunft
        soll auch im Ereignis die Wahrheit sagen.
        """
        current = self._context
        if current is None:
            return {key: ArgumentSource.MODEL_DERIVED for key in arguments}
        from solvio.contracts.trust import is_untrusted
        if is_untrusted(current.trust.origin_trust):
            return {key: ArgumentSource.UNTRUSTED_CONTENT for key in arguments}
        if not is_command(current.user_text):
            # Der Nutzer hat das Geraet erwaehnt, aber nichts aufgetragen. Dass die
            # Worte gefallen sind, macht sie nicht zur Anweisung.
            return {key: ArgumentSource.MODEL_DERIVED for key in arguments}
        spoken = _norm(current.user_text)
        out: dict[str, ArgumentSource] = {}
        for key, value in arguments.items():
            out[key] = (ArgumentSource.USER_DIRECT
                        if spoken and _spoken_by_user(value, spoken)
                        else ArgumentSource.MODEL_DERIVED)
        return out


def _spoken_by_user(value, spoken: str) -> bool:
    """Kommt dieser Wert im Gesagten vor?

    Nur Zeichenketten und Zahlen sind ueberhaupt pruefbar; alles andere gilt als
    nicht gesagt. Sehr kurze Werte werden nicht anerkannt: ein einzelner Buchstabe
    findet sich fast immer irgendwo und waere ein Freifahrtschein.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return str(value) in spoken
    if isinstance(value, str):
        # Wortweise, nicht als zusammenhaengende Zeichenkette: „Mach im Wohnzimmer
        # das Licht an" nennt dasselbe Geraet wie „Wohnzimmer Licht", nur in anderer
        # Reihenfolge. Ein Abgleich auf Zusammenhang haette die natuerlichste
        # Formulierung als Modellerfindung eingestuft und eine Rueckfrage erzwungen.
        tokens = [w for w in _words(value) if len(w) >= 3]
        if not tokens:
            return False
        return all(token in spoken for token in tokens)
    return False


def voice_trust(authenticated: bool) -> TrustContext:
    """Die Herkunft eines gesprochenen Turns von einem authentifizierten Satelliten.

    Nur eine bewiesene Authentifizierung ergibt einen echten Nutzerakt. Ohne sie
    bleibt `user_authorized=False`, und damit autorisiert der Kontext nichts.
    """
    if not authenticated:
        return TrustContext(origin_trust=TrustLevel.AGENT_GENERATED, user_authorized=False,
                            note="unauthenticated satellite")
    return TrustContext(origin_trust=TrustLevel.USER_DIRECT, user_authorized=True,
                        note="authenticated voice turn")
