"""M0 Session 3 — latency instrumentation and realtime hot-path cleanup.

TWO THINGS, MEASURED BEFORE THEY WERE CHANGED.

**Instrumentation.** The system had no end-to-end turn latency figures; the audit could only
quote a spread of session-setup numbers. There is now one `core.turn_latency` line per turn
with derived durations, correlated by `session_id` and `turn_id`, all computed from
`time.monotonic()`.

**The hot path.** `_handle_tool_calls` was awaited inside `_oa_reader`'s `async for` over the
provider socket. Reproduced with a synthetic tool blocked on an Event: while the tool ran, the
next audio delta never reached the satellite — and neither would speech_started/stopped,
transcripts or the silent-stop check, because they all arrive on that same socket. Tool work
now goes through a one-worker queue; the reader stays readable.

OUT OF SCOPE, unchanged: ConversationStore, memory, capability contract, Home Assistant
capabilities, approvals, the 30-second inactivity policy.

ASSERTION POLICY: `require*` from `tests/_guard.py` are function calls and survive `-O`.

Direct: python tests/test_m0_realtime_latency.py
"""
import asyncio
import ast as _ast
import base64
import inspect
import json
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from _guard import require, require_equal  # noqa: E402
from solvio.realtime import core_server as CS  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CORE_SRC = os.path.join(REPO, "src", "solvio", "realtime", "core_server.py")
PCM = base64.b64encode(b"\x00\x01" * 480).decode("ascii")


class _Log:
    """Captures structured log calls so the emitted metrics can be inspected."""

    def __init__(self):
        self.events = []

    def _rec(self, event, **kw):
        self.events.append((event, kw))

    info = warning = error = _rec

    def of(self, name):
        return [kw for event, kw in self.events if event == name]


class _Provider:
    def __init__(self, frames):
        self.sent = []
        self._frames = list(frames)
        self.exhausted = asyncio.Event()

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
        self.sent = []
        self.audio = asyncio.Event()

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, (bytes, bytearray)):
            self.audio.set()

    async def close(self, *a, **kw):
        pass

    remote_address = ("192.168.178.194", 51000)


class _Dispatcher:
    """A tool that finishes only when the test releases it."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls = []
        self.approvals = None

    def parse_args(self, raw):
        return json.loads(raw) if raw else {}

    async def dispatch(self, name, args):
        self.calls.append(name)
        self.started.set()
        await self.release.wait()
        return {"success": True, "data": {"tool": name}}


def _session(dispatcher=None):
    srv = CS.CoreServer.__new__(CS.CoreServer)
    srv.dispatcher = dispatcher
    srv.idle_timeout = 999
    srv.credentials = None
    srv.model = "gpt-realtime"
    sat = _Satellite()
    sess = CS.Session(srv, sat)
    sess.active = True
    return sess, sat


def _turn_frames(with_tool=False):
    frames = [
        json.dumps({"type": "input_audio_buffer.speech_started"}),
        json.dumps({"type": "input_audio_buffer.speech_stopped"}),
        json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "was ist die hauptstadt von portugal"}),
        json.dumps({"type": "response.created"}),
        json.dumps({"type": "response.output_audio.delta", "delta": PCM}),
    ]
    if with_tool:
        frames.append(json.dumps({"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "home_assistant_get_state",
             "call_id": "call-42", "arguments": "{}"}]}}))
    frames.append(json.dumps({"type": "response.done", "response": {"output": []}}))
    return frames


# =====================================================================
# A — durations come from a monotonic clock
# =====================================================================
def t_a_durations_use_monotonic_time():
    src = open(CORE_SRC, encoding="utf-8").read()
    tree = _ast.parse(src)
    # every assignment to a t_* timestamp slot or _mark must read time.monotonic()
    for name in ("_mark", "_begin_turn", "_ms"):
        fn = next(n for n in _ast.walk(tree)
                  if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name == name)
        body = _ast.get_source_segment(src, fn) or ""
        require("time.time(" not in body, f"{name} uses wall clock for a duration")
    marks = [n for n in _ast.walk(tree)
             if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
             and n.func.attr == "monotonic"]
    require(len(marks) >= 6, f"only {len(marks)} monotonic reads — timestamps are missing")
    # and the derived helper refuses to invent a duration
    require(CS.Session._ms(None, 5.0) is None, "a missing endpoint produced a duration")
    require(CS.Session._ms(5.0, None) is None, "a missing endpoint produced a duration")
    require_equal(CS.Session._ms(1.0, 1.25), 250, "milliseconds are computed wrongly")


# =====================================================================
# B — a normal turn emits correlated metrics
# =====================================================================
async def t_b_a_normal_turn_emits_correlated_metrics():
    sess, sat = _session()
    provider = _Provider(_turn_frames())
    sess.oa = provider
    real_log = CS.log
    CS.log = _Log()
    try:
        reader = asyncio.create_task(sess._oa_reader())
        await asyncio.wait_for(provider.exhausted.wait(), timeout=10)
        await asyncio.sleep(0)
        captured = CS.log
    finally:
        CS.log = real_log
        reader.cancel()

    turns = captured.of("core.turn_latency")
    require_equal(len(turns), 1, f"expected one turn line, got {len(turns)}")
    t = turns[0]
    require_equal(t["session_id"], sess.session_id, t)
    require_equal(t["turn_id"], f"{sess.session_id}-t1", t)
    for field in ("speech_end_to_response_start_ms", "speech_end_to_first_audio_ms",
                  "provider_first_audio_to_satellite_send_ms", "turn_total_ms",
                  "transcript_ready_ms"):
        require(field in t, f"{field} missing from {t}")
        require(isinstance(t[field], int) and t[field] >= 0, f"{field}={t[field]!r}")
    # every event of the turn carries the same correlation
    for name in ("core.USER_SPEECH_STARTED", "core.USER_SPEECH_ENDED",
                 "core.SOLVIO_RESPONSE_STARTED", "core.response_done"):
        entries = captured.of(name)
        require(entries, f"{name} was not emitted")
        require_equal(entries[0]["turn_id"], t["turn_id"], f"{name}: {entries[0]}")


async def t_b_a_second_turn_gets_its_own_identity():
    sess, sat = _session()
    sess.oa = _Provider(_turn_frames() + _turn_frames())
    real_log = CS.log
    CS.log = _Log()
    try:
        reader = asyncio.create_task(sess._oa_reader())
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.sleep(0)
        captured = CS.log
    finally:
        CS.log = real_log
        reader.cancel()
    ids = [t["turn_id"] for t in captured.of("core.turn_latency")]
    require_equal(len(ids), 2, ids)
    require_equal(ids, [f"{sess.session_id}-t1", f"{sess.session_id}-t2"], ids)


# =====================================================================
# C — instrumentation adds no private or secret material
# =====================================================================
async def t_c_metrics_carry_no_transcript_audio_or_secret():
    sess, sat = _session()
    secret_word = "hauptstadt von portugal"
    sess.oa = _Provider(_turn_frames())
    real_log = CS.log
    CS.log = _Log()
    try:
        reader = asyncio.create_task(sess._oa_reader())
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.sleep(0)
        captured = CS.log
    finally:
        CS.log = real_log
        reader.cancel()
    blob = json.dumps([kw for _e, kw in captured.events], default=str)
    require(secret_word not in blob, f"the transcript leaked into the metrics: {blob[:200]}")
    require(PCM[:24] not in blob, "audio payload leaked into the metrics")
    require("Bearer" not in blob and "sk-" not in blob, "credential material in the metrics")
    for name in ("core.turn_latency", "core.USER_SPEECH_STARTED"):
        for kw in captured.of(name):
            for key in kw:
                require(key not in ("transcript", "delta", "audio", "api_key"),
                        f"{name} carries {key}")


def t_c_no_new_logging_of_audio_or_credentials():
    src = open(CORE_SRC, encoding="utf-8").read()
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if not (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)):
            continue
        if not (isinstance(node.func.value, _ast.Name) and node.func.value.id == "log"):
            continue
        for kw in node.keywords:
            require(kw.arg not in ("audio", "delta", "pcm", "api_key", "authorization",
                                   "secret", "transcript"),
                    f"a log call passes {kw.arg}")


# =====================================================================
# D — a slow tool no longer blocks the provider reader
# =====================================================================
async def t_d_a_slow_tool_does_not_block_the_reader():
    """The reproduction, as a permanent regression. Events only — no sleeps decide this."""
    disp = _Dispatcher()
    sess, sat = _session(disp)
    frames = [
        json.dumps({"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "slow_tool", "call_id": "call-1",
             "arguments": "{}"}]}}),
        json.dumps({"type": "response.output_audio.delta", "delta": PCM}),
    ]
    sess.oa = _Provider(frames)
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    try:
        await asyncio.wait_for(disp.started.wait(), timeout=10)
        require(not disp.release.is_set(), "the tool finished before the assertion")
        # THE assertion: audio arrives while the tool is still running. A bare TimeoutError
        # here would tell a future reader nothing, so name the regression.
        try:
            await asyncio.wait_for(sat.audio.wait(), timeout=10)
        except asyncio.TimeoutError:
            require(False, "the audio delta after the tool call never reached the satellite "
                           "while the tool was running — tool execution is blocking the "
                           "provider reader again")
        require(not disp.release.is_set(), "the tool had already been released")
        disp.release.set()
        await asyncio.wait_for(sess._tool_queue.join(), timeout=10)
    finally:
        reader.cancel()
        worker.cancel()
    require_equal(disp.calls, ["slow_tool"], disp.calls)


def t_d_the_reader_does_not_await_tool_execution():
    """Structural companion: the reader hands the work over, it does not run it."""
    src = open(CORE_SRC, encoding="utf-8").read()
    reader = textwrap.dedent(inspect.getsource(CS.Session._oa_reader))
    require("_tool_queue.put_nowait" in reader, "the reader no longer hands tools over")
    require("await self._handle_tool_calls" not in reader,
            "the reader awaits tool execution again — the blocking is back")
    tree = _ast.parse(src)
    loop = next(n for n in _ast.walk(tree)
                if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_tool_loop")
    body = _ast.get_source_segment(src, loop) or ""
    require("await self._handle_tool_calls" in body, "the worker does not run the tools")
    require("except Exception" in body, "a failing tool would kill the worker")


# =====================================================================
# E — the result still belongs to the right call
# =====================================================================
async def t_e_a_tool_result_maps_to_its_own_call_id():
    disp = _Dispatcher()
    disp.release.set()                      # finish immediately
    sess, sat = _session(disp)
    sess.oa = _Provider([json.dumps({"type": "response.done", "response": {"output": [
        {"type": "function_call", "name": "tool_a", "call_id": "call-A", "arguments": "{}"},
        {"type": "function_call", "name": "tool_b", "call_id": "call-B", "arguments": "{}"},
    ]}})])
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    try:
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.wait_for(sess._tool_queue.join(), timeout=10)
    finally:
        reader.cancel()
        worker.cancel()
    outputs = [json.loads(m) for m in sess.oa.sent if isinstance(m, str)]
    items = [o["item"] for o in outputs if o.get("type") == "conversation.item.create"]
    require_equal([i["call_id"] for i in items], ["call-A", "call-B"],
                  f"tool results were mismatched or reordered: {items}")
    for item, name in zip(items, ("tool_a", "tool_b")):
        require(name in item["output"], f"{item['call_id']} carries the wrong result: {item}")


async def t_e_two_tool_rounds_keep_their_order():
    disp = _Dispatcher()
    disp.release.set()
    sess, sat = _session(disp)
    rounds = [json.dumps({"type": "response.done", "response": {"output": [
        {"type": "function_call", "name": f"tool_{i}", "call_id": f"call-{i}",
         "arguments": "{}"}]}}) for i in range(3)]
    sess.oa = _Provider(rounds)
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    try:
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.wait_for(sess._tool_queue.join(), timeout=10)
    finally:
        reader.cancel()
        worker.cancel()
    require_equal(disp.calls, ["tool_0", "tool_1", "tool_2"],
                  f"one worker did not preserve order: {disp.calls}")


# =====================================================================
# F — closing a session contains outstanding tool work
# =====================================================================
async def t_f_close_cancels_an_outstanding_tool_without_orphans():
    disp = _Dispatcher()                    # never released
    sess, sat = _session(disp)
    sess.oa = _Provider([json.dumps({"type": "response.done", "response": {"output": [
        {"type": "function_call", "name": "hangs_forever", "call_id": "call-1",
         "arguments": "{}"}]}})])
    sess.reader = asyncio.create_task(sess._oa_reader())
    sess.timer = None
    sess._tool_worker = asyncio.create_task(sess._tool_loop())
    try:
        await asyncio.wait_for(disp.started.wait(), timeout=10)
        worker_task, reader_task = sess._tool_worker, sess.reader
        await sess.close(reason="pi")
        await asyncio.sleep(0.05)
        require_equal(sess._tool_worker, None, "close() left the tool worker reference behind")
        require_equal(sess.reader, None, "close() left the reader reference behind")
        require(worker_task.cancelled() or worker_task.done(),
                "the tool worker survived the session")
        require(reader_task.cancelled() or reader_task.done(), "the reader survived")
        outputs = [json.loads(m) for m in sess.oa.sent if isinstance(m, str)] if sess.oa else []
        require(not any(o.get("type") == "conversation.item.create" for o in outputs),
                "a cancelled tool still wrote its result into a closed session")
    finally:
        for t in (sess.reader, sess._tool_worker):
            if t and not t.done():
                t.cancel()


def t_f_close_cancels_every_task_it_created():
    src = open(CORE_SRC, encoding="utf-8").read()
    close = textwrap.dedent(inspect.getsource(CS.Session.close))
    require("self._tool_worker" in close, "close() does not touch the tool worker")
    created = set()
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Call) \
                and isinstance(node.value.func, _ast.Attribute) \
                and node.value.func.attr == "create_task":
            for target in node.targets:
                if isinstance(target, _ast.Attribute):
                    created.add(target.attr)
    for name in created:
        require(name in close, f"close() never cancels self.{name} — orphan task risk")


# =====================================================================
# G — the reason a session ended is a category
# =====================================================================
async def t_g_close_reasons_are_classified():
    expected = {"silent_stop": "silent_stop", "timeout": "inactivity_timeout",
                "open_failed": "provider_error", "disconnect": "satellite_disconnect",
                "pi": "normal_close", "etwas_anderes": "other"}
    for raw, category in expected.items():
        sess, sat = _session()
        sess.oa = None
        sess.reader = sess.timer = sess._tool_worker = None
        real_log = CS.log
        CS.log = _Log()
        try:
            await sess.close(reason=raw)
            captured = CS.log
        finally:
            CS.log = real_log
        lines = captured.of("core.session_closing")
        require_equal(len(lines), 1, f"{raw}: {captured.events}")
        require_equal(lines[0]["close_reason"], category, f"{raw} -> {lines[0]}")
        require_equal(lines[0]["raw_reason"], raw, lines[0])
        require("session_age_ms" in lines[0] and "turns" in lines[0], lines[0])


def t_g_every_close_call_site_has_a_category():
    src = open(CORE_SRC, encoding="utf-8").read()
    import re
    raw = set(re.findall(r'close\(reason="([a-z_]+)"\)', src))
    known = set(CS.Session._CLOSE_REASONS)
    require(raw <= known,
            f"these close reasons fall through to 'other': {sorted(raw - known)}")


# =====================================================================
# H — barge-in survives a running tool (software chain only)
# =====================================================================
async def t_h_barge_in_reaches_the_satellite_while_a_tool_runs():
    """Barge-in here means ONLY the software chain: the provider reports
    speech_started, the reader sends {"type": "flush"}, the satellite drops its
    playback. Whether the microphone can hear the user over the speaker is an
    acoustic question the XVF3800 answers, and this test makes no claim about it.

    Measured against the pre-refactor code, the flush arrived 401 ms after
    speech_started with a 400 ms tool — i.e. only once the tool had finished.
    """
    disp = _Dispatcher()
    sess, sat = _session(disp)
    flushed = asyncio.Event()
    original_send = sat.send

    async def send(data):
        await original_send(data)
        if isinstance(data, str) and json.loads(data).get("type") == "flush":
            flushed.set()
    sat.send = send

    gate = asyncio.Event()

    class _Gated(_Provider):
        def __aiter__(self):
            async def gen():
                yield json.dumps({"type": "response.done", "response": {"output": [
                    {"type": "function_call", "name": "slow", "call_id": "c1",
                     "arguments": "{}"}]}})
                await gate.wait()
                yield json.dumps({"type": "input_audio_buffer.speech_started"})
                self.exhausted.set()
                await asyncio.sleep(3600)
            return gen()

    sess.oa = _Gated([])
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    try:
        await asyncio.wait_for(disp.started.wait(), timeout=10)
        gate.set()
        try:
            await asyncio.wait_for(flushed.wait(), timeout=10)
        except asyncio.TimeoutError:
            require(False, "speech_started did not reach the satellite as a flush while a "
                           "tool was running — barge-in is blocked behind tool execution "
                           "again (measured at 401 ms with a 400 ms tool before the fix)")
        require(not disp.release.is_set(),
                "the tool had already finished — barge-in was not tested against a busy tool")
        require(sess.speaking is True, "speech_started was not processed")
        disp.release.set()
    finally:
        reader.cancel()
        worker.cancel()


async def t_h_speech_started_still_flushes_the_satellite():
    sess, sat = _session()
    sess.oa = _Provider([json.dumps({"type": "input_audio_buffer.speech_started"})])
    reader = asyncio.create_task(sess._oa_reader())
    try:
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.sleep(0)
    finally:
        reader.cancel()
    kinds = [json.loads(m).get("type") for m in sat.sent if isinstance(m, str)]
    require("flush" in kinds, f"barge-in no longer flushes the satellite: {kinds}")

# =====================================================================
# I — a session opens once, and nothing outlives it
# =====================================================================
class _FakeProvider:
    """Enough of the provider to get through open()."""

    def __init__(self):
        self.closed = False

    async def recv(self):
        return json.dumps({"type": "session.created"})

    async def send(self, data):
        pass

    def __aiter__(self):
        async def gen():
            await asyncio.sleep(3600)
            yield ""
        return gen()

    async def close(self, *a, **kw):
        self.closed = True


class _provider_factory:
    """Stubs ws_connect and hands back the list of provider sockets it created."""

    def __init__(self):
        self._made = []
        self._original = None

    async def __aenter__(self):
        self._original = CS.ws_connect

        async def fake(*a, **kw):
            p = _FakeProvider()
            self._made.append(p)
            return p
        CS.ws_connect = fake
        return self._made

    async def __aexit__(self, *exc):
        CS.ws_connect = self._original
        await asyncio.sleep(0.05)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task() and "core_server" in str(t.get_coro()):
                t.cancel()
        return False


def _openable_session():
    srv = CS.CoreServer.__new__(CS.CoreServer)
    srv.dispatcher = None
    srv.idle_timeout = 999
    srv.credentials = None
    srv.model = "m"
    srv.api_key = "k"
    srv.voice = "marin"
    srv.eagerness = "auto"
    srv.effort = "low"
    sess = CS.Session(srv, _Satellite())
    sess.active = True
    return sess


def _live(name):
    return [t for t in asyncio.all_tasks()
            if not t.done() and name in (t.get_coro().__qualname__ or "")]


async def t_i_t1_open_close_open_opens_a_second_session():
    """T1 — die Bedingung, an der der Release-Smoke scheiterte. _closing bedeutete
    frueher 'war schon mal', nicht 'laeuft gerade', und sperrte die Sitzung dauerhaft."""
    async with _provider_factory() as made:
        sess = _openable_session()
        await sess.open()
        await sess.close(reason="pi")
        await sess.open()
        try:
            require(sess.active, "the second session did not become active")
            kinds = [json.loads(m).get("type") for m in sess.ws.sent if isinstance(m, str)]
            require_equal(kinds.count("session_ready"), 2,
                          f"the second session never reported ready: {kinds}")
            require_equal(len(made), 2, f"{len(made)} provider sockets for two sessions")
        finally:
            await sess.close(reason="pi")


async def t_i_t2_a_duplicate_start_does_not_spawn_a_second_generation():
    """T2 — und it must not tear the live session down either: a duplicate start is not
    an instruction to restart."""
    async with _provider_factory() as made:
        sess = _openable_session()
        await sess.open()
        first = (sess.reader, sess.timer, sess._tool_worker)
        try:
            await sess.open()
            await asyncio.sleep(0)
            require_equal((sess.reader, sess.timer, sess._tool_worker), first,
                          "the duplicate start replaced the live session's tasks")
            require(sess.active, "the duplicate start tore down the live session")
            require_equal(len(made), 1, f"{len(made)} provider sockets after a duplicate start")
            for kind in ("_oa_reader", "_timeout_loop", "_tool_loop"):
                require_equal(len(_live(kind)), 1, f"{kind}: {len(_live(kind))} tasks")
            require_equal(sess.open_attempts, 1, "the duplicate start was counted as an open")
        finally:
            await sess.close(reason="pi")


async def t_i_t3_close_leaves_no_resource_of_its_generation():
    """T3 — references cleared, tasks finished, provider socket closed."""
    async with _provider_factory() as made:
        sess = _openable_session()
        await sess.open()
        tasks = (sess.reader, sess.timer, sess._tool_worker)
        await sess.close(reason="pi")
        await asyncio.sleep(0.05)
        require_equal((sess.reader, sess.timer, sess._tool_worker), (None, None, None),
                      "close() left task references behind")
        require_equal(sess.oa, None, "close() left the provider socket attached")
        require_equal(sess.active, False, "the session is still marked active")
        for t in tasks:
            require(t.cancelled() or t.done(), f"{t.get_coro().__qualname__} outlived close()")
        require_equal([p for p in made if not p.closed], [], "a provider socket stayed open")
        for kind in ("_oa_reader", "_timeout_loop", "_tool_loop"):
            require_equal(len(_live(kind)), 0, f"{kind} is still running")


async def t_i_t4_closing_twice_is_safe_and_says_it_once():
    """T4 — _handle_pi always closes in its finally, including after a clean session_end.
    A second close must not send the satellite a second session_end nor log a second
    session."""
    async with _provider_factory():
        sess = _openable_session()
        real_log = CS.log
        CS.log = _Log()
        try:
            await sess.open()
            await sess.close(reason="pi")
            await sess.close(reason="disconnect")
            await sess.close(reason="disconnect")
            captured = CS.log
        finally:
            CS.log = real_log
        require_equal(len(captured.of("core.session_closing")), 1,
                      "the session was reported closed more than once")
        ends = [m for m in sess.ws.sent
                if isinstance(m, str) and json.loads(m).get("type") == "session_end"]
        require_equal(len(ends), 1, f"the satellite got {len(ends)} session_end messages")


async def t_i_t5_a_reopened_session_owns_only_its_own_resources():
    """T5 — nothing of the first generation may reach the second."""
    async with _provider_factory() as made:
        sess = _openable_session()
        await sess.open()
        gen1 = (sess.reader, sess.timer, sess._tool_worker, made[0])
        await sess.close(reason="pi")
        await sess.open()
        try:
            gen2 = (sess.reader, sess.timer, sess._tool_worker)
            for old, new in zip(gen1, gen2):
                require(old is not new, "a task was carried over into the new session")
                require(old.cancelled() or old.done(),
                        "a first-generation task is still running in the second session")
            require(sess.oa is not gen1[3], "the old provider socket is still attached")
            require(gen1[3].closed, "the old provider socket was never closed")
            for kind in ("_oa_reader", "_timeout_loop", "_tool_loop"):
                require_equal(len(_live(kind)), 1, f"{kind}: {len(_live(kind))} tasks running")
        finally:
            await sess.close(reason="pi")


async def t_i_t5_a_silent_stop_does_not_mute_the_next_session():
    """A session ended by silent stop left _stopping set for good. The audio path is
    gated on it, so the NEXT session on that connection would have played nothing."""
    async with _provider_factory():
        sess = _openable_session()
        await sess.open()
        sess._stopping = True                       # was der Silent-Stop setzt
        await sess.close(reason="silent_stop")
        await sess.open()
        try:
            require_equal(sess._stopping, False,
                          "the new session starts muted — _stopping survived the close")
            require_equal(sess._turn, {}, "a half-finished turn survived into the new session")
        finally:
            await sess.close(reason="pi")


async def t_i_t5_queued_tool_rounds_do_not_cross_into_the_next_session():
    """A tool round still queued at close belongs to the old provider session: its
    call_ids mean nothing to the new one."""
    async with _provider_factory():
        sess = _openable_session()
        await sess.open()
        sess._tool_queue.put_nowait([{"name": "x", "call_id": "alt-1", "arguments": "{}"}])
        sess._tool_queue.put_nowait([{"name": "y", "call_id": "alt-2", "arguments": "{}"}])
        real_log = CS.log
        CS.log = _Log()
        try:
            await sess.close(reason="pi")
            captured = CS.log
        finally:
            CS.log = real_log
        require_equal(sess._tool_queue.qsize(), 0,
                      "old tool rounds are still queued for the next session")
        closed = captured.of("core.SESSION_CLOSED")
        require_equal(closed[0].get("dropped_tool_rounds"), 2,
                      f"the discarded rounds were not accounted for: {closed}")


async def t_i_t6_the_protocol_path_opens_a_second_session():
    """T6 — the release smoke itself, as a permanent regression: session_start /
    session_end / session_start over ONE connection, driven through _handle_pi."""
    made = []
    original = CS.ws_connect

    async def fake(*a, **kw):
        p = _FakeProvider()
        made.append(p)
        return p

    class _PiConnection:
        remote_address = ("192.168.178.194", 51000)

        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        async def close(self, *a, **kw):
            pass

        def __aiter__(self):
            async def gen():
                yield json.dumps({"type": "session_start"})
                await asyncio.sleep(0)
                yield json.dumps({"type": "session_end"})
                await asyncio.sleep(0)
                yield json.dumps({"type": "session_start"})
                await asyncio.sleep(0)
            return gen()

    CS.ws_connect = fake
    srv = CS.CoreServer.__new__(CS.CoreServer)
    srv.dispatcher = None
    srv.idle_timeout = 999
    srv.model = "m"
    srv.api_key = "k"
    srv.voice = "marin"
    srv.eagerness = "auto"
    srv.effort = "low"
    srv._busy = False
    srv.credentials = None

    async def no_auth(ws):
        return "pi-wohnzimmer"
    srv._authenticate = no_auth
    ws = _PiConnection()
    try:
        await CS.CoreServer._handle_pi(srv, ws)
        await asyncio.sleep(0.05)
        kinds = [json.loads(m).get("type") for m in ws.sent if isinstance(m, str)]
        require_equal(kinds.count("session_ready"), 2,
                      f"the second session_start did not open a session: {kinds}")
        require_equal([p for p in made if not p.closed], [],
                      "a provider socket outlived the connection")
        for kind in ("_oa_reader", "_timeout_loop", "_tool_loop"):
            require_equal(len(_live(kind)), 0, f"{kind} outlived the connection")
    finally:
        CS.ws_connect = original
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task() and "core_server" in str(t.get_coro()):
                t.cancel()


# =====================================================================
# J — one failing tool does not strand the rest of its batch
# =====================================================================
class _PartlyBrokenDispatcher(_Dispatcher):
    def __init__(self, broken):
        super().__init__()
        self.release.set()
        self.broken = broken

    async def dispatch(self, name, args):
        self.calls.append(name)
        if name == self.broken:
            raise RuntimeError("das Tool ist kaputt")
        return {"success": True, "data": {"tool": name}}


async def t_j_a_failing_tool_does_not_strand_its_siblings():
    """Reproduced before the change: three calls in one response.done with the second
    raising produced ONE function_call_output and no response.create at all. The provider
    then waited for outputs that never came, and the turn hung until the idle timeout."""
    disp = _PartlyBrokenDispatcher("tool_b")
    sess, sat = _session(disp)
    sess.oa = _Provider([json.dumps({"type": "response.done", "response": {"output": [
        {"type": "function_call", "name": n, "call_id": f"call-{n}", "arguments": "{}"}
        for n in ("tool_a", "tool_b", "tool_c")]}})])
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    real_log = CS.log
    CS.log = _Log()
    try:
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.wait_for(sess._tool_queue.join(), timeout=10)
        captured = CS.log
    finally:
        CS.log = real_log
        reader.cancel()
        worker.cancel()
    sent = [json.loads(m) for m in sess.oa.sent if isinstance(m, str)]
    ids = [m["item"]["call_id"] for m in sent if m.get("type") == "conversation.item.create"]
    require_equal(ids, ["call-tool_a", "call-tool_b", "call-tool_c"],
                  f"the failing tool stranded its siblings: {ids}")
    require(any(m.get("type") == "response.create" for m in sent),
            "response.create was never sent — the turn would hang until the idle timeout")
    require_equal(disp.calls, ["tool_a", "tool_b", "tool_c"], disp.calls)
    done = captured.of("core.TOOL_DONE")
    require_equal([d["ok"] for d in done], [True, False, True],
                  f"the failure was not reported honestly: {done}")
    require(captured.of("core.tool_failed"), "the failure was not logged")


async def t_j_a_failing_tool_leaks_nothing_into_the_log():
    disp = _PartlyBrokenDispatcher("tool_b")
    sess, sat = _session(disp)
    sess.oa = _Provider([json.dumps({"type": "response.done", "response": {"output": [
        {"type": "function_call", "name": "tool_b", "call_id": "call-1",
         "arguments": '{"entity_id": "light.wohnzimmer", "token": "sk-geheim"}'}]}})])
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    real_log = CS.log
    CS.log = _Log()
    try:
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.wait_for(sess._tool_queue.join(), timeout=10)
        captured = CS.log
    finally:
        CS.log = real_log
        reader.cancel()
        worker.cancel()
    blob = json.dumps([kw for _e, kw in captured.events], default=str)
    require("sk-geheim" not in blob, f"a credential-shaped argument reached the log: {blob}")
    require("light.wohnzimmer" not in blob, "tool arguments reached the log")

# =====================================================================
# Session setup breakdown
# =====================================================================
def t_session_setup_is_broken_down_not_just_totalled():
    src = open(CORE_SRC, encoding="utf-8").read()
    ready = src[src.index('log.info("core.session_ready"'):]
    ready = ready[:ready.index("\n\n")]
    for field in ("setup_ms", "prepare_ms", "provider_connect_and_handshake_ms",
                  "configure_and_ready_ms"):
        require(field in ready, f"session_ready does not report {field}")
    require("session_id=self.session_id" in ready, "session_ready is not correlated")


def t_security_tree_matches_freeze_and_explicit_n2_amendment():
    from _security_amendment import require_n2_security_tree
    require_n2_security_tree(REPO)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
