"""Die Sperrliste, die VOR dem Router steht — Verteidigung in der Tiefe.

Das hier ist ausdruecklich **nicht statt** der Matrix, sondern **vor** ihr. Auch
ohne diese Datei blieben `BACKGROUND × VERY_CRITICAL = DENY`, der
Tresor-Origin-Zaun, die leere ExecutionIdentity und die Herkunftspruefung der
Erzeugungs-Handler bestehen. Die Sperrliste macht daraus eine Grenze, die ein
Lauf strukturell nicht einmal ADRESSIEREN kann.

Drei Familien, drei verschiedene Gruende:

* **`memory_*`, `secret_*`** — ein Lauf besitzt weder Gedaechtnis noch Tresor.
  Er darf vorschlagen; uebernommen wird ueber die bestehenden Wege mit deren
  Toren.
* **`background_*`, `proactive_*`** — die Kontrollebene der autonomen Arbeit.
  Ein Lauf, der seine eigene Wiederholung anlegen kann, ist nicht mehr
  gedeckelt.
* **`agent_task_*`, `agent_run_*`, `deep_*`** — die eigene Familie samt
  Tiefen-Seam. **Ein Lauf kann keinen Lauf und keine Recherche-Kapsel gebaeren.**
  Hermes-Recherche erreicht ein Lauf ausschliesslich als `specialist`-Schritt
  ueber den Adapter, nicht als Faehigkeit.

Zahlung ist der Sonderfall: alles gesperrt ausser `payment_intent_prepare` —
dem Vorschlagsweg, bei dem die Erwartung des Modells nur VERGLICHEN und nie
uebernommen wird. Geldbewegung braucht `purchase_place`, das kein Werkzeugschema
hat, iPhone-attestiert und biometrisch ist.
"""
from __future__ import annotations

import re

#: Praefixe, die ein Lauf niemals als Faehigkeit nennen darf.
BLOCKED_PREFIXES = (
    "memory_",
    "secret_",
    "background_",
    "proactive_",
    # Die EIGENE Familie, ganz. Nicht `agent_task_` und `agent_run_` einzeln:
    # gefunden von der adversarialen Suite, dass ein kuenftiges
    # `agent_policy_set` oder `agent_budget_set` durch beide Praefixe fiele.
    # Ein Lauf hat in seiner eigenen Familie nichts zu suchen — auch nicht in
    # Teilen davon, die es heute noch nicht gibt.
    "agent_",
    "deep_",
)

#: Der eine Zahlungsname, der erreichbar bleibt — der Vorschlagsweg.
PAYMENT_PREPARE = "payment_intent_prepare"

#: Zahlungsnamen sind gesperrt; die eine Ausnahme steht darueber.
PAYMENT_PREFIXES = ("payment_", "purchase_")

#: Einzelne Namen, die keinem Praefix folgen, aber dieselbe Frage stellen.
BLOCKED_NAMES = frozenset({
    "approval_policy_set",
    "device_revoke",
    "vault_admin",
    "agent_apply_to_production",   # reserviert, in V1 ausdruecklich ungebaut
})

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class CapabilityBlocked(PermissionError):
    """Ein Lauf hat einen Namen genannt, den er strukturell nicht rufen darf."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"agent_capability_blocked:{reason}")
        self.name = name
        self.reason = reason


def is_blocked(name: str) -> str:
    """Der Grund, warum dieser Name gesperrt ist — leer, wenn er erlaubt ist."""
    candidate = (name or "").strip().lower()
    if not candidate or not _NAME.match(candidate):
        return "malformed_name"
    if candidate in BLOCKED_NAMES:
        return "blocked_name"
    if candidate == PAYMENT_PREPARE:
        return ""
    for prefix in PAYMENT_PREFIXES:
        if candidate.startswith(prefix):
            return "payment_family"
    for prefix in BLOCKED_PREFIXES:
        if candidate.startswith(prefix):
            return f"blocked_family:{prefix.rstrip('_')}"
    return ""


def guard(name: str) -> str:
    """Wirft, wenn der Name gesperrt ist. Liefert sonst den normalisierten Namen.

    Fail-closed an einer Stelle, die vor dem Router liegt: ein `capability`-
    Schritt kann einen gesperrten Namen gar nicht erst bilden.
    """
    reason = is_blocked(name)
    if reason:
        raise CapabilityBlocked(name, reason)
    return name.strip().lower()


# =====================================================================
# Provenienz: ehrlich, und sie kennt die Mechanik
# =====================================================================
#
# Die Matrix strengt nur bei `UNTRUSTED_CONTENT` (Overlay); `MODEL_DERIVED`
# hebt den Risikowert, aendert aber keine Matrixzelle. Deshalb die harte Regel:
#
#   * `USER_DIRECT`/`TRUSTED_CONTEXT` nur, wenn ein Argument WOERTLICH aus dem
#     Auftragstext des Nutzers oder aus Core-gemessenen Fakten stammt;
#   * vom Planer formulierte Argumente ohne Spezialisteneinfluss sind
#     `MODEL_DERIVED`;
#   * **alles, was aus Spezialistenausgabe oder fremdem Inhalt abgeleitet ist,
#     wird `UNTRUSTED_CONTENT`** — ein `SpecialistResult` traegt keine
#     Je-Feld-Provenienz, also gibt es fuer seine Inhalte keine mildere
#     Einstufung.

SOURCE_USER = "user"
SOURCE_CORE = "core"
SOURCE_PLANNER = "planner"
SOURCE_SPECIALIST = "specialist"

ARGUMENT_SOURCES = frozenset({SOURCE_USER, SOURCE_CORE, SOURCE_PLANNER,
                              SOURCE_SPECIALIST})


def provenance_for(source: str):
    """Die Provenienz-Stufe fuer eine Argumentquelle.

    Bewusst eine Funktion und kein Woerterbuch, das ein Aufrufer erweitern
    koennte: eine unbekannte Quelle bekommt die STRENGSTE Stufe, nicht die
    bequemste. Wer eine neue Quelle einfuehrt, ohne sie hier einzutragen,
    bekommt damit fail-closed das Overlay — nicht stillschweigend Vertrauen.
    """
    from solvio.capabilities.contract import ArgumentSource as P

    if source == SOURCE_USER:
        return P.USER_DIRECT
    if source == SOURCE_CORE:
        return P.TRUSTED_CONTEXT
    if source == SOURCE_PLANNER:
        return P.MODEL_DERIVED
    return P.UNTRUSTED_CONTENT
