"""A5 packaged shell uses the real private gateway and untouched native assets."""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_nexus_dashboard import world
from solvio.dashboard.hermes_view import ROOT, SHELL_ASSETS


async def t_native_manifest_survives_composition_and_public_shell_does_not_start_work():
    manifest = json.loads((ROOT / 'BUILD.json').read_text())
    before = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in manifest['files']}
    require_equal(before, manifest['files'])
    async with world() as w:
        guest = await w.new_client()
        response = await guest.get('/dashboard/hermes/solvio-view.html?run=ar-0123456789abcdef')
        require_equal(response.status, 200)
        html = await response.text()
        require('id="solvio-workspace"' in html)
        require('<div id="root" hidden>' in html)
        entry = [name for name in manifest['files'] if name.startswith('assets/solvio-view-') and name.endswith('.js')]
        require_equal(len(entry), 1)
        require(entry[0] in html)
        require_equal(response.headers['Cache-Control'], 'no-store')
        require_equal(response.headers['X-Frame-Options'], 'SAMEORIGIN')
        require('microphone=()' in response.headers['Permissions-Policy'])
        for name, source in SHELL_ASSETS.items():
            delivered = await guest.get('/dashboard/hermes/' + name)
            require_equal(delivered.status, 200)
            require_equal(await delivered.read(), source.read_bytes())
            require(not source.is_relative_to(ROOT), 'upstream rebuild must not delete Core shell assets')
        require_equal(w.ledger.recent_runs(), [])
        require_equal(await w.store.list_pending(), [])
    require_equal({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in before}, before)


async def t_static_shell_does_not_grant_access_to_bound_native_observations():
    async with world() as w:
        accepted = await (await w.start()).json()
        guest = await w.new_client()
        foreign = await w.new_client(); await w.login(foreign, 'other-owner')
        for suffix in ('', '/events'):
            path = '/v1/agent/runs/' + accepted['run_id'] + suffix
            require_equal((await guest.get(path)).status, 401)
            require_equal((await foreign.get(path)).status, 404)
            require_equal((await w.client.get(path)).status, 200)
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal(w.ledger.get_run(accepted['run_id']).planner_calls, 0)
        require_equal(w.ledger.steps_for_run(accepted['run_id']), [])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
