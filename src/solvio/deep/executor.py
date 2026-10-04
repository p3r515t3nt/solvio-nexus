"""Den Executor einrichten, starten — und ihm nicht glauben.

Hermes bringt eine grosse Flaeche mit: Terminal, Dateien, Browser, Bildschirm,
Nachrichtendienste, Home Assistant. Nichts davon gehoert in diese Stufe. Was
bleibt, ist Websuche und Textextraktion, also genau das, was Recherche braucht
und sonst nichts.

Die Haltung wird an drei Stellen durchgesetzt, und das ist kein Guertel-und-
Hosentraeger-Reflex, sondern die Lehre aus dem ersten Versuch:

1. `platform_toolsets.api_server: [web]` benennt, was gelten soll.
2. `agent.disabled_toolsets` streicht alles Uebrige noch einmal ausdruecklich,
   weil diese Liste **nach** der ersten angewandt wird.
3. Beim Start fragt SOLVIO den laufenden Prozess, was er tatsaechlich anbietet,
   und weigert sich zu arbeiten, wenn dort mehr steht als `web`.

Punkt 3 ist nicht der Zierrat: der erste Aufbau hier bestand Punkt 1 und meldete
danach `bfl` — eine Videogruppe — als aktiv. Die Konfiguration sagte das eine,
der Prozess tat das andere. Seitdem gilt die Selbstauskunft des laufenden
Prozesses und nicht die Datei, und eine unbekannte neue Werkzeuggruppe in einer
kuenftigen Hermes-Version wird SOLVIO zum Stehen bringen statt stillschweigend
durchzurutschen.

Zur Freigabe: Hermes hat ein eigenes Nachfragesystem. Es wird auf `manual`
gestellt und laeuft bei Zeitablauf auf Verweigerung. Das ist ausdruecklich
**nicht** SOLVIOs Freigabeweg — es ist eine zweite Tuer, die zufaellt, falls die
erste je offenstehen sollte. Autoritaet liegt weiterhin ausschliesslich beim
iPhone.

Zum Modellzugang: Hermes bekommt **keinen** Anbieterschluessel mehr. Bis Provider
Broker V1 stand in dieser `.env` der echte Schluessel des Cores — derselbe, der
die Sprachschicht oeffnet, und lesbar fuer jeden Prozess im Gefaengnis. Was jetzt
dort steht, ist ein undurchsichtiger Broker-Token: er oeffnet keinen Anbieter,
er oeffnet den Broker des Cores auf der Rueckschleife, und auch den nur, solange
der Core fuer das Gateway ein Lease offen haelt. Faellt das letzte Lease, wird
der Token neu gepraegt und der alte ist `401`.

Der Anbietername bleibt `openai-api`. Das ist tragend und keine Gewohnheit: die
Aufloeser fuer `auto`, `custom` und `openrouter` sehen `OPENAI_BASE_URL` gar
nicht an — ein Wechsel des Namens schaltete die Umleitung still ab und schickte
den Kaefig wieder direkt zum Anbieter.

Home Assistant, Kalender, Gmail, die Freigabedatenbank und das Gedaechtnis
erreicht er weiterhin weder ueber Anmeldedaten noch ueber das Netz.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from solvio.deep import isolation
from solvio.deep.hermes import ALLOWED_TOOLSETS, HermesClient
from solvio.logging_setup import get_logger

log = get_logger("deep")

#: Alles, was der Executor NICHT haben darf. Ausdruecklich aufgezaehlt statt
#: „alles ausser web", weil eine Aufzaehlung im Diff sichtbar ist und eine
#: Negation nicht. Fuer alles, was hier fehlt, greift die Startpruefung.
DENIED_TOOLSETS = (
    "bfl", "browser", "code_execution", "coding", "computer_use", "context_engine",
    "cronjob", "debugging", "delegation", "desktop_ui", "discord", "discord_admin",
    "feishu_doc", "feishu_drive", "file", "homeassistant", "image_gen", "kanban",
    "memory", "project", "safe", "search", "session_search", "skills", "spotify",
    "terminal", "todo", "tts", "video", "video_gen", "vision", "x_search", "yuanbao",
)

_CONFIG = """# Von SOLVIO geschrieben. Haendische Aenderungen werden beim Start ueberschrieben.
# Hermes ist hier Ausfuehrender, nicht Entscheider.
approvals:
  mode: manual
  timeout: 30
  cron_mode: deny
  single_query_mode: deny
platform_toolsets:
  api_server:
{allowed}
agent:
  # EIN Versuch je Modellaufruf. Hermes klemmt auf `max(x, 1)`, und seine
  # Schleife laeuft `while retry_count < max_retries` — 1 heisst also genau ein
  # Versuch, ohne Wiederholung (agent/agent_init.py:2048-2053,
  # agent/conversation_loop.py:2801-2810 im angehefteten Stand).
  #
  # Ohne diese Zeile gilt Hermes' Vorgabe 3, und der aeussere Loop wiederholte
  # eine nicht wiederholbare Ablehnung: gemessen am 2026-08-29 drei
  # byte-identische Anfragen im Abstand von ~2,2 s und ~5,7 s gegen ein
  # `429 token_capped` des Brokers. Kosten: null Anbieter-Token, aber drei
  # Buchzeilen und ein Retry-Sturm, den niemand bestellt hat. Eine Ablehnung
  # aus Kappe, Aufgabenbudget oder Doppelung erholt sich durch Zeit, nie durch
  # Wiederholung.
  api_max_retries: 1
  disabled_toolsets:
{denied}
# Die Modell-Kontextgroesse ist kein Aufgabenbudget. Der Live-Lauf vom
# 9.9. wuchs bis 112388 Eingabetoken je Aufruf, ohne die native Schwelle
# zu erreichen. Hermes verdichtet jetzt frueher mit seinem eigenen Weg.
compression:
  enabled: true
  threshold_tokens: 24000
  # Lean bewahrt vor dem Entfernen grosser Werkzeugantworten deren
  # Originaltext fuer native, quellenhaltige Abschnittszusammenfassungen.
  tail_mode: lean
  protect_first_n: 1
  protect_last_n: 4
  target_ratio: 0.25
  abort_on_summary_failure: true
auxiliary:
  # Auch die Verdichtung bleibt beim bestehenden Broker-Zugang und Lease.
  # Keine Wiederholung einer nicht wiederholbaren Budgetablehnung.
  transient_retries: 0
  compression:
    provider: auto
    model: auto
  title_generation:
    enabled: false
memory:
  memory_enabled: false
  user_profile_enabled: false
terminal:
  backend: local
"""


@dataclass(frozen=True)
class ExecutorConfig:
    """Wo der Executor wohnt und womit er redet."""

    jail: str
    venv_bin: str
    python_root: str
    host: str = "127.0.0.1"
    port: int = 8791
    model: str = "gpt-5.4-mini"
    provider: str = "openai-api"
    broker_port: int = isolation.BROKER_PORT

    @property
    def broker_base_url(self) -> str:
        """Was in die `.env` des Gefaengnisses geht.

        Der angeheftete Hermes haengt an diesen Wert selbst `/models` an und
        akzeptiert blankes HTTP auf der Rueckschleife — der LM-Studio-Fall ist
        genau dieser. Das `/v1` gehoert dazu.
        """
        return f"http://127.0.0.1:{self.broker_port}/v1"

    @property
    def home(self) -> str:
        return os.path.join(self.jail, "home")

    @property
    def work(self) -> str:
        return os.path.join(self.jail, "work")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class PostureViolation(RuntimeError):
    """Der Executor bietet mehr an, als er darf. Dann arbeitet er gar nicht."""


def provision(config: ExecutorConfig, *, api_key: str, broker_token: str) -> None:
    """Schreibt die Haltung in das Zuhause des Executors.

    `broker_token` ist **kein** Anbieterzugang. Er steht in der Zeile, die der
    angeheftete Hermes fuer einen Anbieterschluessel haelt, weil dessen
    Aufloeser einen gewoehnlichen Schluesselplatz verlangt — gegen
    `api.openai.com` bekaeme er `401`.

    Die Datei wird **vollstaendig** neu geschrieben. Das ist genau die Form, die
    die Rotation braucht: der Broker meldet einen frischen Token, und diese
    Funktion setzt ihn samt der Nicht-Schluesselzeilen (`API_SERVER_*`) neu.
    """
    os.makedirs(config.home, mode=0o700, exist_ok=True)
    os.makedirs(config.work, mode=0o700, exist_ok=True)

    body = _CONFIG.format(
        allowed="\n".join(f"    - {name}" for name in sorted(ALLOWED_TOOLSETS)),
        denied="\n".join(f"    - {name}" for name in DENIED_TOOLSETS))
    with open(os.path.join(config.home, "config.yaml"), "w", encoding="utf-8") as handle:
        handle.write(body)

    env_path = os.path.join(config.home, ".env")
    previous = os.umask(0o077)
    try:
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write(f"OPENAI_API_KEY={broker_token}\n")
            handle.write(f"OPENAI_BASE_URL={config.broker_base_url}\n")
            handle.write("API_SERVER_ENABLED=true\n")
            handle.write(f"API_SERVER_HOST={config.host}\n")
            handle.write(f"API_SERVER_PORT={config.port}\n")
            handle.write(f"API_SERVER_KEY={api_key}\n")
    finally:
        os.umask(previous)
    os.chmod(env_path, 0o600)
    log.info("deep.executor_provisioned", toolsets=len(ALLOWED_TOOLSETS),
             denied=len(DENIED_TOOLSETS), approvals="manual",
             provider="broker", broker_port=config.broker_port)


async def start(config: ExecutorConfig) -> isolation.SandboxedProcess:
    """Startet den Executor im Gefaengnis."""
    return await isolation.launch(
        [os.path.join(config.venv_bin, "hermes"), "gateway", "run"],
        jail=config.jail, hermes_home=config.home,
        python_root=config.python_root,
        log_path=os.path.join(config.work, "executor.log"),
        broker_port=config.broker_port, gateway_port=config.port)


async def assert_posture(client: HermesClient) -> list[str]:
    """Fragt den laufenden Prozess, was er kann — und haelt ihn beim Wort.

    Wirft, statt zu warnen. Ein Executor mit einem Terminal ist kein etwas
    zu grosszuegig eingestellter Executor, sondern ein anderes Produkt.
    """
    violations = await client.surface_violations()
    if violations:
        log.error("deep.posture_violation", extra=",".join(violations))
        raise PostureViolation(",".join(violations))
    return await client.enabled_toolsets()
