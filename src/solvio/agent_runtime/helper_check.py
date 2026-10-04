"""Static hygiene for reusable native helpers (N8/C4 §3.2); never a safety proof.

A helper is executable data whose text a later model reads. This module bounds
what may be declared (names, purpose, formats, sizes), rejects obvious network,
exec and credential surfaces by AST name (Python) or token (shell), and runs a
py_compile probe in the existing offline extension cage. It executes nothing
and grants nothing: the actual boundary stays the native sandbox plus the Core
re-check of every tool call under the NEW task's grant. Any deterministic change
here alters the helper environment fingerprint and retires published versions.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import unicodedata
import tempfile

from solvio.agent_runtime import result_files as RF

MAX_HELPERS = 4                 # declarations per native result
MAX_FILES = 8                   # files per candidate
MAX_BYTES = 64 * 1024           # per file and per candidate
MAX_NAME = 60
MAX_PURPOSE = 80
MAX_PATH = 240
MAX_PARTS = 8
HELPERS_DIR = ".solvio-helpers"
ALLOWED_SUFFIXES = {".py": "python", ".sh": "shell", ".json": "json", ".txt": "text"}
PURPOSE = re.compile(r"[A-Za-z0-9 äöüÄÖÜß.,;:()_/-]{1,80}")
FORBIDDEN_NAMES = frozenset({"agents.md", "claude.md"})
FORBIDDEN_PREFIXES = ("readme", ".codex", ".claude", "skill")
#: Compute- and text-only standard library modules. Measured 19.09.2026 (third
#: real Durchstich, attempt 13): the worker's ordinary helper imported `io` and
#: `from __future__ import annotations` and the Core refused it as
#: helper_import_forbidden — the list had been written from imagination, not
#: from real helpers. Still absent by design: os (except os.path), subprocess,
#: socket, urllib/http, shutil, tempfile, secrets, ctypes, importlib, pickle.
STDLIB_ALLOWED = frozenset({
    "csv", "json", "pathlib", "re", "math", "statistics", "datetime", "collections",
    "itertools", "functools", "argparse", "sys", "os.path", "decimal", "fractions",
    "textwrap", "string", "typing", "dataclasses",
    "__future__", "io", "hashlib", "enum", "operator", "heapq", "bisect", "copy",
    "difflib", "unicodedata", "calendar", "time", "base64", "html", "html.parser",
    "abc", "contextlib", "warnings", "pprint", "zoneinfo", "numbers", "array"})
FORBIDDEN_CALLS = frozenset({"eval", "exec", "__import__", "compile", "breakpoint"})
#: Attribute that reach the interpreter's machinery from allowed modules (name check).
_ESCAPE_ATTRS = frozenset({"os", "__builtins__", "__globals__", "__subclasses__", "__loader__", "__spec__",
                           "__import__", "system", "popen", "spawn", "execv", "execve", "fork"})
NETWORK_BINARIES = frozenset({"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp",
                              "sftp", "telnet", "security"})
SHEBANG = "#!/bin/sh"
_WRITE_MODES = set("wax+")
#: Schreibende Aufrufe auf einem Empfaenger, der einen Pfad traegt (`Path("/x").write_text`,
#: `io.open("/x", "w")`): ein Literal ausserhalb des Arbeitsordners im Empfaenger ist der
#: Befund (Review Runde 10, W10-3 — vorher fiel nur der blosse Name `open`).
_WRITE_ATTRS = frozenset({"write_text", "write_bytes", "touch", "mkdir", "unlink", "rename",
                          "replace", "rmdir", "symlink_to", "hardlink_to", "chmod"})
_SHELL_SPLIT = re.compile(r"[\s;|&<>()`$'\"=,]+")

# Runs IN THE CHILD of the offline extension cage: compile only, never exec.
_PROBE = """import os, sys
root = os.path.dirname(os.path.abspath(__file__))
for name in sorted(os.listdir(root)):
    if name.endswith('.py') and name != 'probe.py':
        with open(os.path.join(root, name), 'rb') as stream:
            compile(stream.read(), name, 'exec')
sys.stdout.write('ok\\n')
"""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _forbidden_name(name):
    lowered = name.casefold()
    return lowered in FORBIDDEN_NAMES or lowered.startswith(FORBIDDEN_PREFIXES)


def relative_path(value):
    """A declared workspace-relative helper path; never inside the seeded tree."""
    if type(value) is not str or not 1 <= len(value) <= MAX_PATH:
        raise ValueError("native_result_invalid")
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or "\\" in value or not path.parts
            or len(path.parts) > MAX_PARTS or path.parts[0] == HELPERS_DIR
            or any(part in {".", ".."} or RF.safe_name(part) != part for part in path.parts)
            or path.suffix.lower() not in ALLOWED_SUFFIXES or _forbidden_name(path.name)):
        raise ValueError("native_result_invalid")
    return value


def _plain(value):
    """Model text under a strict maxLength arrives padded to the limit with
    invisible characters (measured 19.09.2026, third real Durchstich: NO-BREAK
    SPACE, ZERO WIDTH NON-JOINER at exactly 80 chars). Format and control
    characters are dropped, other spaces become plain spaces, runs collapse."""
    if type(value) is not str:
        return value
    # Format characters (Cf: zero-width joiners/spaces, word joiner) vanish; every
    # Unicode space (Zs) and control character (Cc: newline, tab) is a plain
    # space, so words never merge; runs collapse.
    cleaned = "".join(" " if unicodedata.category(char) in ("Zs", "Cc") else char for char in unicodedata.normalize("NFC", value)
                      if unicodedata.category(char) != "Cf")
    return " ".join(part for part in cleaned.split(" ") if part)


def helper_name(value):
    value = _plain(value)
    if (type(value) is not str or not 1 <= len(value) <= MAX_NAME or RF.safe_name(value) != value
            or any(char.isspace() for char in value) or _forbidden_name(value)):
        raise ValueError("native_result_invalid")
    return value


_PURPOSE_DASHES = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2012": "-", "\u2212": "-", "\u2010": "-", "\u2011": "-"})


def purpose_text(value):
    """The declared purpose, sanitized INTO the closed class rather than refused
    for a stray character: dashes become `-`, every other character outside
    the class is dropped (measured 19.09.2026, attempts 7–15 of the third real
    Durchstich: the model fills the strict 80-char slot and the last character
    was NBSP, ZWNJ or an EN DASH — a refused declaration each time, no Core
    candidate, "nur Standardbibliothek" unjudgeable). The class itself — the
    influence-channel bound of C4 §3.2 — is unchanged; an empty rest refuses."""
    value = _plain(value)
    if type(value) is not str:
        raise ValueError("native_result_invalid")
    value = " ".join("".join(char for char in value.translate(_PURPOSE_DASHES) if PURPOSE.fullmatch(char)).split()).strip(" -")
    if not value or not PURPOSE.fullmatch(value) or len(value) > MAX_PURPOSE:
        raise ValueError("native_result_invalid")
    return value


def split_declarations(value):
    """The optional `helpers[]` result field, read one declaration at a time:
    (accepted, rejected) with rejected = ((index, reason), ...).

    A helper is manufacturing method, never a deliverable (§3.2): a declaration
    the Core cannot take — a name with spaces, a foreign path, a fifth entry —
    is refused WITH its reason and the result beside it stays intact. Measured
    on the third real Durchstich (19.09.2026 11:30): the worker had done the
    whole job and declared its helper as "CSV- und Berichtsgenerator"; the
    strict whole-list check turned that into native_result_invalid and the run
    ended `no_result` with every file lost."""
    if value is None:
        return (), ()
    if type(value) is not list:
        return (), ((None, "helpers_not_a_list"),)
    accepted, rejected, seen = [], [], set()
    for index, item in enumerate(value):
        if index >= MAX_HELPERS:
            rejected.append((index, "helper_count_exceeded"))
            continue
        if type(item) is not dict or set(item) != {"path", "name", "purpose"}:
            rejected.append((index, "helper_declaration_shape"))
            continue
        declared, reason = {}, ""
        for field, check in (("path", relative_path), ("name", helper_name), ("purpose", purpose_text)):
            try:
                declared[field] = check(item[field])
            except ValueError:
                reason = "helper_declaration_invalid_" + field
                break
        if reason:
            rejected.append((index, reason))
            continue
        if declared["path"] in seen:
            rejected.append((index, "helper_path_duplicate"))
            continue
        seen.add(declared["path"])
        accepted.append(declared)
    return tuple(accepted), tuple(rejected)


def check_declarations(value):
    """Strict reading of `helpers[]` (≤ 4 of {path, name, purpose}): any refused
    declaration fails the whole list — for callers that already hold accepted
    declarations (the Core's candidate readback), not for the worker's parse."""
    accepted, rejected = split_declarations(value)
    if rejected:
        raise ValueError("native_result_invalid")
    return accepted


def _decode(data):
    if type(data) is not bytes or not 1 <= len(data) <= MAX_BYTES or b"\0" in data:
        raise ValueError("helper_bytes_invalid")
    return data.decode("utf-8")


def _module_allowed(name):
    return name in STDLIB_ALLOWED or name.split(".")[0] in STDLIB_ALLOWED


def _literal_outside_cwd(node):
    if not isinstance(node, ast.Constant) or type(node.value) is not str:
        return False
    text = node.value
    return (os.path.isabs(text) or text.startswith("~")
            or ".." in PurePosixPath(text).parts or "\\" in text)


def _python(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return "helper_syntax_invalid"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_allowed(alias.name):
                    return "helper_import_forbidden"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or not module:
                return "helper_import_forbidden"
            if module == "os":
                if any(alias.name != "path" for alias in node.names):
                    return "helper_import_forbidden"
            elif not _module_allowed(module):
                return "helper_import_forbidden"
        elif isinstance(node, ast.Attribute):
            # `import os.path` binds `os`; only its `path` namespace is allowed.
            if isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr != "path":
                return "helper_os_attribute_forbidden"
            # `sys.modules["os"]` is the import machinery by another name; so are
            # `path.os` (os.path carries `os`), `__builtins__`, `__globals__`,
            # `__subclasses__` (review round 11, H11-7 — still a name check).
            if isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr in ("modules", "meta_path", "path_hooks"):
                return "helper_sys_modules_forbidden"
            if node.attr in _ESCAPE_ATTRS:
                return "helper_attribute_forbidden"
        elif isinstance(node, ast.Call):
            target = node.func
            # `open`/`FileIO` by any name and on any receiver (`from io import FileIO`,
            # `import io as x; x.open(...)`): the first argument is checked as a path
            # (review round 15, R15-6 — still a name check, never a proof).
            is_open = (isinstance(target, ast.Name) and target.id in ("open", "FileIO")) or (
                isinstance(target, ast.Attribute) and target.attr in ("open", "FileIO"))
            if isinstance(target, ast.Name) and target.id in FORBIDDEN_CALLS:
                return "helper_call_forbidden"
            if is_open:
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                writes = (isinstance(mode, ast.Constant) and type(mode.value) is str
                          and bool(_WRITE_MODES & set(mode.value)))
                # `open(os.path.expanduser('~/x'), 'w')`, `open('/etc/' + name, 'w')`: any
                # path literal outside the working directory in the first argument's
                # expression (review round 12, H12-3 — the receiver chain already walked).
                path_arg = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg == "file"), None)
                if writes and path_arg is not None and any(_literal_outside_cwd(inner) for inner in ast.walk(path_arg)):
                    return "helper_write_outside_cwd"
                # `Path("~/x").open("w")`: the path may sit in the receiver chain.
                if isinstance(target, ast.Attribute) and any(_literal_outside_cwd(inner) for inner in ast.walk(target.value)):
                    return "helper_write_outside_cwd"
            elif isinstance(target, ast.Attribute):
                if target.attr in FORBIDDEN_CALLS:
                    return "helper_call_forbidden"
                if target.attr in _WRITE_ATTRS:
                    # `Path("/etc/x").write_text(...)`: any path literal outside the
                    # working directory in the receiver chain.
                    if any(_literal_outside_cwd(inner) for inner in ast.walk(target.value)):
                        return "helper_write_outside_cwd"
    return ""


def _shell(text):
    first = text.split("\n", 1)[0]
    if first.rstrip("\r") != SHEBANG:
        return "helper_shebang_invalid"
    for token in _SHELL_SPLIT.split(text):
        if token and (token in NETWORK_BINARIES or token.rsplit("/", 1)[-1] in NETWORK_BINARIES):
            return "helper_network_binary"
    return ""


def _json(text):
    try:
        json.loads(text)
    except ValueError:
        return "helper_json_invalid"
    return ""


def check_files(files):
    """Deterministic static check; the same bytes always yield the same verdict."""
    result = {"ok": False, "reason": "", "files": {}}
    if (type(files) is not dict or not 1 <= len(files) <= MAX_FILES
            or sum(len(data) for data in files.values() if type(data) is bytes) > MAX_BYTES):
        result["reason"] = "helper_files_invalid"
        return result
    for name in sorted(files):
        data = files[name]
        try:
            relative_path(name)
            text = _decode(data)
        except ValueError as exc:
            result["reason"] = str(exc) if str(exc) != "native_result_invalid" else "helper_name_invalid"
            return result
        kind = ALLOWED_SUFFIXES[PurePosixPath(name).suffix.lower()]
        reason = {"python": _python, "shell": _shell, "json": _json, "text": lambda _: ""}[kind](text)
        result["files"][name] = {"kind": kind, "sha256": _sha(data), "size": len(data)}
        if reason:
            result["reason"] = reason
            return result
    result["ok"] = True
    return result


async def compile_probe(files):
    """py_compile in the offline extension cage; nothing from the helper runs."""
    from solvio.agent_runtime import extension_process as EP
    sources = [data for name, data in sorted(files.items()) if name.lower().endswith(".py")]
    if not sources:
        return {"ok": True, "reason": "no_python", "process_started": False}
    with tempfile.TemporaryDirectory(prefix="solvio-helper-probe-") as directory:
        root = Path(os.path.realpath(directory))
        probe = _PROBE.encode("utf-8")
        digests = {"probe.py": _sha(probe)}
        (root / "probe.py").write_bytes(probe)
        for index, data in enumerate(sources):
            name = "f" + str(index) + ".py"
            (root / name).write_bytes(data)
            digests[name] = _sha(data)
        invocation = EP.ExtensionInvocation(str(root), "probe.py", digests, timeout_s=EP.MAX_TIMEOUT,
                                            max_input_bytes=1, max_output_bytes=4096)
        outcome = await EP.run_extension(invocation, b"")
    ok = bool(outcome.ok and outcome.exit_code == 0 and outcome.stdout == b"ok\n")
    return {"ok": ok, "reason": "" if ok else (outcome.reason or "compile_failed"),
            "process_started": bool(outcome.process_started)}
