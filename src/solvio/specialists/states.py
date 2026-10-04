"""Was gerade passiert, wenn SOLVIO Rat einholt.

Wie bei den Resolver-Zustaenden gilt: sie entstehen jetzt, damit sie spaeter
nicht rueckwirkend erfunden werden muessen. Und sie sagen ausdruecklich nichts
ueber den Gedankengang eines Spezialisten — nur darueber, dass gerade einer
befragt wird und was dabei herauskam.
"""
from __future__ import annotations

from enum import Enum


class TeamState(str, Enum):
    #: Der haeufigste Zustand, und der wichtigste: es braucht niemanden.
    NOT_NEEDED = "specialist_team_not_needed"
    CONSULTATION_STARTED = "specialist_consultation_started"
    SCOUT_RUNNING = "scout_running"
    ARCHITECT_RUNNING = "architect_running"
    CHALLENGER_RUNNING = "challenger_running"
    UNAVAILABLE = "specialist_unavailable"
    RESULT_RECEIVED = "specialist_result_received"
    DISAGREE = "specialists_disagree"
    SYNTHESIS = "solution_synthesis"
    READY = "solution_ready"
