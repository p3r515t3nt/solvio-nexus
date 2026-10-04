"""Was der Resolver gerade tut — in Worten, die auch ein Display versteht.

Noch gibt es kein HUD. Diese Zustaende entstehen trotzdem jetzt, weil sie sonst
spaeter nachtraeglich erfunden werden muessten, und nachtraeglich erfundene
Zustaende beschreiben immer den Code statt den Vorgang.

Sie sind ausserdem der ehrlichste Teil der Beobachtung: `RESEARCHING` heisst, dass
gerade nachgesehen wird, und `TEMPORARILY_BLOCKED` heisst ausdruecklich etwas
anderes als `CAPABILITY_GAP_IDENTIFIED`. Wer diese beiden zusammenwirft, baut
Entwicklungsvorschlaege fuer Netzausfaelle.
"""
from __future__ import annotations

from enum import Enum


class ResolverState(str, Enum):
    GOAL_RECEIVED = "goal_received"
    DIRECT_PATH_UNAVAILABLE = "direct_path_unavailable"
    ALTERNATIVES_CHECKING = "alternatives_checking"
    RESEARCHING = "researching"
    SOLUTION_FOUND = "solution_found"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    CAPABILITY_GAP_IDENTIFIED = "capability_gap_identified"
    PROPOSAL_READY = "proposal_ready"
    TEMPORARILY_BLOCKED = "temporarily_blocked"
    POLICY_LIMITED = "policy_limited"
