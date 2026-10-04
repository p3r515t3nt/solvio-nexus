"""M0 Session 4 — malformed provider audio must not kill the session.

M0/3 recorded this as debt after tripping over it while writing a probe: a provider audio
delta that decodes to an ODD number of bytes reaches array.frombytes inside
Resampler.process, which raises. The exception travelled up into _oa_reader, which caught it
as core.reader_error and returned — ending the session silently. Everything after it was
lost: the next good audio frame never reached the satellite, and response.done was never
processed.

A malformed frame is now DROPPED, not repaired. Padding a half sample would be inventing
audio; forwarding it would hand the satellite a stream shifted by one byte. Only the
category and the byte count are logged, never the payload.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_m0_operational_stability.py
"""
import asyncio
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from _guard import require, require_equal  # noqa: E402
from solvio.realtime import core_server as CS  # noqa: E402

EVEN = base64.b64encode(b"\x01\x02" * 240).decode("ascii")     # 480 bytes
ODD = base64.b64encode(b"\x01\x02\x03").decode("ascii")        # 3 bytes
LOUD = base64.b64encode(bytes(range(256)) * 4).decode("ascii")  # 1024 bytes, distinctive


class _Log:
    def __init__(self):
        self.events = []

    def _rec(self, event, **kw):
        self.events.append((event, kw))

    info = warning = error = _rec

    def of(self, name):
        return [kw for event, kw in self.events if event == name]


class _Provider:
    def __init__(self, frames):
        self._frames = list(frames)
        self.exhausted = asyncio.Event()
        self.sent = []

    def __aiter__(self):
        async def gen():
            for f in self._frames:
                yield f
            self.exhausted.set()
            await asyncio.sleep(3600)
        return gen()

    async def send(self, data):
        self.sent.append(data)

    async def close(self, *a, **kw):
        pass


class _Satellite:
    def __init__(self):
        self.audio = []
        self.sent = []

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, (bytes, bytearray)):
            self.audio.append(bytes(data))

    async def close(self, *a, **kw):
        pass

    remote_address = ("192.168.178.194", 51000)


def _session():
    srv = CS.CoreServer.__new__(CS.CoreServer)
    srv.dispatcher = None
    srv.idle_timeout = 999
    srv.credentials = None
    srv.model = "gpt-realtime"
    sess = CS.Session(srv, _Satellite())
    sess.active = True
    return sess


def _audio(delta):
    return json.dumps({"type": "response.output_audio.delta", "delta": delta})


async def _run(frames):
    """Drive the real reader over these frames and return (session, captured log, task)."""
    sess = _session()
    sess.oa = _Provider(frames)
    real_log = CS.log
    CS.log = _Log()
    reader = asyncio.create_task(sess._oa_reader())
    try:
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.sleep(0.05)
        captured = CS.log
    finally:
        CS.log = real_log
    return sess, captured, reader


# =====================================================================
# A — the normal path is untouched
# =====================================================================
async def t_a_valid_audio_still_reaches_the_satellite():
    sess, captured, reader = await _run([_audio(EVEN), _audio(EVEN)])
    try:
        require_equal(len(sess.ws.audio), 2, f"{len(sess.ws.audio)} frames forwarded, wanted 2")
        for frame in sess.ws.audio:
            require(len(frame) > 0, "an empty frame was forwarded")
            require_equal(len(frame) % 2, 0, "a partial 16-bit sample was forwarded")
        require_equal(captured.of("core.audio_frame_dropped"), [],
                      "valid audio was reported as dropped")
        require_equal(sess.dropped_audio_frames, 0, "valid audio incremented the drop counter")
    finally:
        reader.cancel()


async def t_f_the_normal_response_path_still_completes():
    """F — a full ordinary turn, unchanged: speech, audio, transcript, response.done."""
    frames = [
        json.dumps({"type": "input_audio_buffer.speech_started"}),
        json.dumps({"type": "input_audio_buffer.speech_stopped"}),
        json.dumps({"type": "response.created"}),
        _audio(EVEN),
        json.dumps({"type": "response.done", "response": {"output": []}}),
    ]
    sess, captured, reader = await _run(frames)
    try:
        require_equal(len(sess.ws.audio), 1, "the response audio did not reach the satellite")
        turns = captured.of("core.turn_latency")
        require_equal(len(turns), 1, f"the turn was not measured: {captured.events}")
        require("speech_end_to_first_audio_ms" in turns[0], turns[0])
        require(captured.of("core.response_done"), "response.done was not processed")
        require_equal(captured.of("core.reader_error"), [], "the reader reported an error")
    finally:
        reader.cancel()


# =====================================================================
# B, C, D — the malformed frame
# =====================================================================
async def t_b_an_odd_byte_frame_does_not_terminate_the_reader():
    sess, captured, reader = await _run([_audio(ODD)])
    try:
        require(not reader.done(), "the reader ended on a malformed audio frame")
        require_equal(captured.of("core.reader_error"), [],
                      "the malformed frame was reported as a reader error")
    finally:
        reader.cancel()


async def t_c_an_odd_byte_frame_is_not_forwarded():
    sess, captured, reader = await _run([_audio(ODD)])
    try:
        require_equal(sess.ws.audio, [], "a malformed frame was forwarded to the satellite")
        require_equal(sess.dropped_audio_frames, 1, "the drop was not counted")
        dropped = captured.of("core.audio_frame_dropped")
        require_equal(len(dropped), 1, f"the drop was not reported once: {dropped}")
        require_equal(dropped[0]["reason"], "odd_byte_count", dropped[0])
        require_equal(dropped[0]["byte_count"], 3, dropped[0])
    finally:
        reader.cancel()


async def t_d_a_valid_frame_after_a_malformed_one_is_still_processed():
    """The heart of the defect: what came AFTER the bad frame used to be lost."""
    frames = [_audio(EVEN), _audio(ODD), _audio(EVEN),
              json.dumps({"type": "response.done", "response": {"output": []}})]
    sess, captured, reader = await _run(frames)
    try:
        require(not reader.done(), "the reader ended before the following events")
        require_equal(len(sess.ws.audio), 2,
                      f"the audio after the malformed frame was lost: {len(sess.ws.audio)}")
        require(captured.of("core.response_done"),
                "response.done after the malformed frame was never processed")
        require_equal(sess.dropped_audio_frames, 1, "exactly one frame should have dropped")
    finally:
        reader.cancel()


async def t_d_an_undecodable_delta_is_handled_the_same_way():
    sess, captured, reader = await _run([_audio("!!!not base64!!!"), _audio(EVEN)])
    try:
        require(not reader.done(), "an undecodable delta ended the reader")
        require_equal(len(sess.ws.audio), 1, "the following valid audio was lost")
        dropped = captured.of("core.audio_frame_dropped")
        require_equal(dropped[0]["reason"], "undecodable_base64", dropped[0])
    finally:
        reader.cancel()


# =====================================================================
# E — the diagnostic says what, not what was in it
# =====================================================================
async def t_e_the_diagnostic_carries_a_category_and_no_payload():
    payload = b"\x01\x02\x03" + b"GEHEIMES-AUDIO-MUSTER" + b"\x04"
    delta = base64.b64encode(payload).decode("ascii")
    require_equal(len(payload) % 2, 1, "the fixture is not actually odd-length")
    sess, captured, reader = await _run([_audio(delta)])
    try:
        dropped = captured.of("core.audio_frame_dropped")
        require_equal(len(dropped), 1, dropped)
        entry = dropped[0]
        require(entry.get("reason"), "no category was reported")
        require(entry.get("byte_count") is not None, "no length was reported")
        blob = json.dumps([kw for _e, kw in captured.events], default=str)
        require(delta not in blob, "the raw delta reached the log")
        require("GEHEIMES-AUDIO-MUSTER" not in blob, "the audio payload reached the log")
        require(base64.b64encode(payload).decode()[:12] not in blob,
                "part of the encoded payload reached the log")
        for key in entry:
            require(key in ("session_id", "reason", "byte_count", "dropped_so_far"),
                    f"the diagnostic carries an unexpected field: {key}")
    finally:
        reader.cancel()


async def t_e_a_flood_of_malformed_frames_does_not_flood_the_log():
    sess, captured, reader = await _run([_audio(ODD) for _ in range(40)])
    try:
        dropped = captured.of("core.audio_frame_dropped")
        require_equal(len(dropped), CS._MAX_AUDIO_DROP_LOGS,
                      f"{len(dropped)} log lines for 40 bad frames")
        require_equal(sess.dropped_audio_frames, 40, "not every drop was counted")
        require_equal(sess.ws.audio, [], "a malformed frame was forwarded")
    finally:
        reader.cancel()


async def t_e_the_total_is_reported_when_the_session_closes():
    """Capped logging must not mean lost accounting."""
    sess, captured, reader = await _run([_audio(ODD) for _ in range(9)])
    real_log = CS.log
    CS.log = _Log()
    try:
        await sess.close(reason="pi")
        closing = CS.log.of("core.session_closing")
    finally:
        CS.log = real_log
        reader.cancel()
    require_equal(closing[0].get("dropped_audio_frames"), 9,
                  f"the session close did not account for the drops: {closing}")


# =====================================================================
# Turn metrics under interruption — classification, not redesign
# =====================================================================
class _Dispatcher:
    def __init__(self):
        self.calls = []
        self.approvals = None

    def parse_args(self, raw):
        return json.loads(raw) if raw else {}

    async def dispatch(self, name, args):
        self.calls.append(name)
        return {"success": True, "data": {"tool": name}}


async def t_turn_metrics_under_interruption_are_incomplete_not_harmful():
    """M0/3 observed that overlapping speech and response leave the interrupted turn's
    partial measurement discarded, and later events carrying turn_id=None.

    This test classifies that observation. Every read of self._turn in core_server.py is a
    log field or the tool_calls counter — no branch, dispatch, timeout or close reason
    depends on it. So the consequence is a MISSING METRIC, not altered behaviour: with a
    turn deliberately interrupted and then cleared, the tool still runs and response.done
    is still processed.

    VOICE UX V2 CHANGED ONE OF THESE ASSERTIONS ON PURPOSE. This test used to require that
    the two audio deltas following the interruption still reached the satellite. That was
    the defect, not the contract: they belong to the response the user just interrupted,
    and playing them means the old answer talks over the new question. Barge-in now closes
    an audio gate (Session._barge_in / _audio_wanted), so the expected count is zero. The
    rest of the assertion — reader alive, tool ran, response.done processed, exactly one
    honest turn measured — is unchanged, and that is still what this test is for.
    """
    disp = _Dispatcher()
    sess = _session()
    sess.server.dispatcher = disp
    frames = [
        json.dumps({"type": "input_audio_buffer.speech_started"}),      # turn 1 opens
        json.dumps({"type": "response.created"}),
        json.dumps({"type": "input_audio_buffer.speech_started"}),      # interrupted: turn 2
        _audio(EVEN),
        json.dumps({"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "t", "call_id": "c1", "arguments": "{}"}]}}),
        _audio(EVEN),
        # Voice UX V2: die Antwort auf den unterbrochenen Turn meldet sich, bevor
        # sie endet. Ohne diese Zeile beschreibt der Ablauf keine echte
        # Unterbrechung mehr — in Wirklichkeit bekommt jeder Turn, der
        # beantwortet wird, ein `response.created`. Und seit ein `response.done`
        # nur noch den Turn abschliesst, ZU DEM ES GEHOERT, entscheidet genau
        # das darueber, ob der ueberlebende Turn gemessen wird oder eine
        # erfundene Zahl entsteht.
        json.dumps({"type": "response.created", "response": {"id": "resp_zwei"}}),
        json.dumps({"type": "response.done", "response": {"id": "resp_zwei",
                                                          "output": []}}),
    ]
    sess.oa = _Provider(frames)
    real_log = CS.log
    CS.log = _Log()
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    try:
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.wait_for(sess._tool_queue.join(), timeout=10)
        await asyncio.sleep(0.05)
        captured = CS.log
    finally:
        CS.log = real_log
        reader.cancel()
        worker.cancel()
    # Behaviour: everything still works.
    require(not reader.done(), "the reader ended on an interrupted turn")
    require_equal(len(sess.ws.audio), 0,
                  "audio from the interrupted response still reached the speaker")
    require_equal(disp.calls, ["t"], f"the tool did not run: {disp.calls}")
    # Voice UX V2: a response.done is now attributed before it is counted. In this
    # synthetic sequence the interrupting speech_started opens t2 without a
    # response.created of its own, so the final done belongs to the PREVIOUS
    # response and is logged as voice.dead_response_done instead. Attributing it
    # to t2 would fabricate a measurement — the exact failure _emit_turn exists to
    # prevent. What this line asserts is unchanged: the event was processed, not
    # swallowed, and the reader survived it.
    require(captured.of("core.response_done") or captured.of("voice.dead_response_done"),
            "response.done was not processed at all")
    require_equal(captured.of("core.reader_error"), [], "the reader reported an error")
    # Metrics: incomplete, and honest about it.
    turns = captured.of("core.turn_latency")
    require_equal(len(turns), 1, f"expected the surviving turn to be measured once: {turns}")
    require_equal(turns[0]["turn_id"], f"{sess.session_id}-t2",
                  "the emitted turn is not the one that survived")
    require(sess.turn_no >= 2, "the interrupted turn was not counted at all")


async def t_marking_a_turn_that_is_not_open_cannot_raise():
    """Events arriving with no open turn are exactly the turn_id=None case. They must be
    inert, not an error path."""
    sess = _session()
    sess._turn = {}
    for key in ("speech_ended", "response_started", "first_audio_recv", "response_done"):
        sess._mark(key)                     # must not raise, must not invent a turn
    require_equal(sess._turn, {}, "marking created a turn out of nothing")
    real_log = CS.log
    CS.log = _Log()
    try:
        sess._emit_turn()
        emitted = CS.log.of("core.turn_latency")
    finally:
        CS.log = real_log
    require_equal(emitted, [], "a turn with no data was emitted as a measurement")


# =====================================================================
# The resampler's own contract is unchanged
# =====================================================================
def t_the_resampler_still_rejects_odd_input_on_its_own():
    """The guard belongs at the call site; the resampler stays strict about its contract
    so a future caller cannot silently feed it half a sample."""
    from solvio.audio.resample import Resampler
    r = Resampler(24000, 16000)
    try:
        r.process(b"\x01\x02\x03")
    except Exception:
        return
    require(False, "Resampler.process silently accepted an odd-length buffer")


def t_a_valid_buffer_still_resamples():
    from solvio.audio.resample import Resampler
    r = Resampler(24000, 16000)
    out = r.process(b"\x01\x02" * 240)
    require(len(out) > 0, "a valid buffer produced no output")
    require_equal(len(out) % 2, 0, "the resampler produced a partial sample")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
