"""Real local checkpoint publication -> regular backup -> independent Git restore.

Only temporary data; the encrypted-volume identity is the sole storage fake.
Neither a provider nor an adapter is executed by this recovery test.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from _guard import enforce_assertions, require, require_equal, require_raises
from solvio.agent_runtime.extension_development import seed_repository
from solvio.autopilot.contract import Contract
from solvio.autopilot.publisher import CheckpointPublisher
from solvio.autopilot.store import AutopilotLedger
from solvio.storage import engine, inventory, restore, volume
from solvio.storage.offsite import identity as OI, pack as OP, restore as ORS

enforce_assertions()
UUID = "AAAAAAAA-1111-2222-3333-444444444444"


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Backup Test",
               GIT_AUTHOR_EMAIL="backup@example.invalid",
               GIT_COMMITTER_NAME="Backup Test",
               GIT_COMMITTER_EMAIL="backup@example.invalid")
    return subprocess.run(["git", "-C", str(repo), *args], env=env,
        capture_output=True, text=True, timeout=30, check=True).stdout.strip()


@contextmanager
def _world():
    with tempfile.TemporaryDirectory(prefix="document-backup-") as directory:
        root = Path(directory).resolve()
        with patch.dict(os.environ, {
            "SOLVIO_STATE_DIR": str(root / "state"),
            "SOLVIO_INVENTORY_ROOT": "",
            "SOLVIO_STORAGE_STATE_DIR": str(root / "backup-state"),
            "SOLVIO_STORAGE_CONFIG": str(root / "backup-config.json"),
        }), patch.object(inventory, "CORE_AGENT_PLIST", str(root / "core.plist")), \
                patch.object(inventory, "CORE_REPO", str(root / "absent-core")), \
                patch.object(inventory, "IOS_REPO", str(root / "absent-ios")):
            yield root


def _item():
    rows = [row for row in inventory.items() if row.name == "document-adapter-store"]
    require_equal(len(rows), 1, "canonical document checkpoint store is not inventoried exactly once")
    return rows[0]


def _backup(root, items):
    mount = root / "volume"
    mount.mkdir()
    disk = dict(VolumeUUID=UUID, VolumeName="Temporary backup volume", FilesystemType="apfs",
        Encryption=True, MountPoint=str(mount), Locked=False, Internal=False,
        GlobalPermissionsEnabled=True)
    state = volume.VolumeState(configured=True, volume_uuid=UUID, present=True,
        mounted=True, mount_point=str(mount), volume_name="Temporary backup volume",
        filesystem="apfs", encrypted=True, locked=False,
        total_bytes=2 * 1024 ** 4, free_bytes=1024 ** 4)
    # DEBT-0317: lock, log and state.json of this backup belong to this temporary world,
    # also for callers that did not enter _world() (test_document_restore_modes did not,
    # and its gate runs wrote into the production backup log).
    with patch.dict(os.environ, {"SOLVIO_STORAGE_STATE_DIR": str(root / "backup-state")}), \
            patch.object(inventory, "items", return_value=items), \
            patch.object(volume, "_diskutil_info", return_value=disk):
        result = engine.run_backup(config=volume.StorageConfig(volume_uuid=UUID),
                                  state=state, include_repos=False)
    require(result.ok, result.errors)
    require((root / "backup-state" / "backup.log").is_file(),
            "the backup did not log into this world's own state directory")
    require(restore.verify_set(result.path)["ok"])
    return result


def t_document_store_follows_core_configuration_and_remains_private():
    with _world() as root:
        item = _item()
        require_equal(item.expanded, str(root / "state/document-adapter-store"))
        require_equal(item.kind, inventory.TREE)
        require_equal(item.dest, "RuntimeState/document-adapter-store")
        require(item.needs_encryption)
        require(not item.required, "pre-N7 hosts need no empty fake repository")
        require(".git" not in item.exclude)
        with (root / "core.plist").open("wb") as stream:
            plistlib.dump({"EnvironmentVariables": {
                "SOLVIO_STATE_DIR": str(root / "launchd-state")}}, stream)
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": ""}):
            require_equal(_item().expanded, str(root / "launchd-state/document-adapter-store"))


def t_real_published_checkpoints_and_evidence_survive_backup_restore():
    with _world() as root:
        item = _item()
        canonical = Path(seed_repository(item.expanded))
        development = AutopilotLedger(str(root / "state/autopilot.sqlite3"))
        milestone = "document-backup-proof"
        development.create_milestone(Contract(milestone, "1.0.0", "Temporary adapter.", ()))
        clone = root / "builder-clone"
        _git(root, "clone", "--quiet", "--no-hardlinks", str(canonical), str(clone))
        publisher = CheckpointPublisher(str(canonical))
        publications = []
        sources = ["print('first diagnostic checkpoint')\n", "print('revised checkpoint')\n"]
        for i, source in enumerate(sources):
            (clone / "adapter.py").write_text(source, encoding="utf-8")
            _git(clone, "add", "adapter.py")
            _git(clone, "commit", "-qm", "Temporary local checkpoint")
            publication = publisher.publish(clone=str(clone),
                commit=_git(clone, "rev-parse", "HEAD"), milestone_id=milestone,
                checkpoint_id=f"cp-backup-{i}")
            development.record_publication(milestone, publication)
            publications.append(publication)
        require(not (canonical / "adapter.py").exists(),
                "the actual adapter exists only in canonical Git objects, not HEAD")
        # Packed refs and objects must survive too; this is not merely a loose-ref copy.
        _git(canonical, "pack-refs", "--all", "--prune")
        _git(canonical, "repack", "-a", "-d")
        snapshot = _git(canonical, "rev-parse", "HEAD")
        ledger_item = replace(next(i for i in inventory.items() if i.name == "autopilot"),
                              source=development.path)
        result = _backup(root, (ledger_item, item))
        entry = next(e for e in result.manifest["entries"] if e["name"] == item.name)
        files = {f["rel"] for f in entry["files"]}
        require(".git/packed-refs" in files)
        require(".git/refs" in entry["directories"])
        require((Path(result.path) / item.dest / ".git/refs").is_dir())
        require(any(p.startswith(".git/objects/pack/") and p.endswith(".pack") for p in files))
        development.close()
        shutil.rmtree(root / "state")
        shutil.rmtree(clone)
        target = root / "restored"
        report = restore.restore_set(result.path, str(target), categories={"runtime"})
        require(report.ok, report.findings)
        require_equal(report.checked, 2)
        restored_repo = target / item.dest
        restored_book = AutopilotLedger(str(target / ledger_item.dest))
        try:
            restored_publisher = CheckpointPublisher(str(restored_repo))
            require_equal(_git(restored_repo, "rev-parse", "HEAD"), snapshot)
            require_equal(seed_repository(str(restored_repo)), str(restored_repo))
            require_equal(set(restored_publisher.published(milestone)),
                {(p.canonical_ref, p.commit) for p in publications})
            _git(restored_repo, "fsck", "--full", "--strict")
            for publication, source in zip(publications, sources):
                require(restored_publisher.reachable(publication.commit))
                require_equal(_git(restored_repo, "show", publication.commit + ":adapter.py"),
                              source.strip())
                evidence = restored_book.fresh_evidence(milestone, kind="checkpoint_ref",
                    commit=publication.commit, env_fingerprint="")
                require(evidence is not None and evidence.ok)
                require_equal(json.loads(evidence.payload_json)["canonical_ref"],
                              publication.canonical_ref)
        finally:
            restored_book.close()
        # Existing offsite tar/age and placement consumers preserve the same
        # directory-bearing set. Fresh ephemeral key only; no keychain or S3.
        identity_line, recipient = OI.create_identity()
        archive = root / "temporary.tar.zst.age"
        OP.pack(result.path, recipient=recipient, out_path=str(archive))
        unpacked = root / "offsite-unpacked"
        OP.unpack(str(archive), identity_line=identity_line, dest_dir=str(unpacked))
        del identity_line
        checked = ORS.verify_unpacked(str(unpacked), require_complete=False)
        require(checked.ok, checked.findings)
        offsite_target = root / "offsite-restored"
        offsite_target.mkdir()
        moved, _ = ORS._place(str(unpacked), str(offsite_target), ())
        require_equal({name for name, _ in moved}, {item.name, ledger_item.name})
        offsite_repo = offsite_target / item.dest
        require_equal(_git(offsite_repo, "rev-parse", "HEAD"), snapshot)
        require_equal(set(CheckpointPublisher(str(offsite_repo)).published(milestone)),
                      {(p.canonical_ref, p.commit) for p in publications})
        _git(offsite_repo, "fsck", "--full", "--strict")
        # A missing canonical object pack is detected by the regular manifest verifier.
        pack = next(f for f in files if f.endswith(".pack"))
        (Path(result.path) / item.dest / pack).unlink()
        require(not restore.verify_set(result.path)["ok"])


def t_pre_n7_absence_is_a_visible_optional_skip():
    with _world() as root:
        item = _item()
        require(not Path(item.expanded).exists())
        result = _backup(root, (item,))
        require_equal(result.entries, 0)
        require_equal(result.skipped, [{"name": item.name, "reason": "nicht vorhanden"}])
        report = restore.restore_set(result.path, str(root / "restored"))
        require(report.ok, report.findings)
        require_equal(report.checked, 0)
        require(not Path(item.expanded).exists(), "backup created runtime state")


def t_directory_manifest_cannot_escape_or_redefine_old_file_only_sets():
    with _world() as root:
        item = _item()
        source = Path(item.expanded)
        source.mkdir(parents=True)
        (source / "empty").mkdir()
        (source / "payload.txt").write_text("temporary recovery content")
        result = _backup(root, (item,))
        manifest_path = Path(result.path) / engine.MANIFEST_NAME
        original = json.loads(manifest_path.read_text())
        # Existing format-v1 sets without directories retain their file contract.
        legacy = json.loads(json.dumps(original))
        del legacy["entries"][0]["directories"]
        manifest_path.write_text(json.dumps(legacy))
        require(restore.verify_set(result.path)["ok"])
        target = root / "legacy-restore"
        report = restore.restore_set(result.path, str(target))
        require(report.ok, report.findings)
        require_equal((target / item.dest / "payload.txt").read_text(),
                      "temporary recovery content")
        for i, unsafe in enumerate(("../../../escaped", str(root / "absolute-escape"))):
            bad = json.loads(json.dumps(original))
            bad["entries"][0]["directories"] = [unsafe]
            manifest_path.write_text(json.dumps(bad))
            require(not restore.verify_set(result.path)["ok"])
            require_raises(restore.RestoreRefused, restore.restore_set,
                           result.path, str(root / f"unsafe-restore-{i}"))
        require(not (root / "absolute-escape").exists())
        require(not (root / "escaped").exists())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
