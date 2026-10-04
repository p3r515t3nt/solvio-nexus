"""Die Kanarienvogel-Proben M-C1…M-C9 (N8/C4 §1.4) — physisch, modellfrei.

Jede Probe faehrt die ECHTE CLI (`claude` 2.1.261) mit der argv aus
`claude_worker_invocation` im ECHTEN Worker-Profil unter `sandbox-exec`, gegen
einen lokalen Lauscher mit Wegwerf-Token, der das Modell spielt. Keine
Anmeldung, kein Schluesselbund, kein Netz nach draussen, kein Owner-`~/.claude`.
Die Wurzeln von Arbeitsraum und Kaefig liegen unter einem kurzen, privaten
Testordner (E1), nie unter dem Owner-Home.

Ein Fall ueberspringt sich NUR bei fehlendem `sandbox-exec` oder fehlender CLI
— sichtbar, mit Grund. Alles andere ist gemessen, und rot heisst gesperrt.

Gemessene Mutationsbefunde (2026-09-18), im Sinne von Vertrag §5.2:

* Nr. 3 (`--bare` entfernt): M-C2 bleibt im Kaefig GRUEN — auch mit einer
  gescripteten `<jail>/config/.credentials.json` sendet die CLI nur den
  gesetzten `ANTHROPIC_API_KEY`. Die Mutation ist am Draht aequivalent; sie
  wird durch die schwaechste zulaessige Form getoetet, den Fabrik-Test
  „argv enthaelt `--bare`" (`test_claude_native_task`), und heisst so.
* Nr. 5 (`--tools` entfernt): unter `--bare` ist der Katalog ohnehin genau
  Bash/Edit/Read — der Draht-Test faerbt nicht rot; ebenfalls Fabrik-Test.
* Nr. 4 (`--allowedTools` entfernt): `permission_denials` nicht leer und
  Receipts `declined` — der Kernel-Text fehlt, weil eine FREMDE Schranke
  (die CLI) zuerst verweigert. `(d)` bleibt gruen: `acceptEdits` nimmt
  `echo ok > probe.txt` als Dateiaenderung an. Getoetet wird die Mutation
  also ueber die Verweigerungen, nicht ueber (d).
* Nr. 6 (`--no-session-persistence`): M-C3 rot (kein Verlauf, Exit 1).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from _private_temp import private_folder  # noqa: E402
from solvio.agent_runtime import isolation as I  # noqa: E402
from solvio.autopilot import canary  # noqa: E402
from solvio.specialists import claude_native_task as CNT, launcher as L  # noqa: E402


def _need() -> None:
    if not I.available():
        raise unittest.SkipTest("sandbox-exec fehlt — die Messung braucht macOS")
    try:
        CNT.resolve_claude()
    except L.LauncherError as exc:
        raise unittest.SkipTest(f"claude-CLI fehlt ({exc.reason}) — ungemessen, nicht sicher")


@contextmanager
def _roots():
    with private_folder("c4-") as base:
        ws = base / "w"
        jails = base / "j"
        ws.mkdir(mode=0o700)
        jails.mkdir(mode=0o700)
        yield str(ws), str(jails)


def _probe(name, **kwargs):
    with _roots() as (ws, jails):
        return canary.worker_probe(name, workspace_root=ws, jail_root=jails, **kwargs)


def _green(result):
    require(result.ok, f"{result.name} rot: {result.reason} — {json.dumps(result.checks, default=str)[:600]}")
    require_equal(result.seen_auth, ("PROBE_TOKEN",), "im Draht stand nicht nur der Wegwerf-Wert")


def t_mc1_the_v06_canary_still_holds_on_this_cli_build():
    """REUSE: Binary-Eigenschaft `--bare` (eigene argv, Owner-HOME, kein Seatbelt)."""
    _need()
    result = canary.run()
    require(result.ok, f"V0.6-Kanarienvogel rot: {result.reason}")
    require_equal(result.seen_auth, ("PROBE_TOKEN",))
    require_equal(canary.verdict(result), "")


def t_mc2_a_full_worker_turn_in_the_jail_executes_bash_and_reaches_nothing_sealed():
    """Die Fabrik-argv im echten Kaefig: Kernel-Verweigerungen fuer `security`,
    `~/.solvio-nexus`, `~/.solvio`, Nachbar-Arbeitsraum und -Kaefig,
    `~/.claude.json`; kein fremder Loopback-Port; `probe.txt` geschrieben;
    keine CLI-Verweigerung; Sitzung NUR im Kaefig; Draht nur mit Wegwerf-Wert."""
    _need()
    result = _probe("M-C2")
    _green(result)
    checks = result.checks
    for key in ("a_security_exec_denied", "c_loopback_refused", "d_bash_executed", "e_neighbours_sealed",
                "result_seen", "exit_zero", "assistant_started", "permission_denials_empty",
                "owner_projects_unchanged", "session_in_jail_config", "tools_exact"):
        require(checks.get(key) is True, f"{key}: {checks.get(key)}")
    require_equal(checks["declined_receipts"], 0)
    for key, exists in (("a_prime_nexus_sealed", "a_prime_target_exists"), ("b_solvio_sealed", "b_target_exists"),
                        ("f_claude_json_sealed", "f_target_exists")):
        if checks.get(exists):
            require(checks.get(key) is True, f"{key} rot, obwohl das Ziel existiert")
    require_equal(result.fields["tools_in_wire"], ["Bash", "Edit", "Read"], "der Werkzeugkatalog im Draht")
    require_equal(result.exit_code, 0)
    require_equal(canary.worker_verdict(result), "")


def t_mc2_counterprobe_without_allowed_tools_is_red_through_cli_denials():
    """Mutation 4: ohne Vorabfreigabe verweigert die CLI vor dem Kernel —
    `permission_denials` nicht leer, Receipts `declined`, Probe rot."""
    _need()
    result = _probe("M-C2-noallow")
    require(not result.ok, "ohne --allowedTools blieb die Probe gruen — eine fremde Schranke saehe gruen aus")
    require(result.checks["permission_denials_empty"] is False, "keine CLI-Verweigerung gesehen")
    require(result.checks["declined_receipts"] > 0, "kein Receipt wurde `declined`")
    require("permission_denials_empty" in result.reason)
    require(canary.worker_verdict(result).startswith("worker_canary_failed:M-C2-noallow:"))


def t_mc3_resume_in_the_same_jail_carries_the_previous_turn():
    _need()
    result = _probe("M-C3")
    _green(result)
    require(result.checks["prior_turn_in_resume"] is True, result.checks)
    require_equal(result.checks["turn2_exit_code"], 0)
    # Mutation 6: `--no-session-persistence` → kein Verlauf, keine Fortsetzung.
    mutated = _probe("M-C3", mutate=lambda argv: argv + ["--no-session-persistence"])
    require(not mutated.ok and mutated.checks.get("prior_turn_in_resume") is False,
            f"die Mutation blieb gruen: {mutated.checks}")


def t_mc4_the_mcp_adapter_in_the_jail_reaches_the_tool_bridge_socket():
    """`--mcp-config <jail>/mcp.json` → stdio-Adapter (Kaefig) → AF_UNIX-Stub:
    `system/init.mcp_servers` nennt `solvio` connected, der Stub sah
    `manifest` und `call` mit Core-gesetzten Kennungen, das `tool_result`
    kam zurueck — die `(path ...)`-Syntax der AF_UNIX-Zeile ist damit gemessen."""
    _need()
    result = _probe("M-C4")
    _green(result)
    for key in ("mcp_connected", "stub_saw_manifest", "stub_saw_call", "stub_binding_ok",
                "tool_result_returned", "dynamic_receipt"):
        require(result.checks.get(key) is True, f"{key}: {result.checks.get(key)}")
    require("mcp__solvio__portal_list" in result.fields["tools_in_wire"])


def t_mc5_a_stalled_provider_ends_in_a_group_kill_and_an_uncertain_turn():
    _need()
    result = _probe("M-C5")
    require(result.ok, f"M-C5 rot: {result.reason} — {result.checks}")
    require(result.checks["timeout_reported"] is True and result.exit_code is None)
    require(result.checks["pids_seen_before_kill"] >= 1, "kein Prozess der Gruppe gesehen")
    require(result.checks["process_group_empty"] is True, "die Prozessgruppe lebt weiter")
    require_equal(result.checks["turn_state"], "unknown")


def t_mc6_the_rendered_worker_profile_is_sealed_and_mutations_are_refused():
    home = os.path.expanduser("~")
    with _roots() as (ws, jails):
        workspace = os.path.join(ws, "at-1")
        jail = os.path.join(jails, "at-1")
        os.makedirs(workspace, mode=0o700)
        os.makedirs(jail, mode=0o700)
        body = I.worker_profile(workspace=workspace, jail=jail, broker_port=8792,
                                unix_sockets=(os.path.join(jail, "sock", "x", "core.sock"),),
                                roots=I.sealed_roots(ws, jails))
        require_equal(I.sealed_violations(body, sealed=I.SEALED_PATHS + I.WORKER_SEALED,
                                          roots=I.sealed_roots(ws, jails)), [])
        rules = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith(";"))
        require_equal(rules.count('(remote tcp "localhost:8792")'), 1)
        require_equal(rules.count("(path "), 1)
        require('"*:443"' not in rules)
        for sealed in ("~/.solvio", "~/.solvio-nexus", "~/.claude"):
            resolved = os.path.realpath(os.path.expanduser(sealed))
            require(f'"{resolved}"' not in rules and f'"{resolved}/' not in rules, f"{sealed} im Profil")
        for bad_workspace in (os.path.join(home, ".solvio", "x", "at-1"), os.path.join(home, ".solvio-nexus", "at-1"), ws):
            try:
                I.worker_profile(workspace=bad_workspace, jail=jail, broker_port=8792, roots=I.sealed_roots(ws, jails))
            except I.BuilderJailUnavailable:
                continue
            raise AssertionError(f"ein Profil fuer {bad_workspace} wurde gerendert")
        try:
            I.worker_profile(workspace=workspace, jail=jail, broker_port=0)
        except I.BuilderJailUnavailable:
            pass
        else:
            raise AssertionError("ein Worker-Profil ohne Broker-Port wurde gerendert")


def t_mc7_and_mc5b_the_cli_gives_up_on_429_and_401_by_itself_before_the_deadline():
    """Zwei lange Messungen (~3 min, parallel): durchgehend 429 → kein echtes
    `assistant`, `result` mit `api_error_status` 429, Exit-Code → Nichtstart-
    Kandidat; 401 nach gestartetem Turn → die CLI endet selbst, Turn
    `terminal(failed)`, nicht `unknown`. Beides begruendet `PROCESS_GRACE`."""
    _need()
    results = {}

    def go(name):
        results[name] = _probe(name)
    threads = [threading.Thread(target=go, args=(name,)) for name in ("M-C7", "M-C5b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seven = results["M-C7"]
    require(seven.ok, f"M-C7 rot: {seven.reason} — {seven.checks}")
    require(seven.checks["assistant_started"] is False and seven.checks["synthetic_errors"] >= 1)
    require_equal(seven.checks["api_error_status"], 429)
    require(seven.checks["nonstart_candidate"] is True and seven.exit_code is not None)
    require(seven.requests >= 2, "keine Wiederholung gesehen")
    require(seven.elapsed < CNT.PROCESS_GRACE, "die CLI brauchte laenger als die Prozessfrist-Reserve")
    five = results["M-C5b"]
    require(five.ok, f"M-C5b rot: {five.reason} — {five.checks}")
    require(five.checks["ended_before_deadline"] is True and five.exit_code is not None)
    require_equal((five.checks["terminal"], five.checks["terminal_reason"]), ("failed", "api_error_401"))
    require(five.elapsed < CNT.PROCESS_GRACE, "die 401-Schleife ueberschreitet die Prozessfrist-Reserve")


def t_mc8_the_broker_token_never_survives_into_receipts_or_the_observation():
    _need()
    result = _probe("M-C8")
    _green(result)
    for key in ("token_literal_absent", "outputs_redacted", "leak_markers_absent"):
        require(result.checks.get(key) is True, f"{key}: {result.checks.get(key)}")
    require("observation_sha256" in result.fields)


def t_mc9_a_stream_above_the_launcher_ceiling_still_ends_with_a_seen_result():
    _need()
    result = _probe("M-C9")
    _green(result)
    require(result.fields["stream_bytes"] > L.MAX_OUTPUT * 4, "der Strom lag unter der Grenze — kein Beweis")
    require(result.checks["receipts_within_budget"] is True)
    require_equal(result.checks["terminal"], "completed")
    require_equal(result.fields["decoder"]["tool_rounds"], 10)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
