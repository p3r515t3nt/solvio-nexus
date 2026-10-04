"""Public source-content integrity plus the retained explicit reviewed amendments.

Original private freeze-tag provenance is outside the public distribution.
This check requires exact files and bytes from the published source snapshot;
it does not assert membership or ancestry in a private repository.
"""
from pathlib import Path
import hashlib
import json
from _guard import require_equal

AMENDMENT = {'src/solvio/security/mobile_approval/bridge.py': '66490a3a477f4ce9ce6ea1df9f35712c62f1e9eb', 'src/solvio/security/mobile_approval/control.py': '851aa900c1fe9982012c06bbe4d02080085f0d2e', 'src/solvio/security/mobile_approval/store.py': '081a0033f90bf44b4dfa7a41328e4d4153d5f900', 'src/solvio/security/mobile_approval/browser_sessions.py': '38bf73873c3a674ef2282b4d9627e1db2eaa054d', 'src/solvio/security/mobile_approval/observer_contract.py': '440937ad37c8765a2d6c2dd1b5fee3cdf4e7ba8a', 'src/solvio/security/mobile_approval/portal_preparation.py': '3f436d14fe110a6c9383752b44bc1468668aeeb5'}
SNAPSHOT_SHA256 = "6a9721a91f2e4040fe52f28ca263d81963b3bdcf2439fcc58ea4d0cbfaba9ba9"


def require_n2_security_tree(repo):
    root = Path(repo)
    path = root / "tests/fixtures/public_security_source_manifest.json"
    require_equal(path.is_symlink(), False, "security snapshot symlink")
    data = path.read_bytes()
    require_equal(hashlib.sha256(data).hexdigest(), SNAPSHOT_SHA256,
                  "public security snapshot manifest changed")
    snapshot = json.loads(data)
    require_equal(snapshot["schema"], 1, "security snapshot schema")
    expected = snapshot["files"]
    for name, digest in AMENDMENT.items():
        require_equal(expected.get(name), digest, "reviewed security amendment: " + name)
    actual = {}
    for item in (root / "src/solvio/security").rglob("*"):
        if "__pycache__" in item.parts or not item.is_file():
            continue
        relative = item.relative_to(root).as_posix()
        require_equal(item.is_symlink(), False, relative)
        content = item.read_bytes()
        actual[relative] = hashlib.sha1(
            b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    require_equal(actual, expected, "public security source differs from the fixed content snapshot")
