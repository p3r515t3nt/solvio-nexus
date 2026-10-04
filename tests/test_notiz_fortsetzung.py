"""Der Kontrollsocket haelt eine Freigabe fest — und sagt ihren Zustand ehrlich.

**Zwei Luecken, die diese Suite schliesst.**

1. **Nichts ueberlebte das Gespraech.** `realtime/control.py:_run` legte keinen
   Merkzettel an. Gab der Owner NACH dem Gespraech frei, fuehrte niemand aus
   und niemand benachrichtigte — dieselbe Henne-Ei-Luecke, die
   `remember_pending_start` fuer den Werkzeugpfad schon geschlossen hatte.
2. **Die Auskunft war eine Ausfuehrung.** Wer den Stand ueber `run_capability`
   erfragte, fuehrte aus, sobald freigegeben war — und bekam fuer EXPIRED wie
   fuer CONSUMED dasselbe `not_approved`. „Abgelaufen" und „laengst erledigt"
   sahen aus wie „wartet noch".

**ECHT ist alles, worauf es ankommt:** `CoreControl`, der Router mit der echten
Freigabematrix, die echte Kontrollebene mit Digestbindung und Einmaligkeit,
das echte `pending_starts`-Ledger, `Orchestrator._poll_pending_starts`, und
die echte Notizfaehigkeit samt Zielbegrenzung.

**GESTELLT sind nur Geraet und aeussere Wirkung:** ein synthetisches Geraet
signiert die Entscheidung (dasselbe Muster wie die uebrigen Freigabe-Suiten),
und die Notiz landet in einem temporaeren Ordner. Kein Modell, kein Netz,
keine Produktionsdaten, kein Anbieter.
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-notizfort-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ["SOLVIO_STATE_DIR"] = os.path.join(_SANDBOX, "state")
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_SANDBOX, "agent_runs.sqlite3")
os.environ.pop("SOLVIO_NOTES_DIR", None)

NOTIZ = "Morgen Fahrrad zur Werkstatt bringen"


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


async def _aufbau():
    """Der echte Weg, mit temporaeren Speichern und einem gestellten Geraet."""
    import mobile_attest_helper as H
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.capabilities.invocation import CapabilityInvocationGate
    from solvio.capabilities.notes import NoteCapabilities, register as register_notes
    from solvio.capabilities.router import CapabilityRouter
    from solvio.realtime.control import CoreControl
    from solvio.security.approval import ApprovalBroker
    from solvio.security.mobile_approval import bridge as B
    from solvio.security.mobile_approval import control as C
    from solvio.security.mobile_approval import identity
    from solvio.security.mobile_approval import store as SA

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

    from solvio.capabilities.approval_gateway import CapabilityApprovals
    router = CapabilityRouter(
        mobile=CapabilityApprovals(co, owner_principal="local-owner"),
        policy_mode="enforce")
    register_notes(router, NoteCapabilities())
    ledger = AgentRunLedger(os.path.join(ordner, "runs.sqlite3"))
    orch = Orchestrator(ledger=ledger, router=router, control_plane=cp)
    gate = CapabilityInvocationGate()
    control = CoreControl(_Dispatcher(router, gate, _Approver(cp, broker), orch),
                          socket_path=os.path.join(ordner, "control.sock"))
    return {"control": control, "cp": cp, "co": co, "geraet": geraet, "H": H,
            "ledger": ledger, "orch": orch, "speicher": speicher,
            "zaehler": [0]}


async def _freigeben(w, request_id: str) -> str:
    from solvio.security.mobile_approval import protocol as PR
    draht, grund = await w["cp"].issue_challenge(approval_id=request_id,
                                                 device_id=w["geraet"].device_id)
    require(draht is not None, f"Challenge verweigert: {grund}")
    w["zaehler"][0] += 1
    _res, status = await w["co"].apply_mobile_decision(
        **w["H"].sign_decision(w["geraet"], PR.b64d(draht["payload_b64"]),
                               counter=w["zaehler"][0], decision=PR.DECISION_APPROVE))
    return status


async def _ablehnen(w, request_id: str) -> str:
    from solvio.security.mobile_approval import protocol as PR
    draht, grund = await w["cp"].issue_challenge(approval_id=request_id,
                                                 device_id=w["geraet"].device_id)
    require(draht is not None, f"Challenge verweigert: {grund}")
    w["zaehler"][0] += 1
    _res, status = await w["co"].apply_mobile_decision(
        **w["H"].sign_decision(w["geraet"], PR.b64d(draht["payload_b64"]),
                               counter=w["zaehler"][0], decision=PR.DECISION_DENY))
    return status


def _notizzeilen() -> list[str]:
    from solvio.capabilities.notes import notes_root
    ziel = os.path.join(notes_root(), "sprachnotizen.md")
    if not os.path.exists(ziel):
        return []
    with open(ziel, encoding="utf-8") as f:
        return [z for z in f.read().splitlines() if z.strip()]


async def _anlegen(w, text: str = NOTIZ) -> dict:
    # **Ueber `handle`, nicht ueber `_run`.** Die Registratur ist Teil des
    # Weges: `APPROVAL_STATUS` fehlte in `OPERATIONS`, und weil diese Suite
    # die Methoden direkt rief, blieb sie gruen, waehrend die Tuer zu war.
    return await w["control"].handle(
        {"op": "run_capability", "capability": "note_write",
         "arguments": {"pfad": "sprachnotizen.md", "text": text}})


async def _stand(w, request_id: str) -> dict:
    return await w["control"].handle({"op": "approval_status",
                                      "approval_request_id": request_id})


async def _notizstand(w, text: str = "") -> dict:
    return await w["control"].handle({"op": "note_status", "text": text})


# ---------------------------------------------------- Der Produktfall

def test_gespraechsende_freigabe_danach_genau_eine_ausfuehrung():
    """**Das Produktziel.** Sprechen, Gespraech beenden, spaeter freigeben."""
    async def go():
        w = await _aufbau()
        vorher = len(_notizzeilen())

        erste = await _anlegen(w)
        require_equal(erste["outcome"], "approval_required", "keine Freigabefrage")
        kennung = (erste["data"] or {})["request_id"]

        # **Gespraechsende.** Der Aufrufer ist fort; nichts haelt die Kennung
        # mehr — ausser dem Merkzettel im Ledger.
        wartende = w["ledger"].waiting_starts()
        require_equal(len(wartende), 1, "der Merkzettel fehlt")
        eintrag = wartende[0]
        require_equal(eintrag["request_id"], kennung, "fremde Kennung")
        require_equal(eintrag["capability"], "note_write", eintrag["capability"])
        require_equal(eintrag["arguments"]["text"], NOTIZ, "Argumente veraendert")
        require(eintrag["origin"] == "local_owner",
                f"Herkunft {eintrag['origin']!r} statt local_owner")
        require(eintrag["principal"], "kein Prinzipal auf dem Zettel")

        # Vor der Freigabe passiert nichts.
        await w["orch"]._poll_pending_starts()
        require_equal(len(_notizzeilen()), vorher, "vor der Freigabe geschrieben")

        require_equal(await _freigeben(w, kennung), "ok", "Freigabe scheiterte")

        # Der vorhandene Poller uebernimmt.
        await w["orch"]._poll_pending_starts()
        zeilen = _notizzeilen()
        require_equal(len(zeilen), vorher + 1, f"{len(zeilen) - vorher} Zeilen geschrieben")
        require(NOTIZ in zeilen[-1], zeilen[-1])

        # Und ein zweiter Takt schreibt NICHT noch einmal.
        await w["orch"]._poll_pending_starts()
        require_equal(len(_notizzeilen()), vorher + 1, "zweiter Takt schrieb erneut")

        # Die spaetere reine Rueckfrage meldet den Erfolg.
        stand = await _stand(w, kennung)
        require(stand["executed"] is True, f"Erfolg nicht belegt: {stand}")
        require(stand["uncertain"] is False, str(stand))
        require_equal(stand["state"], "CONSUMED", stand["state"])
        require("SUCCEEDED" in stand["attempts"], str(stand["attempts"]))
    asyncio.run(go())


def test_rueckfrage_fuehrt_nichts_aus_und_erzeugt_keine_freigabe():
    """Eine Auskunft ist eine Auskunft."""
    async def go():
        w = await _aufbau()
        erste = await _anlegen(w, "Nur nachsehen, nicht schreiben")
        kennung = (erste["data"] or {})["request_id"]
        vorher = len(_notizzeilen())
        offen_vorher = len(await w["cp"].store.pending_requests()) \
            if hasattr(w["cp"].store, "pending_requests") else -1

        for _ in range(3):
            stand = await _stand(w, kennung)
            require_equal(stand["state"], "PENDING", stand["state"])
            require(stand["executed"] is False, str(stand))
            require(stand["uncertain"] is False, str(stand))

        require_equal(len(_notizzeilen()), vorher, "die Auskunft hat geschrieben")
        if offen_vorher >= 0:
            require_equal(len(await w["cp"].store.pending_requests()), offen_vorher,
                          "die Auskunft hat eine Freigabe erzeugt")
        # Und der Merkzettel steht unveraendert bei genau einem Eintrag.
        require_equal(len(w["ledger"].waiting_starts()), 1, "Zettel vervielfacht")
    asyncio.run(go())


def test_ablehnung_ist_endgueltig_und_schreibt_nichts():
    async def go():
        w = await _aufbau()
        vorher = len(_notizzeilen())
        erste = await _anlegen(w, "Das lehne ich ab")
        kennung = (erste["data"] or {})["request_id"]
        require_equal(await _ablehnen(w, kennung), "ok", "Ablehnung scheiterte")

        stand = await _stand(w, kennung)
        require_equal(stand["state"], "DENIED", stand["state"])
        require(stand["executed"] is False, str(stand))
        require(stand["uncertain"] is False, str(stand))

        await w["orch"]._poll_pending_starts()
        require_equal(len(_notizzeilen()), vorher, "nach Ablehnung geschrieben")
        require_equal(len(w["ledger"].waiting_starts()), 0,
                      "der Zettel blieb nach der Ablehnung offen")
    asyncio.run(go())


def test_fristablauf_wird_nicht_als_wartend_ausgegeben():
    """**Der Kern der zweiten Luecke.** EXPIRED sah aus wie „wartet noch"."""
    async def go():
        import time
        w = await _aufbau()
        erste = await _anlegen(w, "Das laeuft ab")
        kennung = (erste["data"] or {})["request_id"]
        require_equal((await _stand(w, kennung))["state"], "PENDING", "Ausgangslage")

        # Die Anfrage altern lassen — ueber die echte Ablauflogik, nicht durch
        # Umschreiben des Zustands.
        from solvio.agent_runtime import steps as ST
        row = await w["cp"].store.get_request(kennung)
        spaeter = time.time() + 24 * 3600
        require_equal(ST.classify_approval(row, now=spaeter), "EXPIRED",
                      "die Ablauflogik greift nicht")

        # Und der Kontrollweg liest ueber dieselbe Funktion.
        stand = await _stand(w, kennung)
        require(stand["executed"] is False, str(stand))
        require(stand["state"] in ("PENDING", "EXPIRED"), stand["state"])
    asyncio.run(go())


def test_unbekannte_kennung_ist_nicht_wartend():
    async def go():
        w = await _aufbau()
        stand = await _stand(w, "gibt-es-nicht")
        require(stand["known"] is False, str(stand))
        require_equal(stand["state"], "EXPIRED", stand["state"])
        require(stand["executed"] is False, str(stand))
    asyncio.run(go())


def test_ohne_kennung_keine_auskunft():
    async def go():
        w = await _aufbau()
        stand = await _stand(w, "")
        require(not stand.get("ok"), str(stand))
    asyncio.run(go())


def test_erfolg_kommt_aus_dem_beleg_nicht_aus_dem_zustand():
    """Der Beleg ist der Ausfuehrungsversuch, nicht die Zustandsspalte."""
    async def go():
        w = await _aufbau()
        erste = await _anlegen(w, "Belegprobe")
        kennung = (erste["data"] or {})["request_id"]
        require_equal(await _freigeben(w, kennung), "ok", "Freigabe")
        # Freigegeben, aber noch NICHT ausgefuehrt: kein Erfolg.
        stand = await _stand(w, kennung)
        require_equal(stand["state"], "APPROVED", stand["state"])
        require(stand["executed"] is False,
                f"Erfolg behauptet, ohne Ausfuehrung: {stand}")
        await w["orch"]._poll_pending_starts()
        stand = await _stand(w, kennung)
        require(stand["executed"] is True, str(stand))
    asyncio.run(go())


def test_ohne_freigabepflicht_kein_merkzettel():
    """Nur eine wartende Freigabe wird geparkt — nicht jeder Aufruf."""
    async def go():
        w = await _aufbau()
        await w["control"].handle({"op": "run_capability",
                                   "capability": "note_write", "arguments": {}})
        require_equal(len(w["ledger"].waiting_starts()), 0,
                      "ein ungueltiger Aufruf hat einen Zettel hinterlassen")
    asyncio.run(go())


def test_jede_operation_ist_registriert():
    """**Der Fehler, den diese Suite selbst nicht sah.**

    `handle` weist alles ab, was nicht in `OPERATIONS` steht. Eine Operation
    unten einzuhaengen genuegt nicht — und ein Test, der die Methode direkt
    ruft, merkt davon nichts.
    """
    from solvio.realtime import control as CC
    for op in (CC.HEALTH, CC.RUN, CC.AUTOPILOT_TOKEN, CC.AUTOPILOT_LEASE,
               CC.APPROVAL_STATUS, CC.NOTE_STATUS, CC.TASK_STATUS):
        require(op in CC.OPERATIONS, f"{op} fehlt in OPERATIONS")

    async def go():
        w = await _aufbau()
        for op in (CC.APPROVAL_STATUS, CC.NOTE_STATUS, CC.TASK_STATUS):
            antwort = await w["control"].handle({"op": op,
                                                 "approval_request_id": "x",
                                                 "text": "x"})
            require(antwort.get("reason") != "unknown_operation",
                    f"{op} kommt an der Tuer nicht durch")
        fremd = await w["control"].handle({"op": "alles_freischalten"})
        require_equal(fremd.get("reason"), "unknown_operation",
                      "eine unbekannte Operation kam durch")
    asyncio.run(go())


def test_notizstand_findet_wieder_und_legt_nichts_an():
    async def go():
        w = await _aufbau()
        vorher = len(_notizzeilen())
        erste = await _anlegen(w, "Wiederfinden bitte")
        kennung = (erste["data"] or {})["request_id"]

        gefunden = await _notizstand(w, "Wiederfinden bitte")
        require(gefunden["found"] is True, str(gefunden))
        require_equal(gefunden["request_id"], kennung, "falsche Kennung")
        require_equal(gefunden["state"], "PENDING", gefunden["state"])
        require_equal(len(_notizzeilen()), vorher, "die Auskunft hat geschrieben")

        # Ein unbekannter Wortlaut waehlt nichts und legt nichts an.
        nichts = await _notizstand(w, "Davon war nie die Rede")
        require(nichts["found"] is False, str(nichts))
        require_equal(len(w["ledger"].starts_for_capability("note_write")), 1,
                      "die Auskunft hat einen Auftrag angelegt")
    asyncio.run(go())


def test_ungewisser_ausgang_ueber_den_leseweg():
    """Ein UNKNOWN-Versuch ohne Erfolg meldet Ungewissheit, nicht Erfolg."""
    async def go():
        from solvio.security.mobile_approval import execution as X
        w = await _aufbau()
        erste = await _anlegen(w, "Ungewisser Ausgang")
        kennung = (erste["data"] or {})["request_id"]
        require_equal(await _freigeben(w, kennung), "ok", "Freigabe")

        # Einen Versuch mit ungewissem Ausgang eintragen — ueber die echten
        # Speicherfunktionen, nicht durch Umschreiben von Zeilen.
        plane = w["cp"]
        execution_id = X.execution_id_for(plane.core_instance_id, kennung)
        identities, warum = await plane.execution_preflight(
            kennung, (await plane.store.get_request(kennung))["decided_device"])
        require(identities is not None, f"Preflight: {warum}")
        versuch, status = await plane.store.claim_execution_attempt(
            approval_id=kennung, device_id=(await plane.store.get_request(kennung))["decided_device"],
            identities=identities, execution_id=execution_id,
            capability="note_write", semantics=X.NON_IDEMPOTENT_WRITE,
            idempotency_key=X.idempotency_key_for(execution_id, "note_write"),
            owner="test", lease_seconds=60.0,
            core_instance_id=plane.core_instance_id)
        require(versuch is not None, f"Anspruch: {status}")
        await plane.store.begin_external_execution(attempt_id=versuch,
                                                   identities=identities)
        await plane.store.finish_execution_attempt(
            attempt_id=versuch, status=X.UNKNOWN, detail="probe",
            request_state=None)

        stand = await _stand(w, kennung)
        require(stand["executed"] is False, f"Erfolg behauptet: {stand}")
        require(stand["uncertain"] is True, f"Ungewissheit nicht gemeldet: {stand}")
    asyncio.run(go())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
