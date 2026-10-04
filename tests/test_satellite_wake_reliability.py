"""Satellite Wake Reliability V1 — die Core-Seite: der Bericht des Ohrs.

DER BEFUND, der das ausgeloest hat: der Core sah den Satelliten ausschliesslich
beim Wecken. Ein Satellit, der das Weckwort nicht mehr erkennt, meldet sich aber
genau NIE — und weil "kein Wecken" nicht von "niemand hat gesprochen" zu
unterscheiden war, stand im Kontrollzentrum alles auf gruen, waehrend im
Wohnzimmer jemand gegen eine Wand sprach. In `probes.py` gab es elf Pruefungen
und keine davon war der Satellit.

WAS HINZUKOMMT: ein eigener Pfad am schon vorhandenen Listener, auf dem der
Satellit Messwerte ueber sein eigenes Hoeren abliefert — dieselbe HMAC-Pruefung,
kein zweiter Port, kein zweites Geheimnis. Der Satellit misst, der Core urteilt.

WAS ES NICHT IST: kein Fernzugriff auf den Pi, keine Autoritaet. Ein Bericht ist
Information; er loest nichts aus und veraendert keinen TrustContext.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_satellite_wake_reliability.py
"""
import asyncio
import inspect
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from _guard import require, require_equal  # noqa: E402
from solvio.control_center import probes as P  # noqa: E402
from solvio.control_center.health import State  # noqa: E402
from solvio.realtime import core_server as CS  # noqa: E402
from solvio.realtime import satellite_auth as SA  # noqa: E402
from solvio.realtime import satellite_health as SH  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SAT_ID = "pi-wohnzimmer"
SECRET = bytes(range(32))


def _creds():
    return SA.SatelliteCredentials({SAT_ID: SECRET})


class _Socket:
    """Ein Satellit, der auf dem Gesundheitspfad ankommt."""

    def __init__(self, replies=None, path="/health"):
        self.sent = []
        self.closed = False
        self._replies = list(replies or [])
        self.request = type("R", (), {"path": path})()

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._replies:
            await asyncio.sleep(3600)
        reply = self._replies.pop(0)
        return reply(self.challenge) if callable(reply) else reply

    async def close(self, code=None, reason=None):
        self.closed = True

    @property
    def challenge(self):
        for raw in self.sent:
            msg = json.loads(raw)
            if msg.get("type") == "auth_challenge":
                return msg["server_nonce"]
        return None

    def replied(self, kind):
        return any(json.loads(r).get("type") == kind for r in self.sent)

    remote_address = ("192.168.178.194", 51000)


def _server():
    srv = CS.CoreServer.__new__(CS.CoreServer)
    srv._busy = False
    srv.credentials = _creds()
    srv.dispatcher = None
    srv.model = "gpt-realtime"
    srv.api_key = "sk-not-used-here"
    srv.host, srv.port = "127.0.0.1", 8766
    srv.satellite_health = SH.SatelliteHealthRegistry()
    return srv


def _hello(challenge):
    client_nonce = SA.new_challenge()
    return json.dumps({
        "type": "hello", "satellite_id": SAT_ID,
        "protocol_version": SA.PROTOCOL_VERSION, "client_nonce": client_nonce,
        "auth": SA.compute_auth(SECRET, protocol_version=SA.PROTOCOL_VERSION,
                                satellite_id=SAT_ID, server_nonce=challenge,
                                client_nonce=client_nonce)})


def _report(**over):
    body = {"type": "satellite_health", "state": "IDLE", "verdict": "healthy",
            "hearing": {"chunks": 500, "chunks_expected": 500, "frames": 125,
                        "frames_nonzero": 125, "rms_l_peak_db": -28.0},
            "repairs": 0}
    body.update(over)
    return json.dumps(body)


# ------------------------------------------------------------------ Weg und Auth

async def t_a_report_reaches_the_core_over_the_health_path():
    srv = _server()
    sock = _Socket([_hello, _report()])
    await srv._route(sock)
    require(sock.replied("health_ack"), f"kein Ack: {sock.sent}")
    latest = srv.satellite_health.latest()
    require(latest is not None and latest.satellite_id == SAT_ID, latest)
    require_equal(latest.verdict, "healthy")
    require_equal(latest.hearing["chunks"], 500)


async def t_an_unauthenticated_device_cannot_report():
    """Die Kennung wird bewiesen, bevor irgendetwas gespeichert wird."""
    srv = _server()
    bad = json.dumps({"type": "hello", "satellite_id": SAT_ID,
                      "protocol_version": SA.PROTOCOL_VERSION,
                      "client_nonce": SA.new_challenge(), "auth": "00" * 32})
    sock = _Socket([bad, _report()])
    await srv._route(sock)
    require(srv.satellite_health.latest() is None,
            "ein nicht bewiesener Absender hat einen Bericht hinterlassen")
    require(not sock.replied("health_ack"), "ein Ack ohne Beweis")


async def t_the_session_path_is_untouched():
    """Der Sitzungsweg liegt weiter auf '/' und geht nicht in den Berichtspfad."""
    srv = _server()
    seen = []

    async def fake_pi(ws):
        seen.append(getattr(ws.request, "path", None))

    srv._handle_pi = fake_pi
    for path in ("/", "", "/?x=1"):
        await srv._route(_Socket(path=path))
    require_equal(len(seen), 3, f"der Sitzungsweg wurde verlassen: {seen}")
    require(srv.satellite_health.latest() is None, "eine Sitzung hat berichtet")


async def t_a_report_never_claims_the_session_lock():
    """Ein Bericht muss auch waehrend eines laufenden Gespraechs ankommen —
    und darf danach kein Gespraech blockieren. `_busy` wird nicht angefasst."""
    srv = _server()
    srv._busy = True
    sock = _Socket([_hello, _report()])
    await srv._route(sock)
    require(sock.replied("health_ack"), "der Bericht kam waehrend eines Gespraechs nicht durch")
    require(srv._busy is True, "der Berichtspfad hat den Sitzungsriegel veraendert")


async def t_a_report_carries_no_authority():
    """Der Berichtspfad darf nichts ausloesen. Er kennt weder Dispatcher noch
    Freigaben — das steht hier als Zusage, nicht als Hoffnung."""
    src = inspect.getsource(CS.CoreServer._handle_satellite_health)
    for forbidden in ("dispatcher", "approv", "capabilit", "execute", "session_start"):
        require(forbidden not in src.lower(),
                f"der Berichtspfad fasst '{forbidden}' an")


# ------------------------------------------------------------- Inhalt und Urteil

def t_a_satellite_cannot_invent_its_own_verdict():
    report = SH.parse_report(SAT_ID, {"verdict": "wunderbar", "state": "IDLE"})
    require_equal(report.verdict, "unknown")
    state, _ = SH.state_for(report)
    require_equal(state, "unknown")


def t_only_known_measurements_are_kept():
    report = SH.parse_report(SAT_ID, {"verdict": "healthy", "hearing": {
        "chunks": 500, "heimliches_feld": "hallo", "score_max": float("nan"),
        "frames": True}})
    require_equal(sorted(report.hearing), ["chunks"])
    # `verdict_age_s` gehoert ausdruecklich dazu: ohne es kann der Core eine
    # stehengebliebene Messung nicht von einer laufenden unterscheiden.
    require("verdict_age_s" in SH.HEARING_FIELDS, SH.HEARING_FIELDS)


def t_silence_is_never_reported_as_health():
    """Der ganze Defekt bestand darin, dass Schweigen wie Gesundheit aussah."""
    fresh = SH.parse_report(SAT_ID, {"verdict": "healthy"}, now=1000.0)
    require_equal(SH.state_for(fresh, now=1000.0 + 10)[0], "healthy")
    stale = SH.state_for(fresh, now=1000.0 + SH.STALE_AFTER_S + 1)
    require_equal(stale[0], "unavailable")
    require("keine Meldung" in stale[1], stale[1])
    # "Noch nie gemeldet" bleibt `unknown`: der Core kann frueher gestartet sein
    # als der Satellit, und das ist wirklich keine Auskunft.
    require_equal(SH.state_for(None)[0], "unknown")


def t_a_satellite_that_went_quiet_counts_as_a_fault():
    """`unknown` zaehlt in der Tafel NICHT als Stoerung — die Zusammenfassung
    haette weiter "Alles laeuft" gesagt, waehrend das Ohr seit Stunden weg war.
    Ein verstummtes Geraet ist ein Ausfall, kein Unbekannter."""
    from solvio.control_center.health import NOT_WELL, State
    gone = SH.parse_report(SAT_ID, {"verdict": "healthy"}, now=1000.0)
    state, _ = SH.state_for(gone, now=1000.0 + SH.STALE_AFTER_S + 1)
    require(State(state) in NOT_WELL,
            f"ein verstummter Satellit erscheint nicht als Stoerung: {state}")
    require(State.UNKNOWN not in NOT_WELL,
            "unknown zaehlt jetzt als Stoerung — dann war die Trennung sinnlos")


def t_a_fresh_report_over_a_frozen_measurement_is_not_health():
    """Der Bericht kommt, aber die Messung dahinter steht. Ohne diese Pruefung
    zeigte der Core dauerhaft `healthy` ueber etwas, das es nicht mehr gibt —
    dieselbe Verwechslung, eine Ebene tiefer."""
    frozen = SH.parse_report(SAT_ID, {"verdict": "healthy",
                                      "hearing": {"verdict_age_s": SH.VERDICT_STALE_S + 60}},
                             now=1000.0)
    state, reason = SH.state_for(frozen, now=1000.0 + 5)
    require_equal(state, "degraded")
    require("steht seit" in reason, reason)
    ok = SH.parse_report(SAT_ID, {"verdict": "healthy", "hearing": {"verdict_age_s": 8}},
                         now=1000.0)
    state, reason = SH.state_for(ok, now=1000.0 + 5)
    require_equal(state, "healthy")
    # Sichtbar, nicht nur geprueft: die Begruendung traegt das Alter der
    # Messung, damit "gruen" eine nachlesbare Grundlage hat.
    require("Messung 8 s alt" in reason, reason)


def t_every_verdict_has_a_meaning_and_none_of_them_lies():
    for verdict in SH.KNOWN_VERDICTS:
        require(verdict in SH.VERDICT_MEANING, f"{verdict} hat keine Bedeutung")
        state, reason = SH.VERDICT_MEANING[verdict]
        require(State(state) is not None and reason, verdict)
    require_equal(SH.VERDICT_MEANING["no_frames"][0], "unavailable")
    require_equal(SH.VERDICT_MEANING["detector_flat"][0], "degraded")
    # `quiet` ist ein stiller Raum, kein Defekt — sonst wuerde jede Nacht rot.
    require_equal(SH.VERDICT_MEANING["quiet"][0], "healthy")


# ------------------------------------------------------------- Gesundheitstafel

async def t_the_control_center_now_knows_about_the_ear():
    class Dispatcher:
        pass

    dispatcher = Dispatcher()
    keys = [p.key for p in P.build(dispatcher)]
    require("satellite" in keys, f"das Ohr fehlt in der Tafel: {keys}")

    probe = {p.key: p for p in P.build(dispatcher)}["satellite"]
    state, reason = await probe.check()
    require_equal(state, State.UNKNOWN)

    registry = SH.SatelliteHealthRegistry()
    dispatcher.satellite_health = registry
    probe = {p.key: p for p in P.build(dispatcher)}["satellite"]
    require_equal((await probe.check())[0], State.UNKNOWN)

    registry.record(SH.parse_report(SAT_ID, {"verdict": "healthy", "state": "IDLE"}))
    require_equal((await probe.check())[0], State.HEALTHY)

    # Ein stiller Raum ist kein Defekt — sonst waere jede Nacht ein Vorfall.
    registry.record(SH.parse_report(SAT_ID, {"verdict": "quiet", "state": "IDLE"}))
    require_equal((await probe.check())[0], State.HEALTHY)

    registry.record(SH.parse_report(SAT_ID, {"verdict": "no_frames", "state": "IDLE"}))
    state, reason = await probe.check()
    require_equal(state, State.UNAVAILABLE)
    require("Mikrofonsignal" in reason, reason)

    registry.record(SH.parse_report(SAT_ID, {"verdict": "detector_flat"}))
    require_equal((await probe.check())[0], State.DEGRADED)


async def t_the_ear_is_wired_before_the_board_is_built():
    """Wuerde die Tafel vor der Verdrahtung gebaut, bliebe das Ohr unsichtbar —
    genau der Zustand, der diesen Milestone ausgeloest hat."""
    src = inspect.getsource(CS.CoreServer.serve)
    wire = src.index("satellite_health")
    build = src.index("approver_from_env")
    require(wire < build, "der Satellit wird nach dem Bau der Tafel verdrahtet")


def t_security_tree_matches_freeze_and_explicit_n2_amendment():
    from _security_amendment import require_n2_security_tree
    require_n2_security_tree(REPO)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
