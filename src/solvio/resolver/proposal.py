"""Ein Aenderungsvorschlag — und warum er kein Selbstumbau ist.

Wenn wirklich eine Faehigkeit fehlt, ist „geht nicht" die schlechteste aller
Antworten. Die zweitschlechteste waere, sich die Faehigkeit selbst zu schreiben.
Zwischen beiden liegt das hier: ein **Vorschlag**, der praezise genug ist, dass
ein Mensch oder der vertraute Entwicklungsweg ihn umsetzen kann, und der
ausdruecklich **kein ausfuehrbarer Code** ist.

Der gefaehrliche Teil eines solchen Vorschlags ist nicht das, was drinsteht,
sondern wer ihn schreibt. Ein Modell, das seinen eigenen Vorschlag verfasst,
schreibt bei jeder Gelegenheit „Risiko: gering, Freigabe: nicht noetig" — nicht
aus Boesartigkeit, sondern weil das die Formulierung ist, die am ehesten zum Ziel
fuehrt. Deshalb sind `risk` und `needs_approval` hier **keine Felder, die man
setzt**, sondern Ergebnisse: sie werden aus der Ausfuehrungssemantik mit genau
denselben Funktionen berechnet, die spaeter auch den echten Aufruf bewerten.

Behauptet ein Vorschlag etwas anderes, wird die Behauptung nicht korrigiert und
vergessen, sondern als `overridden` protokolliert. Ein System, das stillschweigend
zurechtbiegt, verliert den Hinweis darauf, dass jemand es versucht hat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from solvio.capabilities.contract import RiskLevel, requires_approval
from solvio.security.mobile_approval.execution import (
    IDEMPOTENT_WRITE, NON_IDEMPOTENT_WRITE, READ_ONLY, RECONCILABLE_WRITE,
)

#: Semantik -> Grundrisiko. Dieselbe Rangfolge wie im Vertrag, nur in die
#: Gegenrichtung gelesen: ein Vorschlag nennt, WAS er tut, und daraus folgt, was
#: er kosten darf. Nicht umgekehrt.
_RISK_BY_SEMANTICS: dict[str, RiskLevel] = {
    READ_ONLY: RiskLevel.HARMLESS,
    IDEMPOTENT_WRITE: RiskLevel.MUTATING,
    RECONCILABLE_WRITE: RiskLevel.MUTATING,
    NON_IDEMPOTENT_WRITE: RiskLevel.CRITICAL,
}

#: Aenderungen an einem Betriebssystem sind nie idempotent im interessanten Sinn:
#: ein Paket kann Abhaengigkeiten mitbringen, Dienste starten, Konfiguration
#: ueberschreiben. Wer so etwas als IDEMPOTENT_WRITE vorschlaegt, hat sich
#: verschaetzt — hier wird es angehoben.
SYSTEM_CHANGING = ("paket", "package", "install", "apt", "systemd", "dienst",
                   "service", "firmware", "kernel", "sudo", "root")


@dataclass
class CapabilityProposal:
    """Ein technischer Aenderungsvorschlag. Kein Auftrag, keine Ausfuehrung."""

    #: Was der Nutzer woertlich wollte.
    original_goal: str
    #: Was er damit erreichen wollte — das eigentliche Ziel.
    underlying_goal: str
    #: Warum der direkte Weg nicht geht.
    blocker: str
    #: Vorgeschlagener Name der Faehigkeit.
    capability_name: str
    #: Wo sie ausgefuehrt wuerde (Geraet/Laufzeit).
    target_executor: str
    #: Ausfuehrungssemantik — die eine Angabe, aus der alles Weitere folgt.
    semantics: str

    required_inputs: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    reusable_components: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    rollback: str = ""
    tests_required: list[str] = field(default_factory=list)
    human_setup: str = ""
    implementation_route: str = ""
    trust_boundary: str = ""

    #: Was der Verfasser BEHAUPTET hat. Nur zur Nachvollziehbarkeit.
    claimed_risk: str = ""
    claimed_needs_approval: bool | None = None

    #: Berechnet, nicht gesetzt. Wird von `validate()` gefuellt.
    risk: str = ""
    needs_approval: bool = False
    overridden: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def validate(self) -> "CapabilityProposal":
        """Rechnet Risiko und Freigabepflicht aus — und deckt Abweichungen auf."""
        semantics = (self.semantics or "").strip()
        if semantics not in _RISK_BY_SEMANTICS:
            self.problems.append(
                f"unbekannte Ausfuehrungssemantik {semantics!r} — "
                f"erlaubt sind {sorted(_RISK_BY_SEMANTICS)}")
            # Fail-closed: was sich nicht einordnen laesst, gilt als das
            # Gefaehrlichste. Andersherum waere ein Tippfehler ein Freibrief.
            semantics = NON_IDEMPOTENT_WRITE
            self.semantics = semantics

        risk = _RISK_BY_SEMANTICS[semantics]
        haystack = " ".join([self.capability_name, self.underlying_goal,
                             self.original_goal, " ".join(self.side_effects)]).lower()
        if any(word in haystack for word in SYSTEM_CHANGING):
            if int(risk) < int(RiskLevel.CRITICAL):
                self.overridden.append(
                    f"Systemaenderung erkannt — Risiko von {risk.name} auf CRITICAL "
                    f"angehoben")
            risk = RiskLevel.CRITICAL
            if semantics == READ_ONLY:
                self.problems.append(
                    "als READ_ONLY vorgeschlagen, veraendert aber ein System")

        if self.claimed_risk and self.claimed_risk.upper() != risk.name:
            self.overridden.append(
                f"behauptetes Risiko {self.claimed_risk!r} ersetzt durch {risk.name} "
                f"(aus der Semantik berechnet)")
        needs = bool(requires_approval(risk))
        if self.claimed_needs_approval is False and needs:
            self.overridden.append(
                "behauptete Freigabefreiheit ersetzt: diese Aktion braucht eine "
                "Freigabe")

        self.risk = risk.name
        self.needs_approval = needs
        if needs and "freigabe" not in " ".join(self.required_permissions).lower():
            self.required_permissions.append(
                "Freigabe des Nutzers auf dem iPhone (Face ID) je Ausfuehrung")
        if not self.rollback:
            self.problems.append("keine Angabe zur Rueckabwicklung")
        if not self.tests_required:
            self.problems.append("keine Tests benannt")
        return self

    @property
    def is_sound(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_goal": self.original_goal,
            "underlying_goal": self.underlying_goal,
            "blocker": self.blocker,
            "capability_name": self.capability_name,
            "target_executor": self.target_executor,
            "semantics": self.semantics,
            "risk": self.risk,
            "needs_approval": self.needs_approval,
            "required_inputs": self.required_inputs,
            "required_permissions": self.required_permissions,
            "reusable_components": self.reusable_components,
            "side_effects": self.side_effects,
            "rollback": self.rollback,
            "trust_boundary": self.trust_boundary,
            "tests_required": self.tests_required,
            "human_setup": self.human_setup,
            "implementation_route": self.implementation_route,
            "overridden": self.overridden,
            "problems": self.problems,
            # Damit an keiner Stelle der Eindruck entsteht, das hier sei
            # ausfuehrbar oder bereits beschlossen.
            "status": "vorschlag",
            "executable": False,
        }
