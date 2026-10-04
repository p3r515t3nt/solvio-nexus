"""`decided_at` ist der Zeitpunkt der MENSCHLICHEN Entscheidung — und bleibt es.

WARUM ES DIESE DATEI GIBT. Bei der Live-Abnahme von DEBT-0126 am 2026-08-31 fiel
im produktiven Freigabespeicher eine Zeile auf, die nicht sein kann: der Anspruch
`att-bd39f9de…` traegt `claimed_at` 110 ms VOR dem `decided_at` der Freigabe, auf
die er sich stuetzt. Die Ursache war kein Uhrenproblem, sondern die Spalte selbst:
`decided_at` wurde bei JEDEM Zustandswechsel neu gestempelt — beim Anspruch und
beim Verbrauch ebenso wie bei der Entscheidung. Bei einer verbrauchten Freigabe
stand danach der Verbrauchszeitpunkt drin.

Bei einer Zahlung ist das die eine Angabe, auf die es ankommt: WANN hat der Mensch
zugestimmt. Ein Buch, das sie ueberschreibt, kann eine Geldbewegung nicht mehr
belegen — und es behauptet dabei etwas Falsches, statt nur zu schweigen.

WAS NICHT GEAENDERT WURDE. Keine Sicherheitssemantik. Die Praedikate des Anspruchs
(Zustand, Geraet, Frist, Attestierung, Widerruf) stehen unveraendert; `decided_at`
war nie eines von ihnen. Es gibt keine neue Spalte und keine Migration: die
spaeteren Zeitpunkte sind laengst und genauer woanders aufgeschrieben —
`execution_attempts.claimed_at`, `boundary_at`, `finished_at` und jeder Uebergang
mit eigenem `ts` in `audit`.

EHRLICH ZU ALTEN ZEILEN. Freigaben, die VOR dieser Aenderung verbraucht wurden,
tragen weiterhin den Zeitpunkt ihres letzten Zustandswechsels. Das wird nicht
repariert: eine Migration muesste den Zeitpunkt der Zustimmung erfinden, und
erfundene Zeiten in einem Zahlungsbuch sind schlimmer als fehlende. Sie sind
historisch unvollstaendig, und das steht in DEBT-0155.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.

Direkt: python tests/test_approval_decision_time.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions  # noqa: E402

enforce_assertions()

import mobile_attest_helper as H                                   # noqa: E402
from _guard import require, require_equal                          # noqa: E402

from solvio.security.approval import ApprovalBroker                 # noqa: E402
from solvio.security.mobile_approval import bridge as B             # noqa: E402
from solvio.security.mobile_approval import control as C           # noqa: E402
from solvio.security.mobile_approval import identity                # noqa: E402
from solvio.security.mobile_approval import protocol as P           # noqa: E402
from solvio.security.mobile_approval import store as S              # noqa: E402

APP_ID = "WQ8CG7R53R.de.solvio.approvals"
DB = "approval_control.sqlite3"


async def _wire(tmp):
    st = S.ApprovalControlStore(os.path.join(tmp, DB))
    await st.open()
    cp = C.MobileApprovalControlPlane(
        st, identity.MacSigningKey.load_or_create(tmp),
        identity.load_or_create_core_instance_id(tmp),
        attest_verifier=H.fake_verifier(), app_id=APP_ID,
        allowed_environments={"development"})
    approver = B.MobileApprover()
    return st, cp, B.MobileApprovalCoordinator(cp, ApprovalBroker(approver=approver),
                                               approver)


async def _anfrage(cp, tmp, tool="t_non_idempotent"):
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    return await cp.create_request(principal="local-owner", tool=tool, mode="modify",
                                   task="etwas tun", workspace=ws,
                                   human_summary="Testfall")


async def _entscheiden(cp, co, ctx, aid, *, decision=P.DECISION_APPROVE):
    wire, _ = await cp.issue_challenge(approval_id=aid, device_id=ctx.device_id)
    _, status = await co.apply_mobile_decision(
        **H.sign_decision(ctx, P.b64d(wire["payload_b64"]), decision=decision))
    require_equal(status, "ok", f"Entscheidung nicht angenommen: {status}")


def _run(coro):
    return asyncio.run(coro)


def t_der_zeitpunkt_der_zustimmung_ueberlebt_anspruch_und_verbrauch():
    """Der Kern: nach Anspruch UND Verbrauch steht immer noch die Zustimmung da."""
    async def case():
        with tempfile.TemporaryDirectory() as tmp:
            st, cp, co = await _wire(tmp)
            ctx = await H.enroll_attested(cp)
            aid = await _anfrage(cp, tmp)
            await _entscheiden(cp, co, ctx, aid)

            nach_zustimmung = (await st.get_request(aid))["decided_at"]
            require(nach_zustimmung, "die Zustimmung wurde gar nicht gestempelt")

            # Genug Abstand, damit ein erneuter Stempel sichtbar waere.
            await asyncio.sleep(0.05)

            async def executor(action):
                await asyncio.sleep(0.02)
                return True, {"ok": True}

            ergebnis, status = await co.execute_approved(aid, executor)
            require_equal(status, "ok", f"die Ausfuehrung schlug fehl: {status}")

            row = await st.get_request(aid)
            require_equal(row["state"], S.CONSUMED)
            require_equal(row["decided_at"], nach_zustimmung,
                          "der Zeitpunkt der menschlichen Zustimmung wurde "
                          "ueberschrieben — bei einer Zahlung waere damit nicht mehr "
                          "belegbar, wann der Mensch zugestimmt hat")

            # Und die spaeteren Zeitpunkte sind NICHT verloren: sie stehen im Journal,
            # und ihre Reihenfolge ist jetzt ehrlich.
            import sqlite3
            con = sqlite3.connect(f"file:{st.path}?mode=ro", uri=True)
            try:
                a = con.execute(
                    "SELECT claimed_at, boundary_at, finished_at FROM execution_attempts "
                    "WHERE approval_id=?", (aid,)).fetchone()
            finally:
                con.close()
            require(a is not None, "es gibt keinen Anspruch im Journal")
            claimed, boundary, finished = a
            require(claimed >= nach_zustimmung,
                    f"der Anspruch liegt VOR der Zustimmung: {claimed} < {nach_zustimmung}")
            require(boundary >= claimed, "die durable Grenze liegt vor dem Anspruch")
            require(finished >= boundary, "der Ausgang liegt vor der durablen Grenze")
    _run(case())


def t_eine_ablehnung_stempelt_und_bleibt_stehen():
    """Auch ein Nein ist eine Entscheidung und traegt ihren Zeitpunkt."""
    async def case():
        with tempfile.TemporaryDirectory() as tmp:
            st, cp, co = await _wire(tmp)
            ctx = await H.enroll_attested(cp)
            aid = await _anfrage(cp, tmp)
            vorher = time.time()
            await _entscheiden(cp, co, ctx, aid, decision=P.DECISION_DENY)
            row = await st.get_request(aid)
            require_equal(row["state"], S.DENIED)
            require(row["decided_at"] >= vorher,
                    "die Ablehnung traegt keinen plausiblen Zeitpunkt")
    _run(case())


def t_ohne_entscheidung_bleibt_der_zeitpunkt_leer():
    """Eine Anfrage, die niemand beantwortet hat, behauptet keine Entscheidung.

    Der Sweeper und `abandon` bewegen den Zustand, nicht die Zustimmung. Ein
    `decided_at` auf einer nie entschiedenen Anfrage waere eine Aussage ueber
    einen Menschen, den es an dieser Stelle nicht gab.
    """
    async def case():
        with tempfile.TemporaryDirectory() as tmp:
            st, cp, co = await _wire(tmp)
            aid = await _anfrage(cp, tmp)
            require_equal((await st.get_request(aid))["decided_at"], None)
            await st.transition(aid, S.EXPIRED, error="orphaned_by_core_restart")
            row = await st.get_request(aid)
            require_equal(row["state"], S.EXPIRED)
            require_equal(row["decided_at"], None,
                          "eine nie entschiedene Anfrage behauptet einen "
                          "Entscheidungszeitpunkt")
            require_equal(row["error"], "orphaned_by_core_restart")
    _run(case())


def t_ein_gescheiterter_ausgang_ruehrt_den_zeitpunkt_nicht_an():
    """Auch die terminalen Zustaende NACH der Ausfuehrung lassen ihn stehen.

    `FAILED` entsteht ueber denselben Uebergang wie `CONSUMED`. Waere dort ein
    Stempel, ginge der Zeitpunkt der Zustimmung bei genau den Vorgaengen verloren,
    bei denen man ihn am dringendsten braucht — den gescheiterten.
    """
    async def case():
        with tempfile.TemporaryDirectory() as tmp:
            st, cp, co = await _wire(tmp)
            ctx = await H.enroll_attested(cp)
            aid = await _anfrage(cp, tmp)
            await _entscheiden(cp, co, ctx, aid)
            zustimmung = (await st.get_request(aid))["decided_at"]
            await asyncio.sleep(0.05)

            async def executor(action):
                from solvio.security.mobile_approval.execution import SafeExecutionFailure
                raise SafeExecutionFailure("nichts_passiert")

            _, status = await co.execute_approved(aid, executor)
            require_equal(status, "failed_safe", status)
            row = await st.get_request(aid)
            require_equal(row["state"], S.FAILED)
            require_equal(row["decided_at"], zustimmung,
                          "ein gescheiterter Ausgang hat den Zeitpunkt der "
                          "Zustimmung ueberschrieben")
    _run(case())


def t_der_wiederanlauf_ruehrt_den_zeitpunkt_nicht_an():
    """Ein Neustart schaut ins Journal — er datiert keine Zustimmung um.

    `startup_recovery_scan` laeuft bei JEDEM Hochfahren. Schriebe es dabei
    `decided_at`, waere nach dem ersten Neustart jede Zustimmung auf den
    Startzeitpunkt des Cores datiert.
    """
    async def case():
        with tempfile.TemporaryDirectory() as tmp:
            st, cp, co = await _wire(tmp)
            ctx = await H.enroll_attested(cp)
            aid = await _anfrage(cp, tmp)
            await _entscheiden(cp, co, ctx, aid)
            zustimmung = (await st.get_request(aid))["decided_at"]
            await asyncio.sleep(0.05)

            # Der Neustart: prozesslokale Schicht neu, Ablage bleibt.
            st2, cp2, co2 = await _wire(tmp)
            bericht = await co2.startup_recovery_scan()
            require(isinstance(bericht, dict), "der Wiederanlauf lieferte nichts")
            require_equal((await st2.get_request(aid))["decided_at"], zustimmung,
                          "der Wiederanlauf hat den Zeitpunkt der Zustimmung "
                          "umdatiert")

            # Und auch der Fristablauf laesst ihn stehen.
            await st2.transition(aid, S.EXPIRED, error="orphaned_by_core_restart")
            row = await st2.get_request(aid)
            require_equal(row["state"], S.EXPIRED)
            require_equal(row["decided_at"], zustimmung,
                          "der Fristablauf hat den Zeitpunkt der Zustimmung "
                          "ueberschrieben")
    _run(case())


def t_die_autoritaetssemantik_ist_unveraendert():
    """Mobile predicates remain; N2/N5 have closed browser authorizations.

    Keine Methode gewinnt Befugnis durch einen Zeitstempel. Die drei echten
    Entscheidungen duerfen stempeln; Anspruch, Verbrauch und Wiederanlauf nicht.
    Der mobile SQL-Zweig behaelt Geraet, Attestierung und Widerruf; Dashboard
    benutzt den eigenen, erneut gegen den echten Store geprueften Sitzungsbeleg.
    """
    quelle = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                               "security", "mobile_approval", "store.py"),
                  encoding="utf-8").read()
    baum = ast.parse(quelle)
    klasse = next(n for n in baum.body
                  if isinstance(n, ast.ClassDef) and n.name == "ApprovalControlStore")
    methoden = {n.name: n for n in klasse.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    anspruch = methoden["_claim_execution"]
    verzweigung = next(n for n in ast.walk(anspruch) if isinstance(n, ast.If)
        and ast.unparse(n.test) == "type(identities) in (DashboardAuthorization, BrowserCommandAuthorization)")
    mobile_sql = " ".join(n.value for stmt in verzweigung.orelse for n in ast.walk(stmt)
                          if isinstance(n, ast.Constant) and isinstance(n.value, str))
    for pflicht in ("decided_device=?", "d.status=?", "d.attestation_status=?",
                    "NOT EXISTS (SELECT 1 FROM revocations"):
        require(pflicht in mobile_sql, f"das mobile Anspruchspraedikat verlor {pflicht!r}")
    sql = " ".join(n.value for n in ast.walk(anspruch)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))
    for pflicht in ("WHERE approval_id=? AND state=?", "expires_at > ?"):
        require(pflicht in sql, f"das gemeinsame Anspruchspraedikat verlor {pflicht!r}")
    for name in ("_claim_execution", "_claim_execution_attempt", "_execution_authority",
                 "_recheck_execution_authority", "_dashboard_authorization",
                 "_dashboard_request_reason", "_browser_session",
                 "_browser_command_authorization", "_browser_command_attempt_reason"):
        require("decided_at" not in ast.unparse(methoden[name]),
                f"decided_at ist in den Befugnisweg {name} gewandert")

    # AST statt Wortsuche: genau die drei geprueften Entscheidungen benutzen
    # denselben CAS mit literal True. Kein spaeterer Zustand darf erneut stempeln.
    eltern = {kind: elternteil for elternteil in ast.walk(baum)
              for kind in ast.iter_child_nodes(elternteil)}
    aufrufe = []
    for node in ast.walk(baum):
        if not isinstance(node, ast.Call):
            continue
        stempel = [k for k in node.keywords if k.arg == "stamp_decision"]
        if not stempel:
            continue
        require_equal(len(stempel), 1)
        require(isinstance(stempel[0].value, ast.Constant) and stempel[0].value.value is True)
        require_equal(ast.unparse(node.func), "self._cas_transition")
        besitzer = []
        umgebung = node
        while umgebung in eltern:
            umgebung = eltern[umgebung]
            if isinstance(umgebung, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                besitzer.append(umgebung.name)
        aufrufe.append(tuple(reversed(besitzer)))
    require_equal(sorted(aufrufe), [
        ("ApprovalControlStore", "_commit_dashboard_decision"),
        ("ApprovalControlStore", "_commit_decision"),
        # N5: the explicit, closed owner-memory command is itself its decision.
        ("ApprovalControlStore", "_prepare_browser_memory_command")],
        f"Entscheidungsstempel ausserhalb der drei geprueften Pfade: {aufrufe}")


def t_der_aktivitaetsstrom_bekommt_weiter_sinnvolle_zeiten():
    """Der einzige Leser ausserhalb des Speichers darf nicht ins Leere greifen.

    Das Kontrollzentrum ordnet nach `COALESCE(decided_at, created_at)`. Eine nie
    entschiedene Anfrage traegt jetzt dauerhaft `NULL` — der Fallback muss sie
    also tragen, sonst waere aus einer ehrlichen Luecke ein Anzeigefehler
    geworden.
    """
    async def case():
        with tempfile.TemporaryDirectory() as tmp:
            st, cp, co = await _wire(tmp)
            ctx = await H.enroll_attested(cp)
            entschieden = await _anfrage(cp, tmp)
            await _entscheiden(cp, co, ctx, entschieden)
            offen = await _anfrage(cp, tmp)

            import sqlite3
            con = sqlite3.connect(f"file:{st.path}?mode=ro", uri=True)
            try:
                zeilen = con.execute(
                    "SELECT approval_id, COALESCE(decided_at, created_at) "
                    "FROM approval_requests "
                    "ORDER BY COALESCE(decided_at, created_at) DESC").fetchall()
            finally:
                con.close()
            zeiten = {a: t for a, t in zeilen}
            require_equal(len(zeiten), 2)
            for aid, t in zeiten.items():
                require(t and t > 0, f"{aid} hat keine brauchbare Zeit fuer die Anzeige")
            require(zeiten[offen] >= zeiten[entschieden] - 5,
                    "die Ordnung des Aktivitaetsstroms ist unbrauchbar geworden")
    _run(case())


def t_keine_historische_zeit_wird_erfunden():
    """Alte Zeilen werden nicht repariert — und nicht angefasst.

    Eine Migration muesste den Zeitpunkt der Zustimmung ERFINDEN. In einem
    Zahlungsbuch ist eine erfundene Zeit schlimmer als eine fehlende. Geprueft
    wird, dass der Speicher beim Oeffnen nichts an `decided_at` umschreibt.
    """
    async def case():
        with tempfile.TemporaryDirectory() as tmp:
            st, cp, co = await _wire(tmp)
            ctx = await H.enroll_attested(cp)
            aid = await _anfrage(cp, tmp)
            await _entscheiden(cp, co, ctx, aid)

            # Eine „historische" Zeile: der Zeitpunkt steht falsch, so wie ihn
            # der alte Code hinterlassen haette.
            import sqlite3
            con = sqlite3.connect(st.path)
            try:
                con.execute("UPDATE approval_requests SET decided_at=? "
                            "WHERE approval_id=?", (12345.0, aid))
                con.commit()
            finally:
                con.close()

            # Neu oeffnen — genau das, was ein Neustart tut.
            st2, _, _ = await _wire(tmp)
            require_equal((await st2.get_request(aid))["decided_at"], 12345.0,
                          "der Speicher hat eine historische Zeit umgeschrieben")

            # Und der Quelltext kennt keine Migration fuer diese Spalte.
            quelle = open(os.path.join(os.path.dirname(__file__), "..", "src",
                                       "solvio", "security", "mobile_approval",
                                       "store.py"), encoding="utf-8").read()
            require("UPDATE approval_requests SET decided_at=created_at" not in quelle,
                    "es gibt eine Migration, die Zeiten erfindet")
    _run(case())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
