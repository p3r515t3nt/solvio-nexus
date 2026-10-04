"""N8: execute actual window/status JS with local Node ports, never a browser."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()


def t_actual_window_and_workspace_js_preserve_end_reasons_and_handshake_states():
    node = os.environ.get('SOLVIO_TEST_NODE') or shutil.which('node')
    require(node, 'Existing Node runtime is required; this test never installs or skips it')
    env = dict(os.environ)
    # The source override is only for explicit red/counterfactual invocations.
    # Canonical execution always exercises the current Core asset bytes.
    env.pop('SOLVIO_WINDOW_TEST_ASSETS', None)
    result = subprocess.run([node, str(Path(__file__).with_name('window_share_lifecycle_check.cjs'))],
                            capture_output=True, text=True, timeout=15, env=env)
    require_equal(result.returncode, 0, result.stdout + result.stderr)
    report = json.loads(result.stdout)
    require_equal(report['actualSources'], ['window-share.js', 'hermes-workspace.js'])
    assets = Path(__file__).resolve().parents[1] / 'src/solvio/dashboard/assets'
    require_equal(report['sourceRoot'], str(assets))
    require_equal(report['sourceHashes'], {name: hashlib.sha256((assets / name).read_bytes()).hexdigest()
                  for name in ('window-share.js', 'hermes-workspace.js')})
    require_equal(report['browser'], False)
    require_equal(report['network'], False)
    require_equal(len(report['results']), 32)
    require_equal(len({row['name'] for row in report['results']}), 32)
    require(all(row['passed'] is True for row in report['results']))


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
