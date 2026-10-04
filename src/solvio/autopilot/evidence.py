"""Evidence — gemessen, nie erzaehlt.

Harte Fakten kommen aus Werkzeugen: der Commit aus git, das Gate aus
`scripts/run_tests.py`, der Diff aus git. Ein Modell darf sie interpretieren;
erzeugen darf es sie nicht. Deshalb gibt es in diesem Modul keine einzige
Funktion, die Text von einem Builder entgegennimmt.

**Der Umgebungs-Fingerabdruck ist kein Zierat.** Beim Offsite-Release am
2026-09-01 fielen 14 Zusicherungen in einem frisch eingerichteten Worktree:
acht mit `ModuleNotFoundError: No module named 'yaml'`, sechs in den
Gedaechtnis-Suiten. Beides sah aus wie ein Codefehler und war keiner — die
Umgebung war ohne Extras eingerichtet. Ein Testbericht ohne Umgebungsangabe
ist deshalb kein Beweis ueber den Code, sondern ueber eine Umgebung, die
niemand notiert hat.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger
from solvio import git_binary as _GB

log = get_logger("autopilot")

#: Der Gate-Aufruf. Genau der, den auch ein Mensch tippt — eine zweite
#: Testwahrheit waere die erste, die niemand nachzieht.
GATE_SCRIPT = "scripts/run_tests.py"
GATE_TIMEOUT = 3600.0

#: Module, deren Anwesenheit die Umgebung praegt. Sie stehen hier NICHT, weil
#: der Autopilot sie braucht, sondern weil ihr Fehlen Testergebnisse
#: veraendert — gemessen, nicht vermutet.
PROBE_MODULES = ("yaml", "pytest", "sentence_transformers", "torch",
                 "structlog", "aiohttp", "pyrage")

#: Die Zeilen, an denen `run_tests.py` eine rote Suite markiert. Sie sind das
#: Einzige aus der Gate-Ausgabe, das in die Evidence gehoert: ohne sie steht im
#: Buch „48 rot" und nirgends, WO. Live gelernt in der A7-Abnahme — die
#: Nachfrage kostete einen zweiten zwoelfminuetigen Gate-Lauf.
_FAILING_SUITE = re.compile(r"^(\S+\.py)\s+.*<<< FAIL\s*$", re.M)

_SUMMARY = re.compile(
    r"EXPECTED=(\d+)\s+EXECUTED=(\d+)\s+PASSED=(\d+)\s+FAILED=(\d+)\s+SKIPPED=(\d+)")
_SUITES = re.compile(r"Suites:\s*(\d+)")
_DRIFT = re.compile(r"BaselineDrift=(\d+)")
_MISSING = re.compile(r"Missing=(\d+)")

MAX_OUTPUT = 200_000


class EvidenceError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# -- Umgebung -----------------------------------------------------------------
def environment_fingerprint(repo: str, *, python: str = "") -> str:
    """Was die Umgebung ausmacht — als ein Hash, aber aus benannten Teilen.

    Drei Quellen: die Sperrdatei der Abhaengigkeiten, die Python-Fassung und
    welche der praegenden Module tatsaechlich importierbar sind. Die dritte ist
    die wichtigste und die einzige, die man nicht aus Dateien raten kann.
    """
    teile: list[str] = []
    lock = os.path.join(repo, "uv.lock")
    if os.path.isfile(lock):
        with open(lock, "rb") as fh:
            teile.append("lock:" + hashlib.sha256(fh.read()).hexdigest()[:16])
    else:
        teile.append("lock:none")
    teile.append("py:" + (python or ".".join(map(str, sys.version_info[:3]))))
    vorhanden = [name for name in PROBE_MODULES
                 if importlib.util.find_spec(name) is not None]
    teile.append("mods:" + ",".join(sorted(vorhanden)))
    roh = "|".join(teile)
    return "env:" + hashlib.sha256(roh.encode()).hexdigest()[:20]


def environment_detail(repo: str) -> dict[str, Any]:
    """Dieselbe Auskunft in lesbar — fuer Berichte und fuer die Fehlersuche.

    Ein Fingerabdruck sagt „anders". Diese Auskunft sagt „was".
    """
    fehlend = [n for n in PROBE_MODULES if importlib.util.find_spec(n) is None]
    return {"fingerprint": environment_fingerprint(repo),
            "python": ".".join(map(str, sys.version_info[:3])),
            "module_vorhanden": [n for n in PROBE_MODULES if n not in fehlend],
            "module_fehlend": fehlend,
            "hinweis": ("Fehlende Module aendern Testergebnisse, ohne dass sich"
                        " Code aendert (DEBT-0164). Kanonisch ist"
                        " `uv sync --frozen --all-extras`."
                        if fehlend else "")}


# -- git ----------------------------------------------------------------------
def _git(repo: str, *args: str, check: bool = True,
         timeout: float = 120.0) -> subprocess.CompletedProcess:
    """git ohne Umgebungsgedaechtnis — dieselbe Haertung wie im Offsite-Beweis."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    proc = subprocess.run([_GB.resolve(), "-C", repo, *args], env=env, timeout=timeout,
                          capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise EvidenceError("git_failed",
                            f"{' '.join(args[:2])}: {proc.stderr.strip()[:160]}")
    return proc


def head_commit(repo: str) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def is_dirty(repo: str) -> bool:
    return bool(_git(repo, "status", "--porcelain").stdout.strip())


def changed_files(repo: str, base: str, head: str = "HEAD") -> list[str]:
    if not base:
        return []
    proc = _git(repo, "diff", "--name-only", f"{base}..{head}", check=False)
    return [z for z in proc.stdout.split("\n") if z.strip()]


def diffstat(repo: str, base: str, head: str = "HEAD") -> str:
    if not base:
        return ""
    proc = _git(repo, "diff", "--stat", f"{base}..{head}", check=False)
    return proc.stdout.strip()


def diff_text(repo: str, base: str, head: str = "HEAD", *,
              paths: list[str] | None = None, max_chars: int = 40_000) -> str:
    """Der Diff — gezielt und gedeckelt.

    Ein Review, dem man das ganze Repository vorlegt, liest das ganze
    Repository. Der Deckel ist deshalb Teil der Aussage, nicht eine
    Sparmassnahme; wo beschnitten wird, steht es im Text.
    """
    if not base:
        return ""
    args = ["diff", f"{base}..{head}"]
    if paths:
        args += ["--", *paths]
    proc = _git(repo, *args, check=False)
    text = proc.stdout
    if len(text) > max_chars:
        return (text[:max_chars]
                + f"\n\n[... {len(text) - max_chars} Zeichen ausgelassen —"
                  f" der Diff ist laenger als der Deckel ...]")
    return text


# -- Das Test-Gate ------------------------------------------------------------
@dataclass
class GateResult:
    """Was das Gate gemessen hat. `ok` ist die einzige Meinung darin — und sie
    folgt aus Exit-Code UND Zahlen, nicht aus einer davon."""

    ok: bool
    exit_code: int
    failing_suites: tuple[str, ...] = ()
    suites: int = 0
    expected: int = 0
    executed: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    baseline_drift: int = 0
    missing: int = 0
    parsed: bool = False
    reason: str = ""
    output: str = ""

    def summary(self) -> str:
        if not self.parsed:
            return f"Gate ohne lesbare Zusammenfassung (exit {self.exit_code})"
        kern = (f"{self.passed}/{self.executed} bestanden, {self.failed} rot, "
                f"{self.skipped} uebersprungen, {self.suites} Suiten, "
                f"Drift {self.baseline_drift}")
        if self.failing_suites:
            kern += " — rot: " + ", ".join(self.failing_suites[:6])
            if len(self.failing_suites) > 6:
                kern += f" (+{len(self.failing_suites) - 6})"
        return kern

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "exit_code": self.exit_code,
                "failing_suites": list(self.failing_suites),
                "suites": self.suites, "expected": self.expected,
                "executed": self.executed, "passed": self.passed,
                "failed": self.failed, "skipped": self.skipped,
                "baseline_drift": self.baseline_drift, "missing": self.missing,
                "parsed": self.parsed, "reason": self.reason}


def parse_gate(text: str, exit_code: int) -> GateResult:
    """Aus der Gate-Ausgabe ein Urteil — fail-closed.

    Eine unlesbare Ausgabe ist NICHT gruen. Das ist die `_do_verify`-Lehre der
    Agent Runtime in anderer Gestalt: was nicht nachweislich in Ordnung ist,
    laesst nichts gelingen. Ein Gate, dessen Zusammenfassung fehlt, hat
    entweder nicht zu Ende gelaufen oder etwas anderes getan als erwartet.
    """
    zusammen = _SUMMARY.search(text or "")
    if zusammen is None:
        return GateResult(ok=False, exit_code=exit_code, parsed=False,
                          reason="unreadable_summary",
                          output=(text or "")[-MAX_OUTPUT:])
    erwartet, ausgefuehrt, bestanden, rot, uebersprungen = (
        int(g) for g in zusammen.groups())
    suiten = int(m.group(1)) if (m := _SUITES.search(text)) else 0
    drift = int(m.group(1)) if (m := _DRIFT.search(text)) else 0
    fehlend = int(m.group(1)) if (m := _MISSING.search(text)) else 0

    gruende = []
    if exit_code != 0:
        gruende.append(f"exit={exit_code}")
    if rot:
        gruende.append(f"failed={rot}")
    if drift:
        gruende.append(f"baseline_drift={drift}")
    if fehlend:
        gruende.append(f"missing={fehlend}")
    if bestanden != ausgefuehrt:
        # Ausgefuehrt, aber nicht bestanden und nicht als rot gezaehlt: genau
        # der Zwischenfall, den ein Ledger sonst als Erfolg verbucht.
        gruende.append(f"passed!=executed ({bestanden}!={ausgefuehrt})")

    rote = tuple(m.group(1) for m in _FAILING_SUITE.finditer(text or ""))[:20]
    return GateResult(ok=not gruende, exit_code=exit_code,
                      failing_suites=rote, suites=suiten,
                      expected=erwartet, executed=ausgefuehrt,
                      passed=bestanden, failed=rot, skipped=uebersprungen,
                      baseline_drift=drift, missing=fehlend, parsed=True,
                      reason=", ".join(gruende),
                      output=(text or "")[-MAX_OUTPUT:])


def run_gate(repo: str, *, python: str = "", timeout: float = GATE_TIMEOUT) -> GateResult:
    """Das Gate laufen lassen. Im gegebenen Baum, mit dessen Interpreter."""
    skript = os.path.join(repo, GATE_SCRIPT)
    if not os.path.isfile(skript):
        raise EvidenceError("gate_missing", skript)
    interpreter = python or os.path.join(repo, ".venv", "bin", "python")
    if not os.path.isfile(interpreter):
        raise EvidenceError("interpreter_missing", interpreter)
    begonnen = time.time()
    try:
        proc = subprocess.run([interpreter, GATE_SCRIPT], cwd=repo,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return GateResult(ok=False, exit_code=-1, parsed=False,
                          reason=f"timeout_after_{int(timeout)}s")
    ergebnis = parse_gate(proc.stdout + "\n" + proc.stderr, proc.returncode)
    log.info("autopilot.gate_measured", ok=ergebnis.ok,
             passed=ergebnis.passed, failed=ergebnis.failed,
             seconds=round(time.time() - begonnen, 1))
    return ergebnis


# -- Ins Ledger ---------------------------------------------------------------
def record_gate(ledger, milestone_id: str, repo: str, *, commit: str = "",
                reuse: bool = True, python: str = "",
                now: float = 0.0) -> tuple[str, GateResult, bool]:
    """Gate messen (oder eine gueltige Messung wiederverwenden) und buchen.

    Rueckgabe: `(evidence_id, ergebnis, wiederverwendet)`. Wiederverwendung ist
    nur bei gleichem Commit UND gleicher Umgebung erlaubt und wird als eigenes
    Ereignis gebucht — sonst waere die Kostenrechnung geschoent.
    """
    stand = commit or head_commit(repo)
    fingerabdruck = environment_fingerprint(repo, python=python)

    if reuse:
        vorhanden = ledger.fresh_evidence(milestone_id, kind="test_report",
                                          commit=stand,
                                          env_fingerprint=fingerabdruck)
        if vorhanden is not None:
            ledger.note_evidence_reused(milestone_id, vorhanden.evidence_id,
                                        now=now)
            roh = json.loads(vorhanden.payload_json or "{}")
            ergebnis = GateResult(ok=vorhanden.ok, exit_code=roh.get("exit_code", 0),
                                  failing_suites=tuple(
                                      roh.get("failing_suites") or ()),
                                  **{k: roh.get(k, 0) for k in
                                     ("suites", "expected", "executed", "passed",
                                      "failed", "skipped", "baseline_drift",
                                      "missing")},
                                  parsed=bool(roh.get("parsed", True)),
                                  reason=roh.get("reason", ""))
            return vorhanden.evidence_id, ergebnis, True

    ergebnis = run_gate(repo, python=python)
    evidence_id = ledger.record_evidence(
        milestone_id, kind="test_report", commit=stand,
        env_fingerprint=fingerabdruck, ok=ergebnis.ok,
        summary=ergebnis.summary(), payload=ergebnis.as_dict(), now=now)
    return evidence_id, ergebnis, False


def record_commit(ledger, milestone_id: str, repo: str, *, base: str = "",
                  now: float = 0.0) -> str:
    """Den Stand des Arbeitsbereichs als Evidence buchen."""
    stand = head_commit(repo)
    dateien = changed_files(repo, base) if base else []
    return ledger.record_evidence(
        milestone_id, kind="git_commit", commit=stand,
        env_fingerprint="", ok=True,
        summary=f"{stand[:12]}, {len(dateien)} Datei(en) gegen {base[:12] or 'keine Basis'}",
        payload={"commit": stand, "base": base, "changed_files": dateien[:200],
                 "diffstat": diffstat(repo, base)[:4_000], "dirty": is_dirty(repo)},
        now=now)
