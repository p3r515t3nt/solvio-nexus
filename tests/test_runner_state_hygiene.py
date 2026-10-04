"""The canonical runner hands every suite its own SOLVIO_STATE_DIR (N8/C5).

Measured 2026-09-18: test worlds that build their ledger at a temporary path but
never redirect the state directory wrote 492 run-artifact folders into the
PRODUCTION tree ~/.solvio/agent_runs, because every `state_dir()` default
(artifact_root, conversations, memory, broker) resolves to ~/.solvio. The runner
now owns that directory per suite: removed after a clean pass, kept for
diagnosis after a failure. This suite drives the real runner on synthetic
suites and proves the behaviour, not the wording.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_tests.py"


def _run(suite_source: str, *, name: str, env_extra=None):
    """Run the real runner on ONE synthetic suite inside a throwaway tests copy."""
    with tempfile.TemporaryDirectory(prefix="solvio-runner-state-") as folder:
        tests = Path(folder) / "tests"
        tests.mkdir()
        for helper in ("_guard.py", "_harness.py", "_inventory.py"):
            (tests / helper).write_bytes((ROOT / "tests" / helper).read_bytes())
        (tests / name).write_text(textwrap.dedent(suite_source))
        scripts = Path(folder) / "scripts"
        scripts.mkdir()
        for helper in ("run_tests.py", "_test_worker.py", "update_test_baseline.py"):
            (scripts / helper).write_bytes((ROOT / "scripts" / helper).read_bytes())
        (Path(folder) / "src").symlink_to(ROOT / "src")
        marker = Path(folder) / "observed-state-dir.txt"
        env = {**os.environ, "SOLVIO_OBSERVE_STATE_TO": str(marker), **(env_extra or {})}
        env.pop("SOLVIO_STATE_DIR", None)
        baseline = subprocess.run([sys.executable, str(scripts / "update_test_baseline.py")],
                                  cwd=folder, env=env, capture_output=True, text=True, timeout=120)
        require_equal(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        proc = subprocess.run([sys.executable, str(scripts / "run_tests.py"), name],
                              cwd=folder, env=env, capture_output=True, text=True, timeout=300)
        observed = marker.read_text().strip() if marker.exists() else ""
        return proc, observed


SUITE = '''
    import os, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _guard import enforce_assertions, require
    enforce_assertions()
    from solvio.agent_runtime import store as S

    def t_state_dir_is_runner_owned():
        root = S.artifact_root("ar-0000000000000001")
        os.makedirs(root, mode=0o700, exist_ok=True)
        Path(os.environ["SOLVIO_OBSERVE_STATE_TO"]).write_text(S.state_dir())
        require(%s)

    if __name__ == "__main__":
        from _harness import run_module
        raise SystemExit(run_module(globals(), __name__))
'''


def t_a_suite_never_sees_the_installation_state_dir_and_its_dir_is_removed_after_a_pass():
    proc, observed = _run(SUITE % "True", name="test_synthetic_state.py")
    require_equal(proc.returncode, 0, proc.stdout[-800:] + proc.stderr[-800:])
    require(observed, "the synthetic suite did not observe a state dir")
    home = str(Path.home())
    require(not observed.startswith(os.path.join(home, ".solvio")), observed)
    require(os.path.basename(observed).startswith("solvio-st-"), observed)
    require(not Path(observed).exists(), "state dir of a passed suite must be removed")
    require("StateDirs: removed=1 kept=0" in proc.stdout, proc.stdout[-400:])


def t_the_state_dir_of_a_failed_suite_is_kept_and_named():
    proc, observed = _run(SUITE % "False", name="test_synthetic_state.py")
    require(proc.returncode != 0)
    require(observed and Path(observed).is_dir(), "state dir of a failed suite must survive")
    require(observed in proc.stdout, "the kept state dir must be named in the report")
    require("StateDirs: removed=0 kept=1" in proc.stdout, proc.stdout[-400:])
    import shutil
    shutil.rmtree(observed, ignore_errors=True)


def t_an_empty_state_dir_of_a_failed_suite_is_not_kept():
    """Review Runde 5, C5-H2: scheitert eine Suite, bevor sie ihren Zustand anfasst, gibt es
    nichts zu diagnostizieren — der leere Ordner wird entfernt statt als Muell zu bleiben."""
    empty_failure = SUITE.replace("        root = S.artifact_root(\"ar-0000000000000001\")\n        os.makedirs(root, mode=0o700, exist_ok=True)\n", "")
    require(empty_failure != SUITE, "the synthetic suite no longer matches the expected shape")
    proc, observed = _run(empty_failure % "False", name="test_synthetic_state.py")
    require(proc.returncode != 0)
    require(observed and not Path(observed).exists(), "an empty state dir of a failed suite was kept")
    require("StateDirs: removed=1 kept=0" in proc.stdout, proc.stdout[-400:])
    require("kept for diagnosis" not in proc.stdout)


def t_keep_env_keeps_the_state_dir_even_after_a_pass():
    proc, observed = _run(SUITE % "True", name="test_synthetic_state.py", env_extra={"SOLVIO_KEEP_TEST_STATE": "1"})
    require_equal(proc.returncode, 0, proc.stdout[-800:])
    require(observed and Path(observed).is_dir(), "SOLVIO_KEEP_TEST_STATE=1 must keep the dir")
    import shutil
    shutil.rmtree(observed, ignore_errors=True)


STORAGE_SUITE = '''
    import os, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _guard import enforce_assertions
    enforce_assertions()
    from solvio.agent_runtime import store as S
    from solvio.storage import engine

    def t_report_both_state_dirs():
        Path(os.environ["SOLVIO_OBSERVE_STATE_TO"]).write_text(
            S.state_dir() + "\\n" + engine.state_dir())

    if __name__ == "__main__":
        from _harness import run_module
        raise SystemExit(run_module(globals(), __name__))
'''


def t_the_backup_state_dir_lives_in_the_suite_state_dir_even_when_one_is_inherited():
    """DEBT-0317: a suite that ran a real backup wrote lock, log and state.json into
    ~/.solvio/storage and met the real evening backup on its lock. The inherited value
    here is a harmless temporary directory; the runner must replace it all the same."""
    with tempfile.TemporaryDirectory(prefix="solvio-outer-storage-") as outer:
        proc, observed = _run(STORAGE_SUITE, name="test_synthetic_storage.py",
                              env_extra={"SOLVIO_STORAGE_STATE_DIR": outer})
    require_equal(proc.returncode, 0, proc.stdout[-800:] + proc.stderr[-800:])
    state, _, storage = observed.partition("\n")
    require(state and storage, f"the synthetic suite reported {observed!r}")
    require_equal(storage, os.path.join(state, "storage"),
                  "the backup state dir is not the suite's own")
    require(not storage.startswith(os.path.join(str(Path.home()), ".solvio")), storage)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
