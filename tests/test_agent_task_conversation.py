"""Agentenauftraege im Gespraechsweg — der Core-Anteil, am echten Weg.

Der Nutzerablauf, um den es geht: ein laengerer Recherche- oder Bauauftrag
wird im Gespraech erteilt, das Gespraech endet, die Freigabe kommt spaeter,
der Lauf arbeitet — und irgendwann fragt eine FRISCHE Gespraechsinstanz „Was
ist aus meinem Auftrag geworden?", ohne eine Kennung in der Hand zu haben.
Dazu „Habe ich erledigt, mach weiter" und „Brich das ab" — am eindeutig
gewaehlten Auftrag, ueber die vorhandenen Steuerwege.

**ECHT ist alles, worauf es ankommt:** der Kontrollsocket ueber `handle`
(dieselbe Tuer, die der Prototyp benutzt), der Router mit der echten
Freigabematrix, die Kontrollebene mit Digestbindung und Einmaligkeit, das
echte `pending_starts`-Ledger samt neuer Bindung an den Lauf, der echte
Orchestrator mit Takt, Nutzergrenze, Resume und Abbruch, die echte
Auftragsauskunft, der echte proaktive Posteingang — und fuer die Codearbeit
ein echter `WorkspaceManager` auf einem temporaeren Git-Repository mit
echter Ernte.

**GESTELLT sind nur Geraet und Anbietergrenze:** ein synthetisches Geraet
signiert die Entscheidung, der Planer liefert feste Plaene und Urteile, der
Hermes-Seam antwortet in seiner echten Umschlagform, und der Builder ist eine
Funktion, die eine Datei in die Arbeitskopie schreibt. Kein Modell, kein Netz,
kein Unterprozess eines Anbieters, keine Produktionsdaten.

Die lokale Hermes-Testnaht benutzt den echten Umschlagadapter und das echte
Kosten-/Claim-Tor mit ausdruecklichem `free_local`-Beleg. Diese Suite prueft
den Gespraechsvertrag; die native Hermes-Abo-Route wird erst in N3 gebaut.
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-auftrag-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ["SOLVIO_STATE_DIR"] = os.path.join(_SANDBOX, "state")
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_SANDBOX, "agent_runs.sqlite3")

from solvio.agent_runtime import inquiry as Q  # noqa: E402
from solvio.agent_runtime import planner as PL  # noqa: E402
from solvio.agent_runtime import specialists as SP  # noqa: E402
from solvio.agent_runtime import store as S  # noqa: E402
from solvio.realtime import control as CC  # noqa: E402
from solvio.specialists.result import SpecialistResult  # noqa: E402
from _local_hermes_cost import install_local_hermes_cost  # noqa: E402

ZIEL_RECHERCHE = ("Vergleiche die drei Hotels in Hamburg anhand meiner Anforderungen "
                  "und gib mir eine begruendete Empfehlung mit Quellen")
ZIEL_BAU = ("Untersuche den beschriebenen Fehler im Beispielprojekt und bereite "
            "eine gepruefte Korrektur vor")
QUELLEN = ["https://www.beispiel-hotel-a.de/zimmer",
           "https://www.beispiel-hotel-b.de/preise"]
ANTWORT_1 = ("Hotel A liegt zentral und ruhig, Hotel B ist guenstiger, Hotel C hat "
             "fuer morgen keine freien Zimmer mehr.")
ANTWORT_2 = ("Empfehlung: Hotel A — zentral, ruhig, Fruehstueck inklusive; Hotel B "
             "nur als guenstigere Ausweichmoeglichkeit.")
ANFORDERUNGEN = {"auskunft": [{"id": "a1", "text": "Begruendete Empfehlung mit Quellen"}],
                 "handlungen": [], "unklar": [], "belege": {"mindestens": 2}}
URTEIL_FERTIG = {"beantwortet": [{"id": "a1", "belege": list(QUELLEN)}],
                 "offen": [], "fehlend": [], "unsicher": [], "weiterarbeit_noetig": False}
URTEIL_WEITER = {"beantwortet": [], "offen": ["a1"], "fehlend": [], "unsicher": [],
                 "weiterarbeit_noetig": True}
#: Eine Faehigkeit aus `VERY_CRITICAL_BY_BIRTH`, die es heute nicht gibt und die
#: den Lauf-Sperrfilter passiert: aus dem Hintergrund verweigert die Matrix sie
#: (`BACKGROUND × VERY_CRITICAL = DENY`), und genau daraus wird eine
#: Nutzergrenze. Der Handler darf nie laufen.
SEHR_KRITISCH = "security_disable"

SCOUT = PL.PlannedStep(kind="specialist", profile="researcher/hermes",
                       instruction="Vergleiche die drei Hotels")
BAU = PL.PlannedStep(kind="specialist", profile="builder/codex",
                     instruction="Bereite die Korrektur vor")
GRENZE = PL.PlannedStep(kind="capability", capability=SEHR_KRITISCH, arguments={})


# --------------------------------------------------------------- Aufbau

class _Dispatcher:
    """Genau die Attribute, die `CoreControl` anfasst — nicht mehr."""

    def __init__(self, router, gate, approver, agent_runtime) -> None:
        self.capabilities = router
        self.capability_gate = gate
        self.approver_runtime = approver
        self.agent_runtime = agent_runtime


class _Approver:
    def __init__(self, control_plane, approvals) -> None:
        self.control_plane = control_plane
        self.approvals = approvals
        self.port = 0


class Planer:
    """Feste Plaene, feste Anforderungen, feste Urteile — als ROHTEXT wie vom
    Broker, damit der Umschlagweg mitgeprueft wird."""

    def __init__(self, plaene, *, anforderungen=None, urteile=None) -> None:
        import json
        self._json = json
        self.plaene = list(plaene)
        self.anforderungen = anforderungen
        self.urteile = list(urteile or [])
        self.plan_calls = 0
        self.assess_calls = 0

    async def plan(self, *, goal, scope, allowed_profiles, known_capabilities,
                   ledger, run_id, context="", event_ordinal=0, capability_contracts=None):
        self.plan_calls += 1
        ledger.check_planner()
        ledger.note_planner_call()
        steps = self.plaene[min(self.plan_calls - 1, len(self.plaene) - 1)]
        call = PL.PlannerCall(True)
        block = {"schritte": []}
        if self.anforderungen is not None:
            block["anforderungen"] = self.anforderungen
        call.text = self._json.dumps(block, ensure_ascii=False)
        return PL.Plan(goal=goal, steps=tuple(steps)), call

    async def assess(self, *, objective, bound, snapshot_body, run_id, repair_hint=""):
        self.assess_calls += 1
        if not self.urteile:
            return PL.PlannerCall(False, reason="assessment_failed")
        call = PL.PlannerCall(True)
        urteil = self.urteile[min(self.assess_calls - 1, len(self.urteile) - 1)]
        call.text = self._json.dumps(urteil, ensure_ascii=False)
        return call


class Rechercheur:
    """Der Hermes-Seam, ohne Hermes — in GENAU der Umschlagform, die
    `DeepCapabilities._settled` liefert."""

    def __init__(self, antworten=(ANTWORT_1,), quellen=QUELLEN) -> None:
        self.antworten = list(antworten)
        self.quellen = list(quellen)
        self.calls: list[dict] = []

    def _umschlag(self):
        text = self.antworten[min(len(self.calls) - 1, len(self.antworten) - 1)]
        return {"task_id": "dt-test", "status": "succeeded", "lage": "fertig",
                "abgeschlossen": True,
                "ergebnis": {"zusammenfassung": text, "quellen": list(self.quellen),
                             "offene_fragen": []},
                "quellen": list(self.quellen), "content_trust": "untrusted_executor"}

    async def research(self, arguments):
        self.calls.append(arguments)
        return self._umschlag()

    async def status(self, arguments):
        return self._umschlag()


def _bauender_spezialist(datei: str = "korrektur.txt"):
    """Der Builder an der Anbietergrenze: schreibt eine Datei in die
    Arbeitskopie und meldet Erfolg. Kein Codex, kein Unterprozess."""
    async def _run_specialist(request, *, invocation_factory=None, researcher=None, on_event=None):
        if request.workdir:
            with open(os.path.join(request.workdir, datei), "w", encoding="utf-8") as f:
                f.write("korrigiert\n")
        return SP.SpecialistRun(result=SpecialistResult(
            role="builder", provider="codex", question=request.objective,
            ok=True, findings=["Die Korrektur liegt als Datei in der Arbeitskopie."],
            recommended_path="korrektur.txt angelegt"))
    return _run_specialist


async def _aufbau(*, plaene=None, anforderungen=ANFORDERUNGEN, urteile=None,
                  rechercheur=None, arbeitsbereiche=None):
    """Der echte Weg, mit temporaeren Speichern und einem gestellten Geraet."""
    import mobile_attest_helper as H
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.capabilities.agent import AgentCapabilities, register as register_agent
    from solvio.capabilities.approval_gateway import CapabilityApprovals
    from solvio.capabilities.contract import CapabilitySpec, ExecutionClass
    from solvio.capabilities.invocation import CapabilityInvocationGate
    from solvio.capabilities.router import CapabilityRouter
    from solvio.proactive.store import ProactiveStore
    from solvio.realtime.control import CoreControl
    from solvio.security.approval import ApprovalBroker
    from solvio.security.mobile_approval import bridge as B
    from solvio.security.mobile_approval import control as C
    from solvio.security.mobile_approval import identity
    from solvio.security.mobile_approval import store as SA
    from solvio.security.mobile_approval.execution import IDEMPOTENT_WRITE
    from solvio.tools.base import RiskLevel

    ordner = tempfile.mkdtemp(dir=_SANDBOX)
    speicher = SA.ApprovalControlStore(os.path.join(ordner, "approval.sqlite3"))
    await speicher.open()
    cp = C.MobileApprovalControlPlane(
        speicher, identity.MacSigningKey.load_or_create(ordner),
        identity.load_or_create_core_instance_id(ordner),
        attest_verifier=H.fake_verifier(),
        app_id="WQ8CG7R53R.de.solvio.approvals",
        allowed_environments={"development"})
    freigeber = B.MobileApprover()
    broker = ApprovalBroker(approver=freigeber)
    co = B.MobileApprovalCoordinator(cp, broker, freigeber)
    geraet = await H.enroll_attested(cp)

    router = CapabilityRouter(
        mobile=CapabilityApprovals(co, owner_principal="local-owner"),
        policy_mode="enforce")
    posteingang = ProactiveStore(os.path.join(ordner, "proactive.sqlite3"))
    if hasattr(posteingang, "open"):
        await posteingang.open()
    ledger = AgentRunLedger(os.path.join(ordner, "runs.sqlite3"))
    orch = Orchestrator(
        ledger=ledger, router=router, control_plane=cp, proactive=posteingang,
        planner=Planer(plaene if plaene is not None else [[SCOUT]],
                       anforderungen=anforderungen,
                       urteile=urteile if urteile is not None else [URTEIL_FERTIG]),
        researcher=rechercheur if rechercheur is not None else Rechercheur(),
        workspaces=arbeitsbereiche)
    install_local_hermes_cost(orch, fixture_file=__file__)
    register_agent(router, AgentCapabilities(orch))

    async def _nie(arguments):
        raise AssertionError("eine sehr kritische Faehigkeit lief aus einem Lauf")

    router.register(CapabilitySpec(
        name=SEHR_KRITISCH, version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.MUTATING, semantics=IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {}}, executor="inline",
        timeout=5.0, description="Testfaehigkeit: sehr kritisch, aus dem "
                                 "Hintergrund verweigert"), _nie)

    control = CoreControl(_Dispatcher(router, CapabilityInvocationGate(),
                                      _Approver(cp, broker), orch),
                          socket_path=os.path.join(ordner, "control.sock"))
    return {"control": control, "cp": cp, "co": co, "geraet": geraet, "H": H,
            "ledger": ledger, "orch": orch, "speicher": speicher, "router": router,
            "posteingang": posteingang, "ordner": ordner, "zaehler": [0]}


async def _entscheiden(w, request_id: str, *, ja: bool) -> str:
    from solvio.security.mobile_approval import protocol as PR
    draht, grund = await w["cp"].issue_challenge(approval_id=request_id,
                                                 device_id=w["geraet"].device_id)
    require(draht is not None, f"Challenge verweigert: {grund}")
    w["zaehler"][0] += 1
    _res, status = await w["co"].apply_mobile_decision(
        **w["H"].sign_decision(
            w["geraet"], PR.b64d(draht["payload_b64"]), counter=w["zaehler"][0],
            decision=PR.DECISION_APPROVE if ja else PR.DECISION_DENY))
    return status


async def _run(w, capability: str, arguments: dict) -> dict:
    """Ueber `handle` — dieselbe Tuer wie der Prototyp."""
    return await w["control"].handle({"op": "run_capability", "capability": capability,
                                      "arguments": arguments})


async def _auskunft(w, text: str = "", key: str = "") -> dict:
    return await w["control"].handle({"op": "agent_task_status", "text": text,
                                      "key": key})


async def _stand(w, request_id: str) -> dict:
    return await w["control"].handle({"op": "approval_status",
                                      "approval_request_id": request_id})


async def _beauftragen(w, ziel: str = ZIEL_RECHERCHE, *, art: str = "agent_task_research",
                       **mehr) -> str:
    """Ein Auftrag ueber den Socket — gibt die Freigabekennung zurueck."""
    erste = await _run(w, art, {"objective": ziel, **mehr})
    require_equal(erste["outcome"], "approval_required",
                  f"keine Freigabefrage: {erste}")
    kennung = str((erste["data"] or {}).get("request_id") or "")
    require(kennung, "Freigabe ohne Kennung")
    return kennung


async def _starten(w, ziel: str = ZIEL_RECHERCHE, **mehr):
    """Beauftragen, freigeben, den Takt uebernehmen lassen — der Lauf."""
    kennung = await _beauftragen(w, ziel, **mehr)
    require_equal(await _entscheiden(w, kennung, ja=True), "ok", "Freigabe scheiterte")
    await w["orch"]._poll_pending_starts()
    laeufe = w["ledger"].recent_runs()
    require_equal(len(laeufe), 1, f"{len(laeufe)} Laeufe statt genau einem")
    return kennung, laeufe[0]


async def _takte(w, n: int) -> None:
    for _ in range(n):
        await w["orch"].tick()
        run = w["ledger"].recent_runs()
        if run and run[0].terminal:
            return


async def _posteingang(w) -> list[dict]:
    return [dict(x) for x in (await w["posteingang"].unread(limit=50)) or []]


def _altern_lassen(w, request_id: str) -> None:
    """Eine Anfrage verfallen lassen — ueber die Zeile, nicht den Zustand."""
    with sqlite3.connect(w["speicher"].path) as db:
        db.execute("UPDATE approval_requests SET expires_at=1 WHERE approval_id=?",
                   (request_id,))


async def _versuch_buchen(w, request_id: str, status: str, request_state=None) -> None:
    """Einen Ausfuehrungsversuch ins echte Journal — ueber die Speicherfunktionen."""
    from solvio.security.mobile_approval import execution as X
    plane = w["cp"]
    execution_id = X.execution_id_for(plane.core_instance_id, request_id)
    row = await plane.store.get_request(request_id)
    identities, warum = await plane.execution_preflight(request_id, row["decided_device"])
    require(identities is not None, f"Preflight: {warum}")
    versuch, st = await plane.store.claim_execution_attempt(
        approval_id=request_id, device_id=row["decided_device"],
        identities=identities, execution_id=execution_id,
        capability=str(row.get("tool") or ""), semantics=X.NON_IDEMPOTENT_WRITE,
        idempotency_key=X.idempotency_key_for(execution_id, str(row.get("tool") or "")),
        owner="test", lease_seconds=60.0, core_instance_id=plane.core_instance_id)
    require(versuch is not None, f"Anspruch: {st}")
    await plane.store.begin_external_execution(attempt_id=versuch, identities=identities)
    await plane.store.finish_execution_attempt(attempt_id=versuch, status=status,
                                               detail="probe", request_state=request_state)


def _beispielrepo() -> str:
    """Ein temporaeres Git-Repository mit einem Commit — das „Projekt"."""
    repo = os.path.realpath(tempfile.mkdtemp(dir=_SANDBOX, prefix="repo-"))
    env = {"PATH": "/usr/bin:/bin", "HOME": repo, "GIT_CONFIG_NOSYSTEM": "1",
           "LANG": "C.UTF-8"}
    subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main", repo], check=True, env=env)
    with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
        f.write("# Beispielprojekt\n")
    subprocess.run(["/usr/bin/git", "-C", repo, "add", "-A"], check=True, env=env)
    subprocess.run(["/usr/bin/git", "-C", repo, "-c", "user.name=Test",
                    "-c", "user.email=test@solvio.local", "commit", "-q",
                    "-m", "Ausgangslage"], check=True, env=env)
    return repo


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": repo, "GIT_CONFIG_NOSYSTEM": "1",
           "LANG": "C.UTF-8"}
    return subprocess.run(["/usr/bin/git", "-C", repo, *args], env=env,
                          capture_output=True, text=True)


# =====================================================================
# 1 — Auftrag anfragen, Gespraech beenden, danach freigeben: EIN Lauf,
#     Ergebnis und Posteingang ueber den tatsaechlichen Abschlussweg
# =====================================================================

def test_auftrag_anfragen_gespraechsende_freigabe_ein_lauf_ergebnis_posteingang():
    async def go():
        w = await _aufbau()
        kennung = await _beauftragen(w)

        # Der Merkzettel — und noch KEIN Lauf, auch nicht nach einem Takt.
        zettel = w["ledger"].waiting_starts()
        require_equal(len(zettel), 1, "der Merkzettel fehlt")
        require_equal(zettel[0]["capability"], "agent_task_research", zettel[0])
        require_equal(zettel[0]["arguments"]["objective"], ZIEL_RECHERCHE,
                      "Auftragstext veraendert")
        require_equal(zettel[0]["origin"], "local_owner", zettel[0]["origin"])
        require_equal(zettel[0]["run_id"], "", "vor der Freigabe ein Lauf verzeichnet")
        await w["orch"]._poll_pending_starts()
        require_equal(w["ledger"].recent_runs(), [], "vor der Freigabe entstand ein Lauf")

        # Die Auskunft kennt den Auftrag schon VOR dem Lauf — ohne Kennung.
        a = await _auskunft(w)
        require(a["found"] is True, str(a))
        require_equal(a["auftrag"]["phase"], Q.PHASE_BEFORE_RUN, str(a["auftrag"]))
        require_equal(a["auftrag"]["zustand_code"], Q.FREIGABE_OFFEN, str(a["auftrag"]))
        require_equal(a["auftrag"]["auftrag"], ZIEL_RECHERCHE, "Wortlaut fehlt")
        require_equal(a["auftrag"]["art"], "Recherche", a["auftrag"]["art"])

        # Gespraechsende. Danach die Freigabe am (gestellten) Geraet.
        require_equal(await _entscheiden(w, kennung, ja=True), "ok", "Freigabe")
        await w["orch"]._poll_pending_starts()
        laeufe = w["ledger"].recent_runs()
        require_equal(len(laeufe), 1, f"{len(laeufe)} Laeufe statt genau einem")
        run = laeufe[0]
        # Ein zweiter Takt erzeugt keinen zweiten Lauf.
        await w["orch"]._poll_pending_starts()
        require_equal(len(w["ledger"].recent_runs()), 1, "ein zweiter Lauf entstand")

        # Der Lauf laeuft ueber den ECHTEN Takt zu Ende.
        await _takte(w, 14)
        final = w["ledger"].get_run(run.run_id)
        require_equal(final.state, S.SUCCEEDED,
                      f"{final.state} / {final.failure_category} / {final.result_summary}")

        # Das Ergebnis liegt im ECHTEN Posteingang — mit Befunden und Quellen.
        meldungen = [m for m in await _posteingang(w) if m.get("lauf") == run.run_id]
        require(meldungen, "keine Abschlussmeldung im Posteingang")
        require(any("Quelle:" in b for b in meldungen[0]["befunde"]),
                f"keine Quelle in der Meldung: {meldungen[0]}")

        # Die FRISCHE Instanz findet ihn ohne Kennung — mit Ergebnis und Belegen.
        a = await _auskunft(w)
        require(a["found"] is True and not a["ambiguous"], str(a))
        sicht = a["auftrag"]
        require_equal(sicht["phase"], Q.PHASE_RUN, str(sicht))
        require_equal(sicht["kennung"], run.run_id, "falscher Lauf")
        require_equal(sicht["zustand_code"], S.SUCCEEDED, sicht["zustand_code"])
        require_equal(sicht["zustand"], "fertig", sicht["zustand"])
        require_equal(sicht["auftrag"], ZIEL_RECHERCHE, "Wortlaut fehlt in der Laufsicht")
        require(sicht["ergebnis"], "kein Ergebnis in der Sicht")
        require_equal(sorted(sicht["quellen"]), sorted(QUELLEN), f"Quellen: {sicht['quellen']}")
        require(any(ANTWORT_1 in b for b in sicht["befunde"]), f"Befunde: {sicht['befunde']}")
        require_equal(sicht["freigabe"], kennung, "die Startfreigabe fehlt in der Sicht")
        require(sicht["wartet_auf"] is None, "eine Grenze, die es nicht gibt")

        # Der exakte Wortlaut und jede Kennung fuehren zum Auftrag; aehnliche
        # Woerter nennen ihn nur als Kandidaten zur Bestaetigung.
        a = await _auskunft(w, text=ZIEL_RECHERCHE)
        require(a["found"] is True and a["treffer"] == "wortlaut", str(a))
        require_equal(a["auftrag"]["kennung"], run.run_id, "Wortlaut")
        a = await _auskunft(w, text="die Hotels in Hamburg vergleichen")
        require(a["found"] is False and a.get("bestaetigung_noetig") is True, str(a))
        require_equal(a["reason"], "aehnlich", str(a))
        require_equal([k["kennung"] for k in a["candidates"]], [run.run_id], str(a))
        for key in (run.run_id, run.task_id, kennung):
            a = await _auskunft(w, key=key)
            require(a["found"] is True and a["treffer"] == "kennung", f"{key}: {a}")
            require_equal(a["auftrag"]["kennung"], run.run_id, key)
        nichts = await _auskunft(w, key="ar-gibtsnicht")
        require(nichts["found"] is False and not nichts["ambiguous"], str(nichts))
    asyncio.run(go())


# =====================================================================
# 2 — Die Freigabe kennt ihren Lauf aus gebundenen Daten, nie aus Aehnlichkeit
# =====================================================================

def test_freigabe_und_lauf_sind_im_buch_verbunden_nicht_erraten():
    async def go():
        w = await _aufbau()
        # Zwei Auftraege mit DEMSELBEM Wortlaut — ein ausdruecklich neuer.
        # **Gemessen am Router:** die zweite Anfrage derselben Faehigkeit
        # verdraengt die offene erste (`superseded_by_new_request`), die damit
        # als verfallen gilt. Genau deshalb muss die Bindung an den Lauf aus
        # der Ausfuehrung kommen — beide Zeilen tragen denselben Wortlaut.
        erste = await _beauftragen(w)
        zweite = await _beauftragen(w)
        require(erste != zweite, "dieselbe Freigabekennung fuer zwei Anfragen")
        require_equal((await _stand(w, erste))["state"], "EXPIRED",
                      "die verdraengte Anfrage gilt noch als offen")
        # Nur die zweite wird freigegeben.
        require_equal(await _entscheiden(w, zweite, ja=True), "ok", "Freigabe")
        await w["orch"]._poll_pending_starts()
        laeufe = w["ledger"].recent_runs()
        require_equal(len(laeufe), 1, f"{len(laeufe)} Laeufe")
        run = laeufe[0]

        # Die Bindung steht im Buch — bei der ZWEITEN, nicht bei der ersten.
        start = w["ledger"].start_for_run(run.run_id)
        require(start is not None, "der Lauf kennt seine Freigabe nicht")
        require_equal(start["request_id"], zweite, "an die falsche Freigabe gebunden")
        require_equal(w["ledger"].get_pending_start(erste)["run_id"], "",
                      "die nicht freigegebene Anfrage bekam einen Lauf zugeschrieben")
        events = [e for e in w["ledger"].events_for_run(run.run_id)
                  if e.kind == "approval_resolved" and e.ref == zweite]
        require(events, "das Ereignis der Startfreigabe fehlt am Lauf")
        # Und die Bindung ist einmalig: ein zweiter Versuch aendert nichts.
        require(not w["ledger"].bind_pending_start_run(zweite, "ar-fremd"),
                "die Bindung liess sich umschreiben")
        require_equal(w["ledger"].start_for_run(run.run_id)["request_id"], zweite)

        # Die Auskunft: gleicher Wortlaut ⇒ ZWEI Kandidaten, KEINE Wahl.
        a = await _auskunft(w, text=ZIEL_RECHERCHE)
        require(a["ambiguous"] is True and a["auftrag"] is None, str(a))
        phasen = sorted(k["phase"] for k in a["candidates"])
        require_equal(phasen, [Q.PHASE_RUN, Q.PHASE_BEFORE_RUN], str(a["candidates"]))
        # Die erste Anfrage bleibt, was sie ist: verfallen und ohne Lauf —
        # obwohl ihr Wortlaut dem des Laufs gleicht.
        a = await _auskunft(w, key=erste)
        require_equal(a["auftrag"]["zustand_code"], Q.ABGELAUFEN, str(a["auftrag"]))
        require_equal(a["auftrag"]["lauf"], "", "ein Lauf wurde erraten")
    asyncio.run(go())


# =====================================================================
# 3 — Offen, abgelehnt, abgelaufen und ungewiss: wahrheitsgemaess wiedergefunden
# =====================================================================

def test_offene_abgelehnte_abgelaufene_und_ungewisse_starts_werden_wahr_wiedergefunden():
    async def go():
        from solvio.security.mobile_approval import execution as X
        from solvio.security.mobile_approval import store as SA
        w = await _aufbau()
        # **Der Reihe nach, nicht auf einmal:** der Router haelt je Faehigkeit
        # hoechstens EINE offene Anfrage und verdraengt die vorige. Jeder
        # Zustand entsteht deshalb, bevor die naechste Anfrage gestellt wird.
        abgelehnt = await _beauftragen(w, "Recherchiere die Preise der Fahrradwerkstatt")
        require_equal(await _entscheiden(w, abgelehnt, ja=False), "ok", "Ablehnung")
        # **Gemessen am Router:** die NAECHSTE Anfrage derselben Faehigkeit
        # bekommt die Ablehnung quittiert — auch mit anderem Ziel, und ohne
        # Kennung. Erst die Anfrage danach entsteht. Der Sprachadapter kennt
        # diese Quittung und beauftragt nichts doppelt (siehe DEBT-Eintrag).
        quittung = await _run(w, "agent_task_research",
                              {"objective": "Recherchiere den Zugfahrplan nach Berlin"})
        require_equal(quittung["reason"], "denied", str(quittung))
        require(not quittung.get("data"), "die Quittung trug eine Kennung")
        abgelaufen = await _beauftragen(w, "Recherchiere den Zugfahrplan nach Berlin")
        _altern_lassen(w, abgelaufen)
        # Der Takt schliesst Ablehnung und Verfall — und startet NICHTS.
        await w["orch"]._poll_pending_starts()
        require_equal(w["ledger"].recent_runs(), [], "ein Lauf entstand")

        # Ungewiss: freigegeben, ein Versuch mit ungewissem Ausgang, kein Lauf.
        ungewiss = await _beauftragen(w, "Recherchiere die Wetterlage am Wochenende")
        require_equal(await _entscheiden(w, ungewiss, ja=True), "ok", "Freigabe")
        await _versuch_buchen(w, ungewiss, X.UNKNOWN)
        # Alt: eingeloest (SUCCEEDED), genommen — aber kein Lauf verzeichnet.
        # Das ist die Zeile von VOR der Spalte `run_id`; sie bekommt keine
        # erfundene Verbindung.
        alt = await _beauftragen(w, "Recherchiere die Parkplaetze am Bahnhof")
        require_equal(await _entscheiden(w, alt, ja=True), "ok", "Freigabe")
        require(w["ledger"].claim_pending_start(alt), "Anspruch")
        await _versuch_buchen(w, alt, X.SUCCEEDED, request_state=SA.CONSUMED)
        # Offen: zuletzt gestellt, noch unentschieden.
        offen = await _beauftragen(w, "Recherchiere die Oeffnungszeiten der Bibliothek")

        erwartet = {offen: Q.FREIGABE_OFFEN, abgelehnt: Q.ABGELEHNT,
                    abgelaufen: Q.ABGELAUFEN, ungewiss: Q.START_UNGEWISS,
                    alt: Q.START_UNGEWISS}
        for key, code in erwartet.items():
            a = await _auskunft(w, key=key)
            require(a["found"] is True, f"{code}: {a}")
            require_equal(a["auftrag"]["zustand_code"], code,
                          f"{key}: {a['auftrag']['zustand_code']} statt {code}")
            require_equal(a["auftrag"]["phase"], Q.PHASE_BEFORE_RUN, code)
            require_equal(a["auftrag"]["lauf"], "", f"{code}: ein Lauf wurde erraten")
        # Ohne Wortlaut: fuenf Auftraege, KEINE Wahl — und der offene zuerst.
        a = await _auskunft(w)
        require(a["ambiguous"] is True and a["auftrag"] is None, str(a))
        require_equal(a["gesamt"], 5, str(a))
        require_equal(a["candidates"][0]["kennung"], offen,
                      f"der offene Auftrag steht nicht vorn: {a['candidates']}")
        # Ein Wortlaut, der auf nichts passt, waehlt nichts und nennt Kandidaten.
        a = await _auskunft(w, text="Buche mir ein Konzertticket")
        require(a["found"] is False and a["ambiguous"] is False, str(a))
        require_equal(a["reason"], "nicht_gefunden", a.get("reason"))
        require(a["candidates"], "keine Kandidaten genannt")
        require_equal(w["ledger"].recent_runs(), [], "die Auskunft erzeugte einen Lauf")
    asyncio.run(go())


def test_juengster_auftrag_bleibt_neben_ungewissem_altbestand_sichtbar():
    async def go():
        from solvio.security.mobile_approval import execution as X
        from solvio.security.mobile_approval import store as SA
        w = await _aufbau()
        alte = []
        for i in range(7):
            key = await _beauftragen(w, f"Alte Recherche zu Solarstrom Variante {i}")
            await _entscheiden(w, key, ja=True)
            require(w["ledger"].claim_pending_start(key), "alter Start nicht genommen")
            await _versuch_buchen(w, key, X.SUCCEEDED, request_state=SA.CONSUMED)
            alte.append(key)
        _, run = await _starten(w)
        await w["orch"]._finish(run.run_id, S.FAILED, "budget_exhausted",
                                "Die Recherche lieferte kein Ergebnis.")
        a = await _auskunft(w)
        require(a["ambiguous"] and not a["found"], "neuester wurde automatisch gewaehlt")
        require_equal(a["candidates"][0]["kennung"], run.run_id,
                      "der juengste Auftrag wurde von Altzetteln verdraengt")
        require_equal(a["candidates"][0]["zustand_code"], S.FAILED)
        require_equal(a["weitere"], 3, "der Ausschnitt verschweigt weitere Auftraege")
        for key in alte:
            alt = await _auskunft(w, key=key)
            require_equal(alt["auftrag"]["zustand_code"], Q.START_UNGEWISS)
            require_equal(w["ledger"].get_pending_start(key)["run_id"], "")
        such = await _auskunft(w, text="Hotels in Hamburg")
        require(such["bestaetigung_noetig"] and not such["found"], str(such))
        require_equal([c["kennung"] for c in such["candidates"]], [run.run_id])
        require_equal(len(w["ledger"].recent_runs()), 1, "Suche erzeugte einen Lauf")
    asyncio.run(go())


def test_zustandsworte_der_startfreigabe():
    """Die Tabelle hinter `start_state` — jede Zeile eine Beweislast."""
    wartend = {"request_id": "ap-1", "state": S.START_WAITING, "run_id": ""}
    genommen = {"request_id": "ap-1", "state": S.START_TAKEN, "run_id": ""}
    faelle = [
        ({"request_id": "ap-1", "state": S.START_TAKEN, "run_id": "ar-1"}, None, Q.GESTARTET),
        (wartend, None, Q.UNBEKANNT),
        (wartend, {"ok": False}, Q.UNBEKANNT),
        (wartend, {"ok": True, "state": "PENDING"}, Q.FREIGABE_OFFEN),
        (wartend, {"ok": True, "state": "APPROVED"}, Q.FREIGABE_ERTEILT),
        (wartend, {"ok": True, "state": "DENIED"}, Q.ABGELEHNT),
        (wartend, {"ok": True, "state": "EXPIRED"}, Q.ABGELAUFEN),
        (wartend, {"ok": True, "state": "APPROVED", "uncertain": True}, Q.START_UNGEWISS),
        (genommen, {"ok": True, "state": "CONSUMED", "executed": True}, Q.START_UNGEWISS),
        (genommen, {"ok": True, "state": "FAILED", "attempts": ["FAILED_SAFE"]},
         Q.START_GESCHEITERT),
        (genommen, {"ok": True, "state": "APPROVED"}, Q.START_UNGEWISS),
    ]
    for eintrag, approval, erwartet in faelle:
        require_equal(Q.start_state(eintrag, approval), erwartet,
                      f"{eintrag['state']}/{approval}: {Q.start_state(eintrag, approval)}")
    require(set(Q.START_WORDS) == Q.START_STATES, "ein Startzustand hat kein Wort")


# =====================================================================
# 4 — Eine Statusfrage startet, setzt fort und bricht nichts ab
# =====================================================================

def test_statusfragen_erzeugen_weder_freigabe_noch_lauf_noch_zettel():
    async def go():
        w = await _aufbau()
        kennung = await _beauftragen(w)

        async def lage():
            return (len(await w["cp"].store.list_pending()),
                    len(w["ledger"].starts_for_capability("agent_task_research")),
                    len(w["ledger"].starts_for_capability("agent_run_resume")),
                    len(w["ledger"].recent_runs()),
                    (await _stand(w, kennung))["state"])

        vorher = await lage()
        for _ in range(3):
            await _auskunft(w)
            await _auskunft(w, text=ZIEL_RECHERCHE)
            await _auskunft(w, key=kennung)
        # Auch die Laufansicht ueber den Router liest nur.
        status = await _run(w, "agent_run_status", {})
        require(status["ok"], str(status))
        require_equal(await lage(), vorher, "eine Statusfrage hat etwas veraendert")
        require_equal(vorher[4], "PENDING", "die Freigabe ist nicht mehr offen")
    asyncio.run(go())


# =====================================================================
# 5 — Nutzergrenze: verstaendlich ausgegeben, und die Fortsetzung setzt
#     DENSELBEN Lauf ueber den vorhandenen Resume-Weg fort
# =====================================================================

def test_nutzergrenze_wird_ausgegeben_und_die_fortsetzung_setzt_denselben_lauf_fort():
    async def go():
        w = await _aufbau(plaene=[[SCOUT, GRENZE, SCOUT]],
                          urteile=[URTEIL_WEITER, URTEIL_FERTIG],
                          rechercheur=Rechercheur(antworten=(ANTWORT_1, ANTWORT_2)))
        _kennung, run = await _starten(w)
        for _ in range(8):
            await w["orch"].tick()
            if w["ledger"].get_run(run.run_id).state == S.WAITING_USER:
                break
        zwischen = w["ledger"].get_run(run.run_id)
        require_equal(zwischen.state, S.WAITING_USER,
                      f"keine Nutzergrenze: {zwischen.state} {zwischen.result_summary}")

        # Die Auskunft sagt konkret, was noetig ist und wie es weitergeht.
        a = await _auskunft(w, key=run.run_id)
        grenze = a["auftrag"]["wartet_auf"]
        require(grenze, "die Grenze fehlt in der Auskunft")
        require_equal(grenze["art"], "policy_refusal", str(grenze))
        require("iPhone" in grenze["handlung"], grenze["handlung"])
        require(grenze["danach"], "was danach passiert, fehlt")
        require_equal(a["auftrag"]["zustand"], "wartet auf dich", a["auftrag"]["zustand"])
        # Die Meldung der Grenze liegt im Posteingang.
        require(any(m.get("lauf") == run.run_id for m in await _posteingang(w)),
                "die Grenze wurde nicht gemeldet")

        # „Habe ich erledigt, mach weiter" — vom lokalen Rechner kostet das
        # Face ID. Die Anfrage landet im Merkzettel, nicht in der Luft.
        r = await _run(w, "agent_run_resume", {"run_id": run.run_id})
        require_equal(r["outcome"], "approval_required", str(r))
        fortsetzung = str((r["data"] or {}).get("request_id") or "")
        zettel = [z for z in w["ledger"].waiting_starts()
                  if z["capability"] == "agent_run_resume"]
        require_equal(len(zettel), 1, "die Wiederaufnahme hat keinen Merkzettel")
        require_equal(zettel[0]["request_id"], fortsetzung)
        require_equal(zettel[0]["arguments"], {"run_id": run.run_id})
        # Und die Laufsicht weiss, dass diese Freigabe offen ist.
        a = await _auskunft(w, key=run.run_id)
        require(a["auftrag"]["fortsetzung"], "die offene Wiederaufnahme fehlt in der Sicht")
        require_equal(a["auftrag"]["fortsetzung"]["kennung"], fortsetzung)

        # Statusfragen setzen nichts fort.
        for _ in range(2):
            await _auskunft(w, key=run.run_id)
            await w["orch"].tick()
        require_equal(w["ledger"].get_run(run.run_id).state, S.WAITING_USER,
                      "eine Statusfrage hat den Lauf fortgesetzt")

        # Gespraechsende. Danach die Freigabe — und der Takt nimmt DENSELBEN
        # Lauf wieder auf.
        require_equal(await _entscheiden(w, fortsetzung, ja=True), "ok", "Freigabe")
        await w["orch"]._poll_pending_starts()
        danach = w["ledger"].get_run(run.run_id)
        require(danach.state != S.WAITING_USER,
                f"die Freigabe hat den Lauf nicht wieder aufgenommen: {danach.state}")
        require([e for e in w["ledger"].events_for_run(run.run_id)
                 if e.kind == "boundary_resumed"], "kein boundary_resumed im Buch")
        await _takte(w, 14)
        final = w["ledger"].get_run(run.run_id)
        require(final.terminal, f"der Lauf endete nicht: {final.state}")
        require_equal(len(w["ledger"].recent_runs()), 1, "ein zweiter Lauf entstand")
        schritte = w["ledger"].steps_for_run(run.run_id)
        dritter = [s for s in schritte if s.seq == 3 and s.kind == "specialist"]
        require(dritter and dritter[0].state == "succeeded",
                f"der Schritt nach der Grenze lief nicht: {[(s.seq, s.kind, s.state) for s in schritte]}")
        require_equal(final.specialist_count, 2, "der Lauf hat nach der Grenze nicht weitergearbeitet")
        # Was der Lauf danach meldet, ist die Wahrheit des Cores — hier
        # festgehalten, nicht beschoenigt: der aus Politikgruenden verweigerte
        # Schritt bleibt im Buch stehen und zaehlt gegen den Abschluss.
        a = await _auskunft(w, key=run.run_id)
        require(a["auftrag"]["wartet_auf"] is None, "die Grenze steht nach dem Ende noch")
        require(a["auftrag"]["fortsetzung"] is None, "die Wiederaufnahme gilt noch als offen")
        require(any(ANTWORT_2 in b for b in a["auftrag"]["befunde"]),
                f"das Ergebnis nach der Grenze fehlt: {a['auftrag']['befunde']}")
        return final
    final = asyncio.run(go())
    print(f"  [Messung] Ausgang nach Nutzergrenze und Fortsetzung: "
          f"{final.state} / {final.failure_category or '-'}")


# =====================================================================
# 6 — Abbruch beendet den gewaehlten Lauf; Gespraechsende allein tut das nicht
# =====================================================================

def test_abbruch_beendet_den_gewaehlten_lauf_gespraechsende_nicht():
    async def go():
        w = await _aufbau(plaene=[[SCOUT, SCOUT, SCOUT]])
        _kennung, run = await _starten(w)
        # Die Instanz, die den Auftrag gab, ist fort. Der Lauf arbeitet weiter.
        await w["orch"].tick()
        require(not w["ledger"].get_run(run.run_id).terminal,
                "das Gespraechsende hat den Lauf beendet")

        # Ein zweiter, offener Start daneben — der Abbruch darf ihn nicht treffen.
        andere = await _beauftragen(w, "Recherchiere etwas ganz anderes bitte")

        r = await _run(w, "agent_run_cancel", {"run_id": run.run_id})
        require(r["ok"] is True, str(r))
        require_equal(w["ledger"].get_run(run.run_id).state, S.CANCELLED)
        a = await _auskunft(w, key=run.run_id)
        require_equal(a["auftrag"]["zustand"], "abgebrochen", a["auftrag"]["zustand"])
        require(a["auftrag"]["offen"] is False, "abgebrochen und trotzdem offen")

        # Ein zweiter Abbruch findet nichts mehr, und der andere Start blieb offen.
        r = await _run(w, "agent_run_cancel", {"run_id": run.run_id})
        require(r["ok"] is False and r["reason"] == "not_cancellable", str(r))
        r = await _run(w, "agent_run_cancel", {"run_id": andere})
        require(r["ok"] is False, "eine Freigabekennung galt als abbrechbarer Lauf")
        a = await _auskunft(w, key=andere)
        require_equal(a["auftrag"]["zustand_code"], Q.FREIGABE_OFFEN, str(a["auftrag"]))
    asyncio.run(go())


# =====================================================================
# 7 — Codearbeit ueber den vorgesehenen Weg: vorbereitet, nicht uebernommen
# =====================================================================

def test_codearbeit_laeuft_ueber_den_vorgesehenen_weg_und_wird_als_vorbereitet_gemeldet():
    async def go():
        from solvio.agent_runtime import workspace as WS
        repo = _beispielrepo()
        kopf_vorher = _git(repo, "rev-parse", "HEAD").stdout.strip()
        w = await _aufbau(plaene=[[BAU]], anforderungen=None,
                          arbeitsbereiche=WS.WorkspaceManager(allowed=(repo,)))
        gesichert = (SP.builder_available, SP.run_specialist)
        SP.builder_available = lambda spec: (True, "")
        SP.run_specialist = _bauender_spezialist()
        try:
            _kennung, run = await _starten(w, ZIEL_BAU, art="agent_task_build",
                                           repository=repo)
            await _takte(w, 12)
            final = w["ledger"].get_run(run.run_id)
        finally:
            SP.builder_available, SP.run_specialist = gesichert
        require_equal(final.state, S.SUCCEEDED,
                      f"{final.state} / {final.failure_category} / {final.result_summary}")
        require(final.branch_ref.startswith("refs/agents/"), final.branch_ref)

        a = await _auskunft(w, key=run.run_id)
        sicht = a["auftrag"]
        require_equal(sicht["art"], "Codearbeit", sicht["art"])
        require_equal(sicht["projekt"], repo, sicht["projekt"])
        require(sicht["vorbereitet"] is True and sicht["uebernommen"] is False, str(sicht))
        require_equal(sicht["arbeitsergebnis"], final.branch_ref)
        require("uebernehmen ist deine Entscheidung" in sicht["ergebnis"], sicht["ergebnis"])
        require(sicht["pruefung"] and sicht["pruefung"]["zustand"] == "succeeded",
                f"der Pruefstand fehlt: {sicht.get('pruefung')}")

        # Das Projekt selbst ist unberuehrt; die Ernte liegt im Ernte-Repo.
        require_equal(_git(repo, "rev-parse", "HEAD").stdout.strip(), kopf_vorher,
                      "der Produktivzweig hat sich bewegt")
        require_equal(_git(repo, "status", "--porcelain").stdout.strip(), "",
                      "das Projekt traegt Aenderungen")
        require("agent/" not in _git(repo, "branch", "--list").stdout,
                "der Agentenzweig liegt im Projekt")
        ernte = _git(WS.harvest_path(), "rev-parse", final.branch_ref)
        require_equal(ernte.returncode, 0, f"die Ernte fehlt: {ernte.stderr}")
        require("korrektur.txt" in _git(WS.harvest_path(), "ls-tree", "--name-only",
                                        final.branch_ref).stdout,
                "die vorbereitete Korrektur ist nicht in der Ernte")
    asyncio.run(go())


# =====================================================================
# 8 — Verschwindet der Builder zwischen Anfrage und Freigabe, ist der Start
#     ungewiss, nicht gestartet — und niemand erfindet einen Lauf
# =====================================================================

def test_ein_start_ohne_lauf_bleibt_ungewiss_und_wird_gemeldet():
    async def go():
        w = await _aufbau(plaene=[[BAU]], anforderungen=None)
        gesichert = SP.builder_available
        SP.builder_available = lambda spec: (True, "")
        try:
            kennung = await _beauftragen(w, ZIEL_BAU, art="agent_task_build")
            SP.builder_available = lambda spec: (False, "sandbox_missing")
            require_equal(await _entscheiden(w, kennung, ja=True), "ok", "Freigabe")
            await w["orch"]._poll_pending_starts()
        finally:
            SP.builder_available = gesichert
        require_equal(w["ledger"].recent_runs(), [], "ein Lauf entstand ohne Builder")
        a = await _auskunft(w, key=kennung)
        require(a["found"] is True, str(a))
        require(a["auftrag"]["zustand_code"] in (Q.START_UNGEWISS, Q.START_GESCHEITERT),
                a["auftrag"]["zustand_code"])
        require_equal(a["auftrag"]["lauf"], "", "ein Lauf wurde erraten")
        require(any("nicht mehr starten" in str(m.get("zusammenfassung"))
                    for m in await _posteingang(w)),
                "der gescheiterte Start wurde nicht gemeldet")
        return a["auftrag"]["zustand_code"]
    code = asyncio.run(go())
    print(f"  [Messung] Start ohne Builder nach Freigabe: {code}")


# =====================================================================
# 8a — Aehnliche Woerter sind Kandidaten, keine Identitaet
# =====================================================================

def test_aehnliche_woerter_sind_keine_identitaet():
    """**Das Gegenbeispiel aus dem Review.** Anfrage „Vergleiche Hotels in
    Hamburg", gespeichert „... in Berlin": vorher wurde der Berliner Auftrag
    als der gesuchte zurueckgegeben. Jetzt ist er ein Kandidat zur
    Bestaetigung — und ein Auftrag mit anderer Stadt nie der gesuchte."""
    async def go():
        w = await _aufbau()
        berlin = "Vergleiche Hotels in Berlin fuer das naechste Wochenende"
        kennung = await _beauftragen(w, berlin)
        a = await _auskunft(w, text="Vergleiche Hotels in Hamburg")
        require(a["ok"] is True and a["found"] is False, str(a))
        require(a["auftrag"] is None, "ein anderer Auftrag wurde als der gesuchte ausgegeben")
        require_equal(a["reason"], "aehnlich", str(a))
        require(a.get("bestaetigung_noetig") is True, str(a))
        require_equal([k["kennung"] for k in a["candidates"]], [kennung], str(a))
        require("Berlin" in a["candidates"][0]["auftrag"], str(a["candidates"]))
        # Der exakte Wortlaut und die Kennung binden weiterhin.
        a = await _auskunft(w, text=berlin)
        require(a["found"] is True and a["treffer"] == "wortlaut", str(a))
        a = await _auskunft(w, text=berlin.upper() + "!")
        require(a["found"] is True and a["treffer"] == "wortlaut", "Schreibung entscheidet")
        a = await _auskunft(w, key=kennung)
        require(a["found"] is True and a["treffer"] == "kennung", str(a))
        # Ohne jede Aehnlichkeit: nichts gefunden, Kandidaten nur genannt.
        a = await _auskunft(w, text="Buche mir ein Konzertticket")
        require(a["found"] is False and a["reason"] == "nicht_gefunden", str(a))
        require(a.get("bestaetigung_noetig") is not True, str(a))

        # Zwei aehnliche Auftraege: zwei Kandidaten, keine Wahl — auch nicht
        # der juengere oder der offene.
        _k2, run = await _starten(w)          # ZIEL_RECHERCHE: die Hamburger
        a = await _auskunft(w, text="Vergleiche Hotels in Berlin")
        require(a["found"] is False and a["reason"] == "aehnlich", str(a))
        require_equal(sorted(k["kennung"] for k in a["candidates"]),
                      sorted([kennung, run.run_id]), str(a["candidates"]))
        # Und nichts davon hat etwas angelegt.
        require_equal(len(w["ledger"].recent_runs()), 1, "die Suche erzeugte einen Lauf")
    asyncio.run(go())


# =====================================================================
# 8b — Ein Lesefehler ist kein leerer Bestand
# =====================================================================

def test_ein_lesefehler_ist_kein_leerer_bestand():
    """**Aus dem Review vom 09.09.2026.** Wer nach einem ungewissen Start
    nachsieht und „keine Auftraege" hoert, glaubt, sein Auftrag sei nie
    angekommen. Ein unlesbares Buch muss deshalb als unlesbar zurueckkommen —
    ueber die Tuer, nicht nur aus der Funktion."""
    async def go():
        w = await _aufbau()
        kennung = await _beauftragen(w)
        echt = w["orch"].ledger

        class Kaputt:
            """Das echte Buch — bis auf die eine Lesung, die scheitert."""
            def __init__(self, was: str) -> None:
                self.was = was
            def __getattr__(self, name):
                if name == self.was:
                    def _scheitert(*a, **k):
                        raise OSError("disk I/O error")
                    return _scheitert
                return getattr(echt, name)

        try:
            for lesung in ("recent_runs", "starts_for_capability"):
                w["orch"].ledger = Kaputt(lesung)
                for args in ({}, {"text": ZIEL_RECHERCHE}, {"key": kennung}):
                    a = await w["control"].handle({"op": "agent_task_status", **args})
                    require(a["ok"] is False, f"{lesung}/{args}: {a}")
                    require_equal(a["reason"], "ledger_unreadable", f"{lesung}/{args}: {a}")
                    require(a["found"] is False and not a["candidates"],
                            f"{lesung}/{args}: ein Lesefehler sah aus wie ein Bestand: {a}")
        finally:
            w["orch"].ledger = echt
        # Mit lesbarem Buch ist der Auftrag da — der Bestand war nie leer.
        a = await _auskunft(w, key=kennung)
        require(a["ok"] is True and a["found"] is True, str(a))
    asyncio.run(go())


# =====================================================================
# 9 — Die Tuer, die Vokabeln und die eine Wortliste
# =====================================================================

@contextlib.contextmanager
def _inquiry_result(*, findings=True, verify=True, state=S.FAILED,
                    category="goal_unverified", scope=S.SCOPE_RESEARCH,
                    summary="Eine Flugoption ist gefunden; der Gepäckpreis fehlt."):
    """Persisted result shape only: no planner, provider or authority creation."""
    with tempfile.TemporaryDirectory(prefix="solvio-inquiry-result-") as directory:
        ledger = S.AgentRunLedger(os.path.join(directory, "runs.sqlite3"))
        task = ledger.create_task(objective="Vergleiche Flüge mit Gepäck nach New York",
            scope=scope, created_origin="trusted_interactive_app", created_principal="test-owner")
        run = ledger.create_run(task_id=task.task_id)
        for status in (S.PLANNING, S.RUNNING, S.VERIFYING):
            ledger.transition(run.run_id, status)
        if verify:
            step = ledger.create_step(run_id=run.run_id, seq=1, kind="verify")
            ledger.update_step(step.step_id, state="succeeded", finished=True,
                               summary="Alle Schritte haben ein Ergebnis.")
        body = json.dumps({"befunde": ["Eine Flugoption ist gefunden."] if findings else [],
                           "quellen": ["https://example.org/flights"]}).encode()
        path = os.path.join(directory, "report.json")
        with open(path, "wb") as handle:
            handle.write(body)
        ledger.add_artifact(run_id=run.run_id, kind="report", path=path,
                            sha256=hashlib.sha256(body).hexdigest(), size=len(body))
        final = ledger.transition(run.run_id, state, failure_category=category,
                                  result_summary=summary)
        yield ledger, task, final


def test_unvollstaendige_recherche_zeigt_keine_erfolgreiche_abschlusspruefung():
    with _inquiry_result() as (ledger, task, final):
        before = (ledger.get_task(task.task_id), ledger.get_run(final.run_id),
                  ledger.steps_for_run(final.run_id), ledger.events_for_run(final.run_id))
        view = Q.run_view(ledger, final)
        require_equal(view["pruefung"]["zustand"], "failed",
                      "ein technisch fertiger Schritt ist keine bestätigte Auftragserfüllung")
        require("nicht bestätigt" in view["pruefung"]["zusammenfassung"])
        require_equal(view["zustand"], "Recherche unvollständig")
        require_equal(view["zustand_code"], S.FAILED)
        require_equal(view["grund_code"], "goal_unverified")
        require_equal(view["ergebnis"], final.result_summary)
        require_equal(view["befunde"], ["Eine Flugoption ist gefunden."])
        require_equal(view["quellen"], ["https://example.org/flights"])
        require_equal(view["offen"], False)
        require_equal(view["fortsetzung"], None)
        require_equal((ledger.get_task(task.task_id), ledger.get_run(final.run_id),
                       ledger.steps_for_run(final.run_id), ledger.events_for_run(final.run_id)), before)


def test_unbestaetigter_abschluss_ohne_befunde_erfindet_kein_teilergebnis():
    for verify in (False, True):
        for summary in ("", "Ich konnte das Ergebnis nicht beurteilen lassen."):
            with _inquiry_result(findings=False, verify=verify, summary=summary) as (ledger, _task, final):
                view = Q.run_view(ledger, final)
                require_equal(view["zustand"], "Abschluss nicht bestätigt")
                require_equal(view["zustand_code"], S.FAILED)
                require_equal(view["pruefung"]["zustand"], "failed")
                require("nicht bestätigt" in view["pruefung"]["zusammenfassung"])
                require("ich habe ein Ergebnis" not in view["grund"])
                require("Teilergebnis" not in json.dumps(view, ensure_ascii=False))
                require_equal(view["ergebnis"], summary)
                require_equal(view["befunde"], [])
                require_equal(view["grund_code"], "goal_unverified")


def test_auskunft_belaesst_erfolg_technischen_fehler_und_anderen_umfang():
    for state, category, scope, label, verification in (
        (S.SUCCEEDED, "", S.SCOPE_RESEARCH, "fertig", "succeeded"),
        (S.FAILED, "specialist_failed", S.SCOPE_RESEARCH, "fehlgeschlagen", "succeeded"),
        (S.FAILED, "goal_unverified", S.SCOPE_BUILD, "Abschluss nicht bestätigt", "failed"),
    ):
        with _inquiry_result(state=state, category=category, scope=scope) as (ledger, _task, final):
            view = Q.run_view(ledger, final)
            require_equal(view["zustand"], label)
            require_equal(view["zustand_code"], state)
            require_equal(view["grund_code"], category)
            require_equal(view["pruefung"]["zustand"], verification)
            require_equal(view["ergebnis"], final.result_summary)


def test_die_auskunft_ist_ueber_die_tuer_erreichbar_und_teilt_ihre_vokabeln():
    from solvio.agent_runtime import endpoint as E
    from solvio.agent_runtime import steps as ST
    from solvio.tools.agent_capability_tools import (CONTINUABLE_CAPABILITIES,
                                                     CREATION_CAPABILITIES)
    require(CC.TASK_STATUS in CC.OPERATIONS, "agent_task_status fehlt in OPERATIONS")
    require_equal(set(Q.SCOPE_OF), set(CREATION_CAPABILITIES),
                  "die Auskunft kennt andere Startfaehigkeiten als der Merkzettel")
    require("agent_run_resume" in CONTINUABLE_CAPABILITIES
            and "agent_run_resume" in ST.RESUMABLE_STARTS,
            "die Wiederaufnahme ueberlebt das Gespraechsende nicht")
    require(E._WORDS is Q.STATE_WORDS and E._REASONS is Q.REASON_WORDS,
            "der Nexus-Endpunkt fuehrt eine zweite Wortliste")
    require_equal(set(Q.STATE_WORDS), set(S.ALL_STATES), "ein Zustand ohne Wort")
    require_equal(set(Q.REASON_WORDS), set(S.FAILURE_CATEGORIES), "ein Grund ohne Wort")

    async def go():
        w = await _aufbau()
        antwort = await w["control"].handle({"op": "agent_task_status"})
        require(antwort.get("reason") != "unknown_operation", "die Tuer ist zu")
        require(antwort["ok"] is True and antwort["found"] is False, str(antwort))
        require_equal(antwort["reason"], "keine_auftraege", str(antwort))
    asyncio.run(go())


def test_lange_kandidaten_tragen_originalwortlaut_und_behalten_ihre_kennung():
    async def go():
        w = await _aufbau()
        prefix = "Vergleiche die Hotels anhand aller genannten Anforderungen. " * 4
        objectives = [prefix + "Ziel ist Hamburg-Altona.", prefix + "Ziel ist Berlin-Mitte."]
        erwartete = {}
        for objective in objectives:
            _task, run = w["orch"].create_task(objective=objective, scope="research",
                origin="trusted_interactive_app", principal="test-owner")
            erwartete[run.run_id] = objective
        vorher = [(r.run_id, r.state) for r in w["ledger"].recent_runs()]
        antwort = await _auskunft(w)
        require(antwort["ambiguous"] and not antwort["found"], str(antwort))
        kandidaten = antwort["candidates"]
        require_equal(len(kandidaten), 2)
        require_equal(len({k["auftrag"] for k in kandidaten}), 1,
                      "die Ausgangslage hat keine identischen Kurztexte")
        for kandidat in kandidaten:
            require_equal(kandidat["auftrag_vollstaendig"], erwartete[kandidat["kennung"]])
            require_equal(len(kandidat["auftrag"]), Q.MAX_OBJECTIVE_IN_CANDIDATE)
            # Die echte Kandidatenkennung fuehrt zum Auftrag. Der gekuerzte
            # Text darf weiterhin keinen eindeutigen Treffer behaupten.
            gelesen = await _auskunft(w, key=kandidat["kennung"])
            require(gelesen["found"] and gelesen["treffer"] == "kennung", str(gelesen))
            require_equal(gelesen["auftrag"]["auftrag"], kandidat["auftrag_vollstaendig"])
        kurz = await _auskunft(w, text=kandidaten[0]["auftrag"])
        require(not kurz["found"] and kurz["reason"] == "aehnlich", str(kurz))
        require_equal([(r.run_id, r.state) for r in w["ledger"].recent_runs()], vorher)
        require_equal(w["ledger"].waiting_starts(), [], "die Lesung erzeugte einen Start")
    asyncio.run(go())


def test_kandidatenvolltext_bleibt_auch_bei_uebergrossem_start_altbestand_begrenzt():
    async def go():
        w = await _aufbau()
        for suffix in ("a", "b"):
            w["ledger"].remember_pending_start(request_id="alter-start-" + suffix,
                capability="agent_task_research",
                arguments={"objective": "Historischer Rechercheauftrag " + suffix * (S.MAX_OBJECTIVE + 100)},
                principal="test-owner", origin="local_owner", commanded=True)
        vorher = w["ledger"].waiting_starts()
        antwort = await _auskunft(w)
        require(antwort["ambiguous"] and not antwort["found"], str(antwort))
        require_equal(len(antwort["candidates"]), 2)
        for kandidat in antwort["candidates"]:
            require_equal(len(kandidat["auftrag_vollstaendig"]), S.MAX_OBJECTIVE)
            require_equal(len(kandidat["auftrag"]), Q.MAX_OBJECTIVE_IN_CANDIDATE)
            require(kandidat["kennung"].startswith("alter-start-"))
        require_equal(w["ledger"].waiting_starts(), vorher, "die Auskunft veraenderte den Altbestand")
        require_equal(w["ledger"].recent_runs(), [], "die Auskunft startete Arbeit")
    asyncio.run(go())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
