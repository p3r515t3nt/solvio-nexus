"""Warum etwas kaputt ist — und was daraus folgen darf.

Der Unterschied zwischen Gesundheit und Diagnose ist der ganze Sinn dieses
Meilensteins. „Kalender: Anmeldung noetig" ist ein Zustand. „Googles
Aktualisierungstoken wurde mit `invalid_grant` abgelehnt" ist eine Diagnose — sie
nennt eine Ursache, sie stuetzt sich auf einen Beleg, und aus ihr folgt genau
eine sinnvolle Handlung.

Zwei Regeln haben die Form bestimmt:

* **Keine Ursache ohne Beleg.** Ein Feld `probable_cause` ohne `evidence` ist
  eine Vermutung im Gewand einer Feststellung, und die ist schlimmer als ein
  ehrliches „unbekannt". Deshalb stuft `conclude()` die Zuversicht herab, wenn
  kein Beleg dabei ist.
* **Aus der Diagnose folgt die Reparaturklasse, nicht umgekehrt.** Wer erst
  entscheidet, was er tun will, und dann die Ursache dazu sucht, findet immer
  eine.

Was hier NICHT gespeichert wird: Gedankengaenge. Belege und Schluesse, sonst
nichts. Ein aufgehobener Gedankengang liest sich spaeter wie eine Begruendung,
ohne je eine gewesen zu sein.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from solvio.control_center.health import State


class RepairClass(str, Enum):
    """Was mit diesem Befund ueberhaupt anzufangen ist. Bewusst neun Faelle."""

    #: Nichts zu tun — der Zustand ist in Ordnung oder erledigt sich selbst.
    NO_ACTION = "no_action"
    #: Noch einmal versuchen. Nur bei etwas, das nachweislich voruebergehend ist.
    RETRY = "retry"
    #: Eine Verbindung neu aufbauen, ohne einen Prozess anzufassen.
    RECONNECT = "reconnect"
    #: Einen SOLVIO-eigenen Kindprozess neu starten.
    RESTART_COMPONENT = "restart_component"
    #: Konfiguration neu einlesen.
    RELOAD_CONFIGURATION = "reload_configuration"
    #: Ein Zugang ist abgelaufen. Nur ein Mensch kann das.
    REAUTH_REQUIRED = "reauth_required"
    #: Ein Mensch muss etwas anderes tun (Geraet einschalten, App oeffnen).
    USER_ACTION_REQUIRED = "user_action_required"
    #: Unklarer Ausgang — jemand muss nachsehen, bevor irgendetwas wiederholt wird.
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"
    #: Es gibt kein zugelassenes Vorgehen dafuer. Ausdruecklich kein Freibrief.
    UNSUPPORTED_REPAIR = "unsupported_repair"


#: Klassen, bei denen SOLVIO selbst etwas tun darf — wenn ein Vorgehen
#: hinterlegt ist. Alles andere gehoert einem Menschen.
SELF_REPAIRABLE = frozenset({RepairClass.RETRY, RepairClass.RECONNECT,
                             RepairClass.RESTART_COMPONENT,
                             RepairClass.RELOAD_CONFIGURATION})

#: Klassen, die einen Menschen brauchen — und zwar SO, dass niemand auf die Idee
#: kommt, das automatisch zu erledigen.
NEEDS_HUMAN = frozenset({RepairClass.REAUTH_REQUIRED,
                         RepairClass.USER_ACTION_REQUIRED,
                         RepairClass.MANUAL_RECOVERY_REQUIRED})


class Confidence(str, Enum):
    HIGH = "hoch"
    MEDIUM = "mittel"
    LOW = "niedrig"


@dataclass
class Diagnosis:
    """Ein Befund. Immer mit Beleg oder ausdruecklich ohne."""

    component: str
    observed: State
    #: Was beobachtet wurde — die Symptome, in Worten.
    symptoms: list[str] = field(default_factory=list)
    #: Woran es vermutlich liegt. Leer heisst: es ist nicht bekannt.
    probable_cause: str = ""
    #: Worauf sich das stuetzt. Fehlt das, sinkt die Zuversicht.
    evidence: list[str] = field(default_factory=list)
    confidence: Confidence = Confidence.LOW
    #: Geht das von selbst vorbei?
    persistent: bool = True
    #: Was der Mensch davon merkt — in seinen Worten, nicht in unseren.
    user_impact: str = ""
    repair: RepairClass = RepairClass.UNSUPPORTED_REPAIR
    #: Der Name des hinterlegten Vorgehens, falls es eines gibt.
    playbook: str = ""
    #: Was danach gelten muss, damit die Reparatur als gelungen zaehlt.
    verification: str = ""
    at: float = field(default_factory=time.time)

    @property
    def repair_available(self) -> bool:
        """Nur wahr, wenn es wirklich ein hinterlegtes Vorgehen gibt.

        Ausdruecklich an den Namen gebunden, nicht an die Klasse: eine Klasse
        sagt, was zu tun WAERE; ein Name sagt, dass es dafuer auch etwas gibt.
        Ohne diese Trennung zeigt die Oberflaeche einen Knopf, hinter dem nichts
        liegt.
        """
        return bool(self.playbook) and self.repair in SELF_REPAIRABLE

    @property
    def human_action_required(self) -> bool:
        return self.repair in NEEDS_HUMAN

    @property
    def authority_required(self) -> bool:
        """Ob fuer diese Reparatur eine Freigabe noetig waere.

        In V1 bleibt das falsch, weil nur oertliche, umkehrbare Eingriffe an
        SOLVIO-eigenen Kindprozessen zugelassen sind — dieselbe Begruendung wie
        beim Kontrollzentrum. Das Feld existiert trotzdem: die erste Reparatur
        mit Aussenwirkung soll hier auffallen und nicht stillschweigend
        durchlaufen.
        """
        return (self.repair not in SELF_REPAIRABLE
                and self.repair is not RepairClass.NO_ACTION
                and self.repair is not RepairClass.UNSUPPORTED_REPAIR
                and not self.human_action_required)

    def as_dict(self) -> dict[str, Any]:
        return {
            "komponente": self.component,
            "zustand": self.observed.value,
            "symptome": self.symptoms[:6],
            "ursache": self.probable_cause or "nicht bekannt",
            "belege": self.evidence[:6],
            "zuversicht": self.confidence.value,
            "dauerhaft": self.persistent,
            "auswirkung": self.user_impact,
            "reparatur": self.repair.value,
            "reparierbar": self.repair_available,
            "braucht_dich": self.human_action_required,
            "pruefung": self.verification,
            "zeit": self.at,
        }


def conclude(component: str, observed: State, *, symptoms: list[str] | None = None,
             cause: str = "", evidence: list[str] | None = None,
             repair: RepairClass = RepairClass.UNSUPPORTED_REPAIR,
             playbook: str = "", persistent: bool = True, impact: str = "",
             verification: str = "") -> Diagnosis:
    """Baut einen Befund — und stuft die Zuversicht nach den Belegen ab.

    Die Abstufung passiert hier und nicht beim Aufrufer, damit sie nicht
    vergessen werden kann. Wer eine Ursache ohne Beleg nennt, bekommt
    `niedrig`; wer gar keine nennt, ebenfalls. Hohe Zuversicht gibt es nur fuer
    eine mehrfach belegte Ursache.
    """
    marks = list(evidence or [])
    if cause and len(marks) >= 2:
        confidence = Confidence.HIGH
    elif cause and marks:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW
    return Diagnosis(component=component, observed=observed,
                     symptoms=list(symptoms or []), probable_cause=cause,
                     evidence=marks, confidence=confidence,
                     persistent=persistent, user_impact=impact, repair=repair,
                     playbook=playbook, verification=verification)


def healthy(component: str) -> Diagnosis:
    return Diagnosis(component=component, observed=State.HEALTHY,
                     confidence=Confidence.HIGH, persistent=False,
                     repair=RepairClass.NO_ACTION)
