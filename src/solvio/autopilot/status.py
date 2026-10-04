"""Status — objektive Zahlen, keine erfundenen Prozente.

„7/10 Acceptance Criteria PROVEN" ist eine Auskunft. „73 % fertig" ohne
objektive Basis ist eine Erfindung, und sie wird geglaubt. Deshalb rechnet
dieses Modul nichts aus, was es nicht gemessen hat.
"""
from __future__ import annotations

import json
from typing import Any

from solvio.autopilot import store as S


def snapshot(ledger: S.AutopilotLedger, milestone_id: str) -> dict[str, Any]:
    zustand = ledger.milestone(milestone_id)
    bewiesen, gesamt = ledger.acceptance_counts(milestone_id)
    findings = ledger.findings(milestone_id, open_only=True)
    grenzen = ledger.open_boundaries(milestone_id)
    phasen = ledger.phases(milestone_id, limit=1)
    letztes_gate = None
    for eintrag in ledger.events(milestone_id, limit=100):
        if eintrag["kind"] == "evidence_recorded" and eintrag["ref"]:
            beleg = ledger.evidence(eintrag["ref"])
            if beleg is not None and beleg.kind == "test_report":
                letztes_gate = {"ok": beleg.ok, "summary": beleg.summary,
                                "commit": beleg.commit[:12],
                                "evidence": beleg.evidence_id}
                break
    return {
        "milestone": milestone_id,
        "state": zustand.state,
        "block_reason": zustand.block_reason or None,
        "builder": zustand.builder or None,
        "model": zustand.model or None,
        "acceptance_proven": bewiesen,
        "acceptance_total": gesamt,
        "current_task": zustand.current_task or None,
        "last_commit": zustand.last_commit[:12] or None,
        "last_gate": letztes_gate,
        "open_findings": [{"id": f.finding_id, "severity": f.severity,
                           "title": f.title} for f in findings],
        "human_required": ([{"id": g["boundary_id"], "category": g["category"],
                             "kind": g["kind"], "question": g["question"]}
                            for g in grenzen] or None),
        "runtime": {"letzte_phase": (phasen[0]["kind"] if phasen else None),
                    "contract_version": zustand.contract_version,
                    "contract_hash": zustand.contract_hash},
        "token_usage": ledger.usage_summary(milestone_id),
        "capacity": {rolle: eintrag["state"]
                     for rolle, eintrag in ledger.capacity(milestone_id).items()},
    }


def assess(ledger: S.AutopilotLedger | None = None) -> tuple[str, str]:
    """Die Probe fuer Doctor und Kontrollzentrum — `(Wort, Grund)`.

    Zeichenketten statt `State`, damit dieses Modul ohne das Kontrollzentrum
    testbar ist. Dieselbe Bauart wie bei Speicher, Tresor und Offsite.

    Zwei Zustaende sind ausdruecklich NICHT gruen, obwohl nichts kaputt ist:
    ein Milestone, der auf den Menschen wartet, und einer, der auf ein
    Kontingent wartet. Beide brauchen etwas — nur eben nichts Technisches.
    """
    # Fail closed ueber die GANZE Messung, nicht nur ueber das Oeffnen. Eine
    # Sonde, die beim Lesen abstuerzt, liefert keine Auskunft — und eine
    # Ausnahme, die nach oben durchschlaegt, macht aus „ich weiss es nicht"
    # ein „das Kontrollzentrum ist kaputt".
    try:
        led = ledger or S.AutopilotLedger()
        laufend = list(led.milestones(active_only=True))
    except Exception as exc:  # noqa: BLE001
        return "unavailable", f"das Autopilot-Buch ist unlesbar: {type(exc).__name__}"
    if not laufend:
        return "unknown", "kein Milestone im Bau"
    wartend = [m for m in laufend if m.state == S.HUMAN_REQUIRED]
    if wartend:
        return "auth_required", (f"{len(wartend)} Milestone(s) warten auf dich: "
                                 f"{', '.join(m.milestone_id for m in wartend)}")
    blockiert = [m for m in laufend if m.state == S.BLOCKED]
    if blockiert:
        gruende = ", ".join(sorted({m.block_reason for m in blockiert}))
        return "degraded", f"{len(blockiert)} Milestone(s) blockiert ({gruende})"
    namen = ", ".join(m.milestone_id for m in laufend[:3])
    return "healthy", f"{len(laufend)} Milestone(s) im Bau: {namen}"
