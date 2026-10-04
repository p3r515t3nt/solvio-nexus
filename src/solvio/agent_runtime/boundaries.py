"""Nutzergrenzen: wo SOLVIO aufhoert und ein Mensch anfaengt.

Eine Nutzergrenze ist kein Fehler. Sie ist der Punkt, an dem alles, was ohne
den Menschen geht, schon getan ist — und genau EINE Handlung uebrig bleibt, die
nur er tun kann.

Der Unterschied zur Freigabe ist wichtig und wird hier festgehalten: **Face ID
ist NIE eine Nutzergrenze.** Face ID ist der Freigabeweg (WAITING_APPROVAL), und
der ist gebaut, wiederaufnehmbar und automatisch. Eine Nutzergrenze ist etwas,
wofuer es keinen Weg gibt: eine Anmeldung im Browser, ein MFA-Code, eine
physische Handlung, ein einmalig sichtbares Geheimnis, eine Produktentscheidung
— oder eine Handlung, die die Matrix aus dem Hintergrund verweigert
(`BACKGROUND × VERY_CRITICAL = DENY`). Dann bittet der Lauf den Menschen, sie
selbst auszuloesen. Er sucht keinen Weg um sie herum.

Der Datensatz ist dauerhaft und beantwortet drei Fragen: WAS gebraucht wird,
WARUM, und WIE es weitergeht.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

#: Die geschlossene Liste der Grenzarten. Wer eine vierte braucht, traegt sie
#: hier ein — und merkt dabei, dass er eine trifft.
BROWSER_LOGIN = "browser_login"
MFA_CODE = "mfa_code"
PHYSICAL_ACTION = "physical_action"
ONE_TIME_SECRET = "one_time_secret"
PRODUCT_DECISION = "product_decision"
POLICY_REFUSAL = "policy_refusal"
NATIVE_PASSWORD = "native_password"

BOUNDARY_KINDS = frozenset({
    BROWSER_LOGIN, MFA_CODE, PHYSICAL_ACTION, ONE_TIME_SECRET,
    PRODUCT_DECISION, POLICY_REFUSAL, NATIVE_PASSWORD,
})

#: Laengendeckel. Eine Grenze ist eine Bitte, kein Bericht.
MAX_FIELD = 400


class BoundaryError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class UserBoundary:
    """Ein dauerhafter Grenz-Datensatz.

    `action` ist bewusst EINE Handlung. „Melde dich an und hol den Code und
    trag ihn ein" ist drei — und ein Mensch, der drei Dinge liest, macht das
    erste und vergisst das dritte.
    """

    kind: str
    #: Was genau der Mensch tun soll. Eine Handlung, in seiner Sprache.
    action: str
    #: Warum es ohne ihn nicht geht.
    reason: str
    #: Was danach passiert.
    resume_hint: str = ""
    #: Der Schritt, an dem der Lauf steht.
    step_id: str = ""
    #: Die Plannummer dieses Schritts. Ohne sie weiss die Wiederaufnahme nicht,
    #: WO der Lauf stand — und `step_id` allein sagt es ihr nicht.
    seq: int = 0
    #: **Was nach der Wiederaufnahme mit DIESEM Schritt geschieht.**
    #:
    #: Die beiden Grenzen dieses Systems meinen Entgegengesetztes, und das ist
    #: keine Feinheit:
    #:
    #: * `policy_refusal` — der Mensch loest die Handlung SELBST aus. Sie danach
    #:   noch einmal zu versuchen waere eine zweite Aussenwirkung.
    #: * `product_decision` nach mehrfach verfallener Freigabe — der Mensch gibt
    #:   FREI. Dann muss genau dieser Schritt noch einmal laufen.
    #:
    #: Vorgabe ist `False`, also weitergehen: das ist die Richtung, in der ein
    #: Irrtum nichts nach draussen wiederholt.
    repeat_step: bool = False

    def __post_init__(self) -> None:
        if self.kind not in BOUNDARY_KINDS:
            raise BoundaryError(f"unknown_boundary_kind:{self.kind}")
        if not self.action.strip():
            raise BoundaryError("boundary_without_action")

    def as_dict(self) -> dict:
        return {"art": self.kind,
                "handlung": self.action[:MAX_FIELD],
                "grund": self.reason[:MAX_FIELD],
                "danach": self.resume_hint[:MAX_FIELD],
                "schritt": self.step_id,
                "nummer": int(self.seq),
                "wiederholen": bool(self.repeat_step)}

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)

    @property
    def message(self) -> str:
        """Der Satz, der beim Menschen ankommt."""
        tail = f" Danach: {self.resume_hint}" if self.resume_hint else ""
        return f"{self.action} — {self.reason}.{tail}"

    @classmethod
    def from_json(cls, raw: str) -> "UserBoundary | None":
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls(kind=str(data.get("art", "")),
                       action=str(data.get("handlung", "")),
                       reason=str(data.get("grund", "")),
                       resume_hint=str(data.get("danach", "")),
                       step_id=str(data.get("schritt", "")),
                       seq=int(data.get("nummer") or 0),
                       # Eine alte Zeile ohne dieses Feld heisst „weitergehen".
                       # Das ist die sichere Richtung — siehe `repeat_step`.
                       repeat_step=bool(data.get("wiederholen") or False))
        except BoundaryError:
            return None


def policy_refusal(capability: str, step_id: str = "",
                   seq: int = 0) -> UserBoundary:
    """`BACKGROUND × VERY_CRITICAL = DENY` wird zur Bitte, nicht zum Fehler.

    Der Lauf fragt nicht nach einer Freigabe — die Matrix laesst aus dem
    Hintergrund fuer sehr Kritisches nicht einmal die FRAGE entstehen. Er sagt
    dem Menschen, was er selbst ausloesen muss.
    """
    return UserBoundary(
        kind=POLICY_REFUSAL, step_id=step_id, seq=seq,
        # Der Mensch loest die Handlung selbst aus — SOLVIO wiederholt sie nicht.
        repeat_step=False,
        action=f"Loese „{capability}“ bitte selbst vom iPhone aus",
        reason=("das ist aus einem Hintergrundlauf nicht erlaubt — dafuer "
                "musst du selbst dabei sein"),
        resume_hint="danach nehme ich den Lauf wieder auf")
