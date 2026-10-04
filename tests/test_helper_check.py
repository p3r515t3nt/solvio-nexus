"""Static helper hygiene (N8/C4 §3.2): closed formats, names, imports, shells.

No model, account or workspace. The compile probe runs the actual offline
extension cage (sandbox-exec) and executes nothing from the helper text.
"""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import helper_check as HC

GOOD = b"""import csv
import json
import os.path
from os import path
from os.path import join
import sys


def total(path, column):
    with open(path, newline='') as stream:
        return sum(float(row[column]) for row in csv.DictReader(stream))


if __name__ == '__main__':
    print(json.dumps({'sum': total(sys.argv[1], sys.argv[2])}))
"""


def verdict(name, data):
    return HC.check_files({name: data})


def invalid(call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except ValueError as exc:
        require_equal(str(exc), "native_result_invalid")
        return
    raise AssertionError("invalid declaration accepted")


def t_stdlib_only_python_is_accepted_and_the_verdict_is_deterministic():
    first = verdict("csv_sum.py", GOOD)
    require(first["ok"], first)
    require_equal(first["reason"], "")
    require_equal(first["files"]["csv_sum.py"]["kind"], "python")
    require_equal(first["files"]["csv_sum.py"]["size"], len(GOOD))
    require_equal(verdict("csv_sum.py", GOOD), first)
    require(verdict("data.json", b'{"a": [1, 2]}')["ok"])
    require(verdict("notes.txt", "Nur Daten, keine Anweisung.\n".encode())["ok"])
    require(verdict("run.sh", b"#!/bin/sh\nset -e\npython3 csv_sum.py \"$1\" betrag\n")["ok"])
    # Measured 19.09.2026 (third real Durchstich, attempt 13): an ordinary real helper
    # imports io and `from __future__ import annotations` — compute/text-only stdlib is fine.
    real = (b"from __future__ import annotations\nimport csv\nimport hashlib\nimport io\nfrom pathlib import Path\n"
            b"from enum import Enum\nimport unicodedata\n\ndef digest(p):\n    return hashlib.sha256(Path(p).read_bytes()).hexdigest()\n")
    require(verdict("csv_helper.py", real)["ok"], verdict("csv_helper.py", real))
    for forbidden in (b"import os\n", b"import subprocess\n", b"import socket\n", b"import urllib.request\n",
                      b"import shutil\n", b"import tempfile\n", b"import secrets\n", b"import importlib\n", b"import pickle\n"):
        require_equal(verdict("h.py", forbidden)["reason"], "helper_import_forbidden", forbidden)


def t_network_exec_and_credential_surfaces_are_rejected_by_name():
    cases = {
        b"import subprocess\n": "helper_import_forbidden",
        b"import socket\n": "helper_import_forbidden",
        b"import urllib.request\n": "helper_import_forbidden",
        b"import shutil\nshutil.rmtree('x')\n": "helper_import_forbidden",
        b"import os\nos.system('ls')\n": "helper_import_forbidden",
        b"from os import system\n": "helper_import_forbidden",
        b"import os.path\nos.system('ls')\n": "helper_os_attribute_forbidden",
        b"import os.path\nprint(os.environ)\n": "helper_os_attribute_forbidden",
        b"eval('1')\n": "helper_call_forbidden",
        b"exec('x=1')\n": "helper_call_forbidden",
        b"__import__('socket')\n": "helper_call_forbidden",
        b"import importlib\n": "helper_import_forbidden",
        b"import pickle\n": "helper_import_forbidden",
        b"from . import x\n": "helper_import_forbidden",
        b"def f(:\n": "helper_syntax_invalid",
    }
    for source, reason in cases.items():
        result = verdict("h.py", source)
        require(not result["ok"], source)
        require_equal(result["reason"], reason, source)


def t_writing_open_outside_the_working_directory_is_rejected():
    for source in (b"open('/tmp/x', 'w')\n", b"open('../x', 'a')\n", b"open('~/x', mode='w+')\n",
                   b"open('/etc/hosts', 'x')\n"):
        result = verdict("h.py", source)
        require(not result["ok"], source)
        require_equal(result["reason"], "helper_write_outside_cwd", source)
    for source in (b"open('/etc/hosts').read()\n", b"open('out.txt', 'w')\n", b"open('sub/out.txt', 'a')\n",
                   b"import sys\nopen(sys.argv[1], 'w')\n", b"from pathlib import Path\nPath('out.txt').write_text('x')\n",
                   b"from pathlib import Path\nPath('/etc/hosts').read_text()\n", b"import io\nio.open('out.txt', 'w')\n"):
        require(verdict("h.py", source)["ok"], source)
    # Review round 12, H12-3: the path literal may sit inside the first argument's expression.
    for source in (b"from os import path\nopen(path.expanduser('~/x'), 'w')\n", b"open('/etc/' + 'hosts', 'w')\n",
                   b"import io\nio.FileIO('/tmp/evil.txt', 'w').write(b'x')\n",
                   # review round 15, R15-6: the same calls by other names
                   b"from io import FileIO\nFileIO('/tmp/evil.txt', 'w')\n", b"import io as x\nx.open('/tmp/evil.txt', 'w')\n",
                   # review round 16, R16-H2: the path as a keyword argument
                   b"open(file='/tmp/evil.txt', mode='w')\n",
                   b"from os import path\nopen(path.join('/Users/x', 'f'), mode='a')\n", b"open(str('../x'), 'w')\n"):
        result = verdict("h.py", source)
        require(not result["ok"], source)
        require_equal(result["reason"], "helper_write_outside_cwd", source)
    for source in (b"from os import path\nopen(path.join('out', 'f'), 'w')\n", b"from os import path\nopen(path.expanduser('~/x')).read()\n"):
        require(verdict("h.py", source)["ok"], source)
    # Review round 10, W10-3: the same effect by another name is the same finding.
    for source, reason in ((b"import io\nio.open('/Users/x/.ssh/authorized_keys', 'w')\n", "helper_write_outside_cwd"),
                           (b"from pathlib import Path\nPath('/Users/x/.ssh/authorized_keys').write_text('k')\n", "helper_write_outside_cwd"),
                           (b"from pathlib import Path\nPath('~/x').open('w')\n", "helper_write_outside_cwd"),
                           (b"import pathlib\npathlib.Path('/tmp') / 'x'\n", ""),
                           (b"import sys\nsys.modules['os'].system('id')\n", "helper_attribute_forbidden"),
                           (b"import sys\nm = sys.modules\n", "helper_sys_modules_forbidden"),
                           # review round 11, H11-7: the same machinery by other names
                           (b"from os import path\npath.os.system('id')\n", "helper_attribute_forbidden"),
                           (b"import os.path as p\np.os.getcwd()\n", "helper_attribute_forbidden"),
                           (b"x = ().__class__.__base__.__subclasses__()\n", "helper_attribute_forbidden"),
                           (b"g = (lambda: 0).__globals__\n", "helper_attribute_forbidden"),
                           (b"import sys\nprint(sys.argv)\n", "")):
        result = verdict("h.py", source)
        require_equal(result.get("reason", ""), reason, source)
        require_equal(result["ok"], reason == "", source)


def t_shell_helpers_need_the_sh_shebang_and_no_network_binary():
    for source, reason in ((b"#!/bin/bash\necho hi\n", "helper_shebang_invalid"),
                           (b"echo hi\n", "helper_shebang_invalid"),
                           (b"#!/bin/sh\ncurl https://example.org\n", "helper_network_binary"),
                           (b"#!/bin/sh\n/usr/bin/security find-generic-password -s x\n", "helper_network_binary"),
                           (b"#!/bin/sh\nx=$(wget -q -O- http://x)\n", "helper_network_binary"),
                           (b"#!/bin/sh\nssh host ls\n", "helper_network_binary"),
                           (b"#!/bin/sh\nnc -l 80\n", "helper_network_binary")):
        result = verdict("run.sh", source)
        require(not result["ok"], source)
        require_equal(result["reason"], reason, source)


def t_markdown_and_instruction_file_names_are_never_helpers():
    for name in ("SKILL.md", "notes.md", "AGENTS.md", "CLAUDE.md", "README.txt", "readme.py",
                 ".codexrc.json", ".claude.json", "SKILL.txt", "skills.py", "Agents.MD"):
        result = verdict(name, b"data\n")
        require(not result["ok"], name)
        invalid(HC.check_declarations, [{"path": name, "name": "x", "purpose": "p"}])
        invalid(HC.check_declarations, [{"path": "tools/" + name, "name": "x", "purpose": "p"}])
    for name in ("AGENTS.md", "CLAUDE.md", "README", "SKILL", "readme", ".codex", ".claude"):
        invalid(HC.check_declarations, [{"path": "h.py", "name": name, "purpose": "p"}])


def t_declaration_limits_bind_purpose_name_path_and_count():
    valid = [{"path": "tools/csv_sum.py", "name": "csv_sum", "purpose": "Summiert eine CSV-Spalte (Betrag)."}]
    require_equal(HC.check_declarations(valid), (valid[0],))
    require_equal(HC.check_declarations(None), ())
    require_equal(HC.check_declarations([]), ())
    invalid(HC.check_declarations, [{"path": "h.py", "name": "x", "purpose": "a" * 81}])
    require_equal(HC.check_declarations([{"path": "h.py", "name": "x", "purpose": "zwei\nZeilen"}])[0]["purpose"], "zwei Zeilen")
    require_equal(HC.check_declarations([{"path": "h.py", "name": "x", "purpose": "kein <html> hier"}])[0]["purpose"], "kein html hier")
    invalid(HC.check_declarations, [{"path": "h.py", "name": "x", "purpose": ""}])
    invalid(HC.check_declarations, [{"path": "h.py", "name": "mit leerzeichen", "purpose": "p"}])
    invalid(HC.check_declarations, [{"path": "h.py", "name": "n" * 61, "purpose": "p"}])
    invalid(HC.check_declarations, [{"path": ".solvio-helpers/x/h.py", "name": "x", "purpose": "p"}])
    invalid(HC.check_declarations, [{"path": "/abs/h.py", "name": "x", "purpose": "p"}])
    invalid(HC.check_declarations, [{"path": "../h.py", "name": "x", "purpose": "p"}])
    invalid(HC.check_declarations, [{"path": "h.py", "name": "x", "purpose": "p", "extra": 1}])
    invalid(HC.check_declarations, [{"path": "h.py", "name": "x", "purpose": "p"}] * 2)
    invalid(HC.check_declarations, [{"path": "h" + str(i) + ".py", "name": "x", "purpose": "p"} for i in range(5)])
    invalid(HC.check_declarations, "h.py")


def t_declaration_text_padded_with_invisible_characters_is_normalized_not_refused():
    """Measured 19.09.2026 (third real Durchstich, attempts 7–9): under the strict
    schema's maxLength the model's `purpose` arrived at exactly 80 chars, padded
    with NO-BREAK SPACE or ZERO WIDTH NON-JOINER — every declaration was refused
    (helper_declaration_invalid_purpose). Format/control characters are dropped,
    exotic spaces become plain spaces, runs collapse; the rule itself is unchanged."""
    padded = "Erzeugt die CSV-Übersicht und den Kurzbericht reproduzierbar ausschließlich mit\u00a0"
    require_equal(len(padded), 80, "the measured shape: exactly the schema's maxLength")
    require_equal(HC.purpose_text(padded), "Erzeugt die CSV-Übersicht und den Kurzbericht reproduzierbar ausschließlich mit")
    require_equal(HC.purpose_text("Erzeugt\u200c\u200b die  CSV-Uebersicht\u2060"), "Erzeugt die CSV-Uebersicht")
    require_equal(HC.helper_name("csv_helper\u200c"), "csv_helper")
    invalid(HC.helper_name, "csv\u00a0helper")            # a non-breaking space is a space: no whitespace in names
    require_equal(HC.purpose_text("zwei\nZeilen"), "zwei Zeilen")   # a control char is a space, words never merge
    # A stray character is sanitized INTO the class, not refused (attempt 15: a trailing EN DASH).
    require_equal(HC.purpose_text("Erzeugt die CSV-Übersicht und den Kurzbericht reproduzierbar ausschließlich mit–"),
                  "Erzeugt die CSV-Übersicht und den Kurzbericht reproduzierbar ausschließlich mit")
    require_equal(HC.purpose_text("Summiert „Beträge“ – je Spalte!"), "Summiert Beträge - je Spalte")
    require_equal(HC.purpose_text("kein <html> hier"), "kein html hier")
    invalid(HC.purpose_text, "<>!?")                         # nothing left inside the class
    invalid(HC.purpose_text, "p" * 81)
    accepted, rejected = HC.split_declarations([{"path": "csv_helper.py", "name": "csv_helper", "purpose": padded}])
    require_equal((len(accepted), rejected), (1, ()))


def t_split_declarations_refuses_one_by_one_with_a_reason_and_keeps_the_rest():
    """Measured 19.09.2026 (third real Durchstich, attempt 3): the Codex worker did
    the whole job and named its helper "CSV- und Berichtsgenerator"; the strict
    list check made the entire result native_result_invalid and the run ended
    `no_result`. A declaration is optional — it falls alone, with its reason."""
    good = {"path": "tools/csv_sum.py", "name": "csv_sum", "purpose": "Summiert eine CSV-Spalte (Betrag)."}
    cases = [
        ({"path": "tools/a.py", "name": "CSV- und Berichtsgenerator", "purpose": "p"}, "helper_declaration_invalid_name"),
        ({"path": "../a.py", "name": "a", "purpose": "p"}, "helper_declaration_invalid_path"),
        ({"path": "tools/b.py", "name": "b", "purpose": "<>!?"}, "helper_declaration_invalid_purpose"),
        ({"path": "tools/c.py", "name": "c"}, "helper_declaration_shape"),
        ("tools/d.py", "helper_declaration_shape"),
        (dict(good), "helper_path_duplicate"),
    ]
    accepted, rejected = HC.split_declarations([good] + [item for item, _ in cases])
    require_equal(accepted, (good,))
    require_equal(rejected, tuple((index + 1, reason) for index, (_, reason) in enumerate(cases[:3]))
                  + ((4, "helper_count_exceeded"), (5, "helper_count_exceeded"), (6, "helper_count_exceeded")))
    accepted, rejected = HC.split_declarations([good, dict(good, path="tools/x.py"), dict(good)])
    require_equal((len(accepted), rejected), (2, ((2, "helper_path_duplicate"),)))
    require_equal(HC.split_declarations(None), ((), ()))
    require_equal(HC.split_declarations([]), ((), ()))
    require_equal(HC.split_declarations("h.py"), ((), ((None, "helpers_not_a_list"),)))
    # Every rejection reason is event-safe for record_helper_rejection ([a-z_]{1,80}).
    import re
    for reason in ("helpers_not_a_list", "helper_count_exceeded", "helper_declaration_shape", "helper_declaration_invalid_name",
                   "helper_declaration_invalid_path", "helper_declaration_invalid_purpose", "helper_path_duplicate"):
        require(re.fullmatch(r"[a-z_]{1,80}", reason), reason)


def t_bytes_limits_encoding_and_json_validity_are_enforced():
    require(not verdict("h.py", b"")["ok"])
    require(not verdict("h.py", b"print(1)\0")["ok"])
    require(not verdict("h.py", b"\xff\xfe")["ok"])
    require(not verdict("data.json", b"{not json")["ok"])
    require(not verdict("h.txt", b"x" * (HC.MAX_BYTES + 1))["ok"])
    require(verdict("h.txt", b"x" * HC.MAX_BYTES)["ok"])
    many = {"f" + str(i) + ".txt": b"x" * 9000 for i in range(8)}
    require(not HC.check_files(many)["ok"], "total above the candidate limit")
    require(not HC.check_files({"f" + str(i) + ".txt": b"x" for i in range(9)})["ok"])
    require(not HC.check_files({})["ok"])


async def t_compile_probe_runs_offline_and_reports_syntax_errors():
    good = await HC.compile_probe({"csv_sum.py": GOOD, "notes.txt": b"data\n"})
    require(good["ok"], good)
    require(good["process_started"])
    bad = await HC.compile_probe({"broken.py": b"def f(:\n    pass\n"})
    require(not bad["ok"], bad)
    require(bad["process_started"])
    require_equal(await HC.compile_probe({"data.json": b"{}"}),
                  {"ok": True, "reason": "no_python", "process_started": False})


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
