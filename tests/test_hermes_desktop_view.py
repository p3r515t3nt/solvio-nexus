"""The packaged Desktop reader remains a private, non-executing projection."""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_nexus_dashboard import world
from solvio.dashboard.hermes_view import ROOT, TYPES


async def t_exact_desktop_package_delivers_original_rendering_assets_and_no_execution_bridge():
    manifest = json.loads((ROOT / 'BUILD.json').read_text())
    proof = json.loads((ROOT / 'SOURCE.json').read_text())
    require_equal(manifest['commit'], 'fcbd1076a93841fa88855acce810e342a5b78101')
    require_equal(manifest['renderer'], 'apps/desktop/src/components/assistant-ui/thread/list.tsx::ThreadMessageList')
    require_equal(proof['execution_transport_modules'], 0)
    require_equal(proof['host_bridge'], 'disabled_at_build')
    for name in manifest['components']:
        require(name in proof['sources'])
    for name in proof['sources']:
        require('/src/api/' not in name and not name.endswith('/store/gateway.ts'))
    async with world() as w:
        guest = await w.new_client()
        for name, expected in manifest['files'].items():
            payload = (ROOT / name).read_bytes()
            require_equal(hashlib.sha256(payload).hexdigest(), expected)
            if Path(name).suffix in TYPES and not name.endswith('.html'):
                response = await guest.get('/dashboard/hermes/' + name)
                require_equal(response.status, 200, name)
                require_equal(await response.read(), payload, name)
                require_equal(response.headers['Cache-Control'], 'no-store')
            if name.endswith('.js'):
                require(b'hermesDesktop' not in payload, 'native host bridge must not survive compilation')
        for name in ('BUILD.json', 'SOURCE.json', 'missing.js', 'other.html', 'README.txt'):
            require_equal((await guest.get('/dashboard/hermes/' + name)).status, 404)
        require_equal(w.ledger.recent_runs(), [])
        require_equal(await w.store.list_pending(), [])


async def t_missing_and_revoked_owner_projection_never_becomes_a_new_task():
    async with world() as w:
        accepted = await (await w.start()).json()
        run = accepted['run_id']
        for suffix in ('', '/events'):
            require_equal((await w.client.get('/v1/agent/runs/ar-0000000000000000' + suffix)).status, 404)
        identity = await (await w.client.get('/v1/browser/session')).json()
        response = await w.client.post('/v1/browser/session/logout', json={},
            headers={'Origin': w.origin, 'X-CSRF-Token': identity['csrf_token']})
        require_equal(response.status, 200)
        for suffix in ('', '/events'):
            require_equal((await w.client.get('/v1/agent/runs/' + run + suffix)).status, 401)
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal(w.ledger.steps_for_run(run), [])
        require_equal(w.ledger.get_run(run).planner_calls, 0)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
