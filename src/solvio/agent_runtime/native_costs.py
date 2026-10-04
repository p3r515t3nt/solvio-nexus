"""Plan-only pricing at the existing physical invocation cost gate.

REUSE: native account readers, existing invocation factories, CostQuote and the
task/interaction ledger. This adapter owns no account, budget or task store.

These are bounded observations of provider-controlled accounts, not a promise
that a human cannot later change billing settings. Every physical invocation
gets a new native read and a final context/age check. Claude must explicitly
report disabled usage credits; Codex must use the personal ChatGPT plan with
no available credits by default. Owner decision 2026-10-01 adds a separate
account-bound Face-ID grant for existing credits: its amount/EUR equivalent
remains unmeasured, never a zero quote. API routes, unknown fields and unlimited
credit configurations remain held. This adapter cannot buy or enable reloads;
provider billing settings remain outside its observation.

N8/C4 — the Claude task WORKER (`worker/claude`): its invocation is the
`sandbox-exec` form of `claude_native_task.claude_worker_invocation`. The
reader seam (`claude_usage._context`, Root-owned binary binding) stays
untouched; this adapter derives the READER invocation (the reviewed Claude
binary without the `sandbox-exec` prefix) and binds the dispatch form to the
factory. The zero quote `included_no_extra_charge` holds only when ALL of:
extra usage disabled on a pro|max plan, the vault credential is a
subscription OAuth token granting `nexus.claude_worker` to the Anthropic
broker executor, the broker is running with that credential present, and the
pot proof setting is `live-proof` or the digest of an evidence file bound to
this account and vault version. Anything missing → `CostQuote()` (held).
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

from solvio.agent_runtime import cost_dispatch as D, costs as C
from solvio.specialists import launcher as L, providers as P, subscription as U
from solvio.specialists import claude_usage as A, openai_usage as O
from solvio.logging_setup import get_logger

log = get_logger("native_costs")

#: The worker's vault requirements (describe() never opens a value).
WORKER_CAPABILITY = "nexus.claude_worker"
WORKER_SECRET_KIND = "oauth_refresh_token"
WORKER_EXECUTOR = "anthropic_broker"
LIVE_PROOF = "live-proof"
#: The evidence file of the ONE owner-released pot-proof turn (§5.3 step 1).
POT_PROOF_FILE = Path(__file__).resolve().parents[3] / "docs" / "plan" / "evidence" / "n8-c4-claude-pot-proof.json"
SANDBOX_PROFILE_NAME = "builder.sandbox.sb"


def _option(argv, flag, default=""):
    if flag not in argv:
        return default
    if argv.count(flag) != 1 or argv.index(flag) + 1 == len(argv):
        raise ValueError("invalid_invocation")
    return argv[argv.index(flag) + 1]


def _running_broker():
    from solvio.provider_broker import service
    return service.running()


def worker_form(invocation):
    """The `claude_worker_invocation` arguments reconstructed from a dispatch
    invocation, or None when this is not the sandbox-exec worker shape.

    `jail` comes from the profile path (`argv[1]`), `task_id` from the jail
    leaf, `session_id`/`resume`/`mcp_mode` from argv, `broker_port` from the
    RUNNING broker and `workdir` from `cwd`. Nothing here is trusted by
    itself: the caller compares the rebuilt factory result for equality.
    """
    from solvio.agent_runtime import isolation
    if type(invocation) is not L.Invocation or not invocation.prompt_via_stdin:
        return None
    argv = invocation.argv
    if (invocation.executable != isolation.SANDBOX_EXEC or len(argv) < 3 or argv[0] != "-f"
            or os.path.basename(argv[1]) != SANDBOX_PROFILE_NAME):
        return None
    jail = os.path.dirname(argv[1])
    task_id = os.path.basename(jail)
    if not jail or not task_id or ("--session-id" in argv) == ("--resume" in argv):
        return None
    resume = "--resume" in argv
    session_id = _option(argv, "--resume" if resume else "--session-id")
    mcp_mode = "bridge" if _option(argv, "--mcp-config") == os.path.join(jail, "mcp.json") else "none"
    broker = _running_broker()
    if broker is None:
        return None
    return dict(workdir=invocation.cwd, jail=jail, task_id=task_id, session_id=session_id,
                broker_port=int(broker.port), mcp_mode=mcp_mode, resume=resume)


def reader_invocation(invocation):
    """The reviewed Claude binary WITHOUT the sandbox-exec prefix — what the
    Root-owned reader binds; cwd, timeout and stdin prompt are unchanged."""
    argv = invocation.argv
    return replace(invocation, executable=argv[2], argv=tuple(argv[3:]))


def _known_worker_invocation(invocation):
    form = worker_form(invocation)
    if form is None:
        return False
    scope = D.current_scope()
    if (type(scope) is not D.TaskCostScope or scope.phase != "specialist"
            or form["task_id"] != scope.task_id):
        return False
    try:
        from solvio.specialists import claude_native_task as CNT
    except ImportError:
        return False
    return invocation == CNT.claude_worker_invocation(**form)


def _known_invocation(provider, invocation):
    """Compare actual dispatch with the same factories that construct it.

    A config flag, custom endpoint, inherited settings or another worker cannot
    borrow the cost proof for a different command. These are existing concrete
    product paths, not a second provider/capability registry.
    """
    if type(invocation) is not L.Invocation or not invocation.prompt_via_stdin:
        return False
    try:
        if provider == "claude-code" and worker_form(invocation) is not None:
            return _known_worker_invocation(invocation)
        common = dict(workdir=invocation.cwd,
                      model=_option(invocation.argv, "--model"),
                      timeout=invocation.timeout)
        if provider == "claude-code":
            base = P.claude_invocation(**common,
                effort=_option(invocation.argv, "--effort", "medium"))
            research = P.claude_invocation(**common, research=True,
                effort=_option(invocation.argv, "--effort", "medium"))
            return invocation in (base, research,
                                  replace(base, argv=base.argv + ("--tools", "")))
        if provider != "codex":
            return False
        if invocation.argv and invocation.argv[0] == "exec":
            from solvio.agent_runtime.specialists import codex_builder_invocation
            from solvio.agent_runtime.image_inputs import bound_paths
            candidates = (P.codex_invocation(**common),
                U.text_invocation("codex", **common, images=bound_paths()), codex_builder_invocation(**common))
            scope = D.current_scope()
            if type(scope) is D.TaskCostScope and scope.phase == "assessment":
                candidates += (U.text_invocation("codex", **common,
                    images=bound_paths(), assessment=True),)
            # Explicit Core-owned native home is also covered by the reader's
            # account/config/Invocation digest and the physical claim digest.
            return any(invocation == replace(candidate, codex_home=invocation.codex_home)
                       for candidate in candidates)
        from solvio.specialists.hermes_native import NativeResearchConfig, worker_invocation
        from solvio.specialists.image_generation import image_invocation
        config = NativeResearchConfig(
            hermes_python=invocation.executable,
            hermes_source=_option(invocation.argv, "--hermes-source"),
            codex_bin=_option(invocation.argv, "--codex-bin"),
            codex_home=invocation.codex_home,
            model=_option(invocation.argv, "--model"),
            browser_python=_option(invocation.argv, "--browser-python"),
            browser_bin=_option(invocation.argv, "--browser-bin"),
            browser_chrome=_option(invocation.argv, "--browser-chrome"),
            timeout_s=float(_option(invocation.argv, "--timeout")),
            shutdown_grace_s=invocation.shutdown_grace)
        config.validate()
        task_id = _option(invocation.argv, "--task-id")
        if config.browser_python:
            scope = D.current_scope()
            if type(scope) is not D.TaskCostScope or task_id != scope.task_id:
                return False
        if _option(invocation.argv, "--session-mode"):
            from solvio.specialists.hermes_native import continuation_invocation
            scope = D.current_scope()
            if type(scope) is not D.TaskCostScope or task_id != scope.task_id:
                return False
            if _option(invocation.argv, "--worker-profile"):
                from solvio.specialists.native_task import task_invocation
                if scope.phase != "specialist":
                    return False
                return invocation == task_invocation(config, invocation.cwd, task_id=task_id,
                    endpoint=_option(invocation.argv, "--core-tools-socket"),
                    manifest_digest=_option(invocation.argv, "--core-tools-digest"),
                    native_thread_id="" if _option(invocation.argv, "--resume-thread") == "-"
                        else _option(invocation.argv, "--resume-thread"),
                    previous_turn_id="" if _option(invocation.argv, "--previous-turn") == "-"
                        else _option(invocation.argv, "--previous-turn"))
            return invocation == continuation_invocation(config, invocation.cwd, task_id=task_id,
                native_thread_id="" if _option(invocation.argv, "--resume-thread") == "-"
                    else _option(invocation.argv, "--resume-thread"),
                previous_turn_id="" if _option(invocation.argv, "--previous-turn") == "-"
                    else _option(invocation.argv, "--previous-turn"))
        return invocation in (worker_invocation(config, invocation.cwd, task_id=task_id),
                              image_invocation(config, invocation.cwd))
    except (L.LauncherError, OSError, ValueError, TypeError):
        return False


def _reference(provider, observation, worker=None):
    # Fixed safe fields only. Neither raw config/account identity nor prompts
    # reach the existing money ledger. The native snapshot is represented by a
    # digest; its bound account, route and observation time remain distinguishable.
    if provider == "claude-code" and worker is not None:
        proof = {"kind": "subscription_oauth_extra_usage_disabled", "plan": observation.subscription,
            "freshness": observation.freshness, "fetched_at_ms": observation.fetched_at_ms,
            "cache": observation.cache_digest, "diagnostic": observation.diagnostic_digest,
            "pot": worker["pot"], "vault_policy_version": worker["vault_version"]}
    elif provider == "claude-code":
        proof = {"kind": "extra_usage_disabled", "plan": observation.subscription,
            "freshness": observation.freshness, "fetched_at_ms": observation.fetched_at_ms,
            "cache": observation.cache_digest, "diagnostic": observation.diagnostic_digest}
    else:
        proof = {"kind": "personal_plan_no_credits", "plan": observation.plan_type,
            "snapshots": observation.snapshots, "config": observation.config_digest}
    proof.update(account=observation.account_digest, context=observation.context_digest,
                 invocation=observation.invocation_digest, observed_at=observation.observed_at,
                 cli_version=observation.cli_version)
    digest = hashlib.sha256(json.dumps(proof, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return ("native-plan:" + provider + ":" + proof["kind"] + ":"
            + proof["plan"] + ":" + observation.account_digest + ":" + digest)


def _personal_codex_credits(observation):
    """Personal account, positive finite credits; not a per-task hard cap."""
    from decimal import Decimal, InvalidOperation
    if (type(observation) is not O.UsageObservation or observation.state != "observed"
            or observation.auth_type != "chatgpt"
            or observation.plan_type not in {"free", "go", "plus", "pro", "prolite"}):
        return False
    umbrella = observation.snapshots.get("codex", {})
    try:
        credits = umbrella["credits"]
        if (credits["hasCredits"] is not True or credits["unlimited"] is not False
                or not Decimal(credits["balance"]).is_finite() or Decimal(credits["balance"]) <= 0):
            return False
        for row in observation.snapshots.values():
            value = row.get("credits")
            if row.get("spendControlReached") is True:
                return False
            if value is not None and (value["unlimited"] is not False
                    or not Decimal(value["balance"]).is_finite() or Decimal(value["balance"]) < 0):
                return False
    except (KeyError, TypeError, InvalidOperation):
        return False
    return any(umbrella.get(name) is not None for name in ("primary", "secondary"))


def _personal_codex_plan(observation):
    if (type(observation) is not O.UsageObservation or observation.state != "observed"
            or observation.auth_type != "chatgpt"
            or observation.plan_type not in {"free", "go", "plus", "pro", "prolite"}
            or not observation.empty_credit_balance):
        return False
    snapshots = observation.snapshots
    umbrella = snapshots.get("codex", {})
    # A missing window is not evidence of an available plan. A native quota
    # refusal follows the same existing provider-wait route as a turn refusal.
    windows = [umbrella.get(name) for name in ("primary", "secondary")]
    if not any(window is not None for window in windows):
        return False
    if (umbrella.get("spendControlReached") is True
            or umbrella.get("rateLimitReachedType") is not None
            or any(window and window["usedPercent"] >= 100 for window in windows)):
        raise L.LauncherError("quota")
    for snapshot in snapshots.values():
        credits = snapshot.get("credits")
        if credits is not None:
            from decimal import Decimal
            if (credits["hasCredits"] is not False or credits["unlimited"] is not False
                    or credits["balance"] is None or Decimal(credits["balance"]) != 0):
                return False
    return True


# -- The Claude worker's four zero-quote conditions (§2.3) ----------------------

def _vault_description():
    """`describe()` of the Anthropic credential — metadata only, no value."""
    from solvio.provider_broker import anthropic as AN
    from solvio.secret_vault.broker import SecretBroker
    broker = SecretBroker()
    if not broker.exists(AN.SECRET_REF):
        return None
    return broker.describe(AN.SECRET_REF)


def _pot_proof_setting():
    from solvio.config import load_settings
    return str(getattr(load_settings(), "agent_runtime_claude_worker_pot_proof", "") or "").strip()


def _read_pot_proof(setting):
    """The evidence file when the setting is its SHA-256; None otherwise."""
    if not (type(setting) is str and len(setting) == 64 and all(c in "0123456789abcdef" for c in setting)):
        return None
    try:
        raw = POT_PROOF_FILE.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != setting:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    return body if type(body) is dict else None


def claude_worker_preconditions():
    """Conditions 2–4 without an account observation: (ok, reason, details).

    Used by `provider_switch.configured(..., worker=True)` so Claude is not
    even offered when the worker cannot be priced; the dispatch-time gate
    (`claude_worker_gate`) rechecks everything with the fresh observation.
    """
    try:
        if _running_broker() is None:
            return False, "broker_not_running", {}
        described = _vault_description()
    except Exception:  # noqa: BLE001 - a broken vault is a held quote, not a crash
        return False, "vault_unavailable", {}
    if described is None:
        return False, "no_credential", {}
    if (described.get("kind") != WORKER_SECRET_KIND or described.get("status") != "active"
            or WORKER_CAPABILITY not in (described.get("allowed_capabilities") or [])
            or WORKER_EXECUTOR not in (described.get("allowed_executors") or [])):
        return False, "credential_scope", {}
    setting = _pot_proof_setting()
    if not setting:
        return False, "pot_proof_missing", {}
    if setting != LIVE_PROOF and _read_pot_proof(setting) is None:
        return False, "pot_proof_invalid", {}
    return True, "", {"described": described, "pot_setting": setting}


def claude_worker_gate(observation):
    """All four conditions with the fresh observation; returns the reference
    material (`pot`, `vault_version`) or None when the quote must be held."""
    if (type(observation) is not A.UsageObservation or observation.state != "disabled"
            or observation.subscription not in {"pro", "max"}):
        return None
    ok, _, details = claude_worker_preconditions()
    if not ok:
        return None
    described, setting = details["described"], details["pot_setting"]
    version = described.get("version")
    if type(version) is not int:
        return None
    if setting == LIVE_PROOF:
        return {"pot": LIVE_PROOF, "vault_version": version}
    evidence = _read_pot_proof(setting)
    if (evidence is None or evidence.get("account_digest") != observation.account_digest
            or evidence.get("vault_version") != version):
        return None
    return {"pot": setting, "vault_version": version}


class NativeSubscriptionCosts:
    """Installed once by Core and shared with existing memory/development scopes."""
    def __init__(self, *, claude_reader=None, codex_reader=None, credit_policy=None):
        self.claude = claude_reader or A.NativeUsageReader()
        self.codex = codex_reader or O.NativeUsageReader()
        self.credit_policy = credit_policy

    async def credit_account(self):
        """Explicit settings lookup; native account/config reads, no model turn."""
        invocation = U.text_invocation("codex", workdir=str(Path.cwd()))
        observation = await self.codex.read(invocation)
        if (type(observation) is not O.UsageObservation or observation.state != "observed"
                or observation.auth_type != "chatgpt"
                or observation.plan_type not in {"free", "go", "plus", "pro", "prolite"}
                or not observation.applies_to(invocation)):
            raise ValueError("credit_account_unconfirmed")
        return observation.account_digest

    async def __call__(self, provider, invocation):
        if not _known_invocation(provider, invocation):
            return D.CostQuote()
        if provider == "claude-code" and worker_form(invocation) is not None:
            return await self._claude_worker(invocation)
        if provider == "claude-code":
            observation = await self.claude.read(invocation)
            accepted = (type(observation) is A.UsageObservation
                        and observation.state == "disabled"
                        and observation.subscription in {"pro", "max"})
        else:
            observation = await self.codex.read(invocation)
            if (self.credit_policy is not None and _personal_codex_credits(observation)
                    and observation.applies_to(invocation)):
                grant = self.credit_policy.grant(observation.account_digest)
                if grant:
                    reference = "native-credit:" + grant + ":" + hashlib.sha256(
                        _reference(provider, observation).encode()).hexdigest()
                    return D.CostQuote(None, C.CostEvidence("owner_authorized_credits", reference),
                        validate_before_dispatch=lambda: (
                            _known_invocation(provider, invocation) and observation.applies_to(invocation)
                            and self.credit_policy.grant(observation.account_digest) == grant),
                        native_credit_account=observation.account_digest)
            accepted = _personal_codex_plan(observation)
            if not accepted:
                # Category only: no account, balance, prompt or native payload.
                # This is diagnostic evidence, never permission to use credits.
                credits_present = (type(observation) is O.UsageObservation
                    and observation.state == "observed"
                    and any((row.get("credits") or {}).get("hasCredits") is True
                            or (row.get("credits") or {}).get("unlimited") is True
                            for row in observation.snapshots.values()))
                log.info("native_costs.quote_held", provider="codex",
                         reason="credits_present" if credits_present else "plan_only_evidence_missing")
        if not accepted or not observation.applies_to(invocation):
            return D.CostQuote()
        return D.CostQuote(0,
            C.CostEvidence("included_no_extra_charge", _reference(provider, observation)),
            validate_before_dispatch=lambda: (
                _known_invocation(provider, invocation) and observation.applies_to(invocation)))

    async def _claude_worker(self, invocation):
        # The reader binds the reviewed binary; it receives the derived form,
        # never the sandbox-exec prefix (which its context check refuses).
        reader = reader_invocation(invocation)
        observation = await self.claude.read(reader)
        worker = claude_worker_gate(observation)
        if worker is None or not observation.applies_to(reader):
            return D.CostQuote()
        return D.CostQuote(0,
            C.CostEvidence("included_no_extra_charge", _reference("claude-code", observation, worker)),
            validate_before_dispatch=lambda: (
                _known_invocation("claude-code", invocation) and observation.applies_to(reader)
                and claude_worker_gate(observation) == worker))
