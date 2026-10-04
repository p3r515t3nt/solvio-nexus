"""Abo-Nachweis an beiden echten Aufrufern, ohne Anbieter oder Kontodaten."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from solvio.agent_runtime import specialists as SP
from solvio.specialists import launcher as L, providers as P, team as T


def _claude(**changes):
    data = {"loggedIn": True, "authMethod": "claude.ai",
            "apiProvider": "firstParty", "subscriptionType": "max"}
    data.update(changes)
    return L.Outcome(True, text=json.dumps(data), exit_code=0)


def t_only_a_successful_known_codex_status_proves_subscription():
    status = P.classify_codex_status(L.Outcome(
        True, stderr_note="Logged in using ChatGPT", exit_code=0))
    require(status.available)
    require_equal(status.billing_mode, "subscription")
    require_equal(status.auth, "chatgpt")
    for outcome in (
            L.Outcome(False, stderr_note="Logged in using ChatGPT", exit_code=1),
            L.Outcome(True, stderr_note="ChatGPT status lookup failed", exit_code=0),
            L.Outcome(True, stderr_note="", exit_code=0),
            L.Outcome(True, stderr_note="Logged in using ChatGPT", truncated=True),
            L.Outcome(True, stderr_note="Logged in using ChatGPT", exit_code=1)):
        require(not P.classify_codex_status(outcome).available,
                "ein fehlgeschlagener/mehrdeutiger Status oeffnete das Tor")


def t_codex_stored_api_login_is_metered_and_unavailable():
    status = P.classify_codex_status(L.Outcome(
        True, stderr_note="Logged in using an API key", exit_code=0))
    require(not status.available)
    require_equal(status.billing_mode, "metered_api")
    require_equal(status.reason, "subscription_required")


def t_claude_subscription_is_the_known_pair_of_method_and_provider():
    status = P.classify_claude_status(_claude())
    require(status.available)
    require_equal(status.billing_mode, "subscription")
    require_equal(status.auth, "claude.ai")
    # Zusatzausgaben aus dem Konto gehoeren nicht in Dashboard/Journal.
    status = P.classify_claude_status(_claude(email="private@example.test",
                                            accountId="private-account"))
    require("private" not in json.dumps(status.as_dict()))


def t_claude_api_and_cloud_accounts_never_open_the_subscription_route():
    for auth, provider in (("api_key", "firstParty"), ("apiKey", "firstParty"),
                           ("console", "firstParty"), ("none", "bedrock"),
                           ("none", "vertex"), ("none", "foundry")):
        status = P.classify_claude_status(_claude(authMethod=auth, apiProvider=provider))
        require(not status.available)
        require_equal(status.billing_mode, "metered_api")
        require_equal(status.reason, "subscription_required")


def t_claude_malformed_or_failed_status_is_never_availability():
    for outcome in (
            L.Outcome(True, text="[]"), L.Outcome(True, text="{}"),
            L.Outcome(True, text="not JSON"),
            _claude(loggedIn="true"), _claude(authMethod="unexpected"),
            _claude(apiProvider="unexpected"), _claude(apiProvider=None),
            L.Outcome(False, text=_claude().text, exit_code=1),
            L.Outcome(True, text=_claude().text, truncated=True)):
        status = P.classify_claude_status(outcome)
        require(not status.available)
        require_equal(status.billing_mode, "unknown")


def t_logged_out_with_nonzero_exit_is_a_login_boundary_not_a_crash():
    claude = L.Outcome(False, text=json.dumps({"loggedIn": False}), exit_code=1)
    codex = L.Outcome(False, stderr_note="Not logged in", exit_code=1)
    for status in (P.classify_claude_status(claude), P.classify_codex_status(codex)):
        require(not status.available)
        require_equal(status.reason, "logged_out")


def t_subscription_gate_rechecks_each_dispatch_and_never_switches_provider():
    subscription = P.ProviderStatus("codex", True, auth="chatgpt",
                                     billing_mode="subscription")
    api = P.ProviderStatus("codex", False, "subscription_required", auth="api_key",
                            billing_mode="metered_api")
    check = AsyncMock(side_effect=[subscription, api])
    other = AsyncMock(side_effect=AssertionError("Anbieterwechsel"))
    dispatch = AsyncMock(return_value=L.Outcome(True, text="Antwort"))
    invocation = L.Invocation("/synthetic/codex", (), timeout=1)
    with patch.object(P, "codex_status", check), patch.object(P, "claude_status", other):
        first = asyncio.run(P.run_subscription("codex", invocation, "Eins", runner=dispatch))
        second = asyncio.run(P.run_subscription("codex", invocation, "Zwei", runner=dispatch))
    require(first.ok)
    require(not second.ok)
    require(first.dispatch_started)
    require_equal(first.billing_mode, "subscription")
    require(not second.dispatch_started)
    require_equal(second.billing_mode, "metered_api")
    require_equal(second.reason, "subscription_required")
    require_equal(check.await_count, 2)
    require_equal(dispatch.await_count, 1)
    require_equal(other.await_count, 0)


def t_status_exception_and_unsupported_provider_fail_closed_without_raw_text():
    check = AsyncMock(side_effect=RuntimeError("private-account-details"))
    with patch.object(P, "codex_status", check):
        failed = asyncio.run(P.ensure_subscription("codex"))
        unsupported = asyncio.run(P.ensure_subscription("unavailable"))
    require_equal(failed.reason, "auth_status_failed")
    require("private" not in json.dumps(failed.as_dict()))
    require(not unsupported.available)
    require_equal(unsupported.reason, "provider_unavailable")
    require_equal(check.await_count, 1)


def t_runtime_quota_is_not_success_even_when_cli_exits_zero():
    status = P.ProviderStatus("codex", True, auth="chatgpt", billing_mode="subscription")
    dispatch = AsyncMock(return_value=L.Outcome(
        True, text=json.dumps({"type": "turn.failed", "error": {
            "message": "Usage limit reached. Try again later."}}), exit_code=0))
    invocation = L.Invocation("/synthetic/codex", (), timeout=1)
    with patch.object(P, "codex_status", AsyncMock(return_value=status)), \
            patch.object(L, "run", dispatch):
        result = asyncio.run(SP.run_specialist(SP.SpecialistRequest(
            profile="investigator/codex", objective="Pruefe", workdir="/tmp"),
            invocation_factory=lambda spec, request: invocation))
    require(not result.result.ok)
    require(result.quota)
    require_equal(result.result.reason, "quota")
    require(result.dispatch_started, "eine moegliche Teilausfuehrung wurde verschwiegen")
    require_equal(dispatch.await_count, 1)


def t_successful_quota_analysis_is_not_a_runtime_or_team_failure():
    answer = {"findings": ["Quota und Kontingent sind in der Fehlerbehandlung benannt."],
              "recommended_path": "Die Nutzungslimit-Analyse ist abgeschlossen."}
    for provider, profile, method in (("codex", "investigator/codex", "_challenger"),
                                      ("claude-code", "investigator/claude", "_architect")):
        text = json.dumps(answer)
        if provider == "claude-code":
            text = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                               "result": text})
        outcome = L.Outcome(True, text=text, exit_code=0,
                            stderr_note="Analyse des Kontingents abgeschlossen")
        status = P.ProviderStatus(provider, True, auth="chatgpt" if provider == "codex"
                                  else "claude.ai", billing_mode="subscription")
        check_name = "codex_status" if provider == "codex" else "claude_status"
        with patch.object(P, check_name, AsyncMock(return_value=status)), \
                patch.object(P, "resolve", return_value="/synthetic/cli"), \
                patch.object(P, "run", AsyncMock(return_value=outcome)), \
                patch.object(L, "run", AsyncMock(return_value=outcome)):
            runtime = asyncio.run(SP.run_specialist(SP.SpecialistRequest(
                profile=profile, objective="Analysiere die Quota", workdir="/tmp")))
            team = asyncio.run(getattr(T.SpecialistTeam(), method)("/tmp", "Quota", "", ""))
        require(runtime.result.ok)
        require(not runtime.quota)
        require_equal(runtime.result.findings, answer["findings"])
        require(team.ok)
        require_equal(team.findings, answer["findings"])


def t_session_expiring_after_the_gate_is_a_login_wait_reason():
    for provider, profile, check in (("codex", "investigator/codex", "codex_status"),
                                     ("claude-code", "investigator/claude", "claude_status")):
        status = P.ProviderStatus(provider, True, auth="chatgpt" if provider == "codex"
                                  else "claude.ai", billing_mode="subscription")
        failure = L.Outcome(False, reason="nonzero_exit", exit_code=1,
                            stderr_note="Authentication required. Please log in again.")
        with patch.object(P, check, AsyncMock(return_value=status)), \
                patch.object(P, "resolve", return_value="/synthetic/cli"), \
                patch.object(L, "run", AsyncMock(return_value=failure)):
            result = asyncio.run(SP.run_specialist(SP.SpecialistRequest(
                profile=profile, objective="Pruefe das Projekt", workdir="/tmp")))
        require(not result.result.ok)
        require_equal(result.result.reason, "logged_out")
        require(result.dispatch_started)
        require_equal(result.billing_mode, "subscription")


def t_structured_claude_quota_error_is_recognized_but_success_is_not():
    for is_error, subtype, reason in ((True, "error_during_execution", "quota"),
                                      (False, "success", "")):
        outcome = L.Outcome(True, text=json.dumps({
            "type": "result", "subtype": subtype, "is_error": is_error,
            "result": "Usage limit reached"}), exit_code=0)
        require_equal(P.cli_failure_reason("claude-code", outcome), reason)


def t_claude_runtime_and_direct_team_block_api_accounts_before_dispatch():
    status = P.ProviderStatus("claude-code", False, "subscription_required",
                              auth="api_key", billing_mode="metered_api")
    dispatch = AsyncMock(side_effect=AssertionError("unerlaubter Modellaufruf"))
    with patch.object(P, "claude_status", AsyncMock(return_value=status)), \
            patch.object(P, "resolve", return_value="/synthetic/claude"), \
            patch.object(P, "run", dispatch), patch.object(L, "run", dispatch):
        runtime = asyncio.run(SP.run_specialist(SP.SpecialistRequest(
            profile="investigator/claude", objective="Pruefe", workdir="/tmp")))
        team = asyncio.run(T.SpecialistTeam()._architect("/tmp", "Ziel", "", ""))
    require_equal(runtime.result.reason, "subscription_required")
    require(not runtime.dispatch_started)
    require_equal(team.reason, "subscription_required")
    require_equal(dispatch.await_count, 0)


def _fake_cli(folder: str):
    """Ein echtes Kind liest eine gespeicherte Statusform, nie echte Anmeldung."""
    executable = Path(folder) / "codex"
    state = Path(folder) / "status.txt"
    marker = Path(folder) / "dispatch.txt"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        "folder = Path(__file__).parent\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    print((folder/'status.txt').read_text(), file=sys.stderr)\n"
        "else:\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n"
        "    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        "    (folder/'dispatch.txt').write_text('called')\n"
        "    print(json.dumps({'findings':['Ein belegter Befund'], 'evidence':[],"
        "'uncertainties':[], 'recommended_path':'Lesen abgeschlossen'}))\n",
        encoding="utf-8")
    executable.chmod(0o700)
    return executable, state, marker


def t_real_runtime_adapter_blocks_stored_api_auth_without_api_environment():
    with tempfile.TemporaryDirectory() as folder:
        executable, state, marker = _fake_cli(folder)
        state.write_text("Logged in using an API key", encoding="utf-8")
        with patch.object(P, "resolve", return_value=str(executable)), \
                patch.dict(os.environ, {"HOME": folder}, clear=True):
            result = asyncio.run(SP.run_specialist(SP.SpecialistRequest(
                profile="investigator/codex", objective="Lies das Projekt", workdir=folder)))
        require(not result.result.ok)
        require_equal(result.result.reason, "subscription_required")
        require(not result.dispatch_started)
        require_equal(result.billing_mode, "metered_api")
        require(not marker.exists(), "der eigentliche CLI-Auftrag wurde trotzdem gestartet")


def t_real_runtime_adapter_allows_subscription_through_the_same_launcher():
    with tempfile.TemporaryDirectory() as folder:
        executable, state, marker = _fake_cli(folder)
        state.write_text("Logged in using ChatGPT", encoding="utf-8")
        with patch.object(P, "resolve", return_value=str(executable)), \
                patch.dict(os.environ, {"HOME": folder}, clear=True):
            result = asyncio.run(SP.run_specialist(SP.SpecialistRequest(
                profile="investigator/codex", objective="Lies das Projekt", workdir=folder)))
        require(result.result.ok, result.result.reason)
        require(result.dispatch_started)
        require_equal(result.billing_mode, "subscription")
        require_equal(result.auth, "chatgpt")
        require(marker.exists(), "kein Ausfuehrungsnachweis")
        require_equal(result.result.findings, ["Ein belegter Befund"])


def t_direct_team_cli_has_the_same_gate_even_without_consult_availability():
    with tempfile.TemporaryDirectory() as folder:
        executable, state, marker = _fake_cli(folder)
        state.write_text("Logged in using an API key", encoding="utf-8")
        with patch.object(P, "resolve", return_value=str(executable)), \
                patch.dict(os.environ, {"HOME": folder}, clear=True):
            result = asyncio.run(T.SpecialistTeam()._challenger(folder, "Ziel", "", ""))
        require(not result.ok)
        require_equal(result.reason, "subscription_required")
        require(not marker.exists())


def t_claude_empty_model_uses_the_cli_default_without_an_empty_argument():
    with patch.object(P, "resolve", return_value="/synthetic/claude"):
        default = P.claude_invocation(workdir="/tmp", model="")
        explicit = P.claude_invocation(workdir="/tmp", model="opus")
        investigator = SP.claude_investigator_invocation(workdir="/tmp", model="")
    require("--model" not in default.argv)
    require("--model" not in investigator.argv)
    require_equal(explicit.argv[explicit.argv.index("--model") + 1], "opus")


def t_claude_research_selects_only_native_web_tools_and_preserves_investigator():
    with patch.object(P, "resolve", return_value="/synthetic/claude"):
        prior = P.claude_invocation(workdir="/tmp", model="")
        spec = SP.profile(SP.CLAUDE_RESEARCH_PROFILE)
        research = SP._invocation_for(spec, SP.SpecialistRequest(
            profile=spec.key, objective="Pruefe die Variante", workdir="/tmp"))
        investigator = SP.claude_investigator_invocation(workdir="/tmp", model="",
                                                        timeout=prior.timeout)
    require_equal(investigator, prior)
    require(not spec.needs_workspace)
    require_equal(spec.provider, "claude-code")
    require_equal(research.timeout, spec.timeout)
    require_equal(research.argv[-3:], ("--tools", "WebSearch", "WebFetch"))
    start = research.argv.index("--allowedTools") + 1
    stop = research.argv.index("--disallowedTools")
    require_equal(research.argv[start:stop], P.CLAUDE_RESEARCH_TOOLS)
    denied = research.argv[stop+1:research.argv.index("--json-schema")]
    for name in ("Bash", "Agent", "Write", "Edit", "Read", "Grep", "Glob", "Skill", "SendMessage"):
        require(name in denied, name)
    require(not set(P.CLAUDE_RESEARCH_TOOLS).intersection(denied))
    require("--safe-mode" in research.argv and "--no-chrome" in research.argv)
    require_equal(research.argv[research.argv.index("--mcp-config")+1], '{"mcpServers":{}}')
    require("--bare" not in research.argv and "--model" not in research.argv)
    require("--tools" not in prior.argv)
    from solvio.specialists.hermes_native_worker import RESULT_SCHEMA
    require_equal(json.loads(research.argv[research.argv.index('--json-schema')+1]), RESULT_SCHEMA)


def t_research_provider_is_explicit_independent_and_unknown_stays_unavailable():
    from types import SimpleNamespace
    from solvio import config
    require_equal(config.Settings.model_fields["agent_runtime_research_provider"].default, "codex")
    for provider, expected in (("codex", "researcher/hermes"),
                               ("claude-code", SP.CLAUDE_RESEARCH_PROFILE),
                               ("unknown", ""), ("", "")):
        with patch.object(config, "load_settings", return_value=SimpleNamespace(
                agent_runtime_research_provider=provider,
                agent_runtime_subscription_provider="different_planner")):
            require_equal(SP.selected_research_profile(), expected)


def t_claude_research_without_task_cost_scope_never_reads_auth_or_dispatches():
    with patch.object(P, "run_subscription", AsyncMock(side_effect=AssertionError("unscoped dispatch"))):
        result = asyncio.run(SP.run_specialist(SP.SpecialistRequest(
            profile=SP.CLAUDE_RESEARCH_PROFILE, objective="Pruefe Quelle", workdir="")))
    require_equal(result.result.reason, "cost_unbounded")
    require(not result.dispatch_started)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
