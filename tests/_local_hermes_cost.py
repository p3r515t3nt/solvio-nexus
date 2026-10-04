"""Lokale Hermes-Fixtures hinter echtem Umschlagadapter und Kostenclaim.

Nur fuer Tests: der produktive Kapselweg bleibt bis zur nativen N3-Route
gesperrt. Ein Abo-Login gilt hier ebensowenig wie produktiv als Kostenbeleg.
Der Beleg bindet die ausdruecklich uebergebene lokale Fixtureinstanz und deren
Quelldatei. Native Anmeldung, API oder Kontoeinstellungen werden nie gelesen.
"""
from __future__ import annotations

import inspect
import os
from unittest.mock import patch

from _guard import require, require_equal
from solvio.agent_runtime import costs as C, cost_dispatch as D, specialists as SP
from solvio.specialists import launcher as L

_NATIVE_SPECIALIST = SP.run_specialist


def install_local_hermes_cost(orch, *, fixture_file: str):
    """Nur die lokale Anbieternaht waehrend eines Fixturetakts ersetzen."""
    local = orch.researcher
    if local is None:
        return orch
    source = os.path.realpath(fixture_file)
    require_equal(os.path.realpath(inspect.getsourcefile(type(local)) or ""), source,
                  "Rechercheur ist nicht in der benannten Testdatei definiert")
    require(os.path.dirname(source) == os.path.dirname(os.path.realpath(__file__)),
            "kein lokaler Testquelltext")
    tick = orch.tick

    def quote(provider, invocation):
        require_equal(provider, SP.HERMES)
        require_equal(invocation.executable, source)
        return D.CostQuote(0, C.CostEvidence("free_local", "fixture:" + os.path.basename(source)))

    async def local_tick():
        original = SP.run_specialist

        async def run(request, *, invocation_factory=None, researcher=None, on_event=None):
            if original is not _NATIVE_SPECIALIST or SP.profile(request.profile).provider != SP.HERMES:
                return await original(request, invocation_factory=invocation_factory,
                                      researcher=researcher, on_event=on_event)
            require(researcher is local, "die belegte lokale Fixtureinstanz wurde ersetzt")
            answers = []

            async def local_call(invocation, prompt):
                answers.append(await SP.run_hermes(request, researcher))
                # Abgeschlossener lokaler Funktionsaufruf; kein gestarteter
                # Anbieterprozess wird behauptet (process_started=None).
                return L.Outcome(True, exit_code=0)

            invocation = L.Invocation(source, ("local-hermes-fixture",),
                                      cwd=os.path.dirname(orch.ledger.path), timeout=5)
            claimed = await D.dispatch(SP.HERMES, invocation, request.objective, local_call)
            require(claimed.outcome.ok, claimed.outcome.reason)
            require_equal(claimed.cost_status, "settled")
            require_equal(len(answers), 1)
            answer = answers[0]
            answer.cost_status = claimed.cost_status
            answer.cost_reservation_id = claimed.reservation_id
            answer.cost_invocation_id = claimed.invocation_id
            return answer

        with patch.object(SP, "run_specialist", run):
            await tick()

    orch.cost_quote_adapter = quote
    orch.tick = local_tick
    return orch
