"""Die beiden Abo-gestuetzten Berater — mit genau den Flags, die es wirklich gibt.

Alles hier ist an den **installierten** Fassungen gemessen, nicht aus
Dokumentation abgeleitet:

* Claude Code 2.1.222 kennt `--print`, `--output-format json`,
  `--permission-mode plan`, `--model`, `--effort`, `--allowedTools`,
  `--disallowedTools`, `--strict-mcp-config`. Es kennt **kein** `--max-turns`
  (das gab es einmal) und **kein** ACP-Flag — die Warnung im Auftrag war
  berechtigt, und deshalb laeuft der Weg ueber den Druckmodus.
* Codex 0.147.0 kennt `exec --sandbox read-only --ephemeral
  --ignore-user-config --skip-git-repo-check --cd --color never` und liest die
  Frage von `stdin`.

Zur Abrechnung: beide Werkzeuge koennen mit einem API-Schluessel bezahlen, wenn
einer in der Umgebung steht. Genau deshalb entfernt der Starter diese Namen. Die
Sitzung des Abonnements liegt in `$HOME` und wird vom Werkzeug selbst gelesen —
SOLVIO fasst sie nie an und sieht sie nie.

Ein Wort zu `--bare` bei Claude Code: das Flag zwingt ausdruecklich auf
`ANTHROPIC_API_KEY` und liest OAuth und Schluesselbund NICHT. Es waere also genau
der falsche Weg und wird hier bewusst nicht benutzt.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from solvio.logging_setup import get_logger
from solvio.specialists.launcher import Invocation, LauncherError, Outcome, resolve, run

log = get_logger("specialists")

SUBSCRIPTION = "subscription"
METERED_API = "metered_api"
UNKNOWN = "unknown"

#: Jedes Werkzeug, das CLI **2.1.258** dem Berater in irgendeinem der
#: gemessenen Laeufe angeboten hat. Am 2026-09-02 dreimal am Draht gemessen
#: (lokaler Lauscher, `tools[].name` aus dem Anfragerumpf); je Lauf identisch.
#:
#: Es ist die VEREINIGUNG zweier Faelle, und der Unterschied ist selbst ein
#: Befund: ohne Sperren stehen 24 Namen im Draht, `Glob` und `Grep` aber
#: NICHT — die erscheinen erst, wenn `--allowedTools` sie ausdruecklich
#: nennt. Die Erlaubnisliste nimmt also nichts weg, sie legt hoechstens etwas
#: dazu.
#:
#: Der Katalog steht hier, weil die Sperrliste sonst gegen eine Vermutung
#: geprueft wuerde statt gegen die Wirklichkeit. Waechst er mit einer neuen
#: CLI-Fassung, faellt die Zusicherung — und das ist der Zweck.
CLAUDE_TOOL_CATALOGUE_2_1_258 = (
    "Agent", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync",
    "Edit", "EnterWorktree", "ExitWorktree", "Glob", "Grep", "ListAgents",
    "Monitor", "NotebookEdit", "PushNotification", "Read", "ReportFindings",
    "ScheduleWakeup", "SendMessage", "Skill", "TaskOutput", "TaskStop",
    "WebFetch", "WebSearch", "Workflow", "Write")

#: Werkzeuge, die ein Berater NICHT bekommt. `Bash` steht ganz oben: damit waere
#: jede andere Grenze hinfaellig, weil `cat` alles liest, was der Nutzer lesen
#: darf — einschliesslich der Anmeldedaten der anderen Berater.
#:
#: **Die Liste ist lang, weil `--allowedTools` nichts wegnimmt.** Gemessen am
#: 2026-09-02: derselbe Aufruf einmal mit und einmal ohne
#: `--allowedTools Read Grep Glob` bietet exakt dieselben Werkzeuge an. Nur
#: `--disallowedTools` entfernt etwas. Wer die Erlaubnisliste fuer die Grenze
#: haelt, hat keine.
#:
#: Was die Messung ausserdem zutage foerderte und was hier vorher fehlte:
#:
#: * `Task` entfernt in dieser Fassung tatsaechlich `Agent`, `TaskOutput` und
#:   `TaskStop` — die Sperre wirkte also, aber ueber einen undokumentierten
#:   Zweitnamen. Sie steht jetzt unter ihrem gemessenen Namen da, damit sie
#:   nicht beim naechsten Umbenennen still ausfaellt.
#: * `Workflow` haette einen ganzen Faecher von Unteragenten gestartet,
#:   `CronCreate` einen dauerhaften Zeitplan angelegt, `PushNotification` und
#:   `SendMessage` das Haus verlassen. Ein Berater, der beraet, braucht
#:   nichts davon.
CLAUDE_DENIED = ("Agent", "Bash", "BashOutput", "CronCreate", "CronDelete",
                 "CronList", "DesignSync", "Edit", "EnterWorktree",
                 "ExitWorktree", "KillShell", "ListAgents", "Monitor",
                 "NotebookEdit", "PushNotification", "ReportFindings",
                 "ScheduleWakeup", "SendMessage", "Skill", "Task",
                 "TaskOutput", "TaskStop", "WebFetch", "WebSearch",
                 "Workflow", "Write")

#: Was er darf: lesen, suchen, denken. Mehr braucht ein Entwurf nicht.
CLAUDE_ALLOWED = ("Read", "Grep", "Glob")
CLAUDE_RESEARCH_TOOLS = ("WebSearch", "WebFetch")


@dataclass(frozen=True)
class ProviderStatus:
    """Ob ein Berater ueberhaupt ansprechbar ist — und warum nicht."""

    name: str
    available: bool
    reason: str = ""
    version: str = ""
    auth: str = ""
    billing_mode: str = UNKNOWN

    def as_dict(self) -> dict:
        return {"anbieter": self.name, "erreichbar": self.available,
                "grund": self.reason, "version": self.version,
                "anmeldung": self.auth, "abrechnung": self.billing_mode}


@dataclass
class SubscriptionOutcome(Outcome):
    """Abrechnungsnachweis zusammen mit dem Ausgang desselben Dispatchs."""

    provider: str = ""
    billing_mode: str = UNKNOWN
    auth: str = ""
    # True heisst: an den Starter uebergeben, eine Teilwirkung ist moeglich.
    # Nur ein Tor VOR dem Starter kann False sicher zusagen.
    dispatch_started: bool = False
    usage_reported: bool = False
    cost_reservation_id: str = ""
    cost_invocation_id: str = ""
    cost_status: str = ""


# -- Claude Code -------------------------------------------------------------

async def claude_status() -> ProviderStatus:
    """Fragt das Werkzeug selbst, ob es angemeldet ist. Ohne Verbrauch.

    `claude auth status` gibt JSON zurueck und nennt dabei **keine** Anmeldedaten,
    sondern nur die Art der Anmeldung. Das ist die richtige Quelle: eine Datei im
    Dateisystem zu suchen waere raten, weil die Sitzung unter macOS im
    Schluesselbund liegen kann.
    """
    try:
        executable = resolve("claude")
    except LauncherError:
        return ProviderStatus("claude-code", False, "provider_unavailable")
    outcome = await run(Invocation(executable, ("auth", "status"), timeout=30.0,
                                   prompt_via_stdin=False), "")
    return classify_claude_status(outcome)


def classify_claude_status(outcome: Outcome) -> ProviderStatus:
    """Nur bekannte Statusformen gelten als Nachweis, nie bloss `loggedIn`.

    Am 2026-09-10 ohne Inferenz gemessen: `authMethod=claude.ai`,
    `apiProvider=firstParty`, `subscriptionType=max`, Exit 0. Kontodaten und
    unbekannte Freitextfelder werden nicht in den oeffentlichen Status kopiert.
    """
    import json
    # Gemessen: bei abgemeldetem Konto endet `claude auth status` mit Code 1 und
    # schreibt die Auskunft trotzdem sauber auf stdout. Wer hier zuerst auf den
    # Rueckgabewert schaut, meldet „status_failed" statt „nicht angemeldet" —
    # und aus einem menschlichen Schritt wird eine Stoerung.
    try:
        data = json.loads((outcome.text or "").strip() or "{}")
    except ValueError:
        return ProviderStatus("claude-code", False, "auth_status_failed")
    if not isinstance(data, dict):
        return ProviderStatus("claude-code", False, "auth_status_failed")
    if data.get("loggedIn") is False:
        # Das ist keine Stoerung und kein Mangel an Faehigkeit, sondern ein
        # menschlicher Schritt: `claude auth login` oeffnet einen Browser.
        return ProviderStatus("claude-code", False, "logged_out", auth="none")
    if not outcome.ok or outcome.exit_code not in (None, 0) or outcome.truncated:
        return ProviderStatus("claude-code", False, "auth_status_failed")
    if data.get("loggedIn") is not True:
        return ProviderStatus("claude-code", False, "auth_unknown", auth=UNKNOWN)
    method = str(data.get("authMethod", "")).casefold()
    provider = str(data.get("apiProvider", "")).casefold()
    if method in {"api_key", "apikey", "api-key", "console"} or provider in {
            "bedrock", "vertex", "foundry"}:
        return ProviderStatus("claude-code", False, "subscription_required",
                              auth="api_key", billing_mode=METERED_API)
    if method == "claude.ai" and provider == "firstparty":
        return ProviderStatus("claude-code", True, auth="claude.ai",
                              billing_mode=SUBSCRIPTION)
    return ProviderStatus("claude-code", False, "auth_unknown", auth=UNKNOWN)


def claude_invocation(*, workdir: str, model: str, effort: str = "medium",
                      timeout: float = 300.0, research: bool = False) -> Invocation:
    """Der feste Aufruf. Jedes Flag ist Absicht.

    `--permission-mode plan` ist die eigentliche Leine: in diesem Modus wird
    nichts geschrieben, sondern geplant. `--disallowedTools` haelt zusaetzlich
    `Bash` fern — zwei Sperren, weil die eine ein Modus und die andere eine
    Liste ist und beide anders versagen.
    """
    allowed = CLAUDE_RESEARCH_TOOLS if research else CLAUDE_ALLOWED
    denied = (tuple(name for name in CLAUDE_DENIED if name not in CLAUDE_RESEARCH_TOOLS)
              + CLAUDE_ALLOWED) if research else CLAUDE_DENIED
    argv = ["--print",
              "--output-format", "json",
              "--permission-mode", "plan",
              # Match the native usage reader's account/configuration context.
              # Local settings can contain env/API routing, hooks or plugins;
              # the parent environment allowlist alone does not exclude them.
              "--safe-mode", "--setting-sources", "",
              "--no-session-persistence", "--disable-slash-commands", "--no-chrome",
              "--effort", effort,
              # Keine fremden MCP-Server. Was hier zusaetzlich haengt, waere
              # Werkzeugflaeche, die niemand geprueft hat.
              "--strict-mcp-config",
              "--mcp-config", '{"mcpServers":{}}',
              "--allowedTools", *allowed,
              "--disallowedTools", *denied]
    if research:
        import json
        from solvio.specialists.hermes_native_worker import RESULT_SCHEMA
        argv += ["--json-schema", json.dumps(RESULT_SCHEMA, separators=(",", ":"))]
        # --allowedTools alone does not remove tools. This native allowlist
        # selects exactly the two web tools; no file, shell, Chrome or MCP path.
        argv += ["--tools", *CLAUDE_RESEARCH_TOOLS]
    if model:
        argv += ["--model", model]
    return Invocation(
        executable=resolve("claude"), argv=tuple(argv),
        timeout=timeout, cwd=workdir)


def claude_text(outcome: Outcome, *, structured: bool = False) -> str:
    """Holt die Antwort aus dem JSON-Umschlag des Druckmodus."""
    import json
    try:
        data = json.loads(outcome.text or "{}")
    except ValueError:
        return "" if structured else outcome.text
    if structured:
        if (type(data) is not dict or data.get("type") != "result"
                or data.get("subtype") != "success" or data.get("is_error") is not False
                or type(data.get("structured_output")) is not dict):
            return ""
        return json.dumps(data["structured_output"], ensure_ascii=False)
    if isinstance(data, dict):
        return str(data.get("result") or data.get("text") or outcome.text)
    return outcome.text


# -- Codex -------------------------------------------------------------------

async def codex_status(*, codex_home: str = "", codex_bin: str = "") -> ProviderStatus:
    """`codex login status` sagt die Art der Anmeldung, nie den Token."""
    try:
        executable = codex_bin or resolve("codex")
        if not os.path.isabs(executable) or not os.access(executable, os.X_OK):
            raise LauncherError("not_executable")
    except LauncherError:
        return ProviderStatus("codex", False, "provider_unavailable")
    outcome = await run(Invocation(executable, ("login", "status"), timeout=30.0,
                                   prompt_via_stdin=False, codex_home=codex_home), "")
    return classify_codex_status(outcome)


def classify_codex_status(outcome: Outcome) -> ProviderStatus:
    """Die bekannte CLI-Antwort ist der Nachweis; ein Teilwort ist keiner."""
    # Gemessen: Codex schreibt „Logged in using ChatGPT" auf stderr, nicht auf
    # stdout. Beide Stroeme werden gelesen, sonst bleibt die Auskunft leer und
    # der Anbieter gilt faelschlich als anmeldelos.
    text = ((outcome.text or "") + " " + (outcome.stderr_note or "")).strip()
    low = text.casefold()
    if low in {"not logged in", "logged out"}:
        return ProviderStatus("codex", False, "logged_out", auth="none")
    if not outcome.ok or outcome.exit_code not in (None, 0) or outcome.truncated:
        return ProviderStatus("codex", False, "auth_status_failed")
    # Die exakte Auskunft belegt die ChatGPT-Anmeldung statt einer API-Anmeldung.
    # Ob Zusatzverbrauch ausgeschlossen ist, belegt sie NICHT; das separate
    # Kosten-Tor prueft dies fuer einen Agentenauftrag vor dem Dispatch.
    if low == "logged in using chatgpt":
        return ProviderStatus("codex", True, auth="chatgpt",
                              billing_mode=SUBSCRIPTION)
    if low.startswith(("logged in using an api key", "logged in using api key")):
        return ProviderStatus("codex", False, "subscription_required",
                              auth="api_key", billing_mode=METERED_API)
    return ProviderStatus("codex", False, "auth_unknown", auth=UNKNOWN)


async def ensure_subscription(provider: str, *, codex_home: str = "",
                              codex_bin: str = "") -> ProviderStatus:
    """Frischer Nachweis vor Ausfuehrung. Keine API-Anmeldung als Rueckfall.

    Die Statusfunktionen sind die injizierbare Naht fuer Offline-Tests. Der
    Nachweis wird nicht ueber Auftraege hinweg gecacht: der Owner kann sich
    zwischen zwei Aufrufen anders anmelden.
    """
    check = {"codex": codex_status, "claude-code": claude_status}.get(provider)
    if check is None:
        return ProviderStatus(provider, False, "provider_unavailable")
    try:
        status = (await check(codex_home=codex_home, codex_bin=codex_bin)
                  if provider == "codex" and (codex_home or codex_bin)
                  else await check())
    except Exception:  # noqa: BLE001 - Statusfehler enthaelt keine Rohdaten
        return ProviderStatus(provider, False, "auth_status_failed")
    if status.available and status.billing_mode == SUBSCRIPTION:
        return status
    if status.available:
        return ProviderStatus(provider, False,
                              "subscription_required" if status.billing_mode == METERED_API
                              else "auth_unknown", auth=status.auth,
                              billing_mode=status.billing_mode)
    return status


async def run_subscription(provider: str, invocation: Invocation, prompt: str,
                           *, runner=None, codex_bin: str = "") -> SubscriptionOutcome:
    """Eine bestehende CLI-Ausfuehrung, unmittelbar durch das Abo-Tor.

    Kein alternativer Anbieter, keine Wiederholung, keine Inferenz beim
    Statuslesen. `runner` ist dieselbe Starter-Naht des Agentenadapters.
    """
    status = (await ensure_subscription(provider, codex_home=invocation.codex_home,
                                       codex_bin=codex_bin)
              if invocation.codex_home or codex_bin else await ensure_subscription(provider))
    metadata = {"provider": provider, "billing_mode": status.billing_mode,
                "auth": status.auth}
    if not status.available:
        return SubscriptionOutcome(False, reason=status.reason or "auth_unknown",
                                   **metadata)
    from solvio.agent_runtime import cost_dispatch
    dispatch = await cost_dispatch.dispatch(provider, invocation, prompt, runner or run)
    return SubscriptionOutcome(**asdict(dispatch.outcome), dispatch_started=dispatch.dispatch_started,
        cost_reservation_id=dispatch.reservation_id, cost_invocation_id=dispatch.invocation_id,
        cost_status=dispatch.cost_status, **metadata)


def codex_invocation(*, workdir: str, model: str = "",
                     timeout: float = 300.0) -> Invocation:
    """Der feste Aufruf: nur lesen, nichts behalten, keine Nutzerkonfiguration.

    `--ephemeral` laesst keine Sitzungsdateien zurueck, `--ignore-user-config`
    macht den Lauf unabhaengig davon, was in `config.toml` steht (die Anmeldung
    kommt weiterhin aus `CODEX_HOME`), und `--sandbox read-only` ist die Leine.
    """
    argv = ["exec",
            "--sandbox", "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--color", "never",
            "--cd", workdir]
    if model:
        argv += ["--model", model]
    # Ein einzelner Bindestrich: die Frage kommt von stdin, nicht aus argv.
    argv.append("-")
    return Invocation(executable=resolve("codex"), argv=tuple(argv),
                      timeout=timeout, cwd=workdir)


def codex_text(outcome: Outcome) -> str:
    """Codex schreibt Fortschritt und Antwort auf dieselbe Ausgabe.

    Gesucht wird deshalb der letzte zusammenhaengende JSON-Block; faellt keiner
    an, bleibt der Rohtext. Das Zerlegen macht ohnehin `result.parse`.
    """
    return outcome.text


#: Woran ein erschoepftes Kontingent erkennbar ist. Bewusst am Text des
#: Werkzeugs und nicht an einem Kontostand: Nutzungsuebersichten des Anbieters
#: abzufragen waere genau das Scraping, das hier nicht stattfindet.
QUOTA_MARKERS = ("usage limit", "rate limit", "quota", "too many requests",
                 "429", "limit reached", "exceeded your", "try again later",
                 "nutzungslimit", "kontingent")


def quota_exhausted(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in QUOTA_MARKERS)


def provider_error_reason(text: str, default: str) -> str:
    """Nur auf einem bereits als Fehler erkannten Ausgang verwenden."""
    if quota_exhausted(text):
        return "quota"
    low = text.casefold()
    if any(s in low for s in ("not logged in", "please log in", "please login",
                              "authentication required", "token expired",
                              "please run codex login", "please run /login")):
        return "logged_out"
    return default or "provider_failed"


def cli_failure_reason(provider: str, outcome: Outcome) -> str:
    """Kontingentwoerter sind nur in einem nachgewiesenen FEHLER relevant.

    Erfolgreiche Analysen duerfen Quoten und Fehlerbehandlung besprechen.
    Manche CLI-Fassungen melden hingegen einen Fehler im JSON-Umschlag und
    beenden den Prozess trotzdem mit 0. Beides muss unterscheidbar bleiben.
    """
    import json

    if getattr(outcome, "cost_status", "") == "unknown":
        return "cost_recovery_required"
    if not outcome.ok:
        return provider_error_reason(outcome.text + " " + outcome.stderr_note,
                                     outcome.reason or "specialist_failed")
    try:
        if provider == "claude-code":
            data = json.loads(outcome.text)
            if (isinstance(data, dict) and data.get("type") == "result"
                    and (data.get("is_error") is True
                         or str(data.get("subtype", "")).startswith("error"))):
                detail = str(data.get("result", "")) + " " + str(data.get("errors", ""))
                return provider_error_reason(detail, "specialist_failed")
        elif provider == "codex":
            for line in outcome.text.splitlines():
                data = json.loads(line)
                if isinstance(data, dict) and data.get("type") in {"error", "turn.failed"}:
                    detail = str(data.get("message", "")) + " " + str(data.get("error", ""))
                    return provider_error_reason(detail, "specialist_failed")
    except (ValueError, TypeError):
        # Kein CLI-Fehlerbeleg. Der vorhandene Antwortparser benennt selbst,
        # wenn eine erfolgreiche Ausgabe das Antwortschema nicht einhaelt.
        pass
    return ""
