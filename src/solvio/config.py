"""Konfiguration des SOLVIO Core.

Werte kommen aus Umgebungsvariablen oder einer .env-Datei im Projektwurzel-
verzeichnis. Die .env liegt bewusst NICHT im Repository und enthaelt die
Geheimnisse; .env.example zeigt nur die Struktur.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Stand August 2026, verifiziert an der offiziellen Dokumentation unter
# developers.openai.com. gpt-realtime-2.1 ist allgemein verfuegbar, hat ein
# Kontextfenster von 128k und erlaubt einstellbaren Denkaufwand.
DEFAULT_REALTIME_MODEL = "gpt-realtime-2.1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Zugang zur Sprach-KI. Leer = noch nicht eingerichtet.
    openai_api_key: str = ""
    openai_realtime_model: str = DEFAULT_REALTIME_MODEL

    # Lokaler Dienst, spaeter Gegenstelle fuer den Voice Satellite.
    solvio_host: str = "127.0.0.1"
    solvio_port: int = 8765
    solvio_log_level: str = "INFO"

    # Home Assistant wird spaeter ausschliesslich ueber eine Tool-Schicht
    # angebunden, nicht als Teil von SOLVIO.
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    #: Entitaeten, die der Besitzer ausdruecklich als sicherheitsrelevant
    #: benennt (kommagetrennte `entity_id`s). Sie werden als HA_SECURITY
    #: klassifiziert, egal was Home Assistant ueber sie meldet — die einzige
    #: Antwort auf ein Garagenrelais, das als nackter `switch` exponiert ist.
    home_assistant_security_entities: str = ""

    #: Wie die Freigabepolitik wirkt (Approval Policy V2, ADR-0022).
    #:
    #: `shadow` rechnet die neue Entscheidung, protokolliert sie und laesst die
    #: alte Schwelle entscheiden — die Messphase des Migrationsplans.
    #: `enforce` laesst die Matrix entscheiden.
    #:
    #: Ein unbekannter Wert bedeutet `enforce`: eine vertippte Konfiguration
    #: darf keine stille Ruecknahme der Politik sein.
    approval_policy_mode: str = "enforce"

    #: Ob und wie der kognitive Router wirkt (Cognitive Router V1, ADR-0030).
    #:
    #: `off` — er existiert nicht. `solvio_task` wird nicht registriert, die
    #: vier Arbeitswerkzeuge stehen unveraendert vor dem Modell, und die
    #: Anweisung ist die von vorher. Das ist die Rollback-Lage.
    #: `shadow` — er misst. Die Oberflaeche bleibt unveraendert; nach jedem
    #: echten Werkzeugergebnis schreibt eine Einschaetzung auf, was er gewaehlt
    #: haette. Keine Wirkung, nur eine Zeile.
    #: `active` — er waehlt. `solvio_task` ist die eine Auftragsflaeche, die
    #: vier Chooser-Werkzeuge sind vor dem Modell verborgen (fuer den Core
    #: bleiben sie erreichbar).
    #:
    #: Ein unbekannter Wert bedeutet `off`. Die Polaritaet ist hier UMGEKEHRT
    #: zu `approval_policy_mode`, und das ist Absicht: dort waere ein
    #: Tippfehler eine stille Ruecknahme der Politik, hier waere er eine stille
    #: EINSCHALTUNG. Beide Male faellt der Schalter auf den Zustand zurueck,
    #: der nichts Neues behauptet.
    cognitive_router_mode: str = "off"

    # Planung und Ergebnisbewertung ueber den aktuell gebuchten CLI-Plan.
    # Leeres Modell nutzt die Vorgabe des Anbieters; keine feste Max-Stufe.
    # Unbekannter Anbieter bleibt unerreichbar, niemals API-Rueckfall.
    agent_runtime_subscription_provider: str = "codex"
    agent_runtime_subscription_model: str = ""

    # Recherche hat eine eigene Auswahl; keine Umschaltung von Planung oder
    # stiller Anbieterwechsel bei Quota. Claude nutzt seine native Modellvorgabe.
    agent_runtime_research_provider: str = "codex"

    # N3: Hermes' vorhandener nativer Codex-Transport. Eigenes natives Home
    # mit offizieller CLI-Anmeldung; keine kopierten Tokens oder API-Route.
    # Leer bleibt sichtbar nicht eingerichtet. N8 prueft die echten Pfade,
    # das verfuegbare Modell und die getrennte Zusatznutzungssperre.
    agent_runtime_hermes_python: str = ""
    agent_runtime_hermes_source: str = ""
    agent_runtime_hermes_codex_bin: str = ""
    agent_runtime_hermes_codex_home: str = ""
    agent_runtime_hermes_model: str = ""

    # Optional existing Hermes MCP/agent-browser runtime. Configure all three
    # paths together; the worker binds them to its task and invocation proof.
    agent_runtime_hermes_browser_python: str = ""
    agent_runtime_hermes_browser_bin: str = ""
    agent_runtime_hermes_browser_chrome: str = ""

    # N8/C4: der native Task-Arbeitsraum liegt fuer BEIDE Auftragsarbeiter
    # (Codex und Claude) unter derselben Wurzel, ausserhalb von `~/.solvio`
    # (dort versiegelt der Claude-Kaefig) und ausserhalb von `~/.solvio-nexus`
    # (natives Codex-Home mit auth.json). Nur so ist „Anbieterwechsel erhaelt
    # den Arbeitsraum" woertlich wahr. Der Claude-Kaefig liegt daneben, nie im
    # Arbeitsraum. Eine isolierte Testwelt legt beide Wurzeln unter ihren
    # eigenen Zustandsordner; die Vorgabe zeigt auf das Owner-Home.
    agent_runtime_task_workspace_root: str = "~/.solvio-tasks/workspaces"
    agent_runtime_claude_jail_root: str = "~/.solvio-tasks/claude-jails"

    # N8/C4: welcher Auftragsarbeiter einen NEUEN Auftrag bearbeitet
    # (`codex` | `claude-code`). Einmal beim Planen gelesen und am Lauf
    # festgeschrieben; eine spaetere Aenderung wechselt keinen offenen
    # Auftrag. Ohne Owner-Entscheidung bleibt es Codex.
    agent_runtime_task_worker: str = "codex"

    # N8/C4: der Kontingenttopf-Nachweis fuer den Claude-Arbeiter. Leer heisst
    # KEINE Nullquote — Claude wird nicht angeboten, bis der Nachweis vorliegt.
    # `live-proof` schaltet genau einen vom Owner freigegebenen Nachweisturn
    # frei; danach steht hier der SHA-256 der Belegdatei
    # `docs/plan/evidence/n8-c4-claude-pot-proof.json`, die Konto und
    # Tresorversion bindet und mit beiden verfaellt.
    agent_runtime_claude_worker_pot_proof: str = ""

    # Google Kalender (OAuth2-Refresh-Token). Ohne diese drei Werte ist die
    # Kalenderfaehigkeit schlicht nicht registriert — kein Halbzustand.
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_refresh_token: str = ""
    google_calendar_id: str = "primary"

    # Audio-Satellit (Raspberry Pi).
    voice_satellite_host: str = ""

    # N4: persoenliche Extraktion ueber den vorhandenen Abo-Antwortweg,
    # ohne Werkzeuge und nur mit gebundenem Kostenbeleg. Leeres Modell nimmt
    # die konfigurierte native Vorgabe, keinen fest verdrahteten API-Tarif.
    adaptive_memory_enabled: bool = True
    adaptive_memory_provider: str = "codex"
    adaptive_memory_model: str = ""

    # Telefonie (Telephony Capability V1). Hier stehen ausschliesslich
    # KENNUNGEN — der Anbieterschluessel liegt im Tresor unter
    # `secret://elevenlabs/agents-api-key` und kommt nie durch diese Datei.
    #
    # Die Vorgabe ist leer, und das ist die Schranke: ohne beide Kennungen
    # meldet `has_telephony` false, die Faehigkeit wird nicht registriert, und
    # ein Modell sieht sie gar nicht erst. Eine halb eingerichtete Telefonie
    # soll nicht anrufen koennen.
    telephony_agent_id: str = ""
    telephony_phone_number_id: str = ""

    #: Wie lange ein einzelnes Gespraech hoechstens dauern darf, in Sekunden.
    #: Dieselbe Grenze steht zusaetzlich beim Anbieter am Agenten — zwei
    #: Schranken, weil die Kosten an der Minute haengen und eine einzelne
    #: Fehlkonfiguration sie nicht aufheben soll.
    telephony_max_duration_secs: int = 180

    #: Der Ausschalter. Auch bei vollstaendiger Konfiguration bleibt Telefonie
    #: aus, solange das hier nicht ausdruecklich gesetzt ist.
    telephony_enabled: bool = False

    @property
    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def has_realtime(self) -> bool:
        return self.has_openai_key and bool(self.openai_realtime_model.strip())

    @property
    def has_home_assistant(self) -> bool:
        return bool(self.home_assistant_url.strip() and self.home_assistant_token.strip())

    @property
    def has_google_calendar(self) -> bool:
        return bool(self.google_calendar_client_id.strip()
                    and self.google_calendar_client_secret.strip()
                    and self.google_calendar_refresh_token.strip())

    @property
    def has_voice_satellite(self) -> bool:
        return bool(self.voice_satellite_host.strip())

    @property
    def has_telephony(self) -> bool:
        """Drei Bedingungen, alle noetig. Fehlt eine, gibt es keine Telefonie.

        Der Schalter steht bewusst NEBEN den Kennungen und nicht statt ihrer:
        so laesst sich Telefonie abschalten, ohne die Einrichtung zu verlieren,
        und eine unvollstaendige Einrichtung kann nicht versehentlich anrufen.
        """
        return bool(self.telephony_enabled
                    and self.telephony_agent_id.strip()
                    and self.telephony_phone_number_id.strip())

    def masked(self, value: str) -> str:
        """Gibt niemals den Wert zurueck, nur ob und wie lang er ist."""
        v = value.strip()
        return f"gesetzt ({len(v)} Zeichen, maskiert)" if v else "leer"


def load_settings() -> Settings:
    return Settings()
