"""Native usage reader: only synthetic CLI processes and temporary HOME data."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.specialists import claude_usage as U, launcher as L

SECRET = "SYNTHETIC_SECRET_SIBLING_NEVER_PROJECT"
ACCOUNT = {"accountUuid": "synthetic-account-1234", "emailAddress": "owner@example.invalid"}


def usage():
    return {"subscription_type": "max", "rate_limits_available": True,
        "rate_limits": {"extra_usage": {"is_enabled": False, "monthly_limit": None,
            "used_credits": None, "utilization": None, "currency": None}},
        "session": {"total_cost_usd": 0, "total_api_duration_ms": 0, "model_usage": {}},
        "behaviors": None}


FAKE = r'''
import json, os, pathlib, signal, subprocess, sys, time
ROOT = pathlib.Path(__ROOT__)
MODE = __MODE__
path = pathlib.Path(os.environ['HOME']) / __CONFIG__
debug = pathlib.Path(sys.argv[sys.argv.index('--debug-file') + 1])
root_config = json.loads(path.read_text())
body = __BODY__
(ROOT / 'pids.json').write_text(json.dumps([os.getppid(), os.getpid()]))
assert os.environ.get('CLAUDE_CODE_DISABLE_FAST_MODE') == '1'
assert not any(k in os.environ for k in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY', 'CLAUDE_CONFIG_DIR'))
for flag, value in (('--tools',''), ('--setting-sources',''),
                    ('--mcp-config','{"mcpServers":{}}')):
    assert sys.argv[sys.argv.index(flag) + 1] == value
assert '--strict-mcp-config' in sys.argv and '--safe-mode' in sys.argv
assert '--no-session-persistence' in sys.argv and '--no-chrome' in sys.argv
assert '--disable-slash-commands' in sys.argv
for expected in ('initialize', 'get_usage', 'end_session'):
    frame = json.loads(sys.stdin.readline())
    assert frame['type'] == 'control_request' and frame['request']['subtype'] == expected
    if expected == 'initialize':
        assert frame['request']['hooks'] == {} and frame['request']['sdkMcpServers'] == []
        account = root_config['oauthAccount']
        response = {'account': {'apiProvider': 'firstParty', 'subscriptionType': 'Claude Max',
            'email': account['emailAddress']}}
        if MODE == 'wrong_account_route': response['account']['apiProvider'] = 'bedrock'
        if MODE == 'wrong_account_plan': response['account']['subscriptionType'] = 'Claude Pro'
        if MODE == 'wrong_account_email': response['account']['email'] = 'else@example.invalid'
    elif expected == 'get_usage':
        assert frame['request']['skip_behaviors'] is True
        if MODE in ('timeout', 'cancel'):
            child = subprocess.Popen([sys.executable, '-c',
                'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (ROOT / 'pids.json').write_text(json.dumps([os.getppid(), os.getpid(), child.pid]))
            time.sleep(60)
        diag = '[DEBUG] fetchUtilization: GET (attempt 1)\n[DEBUG] fetchUtilization: 200 after 1 attempt(s)\n'
        if MODE == 'seeded_error': diag += '[ERROR] Usage fetch returned a fieldless or non-object body (in-band error)\n'
        if MODE == 'fetch_error': diag += '[ERROR] Failed to load usage data\n'
        if MODE == 'generic_warning': diag += '[WARN] another native failure\n'
        if MODE == 'missing_fetch': diag = '[DEBUG] no request made\n'
        if MODE == 'duplicate_fetch': diag += diag
        if MODE == 'debug_oversize': diag += 'x' * 2000001
        if MODE != 'missing_debug': debug.write_text(diag)
        if MODE in ('on', 'recent_changed_on'): body['rate_limits']['extra_usage']['is_enabled'] = True
        if MODE == 'recent_window_drift':
            body['rate_limits']['seven_day'] = {'utilization': 33.5, 'resets_at': None}
            body['rate_limits']['limits'] = [{'kind': 'weekly_scoped', 'percent': 35}]
        if MODE == 'recent_disabled_reason_changed': body['rate_limits']['extra_usage']['disabled_reason'] = 'different'
        if MODE == 'missing_enabled': del body['rate_limits']['extra_usage']['is_enabled']
        if MODE == 'missing_behaviors': del body['behaviors']
        if MODE == 'null_rate': body['rate_limits'] = None
        if MODE == 'bad_numeric': body['rate_limits']['extra_usage']['used_credits'] = True
        if MODE == 'nan': body['rate_limits']['extra_usage']['used_credits'] = float('nan')
        if MODE == 'model_activity': body['session']['model_usage'] = {'any': {'tokens': 1}}
        if MODE == 'duration_activity': body['session']['total_api_duration_ms'] = 1
        if MODE == 'unsupported_plan': body['subscription_type'] = 'enterprise'
        if MODE not in ('stale', 'seeded_clean', 'no_cache', 'recent', 'recent_changed_on',
                        'recent_window_drift', 'recent_disabled_reason_changed'):
            root_config['cachedUsageUtilization'] = {'fetchedAtMs': int(time.time() * 1000),
                'accountUuid': root_config['oauthAccount']['accountUuid'],
                'utilization': json.loads(json.dumps(body['rate_limits']))}
            if MODE == 'cache_mismatch': root_config['cachedUsageUtilization']['utilization'] = {'extra_usage': {'is_enabled': True}}
            if MODE == 'cache_future': root_config['cachedUsageUtilization']['fetchedAtMs'] += 60000
            if MODE == 'cache_other_account': root_config['cachedUsageUtilization']['accountUuid'] = 'another-account-987'
            if MODE == 'account_changed': root_config['oauthAccount']['accountUuid'] = 'another-account-987'
            path.write_text(json.dumps(root_config))
        response = body
    else:
        response = {}
    result = {'type': 'control_response', 'response': {'subtype': 'success',
        'request_id': frame['request_id'], 'response': response}}
    if expected == 'end_session' or (MODE == 'missing_usage_payload' and expected == 'get_usage'):
        del result['response']['response']
    if MODE == 'wrong_response': result['response']['request_id'] = 'foreign'
    if MODE == 'model_frame': result = {'type': 'assistant', 'message': 'not a control reply'}
    if MODE == 'stdout_oversize': print('x' * 300000, flush=True); time.sleep(.1); sys.exit(1)
    print(json.dumps(result), flush=True)
if MODE == 'trailing_frame': print(json.dumps({'type': 'result', 'result': 'unexpected'}), flush=True)
if MODE == 'exit_failed': sys.exit(1)
'''


@contextmanager
def fixture(mode="ok", *, legacy=False):
    with tempfile.TemporaryDirectory(prefix="solvio-usage-test-") as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir(mode=0o700)
        name = ".claude/.config.json" if legacy else ".claude.json"
        path = home / name
        path.parent.mkdir(exist_ok=True, mode=0o700)
        config = {"oauthAccount": ACCOUNT.copy(), "primaryApiKey": SECRET,
                  "env": {"ACCESS_TOKEN": SECRET}, "projects": {"private": SECRET}}
        if mode != "no_cache":
            age = 60000 if mode.startswith('recent') else 600000
            config["cachedUsageUtilization"] = {"fetchedAtMs": int(time.time() * 1000) - age,
                "accountUuid": ACCOUNT["accountUuid"], "utilization": usage()["rate_limits"]}
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        program = root / "claude"
        source = ("#!" + sys.executable + "\n" + FAKE.replace("__ROOT__", repr(directory))
            .replace("__MODE__", repr(mode)).replace("__CONFIG__", repr(name))
            .replace("__BODY__", repr(usage())))
        program.write_text(source)
        program.chmod(0o700)
        build = U._ReviewedBuild(str(program), "2.1.261", U._file_digest(str(program)))
        invocation = L.Invocation(str(program), ("--print", "--output-format", "json"),
                                  cwd=directory, timeout=1)
        with patch.dict(os.environ, {"HOME": str(home), "ANTHROPIC_API_KEY": SECRET,
            "CLAUDE_CODE_DISABLE_FAST_MODE": "0", "CLAUDE_CONFIG_DIR": "/not/native"}), \
            patch.object(U, "REVIEWED_BUILD", build):
            yield root, home, path, invocation, build


async def read(mode="ok", **kwargs):
    with fixture(mode, **kwargs) as (_, _, _, invocation, _):
        result = await U.NativeUsageReader().read(invocation)
        if result.state != "unknown":
            require(result.applies_to(invocation))
        return result


def t_fresh_native_off_is_bound_observation_without_quote_or_identity_leak():
    async def go():
        with fixture() as (root, _, _, invocation, _):
            result = await U.NativeUsageReader().read(invocation)
            require_equal(result.state, "disabled")
            require_equal(result.subscription, "max")
            require_equal(result.freshness, "fresh_native_cache")
            require(result.applies_to(invocation))
            exported = json.dumps(asdict(result))
            for private in (SECRET, ACCOUNT['accountUuid'], ACCOUNT['emailAddress'], str(root)):
                require(private not in exported, "private native metadata escaped")
            require(not hasattr(result, 'upper_bound_cents'))
            require(not hasattr(U.NativeUsageReader, 'quote'))
    asyncio.run(go())


def t_native_on_is_reported_as_enabled_never_as_zero():
    require_equal(asyncio.run(read("on")).state, "enabled")


def t_seeded_clean_http_200_with_old_or_missing_native_cache_remains_unknown():
    async def go():
        for mode in ('seeded_clean', 'stale', 'no_cache'):
            require_equal((await read(mode)).state, 'unknown')
    asyncio.run(go())


def t_recent_cache_is_explicitly_time_bounded_and_new_enabled_body_cannot_reuse_off():
    async def go():
        with fixture('recent') as (_, _, _, invocation, _):
            result = await U.NativeUsageReader().read(invocation)
            require_equal(result.state, 'disabled')
            require_equal(result.freshness, 'recent_native_cache')
            require(result.applies_to(invocation))
            with patch.object(U.time, 'time', return_value=result.fetched_at_ms / 1000 + 301):
                require(not result.applies_to(invocation))
        require_equal((await read('recent_changed_on')).state, 'unknown')
        require_equal((await read('recent_disabled_reason_changed')).state, 'unknown')
        drift = await read('recent_window_drift')
        require_equal(drift.state, 'disabled')
        require_equal(drift.freshness, 'recent_native_cache')
    asyncio.run(go())


def t_http_200_with_rejected_body_or_other_native_error_is_unknown():
    async def go():
        for mode in ('seeded_error', 'fetch_error', 'generic_warning'):
            require_equal((await read(mode)).state, 'unknown')
    asyncio.run(go())


def t_missing_duplicate_and_oversize_diagnostics_never_prove_freshness():
    async def go():
        for mode in ('missing_debug', 'missing_fetch', 'duplicate_fetch', 'debug_oversize'):
            require_equal((await read(mode)).state, 'unknown')
    asyncio.run(go())


def t_missing_null_bool_nan_controls_and_unsupported_subscription_hold():
    async def go():
        for mode in ('missing_enabled', 'missing_behaviors', 'null_rate', 'bad_numeric',
                     'nan', 'unsupported_plan'):
            require_equal((await read(mode)).state, 'unknown')
    asyncio.run(go())


def t_response_request_and_terminal_frames_are_exactly_bound():
    async def go():
        for mode in ('wrong_response', 'missing_usage_payload', 'model_frame', 'trailing_frame',
                     'stdout_oversize', 'exit_failed'):
            require_equal((await read(mode)).state, 'unknown')
    asyncio.run(go())


def t_init_route_plan_and_email_must_match_same_native_account():
    async def go():
        for mode in ('wrong_account_route', 'wrong_account_plan', 'wrong_account_email', 'account_changed'):
            require_equal((await read(mode)).state, 'unknown')
    asyncio.run(go())


def t_cache_must_match_body_account_and_actual_probe_time():
    async def go():
        for mode in ('cache_mismatch', 'cache_future', 'cache_other_account'):
            require_equal((await read(mode)).state, 'unknown')
    asyncio.run(go())


def t_model_usage_or_api_duration_is_not_a_readonly_session():
    async def go():
        for mode in ('model_activity', 'duration_activity'):
            require_equal((await read(mode)).state, 'unknown')
    asyncio.run(go())


def t_legacy_native_global_record_is_supported_without_separate_cache():
    require_equal(asyncio.run(read(legacy=True)).state, 'disabled')


def t_no_follow_private_bounded_record_and_duplicate_json_keys():
    async def go():
        for mode in ('symlink', 'permissions', 'oversize', 'duplicate'):
            with fixture() as (root, _, path, invocation, _):
                if mode == 'symlink':
                    other = root / 'other'
                    path.rename(other)
                    path.symlink_to(other)
                elif mode == 'permissions': path.chmod(0o644)
                elif mode == 'oversize': path.write_bytes(b' ' * (U.MAX_CONFIG + 1))
                else: path.write_text('{"oauthAccount":{},"oauthAccount":{}}')
                result = await U.NativeUsageReader().read(invocation)
                require_equal(result.state, 'unknown')
                require(not (root / 'pids.json').exists(), 'native started before metadata validation')
    asyncio.run(go())


def t_binary_mismatch_fails_before_launcher_and_new_invocation_cannot_reuse_observation():
    async def go():
        with fixture() as (root, _, _, invocation, build):
            wrong = replace(build, sha256='0' * 64)
            result = await U.NativeUsageReader(_build=wrong).read(invocation)
            require_equal(result.state, 'unknown')
            require(not (root / 'pids.json').exists())
            result = await U.NativeUsageReader().read(invocation)
            require_equal(result.state, 'disabled')
            require(not result.applies_to(replace(invocation, argv=('--model', 'different'))))
            require(not replace(result, observed_at=result.observed_at - 6).applies_to(invocation))
    asyncio.run(go())


def t_account_change_after_quote_invalidates_binding_without_second_fetch():
    async def go():
        with fixture() as (_, _, path, invocation, _):
            result = await U.NativeUsageReader().read(invocation)
            require(result.applies_to(invocation))
            config = json.loads(path.read_text())
            config['oauthAccount']['accountUuid'] = 'other-account-4567'
            path.write_text(json.dumps(config))
            require(not result.applies_to(invocation))
            # A corrupted new cache must return false, never raise into a
            # caller that is checking its last pre-dispatch binding.
            path.write_text(json.dumps(config).replace('"utilization": null', '"utilization": 1e999'))
            require(not result.applies_to(invocation))
    asyncio.run(go())


def t_changed_home_during_reader_is_unknown_and_temporary_files_are_deleted():
    async def go():
        with fixture() as (root, _, _, invocation, _):
            other = root / 'otherhome'
            other.mkdir()
            directories = []
            async def runner(worker, payload, **kwargs):
                directories.append(worker.cwd)
                outcome = await L.run(worker, payload, **kwargs)
                os.environ['HOME'] = str(other)
                return outcome
            result = await U.NativeUsageReader(_runner=runner).read(invocation)
            require_equal(result.state, 'unknown')
            require(directories and all(not Path(d).exists() for d in directories))
    asyncio.run(go())


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _all_gone(root):
    pids = json.loads((root / 'pids.json').read_text())
    deadline = time.monotonic() + 3
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        await asyncio.sleep(.02)
    require(all(not _alive(pid) for pid in pids), 'native group survived reader stop')


def t_timeout_ends_native_process_and_its_child_through_existing_launcher_group():
    async def go():
        with fixture('timeout') as (root, _, _, invocation, _):
            result = await U.NativeUsageReader(_timeout=.3).read(invocation)
            require_equal(result.state, 'unknown')
            await _all_gone(root)
    asyncio.run(go())


def t_repeated_cancellation_waits_for_actual_group_cleanup_and_temp_deletion():
    async def go():
        with fixture('cancel') as (root, _, _, invocation, _):
            entered, release = asyncio.Event(), asyncio.Event()
            original = L._stop_process
            directories = []
            async def stop(process, grace):
                entered.set()
                await release.wait()
                await original(process, grace)
            async def runner(worker, payload, **kwargs):
                directories.append(worker.cwd)
                return await L.run(worker, payload, **kwargs)
            with patch.object(L, '_stop_process', stop):
                job = asyncio.create_task(U.NativeUsageReader(_runner=runner).read(invocation))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (root / 'pids.json').exists():
                        if len(json.loads((root / 'pids.json').read_text())) == 3: break
                    await asyncio.sleep(.01)
                try:
                    require((root / 'pids.json').exists())
                    job.cancel()
                    await asyncio.wait_for(entered.wait(), 2)
                    job.cancel()
                    await asyncio.sleep(0)
                    require(not job.done(), 'second cancel bypassed owned drain')
                finally:
                    release.set()
                    results = await asyncio.gather(job, return_exceptions=True)
                require(isinstance(results[0], asyncio.CancelledError))
            await _all_gone(root)
            require(directories and all(not Path(d).exists() for d in directories))
    asyncio.run(go())


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
