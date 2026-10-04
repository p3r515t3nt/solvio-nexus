"""Task-scoped research through Hermes' native Codex transport.

Only the transport is reused: there is no Hermes API service, credential pool,
auxiliary model or Broker fallback here. Core remains the authority and ledger.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from solvio.specialists import launcher as L, providers as P
from solvio.specialists.hermes_native_worker import (
    MAX_EVENTS, MAX_INPUT, MAX_TEXT, RESULT_SCHEMA, RUNTIME, VERSION,
    native_config_text, validate_home,
)
from solvio.specialists.hermes_research_worker import (
    MAX_RESEARCH_INPUT, PREREAD_SCHEMA, valid_preread_receipt,
)


@dataclass(frozen=True)
class NativeResearchConfig:
    hermes_python: str = ""
    hermes_source: str = ""
    codex_bin: str = ""
    codex_home: str = ""
    model: str = ""
    timeout_s: float = 480.0
    shutdown_grace_s: float = 3.0
    browser_python: str = ""
    browser_bin: str = ""
    browser_chrome: str = ""

    def validate(self) -> None:
        if not all((self.hermes_python, self.hermes_source, self.codex_bin,
                    self.codex_home, self.model)):
            raise ValueError("native_not_configured")
        for executable in (self.hermes_python, self.codex_bin):
            if not os.path.isabs(executable) or not os.path.isfile(executable) or not os.access(executable, os.X_OK):
                raise ValueError("provider_unavailable")
        source = Path(self.hermes_source)
        if (not source.is_absolute() or not source.is_dir()
                or not (source / "agent/transports/codex_app_server_session.py").is_file()):
            raise ValueError("provider_unavailable")
        if (not math.isfinite(self.timeout_s) or not 0.05 <= self.timeout_s <= 3600
                or not math.isfinite(self.shutdown_grace_s) or not 0 <= self.shutdown_grace_s <= 5):
            raise ValueError("native_timeout_invalid")
        validate_home(self.codex_home, self.model)
        if any((self.browser_python, self.browser_bin, self.browser_chrome)):
            from solvio.specialists.hermes_browser import validate_runtime
            validate_runtime(self.browser_python, self.browser_bin, self.browser_chrome, self.hermes_source)


def worker_invocation(config: NativeResearchConfig, workdir: str, *, task_id: str = "") -> L.Invocation:
    """All executable/auth/model choices are also bound by the N2 argv digest."""
    worker = Path(__file__).with_name("hermes_research_worker.py")
    browser_args = ()
    if config.browser_python:
        from solvio.specialists.hermes_browser import TASK
        if not TASK.fullmatch(task_id):
            raise ValueError("native_browser_binding_invalid")
        browser_args = ("--browser-python", config.browser_python, "--browser-bin", config.browser_bin,
                        "--browser-chrome", config.browser_chrome, "--task-id", task_id)
    return L.Invocation(config.hermes_python,
        ("-I", "-B", str(worker), "--hermes-source", config.hermes_source,
         "--codex-bin", config.codex_bin, "--codex-home", config.codex_home,
         "--model", config.model, "--workdir", workdir,
         "--timeout", str(config.timeout_s), *browser_args),
        timeout=config.timeout_s + config.shutdown_grace_s + 1,
        cwd=workdir, codex_home=config.codex_home,
        shutdown_grace=config.shutdown_grace_s, cleanup_group=True)


def continuation_invocation(config: NativeResearchConfig, workdir: str, *, task_id: str,
                            native_thread_id: str = "", previous_turn_id: str = "") -> L.Invocation:
    """Separate durable read-only transport; ordinary research stays ephemeral."""
    from solvio.specialists.hermes_native_worker import continuation_ids
    from solvio.specialists.hermes_browser import TASK
    if not TASK.fullmatch(task_id):
        raise ValueError("native_session_binding_invalid")
    continuation_ids("durable", native_thread_id or "-", previous_turn_id or "-")
    base = worker_invocation(config, workdir, task_id=task_id)
    # Continuations retain the original entry bytes and task-policy digest.
    native_worker = Path(base.argv[2]).with_name("hermes_native_worker.py")
    base = replace(base, argv=(*base.argv[:2], str(native_worker), *base.argv[3:]))
    task_args = () if config.browser_python else ("--task-id", task_id)
    return replace(base, argv=base.argv + task_args + ("--session-mode", "durable",
        "--resume-thread", native_thread_id or "-", "--previous-turn", previous_turn_id or "-"))


def continuation_policy(config: NativeResearchConfig) -> str:
    """Policy fingerprint for an explicit durable integration, independent of turns."""
    payload = {"protocol": "solvio-native-readonly-v1", "config": asdict(config),
        "worker_sha256": hashlib.sha256(Path(__file__).with_name(
            "hermes_native_worker.py").read_bytes()).hexdigest()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class NativeContinuation:
    sessions: object
    session_id: str

    def binding(self, request, config, *, policy_digest=""):
        from solvio.agent_runtime import cost_dispatch as D
        scope = D.current_scope()
        session = self.sessions.session(self.session_id)
        from solvio.agent_runtime import specialists as SP
        from solvio.agent_runtime.native_sessions import PROVIDERS
        # The session's provider is the closed native worker set. A task
        # worker profile binds its own provider (worker/codex ↔ codex,
        # worker/claude ↔ claude-code); every other native continuation
        # (the Hermes research route on the Codex app server) stays Codex.
        expected_provider = (SP.profile(request.profile).provider
                             if request.profile in SP.WORKER_PROFILES.values() else "codex")
        if (type(scope) is not D.TaskCostScope or not session
                or self.sessions.ledger is not scope.ledger
                or session.provider not in PROVIDERS or session.provider != expected_provider
                or (session.task_id, session.profile) != (scope.task_id, request.profile)
                or scope.run_id != request.run_id
                or session.policy_digest != (policy_digest or continuation_policy(config))):
            raise ValueError("native_session_binding_invalid")
        previous = self.sessions.latest_turn(session.session_id)
        if D.recovery_pending() or (previous and previous.state != "terminal"):
            raise ValueError("cost_recovery_required")
        # Re-verify current authority and physical workspace under the stored policy.
        return self.sessions.bind(task_id=session.task_id, run_id=request.run_id,
            provider=session.provider, profile=session.profile,
            policy_digest=session.policy_digest, workspace=session.workspace)

    async def run(self, invocation, prompt, *, request, on_event=None, bridge=None):
        from solvio.agent_runtime import task_revisions as TR
        # No worker exists at this point. An admission refusal must release
        # this attempted claim rather than inventing a new uncertain effect.
        try:
            claim = self.sessions.active_claim(request.run_id)
            if not claim:
                raise ValueError("native_active_cost_claim_required")
            revision = TR.revision_for_run(self.sessions.ledger, request.run_id)["revision"]
            options = dict(zip(invocation.argv[3::2], invocation.argv[4::2]))
            expected_thread = options.get("--resume-thread", "-")
            expected_turn = options.get("--previous-turn", "-")
            turn, fresh = self.sessions.request_turn(session_id=self.session_id,
                run_id=request.run_id, revision=revision, invocation_id=claim["invocation_id"],
                expected_native_thread_id="" if expected_thread == "-" else expected_thread,
                expected_previous_turn_id="" if expected_turn == "-" else expected_turn)
            if not fresh:
                raise ValueError("native_turn_recovery_required")
        except ValueError:
            return L.Outcome(False, reason="cost_recovery_required", process_started=False,
                             exit_code=None)

        def receive(event):
            if event["event"] == "started":
                self.sessions.bind_thread(turn.invocation_id, event["thread_id"])
                self.sessions.started(turn.invocation_id, native_thread_id=event["thread_id"],
                                      native_turn_id=event["turn_id"])
                if bridge is not None:
                    bridge.started()
            if on_event is not None:
                on_event(event)
        try:
            async with (bridge.open() if bridge is not None else nullcontext()):
                outcome = await _run_worker(invocation, prompt, on_event=receive)
            body, _ = _decode(outcome.text, L.redact)
            if outcome.exit_code is not None and not outcome.truncated:
                if body.get("execution_status") == "not_started":
                    self.sessions.not_started(turn.invocation_id)
                elif body.get("execution_status") == "terminal":
                    self.sessions.terminal(turn.invocation_id, native_thread_id=body.get("thread_id"),
                        native_turn_id=body.get("turn_id"), status=body.get("terminal"))
            return outcome
        finally:
            # A valid terminal/no-start receipt above is immutable; every other
            # exit retains uncertainty, including cancelled callbacks and crashes.
            self.sessions.unknown(turn.invocation_id)


class NativeDecoder:
    """The same strict protocol parser serves live observations and final proof."""
    def __init__(self, redactor, on_event=None):
        self.redactor = redactor
        self.on_event = on_event
        self.events = []
        self.result = None
        self.binding = None

    def feed(self, line):
        data = json.loads(line)
        if not isinstance(data, dict) or data.get("schema") != VERSION or self.result is not None:
            raise ValueError("native_protocol_error")
        if data.get("type") == "event":
            allowed = {"schema", "type", "event", "runtime", "model", "thread_id",
                       "turn_id", "status", "item_id", "query", "seq"}
            if (len(self.events) >= MAX_EVENTS or len(line.encode('utf-8')) > 2400
                    or set(data) - allowed or data.get("event") not in {"started", "web_search", "browser_read"}
                    or data.get("event") == "browser_read" and "query" in data
                    or type(data.get('seq')) is not int or data['seq'] != len(self.events) + 1):
                raise ValueError("native_protocol_error")
            for key in ('thread_id', 'turn_id'):
                value = data.get(key)
                if (not isinstance(value, str) or not 0 < len(value) <= 128
                        or not value.isascii() or not all(c.isalnum() or c in '._:-' for c in value)):
                    raise ValueError("native_protocol_error")
            binding = (data['thread_id'], data['turn_id'])
            if data['event'] == 'started':
                if self.binding is not None or data.get('runtime') != RUNTIME or not isinstance(data.get('model'), str):
                    raise ValueError("native_protocol_error")
                self.binding = binding
            elif (self.binding != binding or data.get('status') not in {'started', 'completed'}
                  or not isinstance(data.get('item_id'), str) or len(data['item_id']) > 120
                  or not isinstance(data.get('query', ''), str) or len(data.get('query', '')) > 350):
                raise ValueError("native_protocol_error")
            event = {k: self.redactor(v) if isinstance(v, str) else v for k, v in data.items()}
            self.events.append(event)
            if self.on_event is not None:
                self.on_event(event)
        elif data.get("type") == "result":
            if self.binding is not None and self.binding != (data.get('thread_id'), data.get('turn_id')):
                raise ValueError("native_protocol_error")
            self.result = data
        else:
            raise ValueError("native_protocol_error")


def _decode(text: str, redactor) -> tuple[dict, list[dict]]:
    decoder = NativeDecoder(redactor)
    for line in text.splitlines():
        decoder.feed(line)
    if decoder.result is None:
        raise ValueError("native_protocol_error")
    return decoder.result, decoder.events


def _structured_text(text: str) -> dict:
    """Accept complete checkpoint-sized native fields, never clipped fields."""
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError("native_result_invalid")
    data = json.loads(text)
    properties = RESULT_SCHEMA["properties"]
    if not isinstance(data, dict) or set(data) != set(properties):
        raise ValueError("native_result_invalid")
    for key, schema in properties.items():
        value = data[key]
        if schema["type"] == "array":
            valid = (isinstance(value, list) and len(value) <= schema["maxItems"]
                     and all(isinstance(v, str) and len(v) <= schema["items"]["maxLength"]
                             for v in value))
        else:
            valid = (isinstance(value, str)
                     and ("maxLength" not in schema or len(value) <= schema["maxLength"])
                     and ("enum" not in schema or value in schema["enum"]))
        if not valid:
            raise ValueError("native_result_invalid")
    if not (any(v.strip() for v in data["findings"]) or data["recommended_path"].strip()):
        raise ValueError("native_result_invalid")
    return data


def _browser_observation(body: dict, events: list[dict]) -> str:
    """Negative/uncertain provenance from the checked current-turn stream.

    The successful fixed worker checks its configured MCP catalogue before
    starting the turn. It does not retain that catalogue or tool success/error
    receipts. Completed progress therefore proves only a paired read attempt.
    """
    from solvio.specialists.result import NATIVE_BROWSER_OBSERVATION_PREFIX
    from solvio.specialists.hermes_native_worker import _source_url
    binding = (body["thread_id"], body["turn_id"])
    complete = (bool(events) and len(events) < MAX_EVENTS
        and events[0].get("event") == "started"
        and events[0].get("model") == body["model"]
        and all((event.get("thread_id"), event.get("turn_id")) == binding for event in events))
    reads = {}
    for event in events:
        if event.get("event") != "browser_read":
            continue
        item = event.get("item_id")
        status = event.get("status")
        if not item or status == "started" and item in reads:
            complete = False
        elif status == "started":
            reads[item] = "started"
        elif status == "completed" and reads.get(item) == "started":
            reads[item] = "completed"
        else:
            complete = False
    if any(status != "completed" for status in reads.values()):
        complete = False
    # A source observed by the browser without a corresponding read item is
    # inconsistent/missing progress, never evidence that there were no reads.
    if not reads and any(_source_url(value) for value in (body.get("browser_sources") or [])[:12]):
        complete = False
    common = "Browser im nativen Lauf angeboten; Katalog nicht separat gespeichert. "
    if complete:
        observation = (f"Beobachtet: {len(reads)} begonnene Leseversuche, "
            f"{sum(status == 'completed' for status in reads.values())} beendet mit passender Item-Kennung. ")
    else:
        observation = ("Anzahl begonnener/beendeter Leseversuche unbekannt: "
            "Beobachtungen unvollständig oder nicht eindeutig zugeordnet. ")
    # This is a separate pre-turn, model-free SDK receipt. It does not add
    # native read items or turn completion proof to the checked event stream.
    preread = body.get("browser_preread")
    if preread is not None:
        observation += (f"Vorablesung: {preread['started_calls']}/{preread['completed_calls']} "
            f"Aufrufe begonnen/beendet, {preread['complete_snapshots']} volle Snapshots, "
            f"{preread['hash_verified_snapshots']} Hashs geprüft, {preread['redacted_snapshots']} "
            f"redigiert, {preread['forwarded_bytes']} Textbytes; {preread['tool_errors']} Fehler, "
            f"{preread['material_limits']} Grenzen; {preread['status']}, Cleanup {preread['cleanup']}. ")
    return (NATIVE_BROWSER_OBSERVATION_PREFIX + common + observation
        + "Dieser Ablaufbeleg bestätigt weder eine Zugriffssperre noch einen erfolgreichen Zugriff "
        "oder eine bestätigte Preis-/Inhaltsprüfung.")


async def run_research(request, *, config: NativeResearchConfig | None = None, on_event=None,
                       continuation: NativeContinuation | None = None):
    from solvio.agent_runtime import specialists as SP, cost_dispatch as D
    from solvio.specialists.result import SpecialistResult

    spec = SP.profile(request.profile)
    config = config or NativeResearchConfig()

    def failure(reason, *, outcome=None):
        metadata = _metadata(outcome) if outcome is not None else {
            "provider": "codex", "billing_mode": "unknown", "dispatch_started": False}
        return SP.SpecialistRun(result=SpecialistResult(role=spec.role, provider="codex",
            question=request.objective, ok=False, reason=reason,
            model=config.model, quota_status="exhausted" if reason == "quota" else ""),
            quota=reason == "quota", runtime=RUNTIME, **metadata)

    try:
        config.validate()
    except (OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "native_config_required"
        return failure(reason)
    if D.current_scope() is None:
        # This new path has no legacy unscoped mode. Auth alone is no quote.
        return failure("cost_unbounded")
    task_id = ""
    if config.browser_python or continuation is not None:
        scope = D.current_scope()
        if not isinstance(scope, D.TaskCostScope) or request.run_id != scope.run_id:
            return failure("native_browser_binding_invalid")
        task_id = scope.task_id
    prompt = SP.build_prompt(spec, request)
    if config.browser_python and continuation is None and not request.short_public_answer:
        prompt = ("Für direkte öffentliche Quell- oder Ergebnis-URLs nutze den angebotenen "
            "MCP-Leseweg solvio-browser/browser_navigate und browser_snapshot mit full=true "
            "sowie nötigen Snapshot-Abschnitten. Native Websuchauszüge ersetzen diese Prüfung "
            "nicht und belegen keine Sperre des angebotenen Browser-Lesewegs.\n\n" + prompt)
    input_limit = MAX_RESEARCH_INPUT if continuation is None else MAX_INPUT
    if len(prompt.encode("utf-8")) > input_limit:
        return failure("native_input_too_large")
    preread_urls = ()
    if (config.browser_python and continuation is None and not request.short_public_answer
            and request.research_strategy == "direct_sources"):
        from solvio.specialists.hermes_native_worker import _source_url
        urls = request.direct_source_urls
        if (not isinstance(urls, tuple) or len(urls) > 3
                or any(not isinstance(url, str) or _source_url(url) != url for url in urls)
                or len(set(urls)) != len(urls)):
            return failure("native_browser_binding_invalid")
        preread_urls = urls
        if urls:
            # Only the current Core-bound sources of this existing phase enter
            # stdin. They cannot select executable, account, tools or argv.
            prompt = json.dumps({"schema": PREREAD_SCHEMA, "phase": "direct_sources",
                "task_id": task_id, "run_id": request.run_id, "prompt": prompt,
                "urls": urls}, ensure_ascii=False)
            if len(prompt.encode("utf-8")) > input_limit:
                return failure("native_input_too_large")

    # Ordinary research keeps its temporary workspace. A durable caller must
    # supply an authority-checked ledger binding, never a model-provided path.
    session = None
    if continuation is not None:
        try:
            session = continuation.binding(request, config)
        except ValueError as exc:
            return failure("cost_recovery_required" if str(exc) == "cost_recovery_required"
                           else "native_session_binding_invalid")
    workspace_context = (nullcontext(session.workspace) if session else
                         tempfile.TemporaryDirectory(prefix="solvio-native-research-"))
    with workspace_context as workdir:
        if session:
            previous = continuation.sessions.latest_native_turn(session.session_id)
            invocation = continuation_invocation(config, workdir, task_id=task_id,
                native_thread_id=session.native_thread_id,
                previous_turn_id=previous.native_turn_id if previous else "")
        else:
            invocation = (worker_invocation(config, workdir, task_id=task_id) if task_id
                          else worker_invocation(config, workdir))
        async def runner(invocation, prompt):
            if continuation is not None:
                return await continuation.run(invocation, prompt, request=request, on_event=on_event)
            return await _run_worker(invocation, prompt, on_event=on_event)
        outcome = await P.run_subscription("codex", invocation, prompt,
                                           codex_bin=config.codex_bin, runner=runner)
    if outcome.cost_status == "unknown":
        return failure("cost_recovery_required", outcome=outcome)
    if not outcome.ok or outcome.truncated:
        return failure(outcome.reason or "native_output_truncated", outcome=outcome)
    try:
        body, events = _decode(outcome.text, SP.redact_specialist_output)
        receipt = body.get("browser_preread")
        if preread_urls:
            if not valid_preread_receipt(receipt, task_id=task_id,
                    run_id=request.run_id, source_count=len(preread_urls)):
                raise ValueError("native_protocol_error")
            if body.get("status") == "completed" and (
                    receipt["cleanup"] != "closed" or receipt["status"] in {"invalid", "cancelled"}):
                raise ValueError("native_protocol_error")
        elif receipt is not None:
            raise ValueError("native_protocol_error")
        if body.get("status") != "completed":
            reason = body.get("reason")
            # Closed worker vocabulary. Never convert successful answer words
            # such as "quota" into a provider error.
            reasons = {"quota", "logged_out", "subscription_required", "provider_failed",
                "execution_budget_exhausted", "context_limit", "cancelled",
                "native_terminal_missing", "native_result_invalid", "native_protocol_error",
                "native_tool_not_allowed", "native_policy_mismatch", "invalid_terminal",
                "native_quota_unknown", "native_model_unavailable", "native_skills_unverified",
                "native_config_unverified", "native_config_mismatch", "native_config_required",
                "native_home_required", "native_home_not_private", "native_workspace_invalid",
                "native_browser_cleanup_failed", "native_browser_unavailable",
                "native_browser_binding_invalid", "native_browser_version_mismatch",
                "native_browser_sandbox_unavailable", "native_session_binding_invalid",
                "native_previous_turn_unconfirmed", "native_credit_binding_invalid",
                "native_input_too_large", "native_timeout_invalid"}
            return failure(reason if reason in reasons else "native_protocol_error", outcome=outcome)
        if (body.get("terminal") != "completed" or body.get("model") != config.model
                or body.get("runtime") != RUNTIME or not body.get("thread_id") or not body.get("turn_id")
                or any(not isinstance(body.get(k), str) or len(body[k]) > 160 for k in ("thread_id", "turn_id"))):
            raise ValueError("native_protocol_error")
        text = SP.redact_specialist_output(body.get("text", ""))
        data = _structured_text(text)
    except (ValueError, TypeError, KeyError):
        return failure("native_result_invalid", outcome=outcome)
    # The tolerant legacy parser clips list entries at 600 characters. Native
    # output has already met a strict schema: preserve every accepted field,
    # including a late qualification or negation, for the completion assessor.
    result = SpecialistResult(role=spec.role, provider="codex", question=request.objective,
        ok=True, model=config.model, elapsed=outcome.elapsed, raw_excerpt=text[:2000], **data)
    # Native observations are distinguishable from the model's own citations.
    from solvio.specialists.hermes_native_worker import _source_url
    # Each transport has its own twelve-source bound. A full web-search list
    # must not discard the browser's independently observed public page URLs.
    observed = [("Native Websuche: ", v) for v in (body.get("sources") or [])[:12]]
    observed += [("Nativer Browser: ", v) for v in (body.get("browser_sources") or [])[:12]]
    for prefix, source in observed:
        url = _source_url(source)
        if url:
            evidence = prefix + SP.redact_specialist_output(url)
            if evidence not in result.evidence:
                result.evidence.append(evidence)
    if config.browser_python and continuation is None:
        observation = _browser_observation(body, events)
        if len(result.uncertainties) < 12:
            result.uncertainties.append(observation)
        else:
            # Keep every model qualification and both twelve-source blocks.
            # CP reserves exactly this bounded final Core observation as 37th.
            result.evidence.append(observation)
    usage = body.get("usage") or {}
    metadata = _metadata(outcome)
    metadata["usage_reported"] = isinstance(usage, dict) and any(
        type(v) is int and v > 0 for v in usage.values())
    return SP.SpecialistRun(result=result, executable=invocation.executable,
        runtime=RUNTIME, native_thread_id=body["thread_id"], native_turn_id=body["turn_id"],
        **metadata)


async def _run_worker(invocation, prompt, *, on_event=None):
    """Worker exit is not proof that the remote native turn has ended.

    Classify before cost_dispatch settles its durable claim. Missing protocol
    evidence retains the reservation and blocks a second provider invocation.
    """
    decoder = NativeDecoder(L.redact, on_event)
    outcome = await L.run(invocation, prompt, on_stdout_line=decoder.feed)
    if outcome.process_started is False or outcome.exit_code is None:
        return outcome
    try:
        body, _ = _decode(outcome.text, L.redact)
        known = (outcome.ok and not outcome.truncated
                 and body.get("execution_status") in {"not_started", "terminal"})
    except (ValueError, TypeError):
        known = False
    if not known:
        return replace(outcome, ok=False, exit_code=None, reason="native_terminal_missing")
    return outcome


def _metadata(outcome) -> dict:
    return {name: getattr(outcome, name) for name in (
        "provider", "billing_mode", "auth", "dispatch_started", "cost_status",
        "cost_invocation_id", "cost_reservation_id", "usage_reported")}


__all__ = ["NativeResearchConfig", "run_research", "native_config_text", "worker_invocation",
           "continuation_invocation", "continuation_policy", "NativeContinuation"]
