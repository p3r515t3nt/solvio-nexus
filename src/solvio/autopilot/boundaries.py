"""Nutzergrenzen des Autopiloten — duenn, weil es sie schon gibt.

Die Grenzarten kommen unveraendert aus `agent_runtime.boundaries`; die Meldung
geht in denselben `ProactiveStore`, den die Hintergrundlaeufe benutzen. **Es
gibt bewusst keinen zweiten Posteingang** und kein Push-System — die
Schnittstelle ist sauber, damit Inbox, iPhone und Voice spaeter darauf
aufsetzen koennen, ohne dass hier etwas umgebaut wird.

Was dieses Modul beisteuert, ist die **Kategorie**: welche der vier
Owner-Kategorien eine Grenze traegt. Sie steht im Auftrag, nicht im Code der
Agentenlaufzeit, und sie entscheidet, wie dringend eine Meldung klingt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("autopilot")

# Die Grenzarten kommen aus der Agentenlaufzeit — aber NICHT ueber einen
# Modulimport. Ein Kernmodul, das die Laufzeit beim Laden zieht, macht sie zur
# Startvoraussetzung des Cores; genau das verbietet
# `t_no_core_module_imports_the_agent_runtime`, und zwar zu Recht: der Core
# muss ohne sie hochkommen. Die Namen stehen deshalb hier als Zeichenketten,
# und eine Zusicherung haelt sie mit dem Original gleich.
BROWSER_LOGIN = "browser_login"
MFA_CODE = "mfa_code"
PHYSICAL_ACTION = "physical_action"
ONE_TIME_SECRET = "one_time_secret"
PRODUCT_DECISION = "product_decision"
POLICY_REFUSAL = "policy_refusal"
NATIVE_PASSWORD = "native_password"

INHERITED_KINDS = frozenset({BROWSER_LOGIN, MFA_CODE, PHYSICAL_ACTION,
                             ONE_TIME_SECRET, PRODUCT_DECISION,
                             POLICY_REFUSAL, NATIVE_PASSWORD})

AUTHORITY_REQUIRED = "AUTHORITY_REQUIRED"
DECISION_REQUIRED = "DECISION_REQUIRED"
PHYSICAL_ACTION_REQUIRED = "PHYSICAL_ACTION_REQUIRED"
FAILED_NEEDS_OWNER = "FAILED_NEEDS_OWNER"
CATEGORIES = frozenset({AUTHORITY_REQUIRED, DECISION_REQUIRED,
                        PHYSICAL_ACTION_REQUIRED, FAILED_NEEDS_OWNER})

#: Zwei Arten, die der Autopilot zusaetzlich braucht. `release_authority` ist
#: die Grenze, an der V0.5 grundsaetzlich anhaelt (kein autonomer
#: Produktions-Release); `contract_change` ist die EINZIGE Aenderungskante des
#: Contracts.
RELEASE_AUTHORITY = "release_authority"
CONTRACT_CHANGE = "contract_change"

KINDS = frozenset(INHERITED_KINDS | {RELEASE_AUTHORITY, CONTRACT_CHANGE})

#: Welche Art in welche Kategorie faellt. Geschlossen — eine Grenze ohne
#: Kategorie waere eine Meldung ohne Dringlichkeit.
CATEGORY_OF: dict[str, str] = {
    MFA_CODE: AUTHORITY_REQUIRED,
    NATIVE_PASSWORD: AUTHORITY_REQUIRED,
    ONE_TIME_SECRET: AUTHORITY_REQUIRED,
    BROWSER_LOGIN: AUTHORITY_REQUIRED,
    RELEASE_AUTHORITY: AUTHORITY_REQUIRED,
    PRODUCT_DECISION: DECISION_REQUIRED,
    CONTRACT_CHANGE: DECISION_REQUIRED,
    PHYSICAL_ACTION: PHYSICAL_ACTION_REQUIRED,
    POLICY_REFUSAL: FAILED_NEEDS_OWNER,
}

#: Was ausdruecklich KEINE Nutzergrenze ist. Die Liste steht hier, damit sie
#: jemand lesen kann — und weil sie im Auftrag woertlich verlangt wurde.
NEVER_A_BOUNDARY = ("roter Test", "normaler Bug", "Builderwechsel",
                    "Kontingent erschoepft", "Modellwechsel")


def category_for(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown_boundary_kind:{kind}")
    return CATEGORY_OF.get(kind, FAILED_NEEDS_OWNER)


@dataclass(frozen=True)
class Ask:
    """Genau eine Handlung, die nur der Mensch tun kann."""

    milestone_id: str
    category: str
    kind: str
    question: str
    boundary_id: str = ""

    def message(self) -> str:
        kopf = {AUTHORITY_REQUIRED: "Ich brauche deine Freigabe",
                DECISION_REQUIRED: "Ich brauche deine Entscheidung",
                PHYSICAL_ACTION_REQUIRED: "Ich brauche dich am Geraet",
                FAILED_NEEDS_OWNER: "Ich komme allein nicht weiter"}
        return f"{kopf.get(self.category, 'Ich brauche dich')}: {self.question}"


async def notify(store, ask: Ask, *, now: float = 0.0) -> bool:
    """Die Meldung in den EINEN Posteingang. `False` heisst „lag schon da".

    Ein Fehlschlag des Posteingangs bricht den Milestone nicht ab — aber er
    wird auch nicht verschwiegen: er steht im Log und kommt als `False`
    zurueck.
    """
    from solvio.agent_runtime import notices
    meldung = notices.Notice(
        run_id=ask.milestone_id,
        kind="user_boundary",
        summary=ask.message(),
        priority="high" if ask.category != DECISION_REQUIRED else "normal")
    gesendet = await notices.send(store, meldung, now=now)
    log.info("autopilot.boundary_notified", milestone=ask.milestone_id,
             category=ask.category, kind=ask.kind, delivered=gesendet)
    return gesendet
