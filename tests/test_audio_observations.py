"""Measured voice boundaries with the real Session; no external audio/provider."""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.realtime.audio_observations import AudioObservations
from solvio.realtime.satellite_health import parse_report
from solvio.realtime import core_server as CS
from test_m0_realtime_latency import _openable_session, _FakeProvider


def session():
    sess = _openable_session()
    sess.active = False
    sess.server.audio_observations = AudioObservations()
    sess.observe_authenticated_device('pi-test')
    return sess


def observed(sess):
    return sess.server.audio_observations.snapshot()['devices'][0]


async def cleanup(sess):
    await sess.close('test')
    await sess._drain()


async def t_connected_is_not_an_open_provider_and_preroll_is_not_forwarding():
    sess = session()
    require_equal(observed(sess)['provider'], 'not_opened')
    require_equal(observed(sess)['capture'], 'unknown')
    await sess.feed_audio(b'\x01\x02' * 480)
    require(observed(sess)['last_received_at'] is not None)
    require_equal(observed(sess)['last_forwarded_at'], None)
    async def connect(*a, **kw): return _FakeProvider()
    with patch.object(CS, 'ws_connect', connect):
        try:
            await sess.open()
            require_equal(observed(sess)['provider'], 'ready')
            require(observed(sess)['last_forwarded_at'] is not None)
        finally:
            await cleanup(sess)


async def t_close_is_confirmed_only_after_provider_close_returns():
    entered, release = asyncio.Event(), asyncio.Event()
    class Provider(_FakeProvider):
        async def close(self):
            entered.set(); await release.wait()
    sess = session()
    async def connect(*a, **kw): return Provider()
    with patch.object(CS, 'ws_connect', connect):
        await sess.open()
        closing = asyncio.create_task(sess.close('test'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            require_equal(observed(sess)['conversation'], 'closing')
            require_equal(observed(sess)['provider'], 'closing')
            require_equal(observed(sess)['capture'], 'unknown')
        finally:
            release.set(); await closing; await sess._drain()
        require_equal(observed(sess)['provider'], 'closed_confirmed')
        require_equal(observed(sess)['conversation'], 'ended')
        await sess.close('duplicate')
        require_equal(observed(sess)['provider'], 'closed_confirmed')


async def t_failed_close_remains_unknown_after_new_generation_succeeds():
    class Broken(_FakeProvider):
        async def close(self): raise OSError('synthetic close failure')
    sess = session(); providers = [Broken(), _FakeProvider()]
    async def connect(*a, **kw): return providers.pop(0)
    with patch.object(CS, 'ws_connect', connect):
        await sess.open(); await cleanup(sess)
        require_equal(observed(sess)['provider'], 'unknown')
        await sess.open()
        try:
            require_equal(observed(sess)['generation'], 2)
            require_equal(observed(sess)['previous_unconfirmed'], 1)
            sess._observe_audio('lost', 1)
            sess._observe_audio('forwarded', 1)
            sess._on_provider_lost(1)
            require_equal(observed(sess)['provider'], 'ready')
            require_equal(observed(sess)['last_forwarded_at'], None)
            require_equal(sess._reconnect_task, None)
        finally:
            await cleanup(sess)
        require_equal(observed(sess)['provider'], 'closed_confirmed')
        require_equal(observed(sess)['previous_unconfirmed'], 1)


async def t_cancelled_close_does_not_claim_audio_stopped():
    entered = asyncio.Event()
    class Blocked(_FakeProvider):
        async def close(self): entered.set(); await asyncio.Event().wait()
    sess = session()
    async def connect(*a, **kw): return Blocked()
    with patch.object(CS, 'ws_connect', connect):
        await sess.open()
        closing = asyncio.create_task(sess.close('test'))
        await asyncio.wait_for(entered.wait(), 2)
        closing.cancel(); await asyncio.gather(closing, return_exceptions=True)
        require_equal(observed(sess)['provider'], 'unknown')
        require_equal(observed(sess)['capture'], 'unknown')
        tasks = [sess.reader, sess.timer, sess._tool_worker, sess._persist_worker]
        for task in tasks:
            if task: task.cancel()
        await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)


async def t_disconnect_during_reconnect_backoff_cannot_reopen_a_provider():
    sess = session(); closed = asyncio.Event(); lost = asyncio.Event(); calls = []
    class Provider(_FakeProvider):
        async def close(self): self.closed = True; closed.set()
        def __aiter__(self):
            async def stream():
                await lost.wait()
                if False: yield ''
            return stream()
    async def connect(*a, **kw):
        provider = Provider(); calls.append(provider); return provider
    with patch.object(CS, 'ws_connect', connect), patch.object(CS, 'RECONNECT_BACKOFF', .03):
        await sess.open()
        lost.set()
        await asyncio.wait_for(closed.wait(), 2)
        # The old provider is closed; a reconnect is waiting with no oa/reader.
        sess.audio_disconnected()
        await cleanup(sess)
        await asyncio.sleep(.06)
        require_equal(len(calls), 1)
        require_equal(sess.active, False)
        require_equal(sess.oa, None)
        require_equal(observed(sess)['provider'], 'closed_confirmed')
        require_equal(observed(sess)['conversation'], 'ended')
        require_equal(observed(sess)['transport_connected'], False)


async def t_successful_reconnect_owns_every_worker_until_final_close():
    sess = session(); lost = asyncio.Event(); ready = asyncio.Event(); made = []
    class Old(_FakeProvider):
        def __aiter__(self):
            async def stream():
                await lost.wait()
                if False: yield ''
            return stream()
    async def connect(*a, **kw):
        p = Old() if not made else _FakeProvider()
        made.append(p); return p
    with patch.object(CS, 'ws_connect', connect), patch.object(CS, 'RECONNECT_BACKOFF', .01):
        await sess.open()
        first = (sess.timer, sess._tool_worker, sess._persist_worker)
        lost.set()
        # Wait on the actual reconnect task and its completed open, not a timer.
        for _ in range(100):
            if sess._reconnect_task is not None: break
            await asyncio.sleep(.001)
        require(sess._reconnect_task is not None)
        try:
            await asyncio.wait_for(asyncio.shield(sess._reconnect_task), 2)
            second = (sess.timer, sess._tool_worker, sess._persist_worker)
            require_equal(len(made), 2)
            require_equal(second[0], first[0])
            require_equal(second[2], first[2])
            require(first[1].done())
            require(second[1] is not first[1])
            await cleanup(sess)
            require(all(task.done() for task in first + second))
        finally:
            await cleanup(sess)
            for task in first:
                if not task.done(): task.cancel()
            await asyncio.gather(*first, return_exceptions=True)


async def t_replacement_lost_during_preroll_is_closed_and_never_reported_ready():
    sess = session(); lost = asyncio.Event(); old_closed = asyncio.Event()
    second_lost = asyncio.Event(); made = []
    class Old(_FakeProvider):
        async def close(self): self.closed = True; old_closed.set()
        def __aiter__(self):
            async def stream():
                await lost.wait()
                if False: yield ''
            return stream()
    class ShortLived(_FakeProvider):
        def __aiter__(self):
            async def stream():
                await second_lost.wait()
                self.closed = True
                if False: yield ''
            return stream()
        async def send(self, data):
            if json.loads(data).get('type') == 'input_audio_buffer.append':
                second_lost.set()
                await asyncio.sleep(0)
    async def connect(*a, **kw):
        p = Old() if not made else ShortLived(); made.append(p); return p
    with patch.object(CS, 'ws_connect', connect), patch.object(CS, 'RECONNECT_BACKOFF', .02):
        try:
            await sess.open(); lost.set()
            await asyncio.wait_for(old_closed.wait(), 2)
            await sess.feed_audio(b'\x01\x02' * 480)
            task = sess._reconnect_task
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            await sess._drain()
            require_equal(len(made), 2)
            require(made[1].closed)
            require_equal(sess.active, False)
            require_equal(sess.oa, None)
            require_equal(observed(sess)['conversation'], 'ended')
            require_equal(observed(sess)['provider'], 'closed_confirmed')
        finally:
            await cleanup(sess)


def t_idle_is_local_capture_and_combined_measurement_age_expires():
    now = [1000.]
    observer = AudioObservations(clock=lambda: now[0])
    report = parse_report('pi-test', {'state':'IDLE', 'verdict':'healthy',
                                   'hearing':{'verdict_age_s':20}}, now=1000.)
    a = observer.snapshot([report])['devices'][0]
    require_equal(a['capture'], 'local_listening_reported')
    require_equal(a['provider'], 'unknown')
    now[0] += 56
    b = observer.snapshot([report])['devices'][0]
    require_equal(b['capture'], 'unknown')
    require_equal(b['measurement_age_s'], 76.)
    require_equal(b['observed_at'], 1000.)
    require_equal(AudioObservations().snapshot()['devices'], [])


def t_devices_and_old_sessions_stay_separate_and_history_is_bounded():
    observer = AudioObservations(limit=4)
    observer.connect('iphone-full-device-id', 'phone1', 'voice_iphone')
    observer.event('iphone-full-device-id', 'phone1', 1, 'opening')
    observer.connect('pi-test', 'pi1', 'voice_satellite')
    observer.event('pi-test', 'pi1', 1, 'opening')
    observer.connect('pi-test', 'pi2', 'voice_satellite')
    observer.event('pi-test', 'pi2', 1, 'opening')
    observer.event('pi-test', 'pi1', 1, 'close_confirmed')
    observer.disconnect('pi-test', 'pi1')
    devices = {d['device_id']: d for d in observer.snapshot()['devices']}
    require_equal(devices['pi-test']['session_id'], 'pi2')
    require_equal(devices['pi-test']['transport_connected'], True)
    require_equal(devices['pi-test']['provider'], 'connecting')
    require_equal(len(observer._rows), 4)
    require(observer.snapshot()['history_uncertain'])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
