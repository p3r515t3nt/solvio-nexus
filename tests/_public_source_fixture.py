"""Published fixed source fixtures; no lookup in private Git history."""
import hashlib
from pathlib import Path
import sys
from types import ModuleType
from _guard import require_equal

ROOT = Path(__file__).resolve().parent / "fixtures"
EXPECTED = {'observer_legacy/browser_sessions.py': '80a87a91a75840080704af8ee70c8488e3454002f840aafea8d2bee5d1def8b6', 'observer_legacy/store.py': '8207f0e016e1ea20941a7ee6b48bf3b33d6e6bec082b0572eeb00dbd621d1846', 'p1b_legacy/store.py': '5c11d3609f3fd9b1de99edb770c84f1a0c07a4ecedea9ce32147e8804f7ee41d', 'rollback_aa6d2ae/cost_subjects.py': '7a5b716f021d6227161532a42bee6237e6fef542d0600cfb876f7f3dc7962b57', 'rollback_aa6d2ae/conversation_store.py': '57fc92f9c3654c9eb3f8fdf3b9805bd7261b0db982624b3aadfd07c6e62c4443'}


def source_bytes(relative):
    require_equal(relative in EXPECTED, True, "unknown published source fixture")
    path = ROOT / relative
    require_equal(path.is_symlink(), False, "fixture symlink")
    require_equal(path.resolve().is_relative_to(ROOT.resolve()), True, "fixture outside published root")
    data = path.read_bytes()
    require_equal(hashlib.sha256(data).hexdigest(), EXPECTED[relative],
                  "published source fixture changed: " + relative)
    return data


def load_module(relative, name, *, package="solvio.security.mobile_approval"):
    source = source_bytes(relative)
    module = ModuleType(name)
    module.__package__ = package
    module.__file__ = str(ROOT / relative)
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    module.REVIEW_SOURCE_SHA256 = hashlib.sha256(source).hexdigest()
    return module
