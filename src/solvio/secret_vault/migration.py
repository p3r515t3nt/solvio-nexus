"""Was aus `.env` in den Tresor gehoert — und was ausdruecklich nicht.

Die Linie verlaeuft nicht zwischen „geheim" und „nicht geheim", sondern zwischen
**Zugang zu einer fremden Sache** und **der eigenen Identitaet** (ADR-0024). Ein
Anbieterschluessel gehoert nicht SOLVIO, er ist widerrufbar, und der Anbieter
kann ihn tauschen — der gehoert in den Tresor. SOLVIOs Signierschluessel gehoert
SOLVIO, es darf ihn genau einmal geben, und eine zweite Kopie waere eine zweite
Autoritaet — der gehoert nirgendwohin ausser dorthin, wo er ist.

Vier Klassen, dieselben wie im Sicherungsbestand
(`solvio/storage/inventory.py`), damit es nicht zwei Einteilungen gibt:

* **A** — Anbieterzugang, widerrufbar. Gehoert in den Tresor.
* **B** — betriebssystemgebunden (Schluesselbund, Secure Enclave). Bleibt dort.
* **C** — heute Datei, spaeter Tresor. Der Plan steht, der Umzug nicht.
* **D** — SOLVIOs eigene Autoritaet. Wird nie exportiert und nie eingelagert.

Und eine fuenfte Sorte, die man leicht mitnimmt und nicht darf: **Konfiguration**.
`HOME_ASSISTANT_URL`, `GOOGLE_CALENDAR_ID`, `SOLVIO_PORT` sind keine
Geheimnisse. Sie in den Tresor zu legen macht den Tresor groesser und den Start
zerbrechlicher, ohne irgendetwas zu schuetzen.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from solvio.secret_vault.policy import ExecutorId, SecretKind

#: Die Faehigkeitsnamen, die den Home-Assistant-Token benutzen duerfen.
#:
#: Ausgeschrieben und nicht als Praefix. Eine neue HA-Faehigkeit wird damit
#: verweigert, bis jemand sie hier eintraegt — und genau das ist gewollt:
#: vergessen kostet Reibung, nie Sicherheit. `home_assistant_health` und
#: `home_assistant_backup` sind keine Modell-Faehigkeiten, sondern die Namen,
#: unter denen die Gesundheitspruefung und der Sicherungsjob ihren Vorgang
#: anmelden.
HA_CAPABILITIES: tuple[str, ...] = (
    "ha_list_devices", "ha_get_state", "ha_turn_on", "ha_turn_off",
    "ha_set_brightness", "home_assistant_health", "home_assistant_backup",
)

#: Dieselbe Google-Anmeldung traegt Kalender UND Mail.
GOOGLE_CAPABILITIES: tuple[str, ...] = (
    "calendar_list_events", "calendar_create_event", "calendar_update_event",
    "calendar_delete_event",
    "calendar_get_event", "calendar_search_events", "calendar_find_availability",
    "gmail_list_recent", "gmail_search", "gmail_read_message",
    "gmail_read_thread", "gmail_create_draft", "gmail_send_draft",
    # Document Capability V1 (2026-09-03): ruft denselben Gmail-Zugang ueber
    # dieselbe Naht auf (`.search()`, `.message()`, `.attachment()`). Ohne
    # diese beiden Namen verweigert der Tresor mit `capability_not_allowed` --
    # gemessen live, mit einer kalten Gmail-Instanz ohne zwischengespeichertes
    # Zugriffstoken. Ein bereits WARMES Token in einer laengst laufenden
    # gemeinsamen Instanz umgeht die Pruefung unbemerkt (`_token()` liest den
    # Cache, bevor `_credentials()` je den Tresor fragt) -- das ist der Grund,
    # warum der Fehlschlag nicht sofort auffiel, und ein eigener Befund
    # (DEBT-0193), keine Rechtfertigung, hier nichts zu aendern.
    "document_find", "document_ask",
)

CLASS_VAULT = "A"
CLASS_OS_BOUND = "B"
CLASS_LATER = "C"
CLASS_NEVER = "D"
CLASS_CONFIG = "config"


@dataclass(frozen=True)
class Plan:
    """Ein geheimnisbehafteter Ort und was mit ihm geschieht. NIE ein Wert."""

    env_name: str
    secret_ref: str
    kind: SecretKind
    secret_class: str
    display_name: str
    service_label: str
    account_label: str
    allowed_capabilities: tuple[str, ...]
    allowed_targets: tuple[str, ...]
    allowed_executors: tuple[ExecutorId, ...]
    allow_background: bool
    why: str


#: Die Anbieterzugaenge aus `.env`, die der Tresor uebernimmt.
#:
#: `allow_background=True` steht bei allen dreien mit Grund: Zeitplan und
#: proaktiver Lauf benutzen genau diese Zugaenge (Kalenderabgleich,
#: Hausbeobachtung, Zusammenfassung). Das ist NICHT dasselbe wie „eine
#: Hintergrundaufgabe darf handeln" — ob die HANDLUNG erlaubt ist, entscheidet
#: unveraendert die Matrix aus ADR-0022. Der Tresor sagt nur, dass das
#: Geheimnis fuer diesen Weg vorgesehen ist.
ENV_PLANS: tuple[Plan, ...] = (
    Plan(
        env_name="HOME_ASSISTANT_TOKEN",
        secret_ref="secret://home-assistant/core",
        kind=SecretKind.API_TOKEN,
        secret_class=CLASS_VAULT,
        display_name="Home Assistant",
        service_label="Home Assistant",
        account_label="Langzeit-Token",
        allowed_capabilities=HA_CAPABILITIES,
        allowed_targets=(),      # zur Laufzeit aus HOME_ASSISTANT_URL gefuellt
        allowed_executors=(ExecutorId.HOME_ASSISTANT,),
        allow_background=True,
        why="Haussteuerung, Gesundheitspruefung und das Abholen der HA-Sicherung.",
    ),
    Plan(
        env_name="GOOGLE_CALENDAR_CLIENT_SECRET",
        secret_ref="secret://google/oauth-client",
        kind=SecretKind.OAUTH_CLIENT_SECRET,
        secret_class=CLASS_VAULT,
        display_name="Google — Anwendungsgeheimnis",
        service_label="Google",
        account_label="OAuth-Client",
        allowed_capabilities=GOOGLE_CAPABILITIES,
        allowed_targets=("https://oauth2.googleapis.com",),
        allowed_executors=(ExecutorId.HTTP,),
        allow_background=True,
        why="Erneuert die Kalender- und Mail-Anmeldung. Ohne ihn laeuft der Token ab.",
    ),
    Plan(
        env_name="GOOGLE_CALENDAR_REFRESH_TOKEN",
        secret_ref="secret://google/refresh",
        kind=SecretKind.OAUTH_REFRESH_TOKEN,
        secret_class=CLASS_VAULT,
        display_name="Google — Anmeldung",
        service_label="Google",
        account_label="Kalender und Mail",
        allowed_capabilities=GOOGLE_CAPABILITIES,
        allowed_targets=("https://oauth2.googleapis.com",),
        allowed_executors=(ExecutorId.HTTP,),
        allow_background=True,
        why="Die eigentliche Anmeldung. Laeuft heute alle sieben Tage ab (DEBT-0043).",
    ),
)


#: Was in `.env` bleibt, weil es Konfiguration ist und kein Geheimnis.
CONFIG_KEYS: tuple[str, ...] = (
    "SOLVIO_HOST", "SOLVIO_PORT", "SOLVIO_LOG_LEVEL",
    "HOME_ASSISTANT_URL", "GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_ID",
    "ADAPTIVE_MEMORY_ENABLED", "APPROVAL_POLICY_MODE",
)

#: Was in `.env` bleibt, OBWOHL es ein Geheimnis ist — mit ausgeschriebenem Grund.
#:
#: Das ist der unangenehmste Eintrag dieses Milestones, und er steht hier statt
#: in einer Fussnote, weil er eine Architekturaussage ist.
#:
#: `OPENAI_API_KEY` wandert NICHT in den Tresor. Nicht aus Bequemlichkeit,
#: sondern weil der Tresor ihn nicht schuetzen koennte: `deep/executor.py:125`
#: schreibt genau diesen Wert beim Bereitstellen des Hermes-Kaefigs in dessen
#: eigene `.env`, weil Hermes ein Sprachmodell braucht und keinen eigenen
#: Anbieterzugang hat. Ein Geheimnis, das ohnehin als Datei im Kaefig landet,
#: gewinnt nichts dadurch, dass es zusaetzlich verschluesselt daneben liegt —
#: es gewinnt nur einen zweiten Ort, an dem es veralten kann.
#:
#: Dazu kommt ein Preis, den die Wanderung kosten wuerde: der Core braucht
#: diesen Schluessel beim START, um die Sprachverbindung zu oeffnen, und sein
#: Start ist fail-closed. Ein gesperrter Schluesselbund wuerde damit aus „kein
#: Haus, kein Kalender" ein „SOLVIO ist stumm" machen. Das ist ein
#: Produktrueckschritt fuer einen Vertraulichkeitsgewinn von null.
#:
#: Die frueher aufgezeichnete Bedingung lautete: „wandert, sobald Hermes einen
#: EIGENEN Anbieterzugang hat". Sie ist **nicht eingetreten** — und sie wird es
#: auch nicht. Provider Broker V1 hat den anderen der beiden von der Roadmap
#: sanktionierten Wege genommen: den Weg ueber den Core. Hermes bekam keinen
#: eigenen Zugang, sondern gar keinen mehr. Die Bedingung wird hier deshalb als
#: nicht eingetreten vermerkt statt still durch eine andere ersetzt.
#:
#: Die HAELFTE des alten Grundes, die ueberlebt: gegen einen Ausbruch mit
#: derselben uid aendert eine Wanderung nichts — wer Code im Core ausfuehren
#: kann, IST der Core, und der Tresor benennt diese Grenze selbst.
#:
#: Die Nachfolgebedingung: die Wanderung gehoert in den Tresor-Nachgang, sobald
#: geklaert ist, was ein fail-closed Start bei GESPERRTEM Schluesselbund tun
#: soll. Der Zielentwurf steht schon fest — eine NEUE, enge
#: `ExecutorId.PROVIDER_BROKER` auf das eine Modul
#: `solvio.provider_broker.upstream`, nach dem Vorbild von `PAYMENT`. Die
#: bestehende `ExecutorId.PROVIDER` taugt dafuer nicht: ihre Praefixe schliessen
#: das breite `solvio.deep.` ein.
STAYS_IN_ENV: tuple[tuple[str, str], ...] = (
    ("OPENAI_API_KEY",
     "Der Core braucht ihn beim fail-closed Start fuer die Sprachverbindung; "
     "ohne ihn ist SOLVIO stumm. Seit Provider Broker V1 erreicht er den "
     "Hermes-Kaefig NICHT mehr — der bekommt einen kurzlebigen Broker-Token. "
     "Wandert im Tresor-Nachgang ueber eine neue, enge "
     "ExecutorId.PROVIDER_BROKER, sobald der gesperrte Schluesselbund beim "
     "Start geklaert ist."),
)


@dataclass(frozen=True)
class Elsewhere:
    """Ein geheimnisbehafteter Ort, der NICHT in den Tresor wandert."""

    what: str
    secret_class: str
    why: str
    plan: str


#: Die ehrliche Gegenliste. Sie ist so wichtig wie die Umzugsliste, weil sie der
#: Grund ist, warum der Tresor nicht zu einer einzigen Stelle wird, an der sich
#: SOLVIOs Autoritaet erzeugen laesst.
STAYS_ELSEWHERE: tuple[Elsewhere, ...] = (
    Elsewhere(
        what="~/.solvio-approvals-production/core_signing_key.pem",
        secret_class=CLASS_NEVER,
        why="SOLVIOs eigener Ausweis. Es darf ihn genau einmal geben (ADR-0024).",
        plan="Bleibt, wo er ist. Kein Export, keine Einlagerung, keine Sicherung.",
    ),
    Elsewhere(
        what="~/.solvio-approvals-production/gateway_key.pem",
        secret_class=CLASS_NEVER,
        why="Privater Schluessel des Freigabe-Gateways; das iPhone pinnt sein Zertifikat.",
        plan="Bleibt, wo er ist. Eine Rotation erzwingt seit jeher eine Neukopplung.",
    ),
    Elsewhere(
        what="App-Attest-Material in der Secure Enclave des iPhones",
        secret_class=CLASS_OS_BOUND,
        why="Hardware-Identitaet. Keine Datei kann sie tragen.",
        plan="Neuanmeldung mit Face ID. So ist es vorgesehen.",
    ),
    Elsewhere(
        what="Schluesselbund de.solvio.vault/kek-v1 (Hauptschluessel des Tresors)",
        secret_class=CLASS_OS_BOUND,
        why="Der Schluessel zum Tresor gehoert nicht in den Tresor.",
        plan="Betriebssystemgebunden. Der Wiederherstellungsumschlag ist der zweite Weg.",
    ),
    Elsewhere(
        what="Schluesselbund de.solvio.portal-vault/master-key (Portaltresor)",
        secret_class=CLASS_OS_BOUND,
        why="Der aeltere, engere Portaltresor bleibt, wie er freigegeben wurde.",
        plan="Siehe DEBT — Zusammenlegung erst, wenn dort ein echter Zugang liegt.",
    ),
    Elsewhere(
        what="~/.ssh/{raspi_claude,solvio_satellite,solvio_deploy}",
        secret_class=CLASS_LATER,
        why=("Private Schluesselbytes gehoeren nicht in einen Speicher, den ein "
             "Prozess auslesen kann. Der Betriebssystem-Agent macht es besser."),
        plan=("ssh-agent bzw. Schluesselbund-gebundene Nutzung. Kein SOLVIO-Code "
              "liest heute Schluesselbytes; das bleibt so."),
    ),
    Elsewhere(
        what="~/.solvio/satellite_auth.json",
        secret_class=CLASS_LATER,
        why=("Der Core prueft damit den Satelliten BEIM START. Ein Tresor, der "
             "beim Start gesperrt sein kann, gehoert nicht in diesen Pfad."),
        plan=("Bleibt Datei mit Rechten 0600. Ein Umzug braucht zuerst eine "
              "Antwort darauf, was ein gesperrter Schluesselbund beim Start tut."),
    ),
    Elsewhere(
        what="~/.solvio-hermes/api_key",
        secret_class=CLASS_LATER,
        why=("Er liegt IM Kaefig, weil Hermes ihn selbst braucht. Ihn in den "
             "Tresor zu legen hiesse, dem Kaefig den Tresor zu oeffnen."),
        plan=("Bleibt im Kaefig. Hermes bekommt weiterhin kein SOLVIO-Geheimnis "
              "und keinen Tresorpfad."),
    ),
    Elsewhere(
        what="~/solvio-node-pki (CA und Clientzertifikate der Knoten)",
        secret_class=CLASS_LATER,
        why="Heute unbenutzt. Ein Umzug ohne Nutzung waere Bewegung ohne Gewinn.",
        plan="Wenn die Knoten in Betrieb gehen, mit ihnen zusammen entscheiden.",
    ),
)


def env_path() -> str:
    from solvio.storage import inventory
    return os.path.join(inventory.CORE_REPO, ".env")


def env_key_names(path: str | None = None) -> list[str]:
    """Die SCHLUESSELNAMEN einer `.env`. Liest nie einen Wert in ein Ergebnis."""
    target = path or env_path()
    names: list[str] = []
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                names.append(stripped.split("=", 1)[0].strip())
    except OSError:
        return []
    return names


def leftover_plaintext(path: str | None = None,
                       refs: set[str] | None = None) -> list[str]:
    """Welche Zugaenge gibt es ZWEIMAL — im Tresor UND als Klartext daneben?

    Das ist die Frage, die eine Wanderung ehrlich macht: solange ein Zugang an
    beiden Orten steht, ist die zweite Kopie die, die niemand pflegt.

    `refs` sind die Verweise, die der Tresor wirklich fuehrt. Ohne sie waere
    diese Funktion falsch herum: sie meldete jeden GEPLANTEN Zugang als
    Dublette, auch einen, der noch gar nicht gewandert ist. Ein Plan ist keine
    Dublette — er ist ein Plan.
    """
    names = set(env_key_names(path))
    if refs is None:
        try:
            from solvio.secret_vault.store import VaultStore, db_path
            refs = set(VaultStore(db_path()).refs()) if os.path.exists(db_path()) else set()
        except Exception:  # noqa: BLE001 - ohne Tresor gibt es keine Dublette
            refs = set()
    return sorted(p.env_name for p in ENV_PLANS
                  if p.env_name in names and p.secret_ref in refs)
