"""Exact candidate deployment and public worker handshake; temp files only."""
from __future__ import annotations
import asyncio
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.portal.build import MODULES, compute_build, differences, expected_build
from solvio.portal.client import PortalClient, WorkerBuildMismatch
from solvio.portal.binding import DEMO_BINDING

REPO = Path(__file__).resolve().parents[1]


def deployer(target):
    spec = importlib.util.spec_from_file_location('isolated_portal_deployer', REPO / 'scripts/portal_deploy.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.TARGET = str(target); module.LINK = str(target / 'app')
    module.run = lambda *_: (_ for _ in ()).throw(AssertionError('no installer command in candidate test'))
    return module


def staged(target):
    module = deployer(target)
    build = expected_build(str(REPO)); path = Path(module.stage(build))
    require_equal(differences(str(REPO), str(path)), [])
    require_equal(compute_build(str(path))[0], build)
    require_equal(module.contamination(str(path)), [])
    require_equal({str(p.relative_to(path)) for p in path.rglob('*') if p.is_file()}, set(MODULES))
    return module, path, build


def t_exact_candidate_is_staged_by_existing_deployer_before_atomic_switch():
    with tempfile.TemporaryDirectory(prefix='portal-deployment-') as directory:
        root = Path(directory).resolve(); old = root / 'old'; old.mkdir()
        (root / 'app').symlink_to(old)
        module, path, build = staged(root)
        require_equal((root / 'app').resolve(), old, 'staging does not switch a live link')
        module.switch(str(path))
        require_equal((root / 'app').resolve(), path)
        require_equal(compute_build(str(root / 'app'))[0], build)
        require(old.is_dir(), 'previous tree is retained')


def t_staged_candidate_detects_missing_or_changed_worker_bytes():
    with tempfile.TemporaryDirectory(prefix='portal-deployment-') as directory:
        _module, path, build = staged(Path(directory))
        source = path / 'solvio/portal/service.py'
        source.write_bytes(source.read_bytes() + b'\n# deliberate isolated drift\n')
        require(compute_build(str(path))[0] != build)
        require_equal(differences(str(REPO), str(path)), ['solvio/portal/service.py'])
        source.unlink()
        require_equal(compute_build(str(path))[1]['solvio/portal/service.py'], 'missing')


async def real_worker_scenario(root):
    _module, path, build = staged(root)
    socket_path = root / 'w.sock'
    # Actual staged PortalWorker and protocol. Only its read-only isolation
    # sentinels point into the temporary fixture: a same-UID test must not open
    # real Core secrets while claiming the production UID separation.
    code = '''import asyncio,os,sys
sys.path.insert(0,sys.argv[1])
from solvio.portal import service
service.SEALED=(sys.argv[2]+".missing-sentinel",)
worker=service.PortalWorker(socket_path=sys.argv[2],core_uid=os.getuid())
async def main():
    try: await worker.serve()
    finally: await worker.shutdown()
asyncio.run(main())
'''
    env = dict(os.environ); env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['SOLVIO_PORTAL_TEST_KEYSTORE'] = str(root / 'keys')
    with (root / 'worker.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(path), str(socket_path)],
                                   cwd=root, env=env, stdout=log, stderr=log)
        try:
            client = PortalClient(str(socket_path), repo_root=str(REPO))
            for _ in range(100):
                if socket_path.exists(): break
                require(process.poll() is None, 'staged worker exited before binding')
                await asyncio.sleep(.02)
            require(socket_path.exists(), 'staged worker bound its real public socket')
            require_equal(await client.verify_build(), build)
            reply = await client.ping()
            require_equal(reply['sessions'], 0)
            require_equal(reply['uid'], os.getuid(), 'temporary worker, not production isolation proof')
            source = path / 'solvio/portal/service.py'
            source.write_bytes(source.read_bytes() + b'\n# changed after process startup\n')
            try: await client.open_session(DEMO_BINDING)
            except WorkerBuildMismatch: pass
            else: raise AssertionError('changed deployed bytes must reject before any browser session')
            require_equal((await client.ping())['sessions'], 0)
        finally:
            process.send_signal(signal.SIGINT)
            try: await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                process.kill(); await asyncio.to_thread(process.wait, 5)


def t_real_staged_worker_and_public_client_match_and_reject_live_drift_before_session():
    with tempfile.TemporaryDirectory(prefix='portal-worker-') as directory:
        asyncio.run(real_worker_scenario(Path(directory)))


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
