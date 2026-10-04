"""Die Prozessgrenze einer Zahlungsfreigabe — Punkt 5 von DEBT-0126.

WARUM ES DIESE DATEI GIBT. Die Live-Abnahme am 2026-08-31 hat den Core echt neu
gestartet, in dem Fenster zwischen einer per Face ID erteilten Freigabe und ihrer
Ausfuehrung. Beobachtet wurde: der frische Core fuehrte nichts aus, verlangte fuer
dieselbe Handlung eine NEUE Freigabe, und der abgeleitete Idempotenzschluessel der
alten Freigabe erreichte den Anbieter nie.

**Was dieser Lauf NICHT zeigen konnte:** ob die alte Freigabe abgelehnt worden
WAERE, haette man sie dem frischen Core vorgelegt. Sie wurde ihm nie vorgelegt —
und sie KANN es ueber die Produktoberflaeche nicht: der Zahlungsendpunkt uebergibt
niemals eine Freigabekennung (`payment/endpoint.py`, `router.execute(...)` ohne
`approval_request_id`), und die prozesslokale Zuordnung Faehigkeit -> Freigabe
(`CapabilityRouter._outstanding`) stirbt mit dem Prozess. Eine Abwesenheit ohne
Reiz ist kein Beweis.

Einen Testpfad in die Produktion zu bauen, nur um den Reiz zu erzeugen, waere die
falsche Antwort: er vergroesserte die Angriffsflaeche, um eine Messung zu
ermoeglichen. Stattdessen wird hier der Reiz DIREKT an der bestehenden
oeffentlichen Naht gesetzt — `MobileApprovalCoordinator.execute_approved`, der
laut eingefrorenem Pfad „the only supported route" ist — und zusaetzlich der
normale Weg ueber den Router gegangen.

WAS EIN „NEUSTART" HIER IST. Die prozesslokale Schicht wird neu gebaut, waehrend
die SQLite-Dateien stehen bleiben: neuer `MobileApprover` (sein `_confirmed` ist
ein Dict im Prozess), neuer Broker, neuer Koordinator, neuer Router (sein
`_outstanding` ebenso). Genau das und nichts anderes verliert ein echter
Neustart; die Datenbank ueberlebt ihn.

**Ehrlich zur Grenze:** der Prozess stirbt hier nicht wirklich. Was diese Datei
beweist, ist die Eigenschaft der prozesslokalen Schicht. Dass ein echter
Prozesstod dieselbe Schicht wegnimmt, ist am 2026-08-31 live gemessen worden
(PID-Wechsel, neuer `gateway_listening`-Eintrag) — beides zusammen traegt Punkt 5,
keines allein.

HERMETISCH: echter Freigabespeicher, echte Kontrollebene, echter Koordinator,
echter S1-Broker, echter Router, echte Zahlungsfaehigkeiten, echter Pruefanbieter
auf der Rueckschleife. Keine Attrappe im Ausfuehrungsweg. Face ID wird NICHT
gefaelscht: die Entscheidung laeuft ueber denselben signierten Weg wie in
`test_p1c_execution_recovery.py`, und die biometrische Naht selbst ist live
belegt und hier nicht Gegenstand.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.

Direkt: python tests/test_payment_restart_boundary.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions  # noqa: E402

enforce_assertions()

import mobile_attest_helper as H                                      # noqa: E402
import payment_harness as PH                                          # noqa: E402
from _guard import require, require_equal                             # noqa: E402

from solvio.capabilities.approval_gateway import CapabilityApprovals   # noqa: E402
from solvio.capabilities.envelope import CapabilityOutcome             # noqa: E402
from solvio.capabilities.payment import register as register_payment   # noqa: E402
from solvio.capabilities.router import CapabilityRouter                # noqa: E402
from solvio.security.approval import ApprovalBroker                    # noqa: E402
from solvio.security.mobile_approval import bridge as B                # noqa: E402
from solvio.security.mobile_approval import control as C               # noqa: E402
from solvio.security.mobile_approval import identity                   # noqa: E402
from solvio.security.mobile_approval import protocol as P              # noqa: E402
from solvio.security.mobile_approval import store as S                 # noqa: E402

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"
OWNER = "local-owner"


class Prozess:
    """Die prozesslokale Schicht des Cores — genau das, was ein Neustart wegnimmt."""

    def __init__(self, store, cp, approver, coordinator, router) -> None:
        self.store = store
        self.cp = cp
        self.approver = approver
        self.coordinator = coordinator
        self.router = router


async def _hochfahren(tmp: str, rig, *, approver=None, outstanding=None) -> Prozess:
    """Faehrt einen Core hoch. Zweimal gerufen = ein Neustart dazwischen.

    `approver` und `outstanding` sind ausschliesslich fuer die Mutationen da: wer
    sie mitgibt, traegt prozesslokale Autoritaet ueber die Grenze — und genau das
    soll die Zusicherung fangen.
    """
    store = S.ApprovalControlStore(os.path.join(tmp, DB))
    await store.open()
    cp = C.MobileApprovalControlPlane(
        store, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID,
        allowed_environments={"development"})
    appr = approver if approver is not None else B.MobileApprover()
    coordinator = B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=appr), appr)
    approvals = CapabilityApprovals(coordinator, owner_principal=OWNER)
    router = CapabilityRouter(mobile=approvals)
    register_payment(router, rig.caps)
    if outstanding is not None:
        router._outstanding.update(outstanding)      # noqa: SLF001 - nur Mutation
    return Prozess(store, cp, appr, coordinator, router)


async def _bis_approved(p1: Prozess, ctx, rig):
    """Der echte Weg: Vorgang -> purchase_place -> Freigabe -> signierte Zustimmung.

    Gibt (approval_id, argumente) zurueck. Nichts ist zu diesem Zeitpunkt
    beansprucht, ausgefuehrt oder belastet.
    """
    view = await PH.prepare_intent(rig)
    args = {"vorgang": view["payment_intent_id"], "pruefsumme": view["pruefsumme"]}

    erste = await _durch_den_router(p1, args)
    require_equal(erste.outcome, CapabilityOutcome.APPROVAL_REQUIRED, str(erste.outcome))
    approval_id = str((erste.data or {}).get("request_id") or "")
    require(approval_id, "der Router hat keine Freigabekennung geliefert")

    wire, _ = await p1.cp.issue_challenge(approval_id=approval_id,
                                          device_id=ctx.device_id)
    _, status = await p1.coordinator.apply_mobile_decision(
        **H.sign_decision(ctx, P.b64d(wire["payload_b64"])))
    require_equal(status, "ok", f"die Zustimmung wurde nicht angenommen: {status}")
    return approval_id, args


async def _durch_den_router(p: Prozess, args: dict):
    """Der NORMALE oeffentliche Weg — dieselbe Herkunft, die der Endpunkt setzt."""
    from solvio.capabilities import policy as AP
    from solvio.capabilities.contract import ArgumentSource
    from solvio.contracts.trust import TrustContext, TrustLevel
    return await p.router.execute(
        "purchase_place", args,
        trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT, user_authorized=True,
                           note="deliberate tap in registered app"),
        provenance={k: ArgumentSource.TRUSTED_CONTEXT for k in args},
        principal="iphone-test-zahlung",
        origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP, commanded=True)


async def _zustand(p: Prozess, approval_id: str, rig) -> dict:
    row = await p.store.get_request(approval_id)
    versuche = await _versuche(p, approval_id)
    stats = await rig.stats()
    return {"state": row["state"] if row else None,
            "ansprueche": versuche,
            "anbieter_calls": stats["charge_attempts"],
            "belastungen": len(rig.charged_rows())}


async def _versuche(p: Prozess, approval_id: str) -> int:
    """Zaehlt Ansprueche direkt in der Ablage — lesend, an der Bruecke vorbei."""
    import sqlite3
    con = sqlite3.connect(f"file:{p.store.path}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM execution_attempts WHERE approval_id=?",
            (approval_id,)).fetchone()[0]
    finally:
        con.close()


# ===================================================== die eine Zusicherung ==
def t_eine_freigabe_ueberlebt_den_prozess_nicht():
    """Punkt 5: nach dem Neustart traegt die alte Freigabe nichts mehr.

    Gemessen wird BEIDES, was der Vertrag meint:

    1. **Der direkte Reiz.** Die alte Freigabe wird dem frischen Koordinator
       ausdruecklich vorgelegt (`execute_approved`, „the only supported route").
       Sie darf nicht ausgefuehrt werden, und der Handler darf nicht laufen.
    2. **Der oeffentliche Weg.** Dieselbe Nutzerhandlung wird erneut eingereicht.
       Sie muss eine NEUE Freigabe verlangen — die alte Autoritaet darf nicht auf
       sie uebertragen werden.

    Und danach: kein Anspruch, kein Anbieteraufruf, keine Belastung.
    """
    async def case():
        rig = await PH.Rig().start()
        tmp = os.path.join(rig.root, "approvals")
        os.makedirs(tmp, exist_ok=True)
        try:
            p1 = await _hochfahren(tmp, rig)
            ctx = await H.enroll_attested(p1.cp, principal=OWNER)
            approval_id, args = await _bis_approved(p1, ctx, rig)

            vorher = await _zustand(p1, approval_id, rig)
            require_equal(vorher["state"], S.APPROVED, str(vorher))
            require_equal(vorher["ansprueche"], 0, "es gab schon einen Anspruch")
            require_equal(vorher["anbieter_calls"], 0, "der Anbieter wurde schon gerufen")
            require_equal(vorher["belastungen"], 0, "es wurde schon belastet")

            # ------------------------------------------------ der Neustart
            p2 = await _hochfahren(tmp, rig)
            require(p2.approver is not p1.approver, "derselbe Approver — kein Neustart")
            require_equal(p2.router._outstanding, {},          # noqa: SLF001
                          "der frische Router kennt noch offene Freigaben")

            # (1) DER DIREKTE REIZ: die alte Freigabe ausdruecklich vorlegen.
            gelaufen = []

            async def executor(action):
                gelaufen.append(action)
                return True, {"nie": "erreicht"}

            ergebnis, status = await p2.coordinator.execute_approved(approval_id, executor)
            require_equal(ergebnis, None, f"die alte Freigabe hat etwas geliefert: {status}")
            require(not gelaufen,
                    "die alte Freigabe hat den Handler laufen lassen — die Autoritaet "
                    "hat den Prozessneustart ueberlebt")
            require(status.startswith("s1_"),
                    f"erwartet wurde eine S1-Absage, bekommen: {status!r}")

            nach_reiz = await _zustand(p2, approval_id, rig)
            require_equal(nach_reiz["state"], S.APPROVED,
                          "die alte Freigabe wurde beansprucht oder verbraucht")
            require_equal(nach_reiz["ansprueche"], 0, "ein Anspruch ist entstanden")
            require_equal(nach_reiz["anbieter_calls"], 0, "der Anbieter wurde gerufen")
            require_equal(nach_reiz["belastungen"], 0, "es wurde belastet")

            # (2) DER OEFFENTLICHE WEG: dieselbe Handlung noch einmal einreichen.
            zweite = await _durch_den_router(p2, args)
            require_equal(zweite.outcome, CapabilityOutcome.APPROVAL_REQUIRED,
                          f"der frische Core hat ohne neue Autoritaet gehandelt: {zweite}")
            neue_id = str((zweite.data or {}).get("request_id") or "")
            require(neue_id, "es wurde keine neue Freigabe verlangt")
            require(neue_id != approval_id,
                    "die alte Autoritaet wurde auf die neue Handlung uebertragen")

            ende = await _zustand(p2, approval_id, rig)
            require_equal(ende["ansprueche"], 0, "die alte Freigabe wurde doch beansprucht")
            require_equal(ende["anbieter_calls"], 0, "der Anbieter wurde doch gerufen")
            require_equal(ende["belastungen"], 0, "es wurde doch belastet")
            require_equal(await _versuche(p2, neue_id), 0,
                          "die neue Freigabe wurde ohne Zustimmung ausgefuehrt")
        finally:
            await rig.stop()
    PH.run(case())


def t_der_frische_core_stuft_die_handlung_nicht_herab():
    """Nach dem Neustart ist die Handlung UNVERAENDERT biometrisch.

    Eine Absage, die aus einer Herabstufung kaeme, waere die falsche Absage. Der
    frische Core muss dieselbe Klasse bilden wie der alte — sonst haette der
    Neustart eine Sicherheitsgrenze bewegt statt einer Prozessgrenze.
    """
    async def case():
        rig = await PH.Rig().start()
        tmp = os.path.join(rig.root, "approvals")
        os.makedirs(tmp, exist_ok=True)
        try:
            p1 = await _hochfahren(tmp, rig)
            ctx = await H.enroll_attested(p1.cp, principal=OWNER)
            approval_id, args = await _bis_approved(p1, ctx, rig)

            alte = await p1.store.get_request(approval_id)
            p2 = await _hochfahren(tmp, rig)
            zweite = await _durch_den_router(p2, args)
            neue = await p2.store.get_request(
                str((zweite.data or {}).get("request_id") or ""))

            require_equal(neue["mode"], alte["mode"], "die Ausfuehrungsklasse hat sich geaendert")
            require_equal(neue["action_digest"], alte["action_digest"],
                          "der frische Core beschreibt dieselbe Handlung anders")
            require_equal(neue["tool"], "purchase_place")
            require_equal(await _versuche(p2, approval_id), 0)
        finally:
            await rig.stop()
    PH.run(case())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
