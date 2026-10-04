"""Explicit owner route selection at an existing subscription pause.

No account reader, dispatch or fallback lives here. The existing run row holds
the current selection; the existing cost ledger decides whether another call
is safe. All admission reads and the resume write share one transaction.

N8/C4 adds a second, phase-bound table: the native task WORKER (Codex or
Claude Code). A worker switch is offered only at a proven non-start (the
orchestrator parks with `resume_allowed=True` solely when
`dispatch_started is False`); a started worker turn never reaches
`eligible()` with `resume_allowed`, and there is no automatic failover.
"""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Lock

PROFILES = {"codex": "researcher/hermes", "claude-code": "researcher/claude"}
#: The native task workers, one per provider. Same objective, same workspace,
#: same grant and cost frame; only the executing CLI differs.
WORKER_PROFILES = {"codex": "worker/codex", "claude-code": "worker/claude"}
SELECTION_KEYS = ("planner", "research", "worker")
REASONS = {"quota", "logged_out", "subscription_required", "auth_unknown",
           "auth_status_failed", "auth_required", "provider_unavailable",
           "native_model_unavailable"}

#: Criteria wording that needs a Core read path (portal catalogue, result
#: file descriptors). Without the MCP bridge the Claude worker has no Core
#: tools, so such an order is not offered to it. Deliberately over-inclusive:
#: an offer withheld is recoverable, an offer that cannot deliver is not.
CORE_TOOL_MARKERS = ("portal_list", "result_files_list", "portal", "gmail", "mail", "kalender",
                     "calendar", "termin")
#: Criteria wording that needs the Hermes browser; the Claude jail has no DNS.
BROWSER_MARKERS = ("browser",)


def selections(raw):
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) - set(SELECTION_KEYS):
        raise ValueError("invalid_provider_selection")
    for item in data.values():
        if (not isinstance(item, dict) or set(item) != {"provider", "boundary_ref"}
                or item["provider"] not in PROFILES
                or not re.fullmatch("[a-f0-9]{64}", item["boundary_ref"])):
            raise ValueError("invalid_provider_selection")
    return data


def selection_key(phase):
    return "worker" if phase == "worker" else "research" if phase == "specialist" else "planner"


def selected(run, phase):
    return selections(run.provider_selection).get(selection_key(phase), {}).get("provider", "")


def reference(run):
    return hashlib.sha256((run.run_id + "\0" + run.boundary).encode()).hexdigest()


def profile_table(profile):
    """Which selection table a parked specialist profile belongs to, or None."""
    if profile in WORKER_PROFILES.values():
        return WORKER_PROFILES
    if profile in PROFILES.values():
        return PROFILES
    return None


_WORKER_CANARY = None
_PROBE_PENDING = object()
# Two fixed slots, one shared worker: repeated status reads neither repeat a
# physical probe nor create more threads while its child is still running.
_PROBE_FUTURES = {"worker": None, "mcp": None}
_PROBE_LOCK = Lock()
_PROBE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="claude-canary")
_PROBE_WAIT_SECONDS = 125.0  # Child deadline is 120 s; leave room for cleanup.


def _measure(function, *, probe):
    """Poll a single physical probe without ever waiting on the Core loop.

    The probe owns its child's deadline and drives its own asyncio.run.
    On the event loop an unfinished Future is a temporary block; later reads
    consume its cached result. Off-loop callers may wait with a deadline.
    A wait timeout retains the same Future, so retries cannot pile up probes.
    """
    import asyncio
    with _PROBE_LOCK:
        future = _PROBE_FUTURES[probe]
        if future is None:
            future = _PROBE_POOL.submit(function)
            _PROBE_FUTURES[probe] = future
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        wait = _PROBE_WAIT_SECONDS
    else:
        wait = 0
    try:
        return future.result(timeout=wait)
    except FutureTimeout:
        if future.done():
            return future.result()  # Preserve a TimeoutError raised by the probe.
        return _PROBE_PENDING


def claude_worker_canary():
    """The M-C2 verdict (worker argv against a local listener), cached per Core process.

    Empty means green. Anything else blocks the offer; the V0.6 canary with
    its fixed argv is deliberately NOT this gate. The verdict comes from the
    worker module built by implementer A; until it exists or while it fails,
    the answer is a named block, never a silent pass.
    """
    global _WORKER_CANARY
    if _WORKER_CANARY is None:
        try:
            from solvio.specialists import claude_native_task as CNT
            verdict = _measure(CNT.worker_canary_verdict, probe="worker")
        except (ImportError, AttributeError):
            verdict = "worker_canary_unavailable"
        except Exception as exc:  # noqa: BLE001 - a crashed probe is a block, not a pass
            verdict = "worker_canary_failed:" + type(exc).__name__
        if verdict is _PROBE_PENDING:
            return "worker_canary_pending"
        _WORKER_CANARY = str(verdict or "")
    return _WORKER_CANARY


def claude_mcp_mode():
    """`bridge` only after M-C4 is green (measured by A's module); default toolless."""
    try:
        from solvio.specialists import claude_native_task as CNT
        mode = _measure(CNT.mcp_mode, probe="mcp")
    except (ImportError, AttributeError):
        return "none"
    except Exception:  # noqa: BLE001
        return "none"
    return mode if mode == "bridge" else "none"


def worker_configured(provider):
    """Local installation, isolation, broker, vault and pot-proof gates for a WORKER.

    Authentication and the zero-cost quote are rechecked at dispatch by the
    existing cost gate; this only decides whether the route is offered at all.
    """
    from solvio.specialists.launcher import resolve, LauncherError
    from solvio.agent_runtime import specialists as SP
    if provider not in WORKER_PROFILES:
        return False
    try:
        resolve("claude" if provider == "claude-code" else "codex")
    except LauncherError:
        return False
    if SP.blocked_reason(WORKER_PROFILES[provider]):
        return False
    if provider == "codex":
        return SP.native_research_configured()
    from solvio.agent_runtime import isolation
    from solvio.agent_runtime import native_costs as NC
    if not isolation.available():
        return False
    if not NC.claude_worker_preconditions()[0]:
        return False
    return claude_worker_canary() == ""


def configured(provider, phase, *, worker=False):
    """Installed local route only; authentication is rechecked at dispatch."""
    from solvio.specialists.launcher import resolve, LauncherError
    from solvio.agent_runtime import specialists as SP
    if worker:
        return phase == "specialist" and worker_configured(provider)
    if provider not in PROFILES:
        return False
    try:
        resolve("claude" if provider == "claude-code" else "codex")
    except LauncherError:
        return False
    if phase == "specialist":
        if SP.blocked_reason(PROFILES[provider]):
            return False
        if provider == "codex" and not SP.native_research_configured():
            return False
        # Claude's existing safe-mode route uses WebSearch/WebFetch only.
        # The optional Hermes browser belongs to Codex, not this route.
    return True


def _criteria_texts(ledger, run):
    from solvio.agent_runtime import requirements as RQ, task_revisions as TR
    view = TR.task_view(ledger, run.run_id)
    bound = RQ.load(view.requirements or "", objective=view.objective) if view else None
    if not bound:
        return []
    texts = []
    for kind in (RQ.ASK, RQ.ACTION, RQ.UNCLEAR):
        for entry in bound.get(kind, []) or []:
            texts.append(str(entry.get("text", "")))
    return texts


def _claude_worker_compatible(db, run, task, steps, *, ledger):
    """Claude is not offered when the bound order needs Core tools (without the
    MCP bridge), the Hermes browser, or image work."""
    # ADR-0040 (Review S1R2-5): the Claude worker may not call the mail/calendar
    # tools; an order that holds them is not offered to it.
    from solvio.agent_runtime.task_start_service import PRIVATE_DATA_CAPABILITIES
    granted = db.execute("SELECT capabilities FROM agent_task_grants WHERE task_id=? AND run_id=?",
                         (run.task_id, run.run_id)).fetchone()
    if granted is not None and any(item.get("name") in PRIVATE_DATA_CAPABILITIES
                                   for item in json.loads(granted["capabilities"])):
        return False
    texts = [text.lower() for text in _criteria_texts(ledger, run)]
    if claude_mcp_mode() != "bridge" and any(
            marker in text for text in texts for marker in CORE_TOOL_MARKERS):
        return False
    if any(marker in text for text in texts for marker in BROWSER_MARKERS):
        return False
    if any(s["specialist_profile"] == "image/codex" for s in steps):
        return False
    from solvio.agent_runtime import result_files as RF, image_inputs as I
    files, note = RF.describe_files(ledger, run.run_id)
    if note or any(item["mime_type"] in I.MIMES for item in files):
        return False
    return True


def _native_turns_settled(db, task_id):
    """The same admission the session layer applies before a new turn: no
    turn of the task that is not terminal with a finished/settled invocation,
    except a proven non-start whose reservation was released."""
    have = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('agent_native_sessions','agent_native_turns')").fetchall()}
    if have != {"agent_native_sessions", "agent_native_turns"}:
        return True  # No native turn exists in this ledger.
    blocked = db.execute(
        "SELECT 1 FROM agent_native_turns t JOIN agent_native_sessions s "
        "ON s.session_id=t.session_id LEFT JOIN agent_provider_invocations i "
        "ON i.reservation_id=t.reservation_id LEFT JOIN agent_cost_reservations c "
        "ON c.reservation_id=t.reservation_id WHERE s.task_id=? "
        "AND (t.state<>'terminal' OR i.state IS NULL OR c.state IS NULL OR NOT "
        "((i.state='finished' AND c.state='settled') OR "
        "(t.terminal_status='not_started' AND i.state='not_dispatched' AND c.state='released'))) LIMIT 1",
        (task_id,)).fetchone()
    return blocked is None


def eligible(db, run, provider, principal, *, ledger):
    """Return the saved boundary only when switching cannot replay unknown work."""
    from solvio.agent_runtime import store as S, checkpoint as CP
    if run is None or run.state != S.WAITING_USER or run.finished_at is not None:
        return None
    task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (run.task_id,)).fetchone()
    if task is None or task["created_principal"] != principal or task["state"] != S.TASK_ACTIVE:
        return None
    try:
        selections(run.provider_selection)
        boundary = json.loads(run.boundary)
        wait = boundary["provider_wait"]
        if (not isinstance(wait, dict) or wait.get("status") != "waiting"
                or wait.get("resume_allowed") is not True
                or wait.get("provider") not in PROFILES or provider not in PROFILES
                or wait["provider"] == provider or wait.get("reason") not in REASONS
                or wait.get("requested_billing_mode") != "subscription"
                or wait.get("phase") not in {"plan", "assessment", "specialist"}
                or wait.get("resume_state") not in {S.PLANNING, S.RUNNING, S.VERIFYING}
                or type(wait.get("dispatch_started")) is not bool
                or float(wait.get("since", 0)) <= 0):
            return None
        phase = wait["phase"]
        steps = db.execute("SELECT * FROM agent_steps WHERE run_id=?", (run.run_id,)).fetchall()
        step, table, worker = None, None, False
        if phase == "specialist":
            step = next((s for s in steps if s["step_id"] == boundary.get("schritt")), None)
            table = profile_table(step["specialist_profile"]) if step is not None else None
            worker = table is WORKER_PROFILES
        if not configured(provider, phase, worker=worker):
            return None
        if any(s["state"] in {"unknown", "running"} for s in steps):
            return None
        if any(s["outcome_reason"] == "research_replan_started" for s in steps):
            return None
        if phase == "specialist":
            # Only these existing runtimes are equivalent in authority: the two
            # read-only research routes, or the two native task workers on the
            # same workspace. File/image generation and coding are not offered.
            if (step is None or table is None or step["state"] != "waiting"
                    or step["outcome_reason"] != "provider_wait"
                    or step["specialist_profile"] != table[wait["provider"]]
                    or step["approval_id"] or step["execution_id"] or step["dispatch_binding_digest"]):
                return None
            checkpoint = CP.decode(run.plan_checkpoint)
            if (checkpoint is None or checkpoint.get("revision") != run.plan_revision
                    or step["attempt"] != int(run.plan_revision or 0) + 1
                    or not 1 <= step["seq"] <= len(checkpoint["schritte"])
                    or checkpoint.get("cursor") != step["seq"] - 1
                    or checkpoint["schritte"][step["seq"] - 1].get("profil") != table[wait["provider"]]):
                return None
            if worker:
                # A worker switch only follows a PROVEN non-start: nothing was
                # dispatched, no turn started. Anything else is the started-
                # turn boundary without a switch offer (E7).
                if wait["dispatch_started"] or wait.get("reason") != "quota":
                    return None
                if not _native_turns_settled(db, run.task_id):
                    return None
                if provider == "claude-code" and not _claude_worker_compatible(
                        db, run, task, steps, ledger=ledger):
                    return None
        # Claude's tool-free assessment has no local image input contract.
        if provider == "claude-code" and phase in {"plan", "assessment"}:
            if any(s["specialist_profile"] == "image/codex" for s in steps):
                return None
            from solvio.agent_runtime import result_files as RF, image_inputs as I
            files, note = RF.describe_files(ledger, run.run_id)
            if note or any(item["mime_type"] in I.MIMES for item in files):
                return None
        # No missing monetary store is interpreted as free execution.
        if not db.execute("SELECT 1 FROM agent_cost_policies WHERE subject_id=?", (run.task_id,)).fetchone():
            return None
        if db.execute("SELECT 1 FROM agent_provider_invocations WHERE task_id=? AND "
                "(state IN ('claimed','unknown') OR (state='finished' AND finished_at IS NULL))", (run.task_id,)).fetchone():
            return None
        if db.execute("SELECT 1 FROM agent_cost_reservations WHERE subject_id=? AND state IN ('reserved','unknown')", (run.task_id,)).fetchone():
            return None
        from solvio.agent_runtime.task_authority import TaskAuthority
        grant = db.execute("SELECT reference FROM agent_task_grants WHERE run_id=?", (run.run_id,)).fetchone()
        if grant is None or not TaskAuthority(ledger)._verify(db, grant["reference"], None, {}, None,
                task_id=run.task_id, run_id=run.run_id, task_only=True).allowed:
            return None
        if wait["dispatch_started"]:
            # Read-only quota may have been returned by a real, now terminal
            # native turn. Require that exact invocation, not a prior success.
            if wait.get("reason") != "quota":
                return None
            invocation = db.execute("SELECT i.*,r.state AS cost_state FROM agent_provider_invocations i "
                "JOIN agent_cost_reservations r USING(reservation_id) WHERE i.run_id=? AND i.phase=? "
                "ORDER BY i.claimed_at DESC LIMIT 1", (run.run_id, phase)).fetchone()
            if (invocation is None or invocation["provider"] != wait["provider"]
                    or invocation["state"] != "finished" or invocation["finished_at"] is None
                    or invocation["cost_state"] != "settled"
                    or invocation["invocation_id"] != wait.get("cost_invocation_id")):
                return None
        return boundary
    except (ValueError, KeyError, TypeError):
        return None


def _parked_worker(ledger, run):
    """True when the parked specialist step of this boundary is a task worker."""
    try:
        step_id = json.loads(run.boundary).get("schritt", "")
    except (ValueError, AttributeError):
        return False
    step = ledger.get_step(step_id) if step_id else None
    return step is not None and profile_table(step.specialist_profile) is WORKER_PROFILES


def offers(ledger, run):
    import sqlite3
    if run is None:
        return []
    task = ledger.get_task(run.task_id)
    if task is None:
        return []
    with ledger._open() as db:
        try:
            possible = [provider for provider in PROFILES if eligible(db, run, provider, task.created_principal, ledger=ledger)]
        except sqlite3.OperationalError:
            return []  # Legacy stores without the task/cost authority cannot switch.
    from solvio.config import load_settings
    settings = load_settings()
    browser = all(getattr(settings, "agent_runtime_hermes_browser_" + key, "")
                  for key in ("python", "bin", "chrome"))
    phase = json.loads(run.boundary).get("provider_wait", {}).get("phase")
    if phase == "specialist" and possible and _parked_worker(ledger, run):
        # The MCP verdict is a measured probe (M-C4, cached per process): ask
        # for it only when Claude is actually among the offers.
        bridge = "claude-code" in possible and claude_mcp_mode() == "bridge"
        return [{"provider": p, "label": "OpenAI / Codex" if p == "codex" else "Claude Code",
                 "werkzeuge": (["Lokale Datei- und Codearbeit im Auftragsordner"]
                               + (["Core-Lesewege"] if p == "codex" or bridge else [])
                               + (["Native Websuche und Webseiten lesen"] if p == "codex" else [])),
                 "hinweis": ("Diese Wahl gilt für die Bearbeitung des Auftrags. Neue native Sitzung "
                             "mit Übergabe des bisherigen Standes; Auftrag, Dateien, Ergebnisse und "
                             "Kostenrahmen bleiben erhalten. Abo-Kontingent; Anmeldung und Kosten "
                             "werden vor dem Aufruf erneut geprüft.")}
                for p in possible]
    return [{"provider": p, "label": "OpenAI / Codex" if p == "codex" else "Claude Code",
             "werkzeuge": (["Textplanung und Ergebnisprüfung"] if phase != "specialist" else
                            (["Websuche (WebSearch)", "Webseiten lesen (WebFetch)"]
                             if p == "claude-code" else ["Native Websuche und Webseiten lesen"])
                            + (["Öffentlicher Hermes-Browser ohne Anmeldung"]
                               if p == "codex" and browser else [])),
             "hinweis": (("Diese Wahl gilt für die Recherche. " if phase == "specialist" else
                          "Diese Wahl gilt für Planung und Ergebnisprüfung. ") +
                         "Bestehender gebuchter Plan; Anmeldung und Kosten werden vor dem Aufruf erneut geprüft. "
                         "Auftrag, Ergebnisse und Kostenrahmen bleiben erhalten.")}
            for p in possible]
