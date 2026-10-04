"""Real backup/restore keeps the Core's immutable document contract intact."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal, require_raises
import test_document_adapter_backup as B
import test_agent_document_execution as T
import test_agent_document_native_execution as N
from solvio.agent_runtime import document_contract as DC
from solvio.agent_runtime.document_results import read_result
from solvio.storage import engine, inventory, restore
from solvio.storage.offsite import restore as ORS

enforce_assertions()


def _mode(path):
    return path.stat().st_mode & 0o7777


def _source(root):
    item = B._item()
    source = Path(item.expanded)
    source.mkdir(parents=True)
    for name, mode in (("sealed", 0o400), ("executable", 0o700), ("regular", 0o600)):
        path = source / name
        path.write_bytes(b"same bytes, distinct source modes")
        path.chmod(mode)
    return item, B._backup(root, (item,))


def t_local_and_offsite_restore_modes_without_changing_shared_backup_inodes():
    with B._world() as root:
        item, backup = _source(root)
        saved = Path(backup.path) / item.dest
        require_equal(_mode(saved / "sealed"), 0o600)
        require((saved / "sealed").stat().st_nlink > 1, "real backup dedup must be exercised")
        local = root / "local"
        report = restore.restore_set(backup.path, str(local))
        require(report.ok, report.findings)
        offsite = root / "offsite"
        offsite.mkdir()
        ORS._place(backup.path, str(offsite), ())
        for target in (local, offsite):
            for name, mode in (("sealed", 0o400), ("executable", 0o700), ("regular", 0o600)):
                path = target / item.dest / name
                require_equal(_mode(path), mode, str(path))
                require_equal(path.stat().st_nlink, 1)
                require_equal(path.read_bytes(), (saved / name).read_bytes())
                require_equal(_mode(saved / name), 0o600, "restore changed shared backup inode")


def t_missing_legacy_mode_remains_private_and_invalid_modes_are_refused():
    with B._world() as root:
        item, backup = _source(root)
        manifest = Path(backup.path) / engine.MANIFEST_NAME
        original = json.loads(manifest.read_text())
        old = json.loads(json.dumps(original))
        for row in old["entries"][0]["files"]:
            row.pop("mode")
        manifest.write_text(json.dumps(old))
        for i, native in enumerate((False, True)):
            target = root / f"legacy-{i}"
            target.mkdir()
            if native:
                ORS._place(backup.path, str(target), ())
            else:
                require(restore.restore_set(backup.path, str(target)).ok)
            require_equal(_mode(target / item.dest / "sealed"), 0o600)
        for i, mode in enumerate(("0o4755", "0o1777", "0o888", "755", True, 0o400, None)):
            bad = json.loads(json.dumps(original))
            bad["entries"][0]["files"][0]["mode"] = mode
            manifest.write_text(json.dumps(bad))
            require_raises(restore.RestoreRefused, restore.restore_set,
                           backup.path, str(root / f"bad-local-{i}"))
            target = root / f"bad-offsite-{i}"
            target.mkdir()
            require_raises(ORS.RestoreError, ORS._place, backup.path, str(target), ())
        require_equal(_mode(Path(backup.path) / item.dest / "sealed"), 0o600)


def t_nonempty_local_destination_cannot_overwrite_a_hardlinked_external_file():
    with B._world() as root:
        item, backup = _source(root)
        sentinel = root / "external-sentinel"
        sentinel.write_bytes(b"must survive a refused restore")
        sentinel.chmod(0o600)
        target = root / "nonempty"
        existing = target / item.dest
        existing.mkdir(parents=True)
        os.link(sentinel, existing / "sealed")
        require_raises(restore.RestoreRefused, restore.restore_set, backup.path, str(target))
        require_equal(sentinel.read_bytes(), b"must survive a refused restore")
        require_equal(_mode(sentinel), 0o600)
        require_equal(sorted(path.name for path in existing.iterdir()), ["sealed"])


async def t_completed_native_document_reads_and_downloads_after_actual_backup_restore():
    async with T.world() as w:
        accepted = await (await w.start(N.configure(w, "docx"))).json()
        run_id = accepted["run_id"]
        original_source = DC.read_for_run(w.ledger, run_id)
        grant = w.orch.task_authority.for_run(run_id)
        finished = await T.advance(w.orch, run_id)
        require_equal(finished.state, T.S.SUCCEEDED, finished.result_summary)
        output = next(a for a in w.ledger.artifacts_for_run(run_id) if a.kind == "document_result")
        original_result = read_result(w.ledger, run_id, output.artifact_id)
        canonical = Path(T.S.state_dir()) / T.S.ARTIFACT_DIRNAME
        item = inventory.Item(name="test-document-artifacts", source=str(canonical),
            dest="RuntimeState/agent-artifacts", kind=inventory.TREE,
            category="runtime", why="Temporary native document restore proof", needs_encryption=True)
        root = Path(T.S.state_dir())
        backup = B._backup(root, (item,))
        before_calls = len(w.calls())
        for i, offsite in enumerate((False, True)):
            shutil.rmtree(canonical)
            target = root / f"restore-{i}"
            target.mkdir()
            if offsite:
                ORS._place(backup.path, str(target), ())
            else:
                report = restore.restore_set(backup.path, str(target))
                require(report.ok, report.findings)
            # Runbook placement within this fixture's original temporary state
            # root: ledger paths and immutable hashes remain the original ones.
            shutil.move(str(target / item.dest), str(canonical))
            bound = DC.check_prepared(w.ledger, task_id=grant.task_id, run_id=run_id,
                                      entries=grant.capabilities)
            with w.ledger._open() as connection:
                require_equal(DC._read_bound(connection, bound), original_source)
            require_equal(read_result(w.ledger, run_id, output.artifact_id), original_result)
            client = await w.new_client()
            await w.login(client)
            response = await client.get(f"/v1/agent/runs/{run_id}/artifacts/{output.artifact_id}/download")
            require_equal(response.status, 200)
            require_equal(await response.read(), original_result)
            require_equal(len(w.calls()), before_calls, "restored read caused a new provider turn")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
