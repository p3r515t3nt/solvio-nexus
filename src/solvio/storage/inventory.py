"""Der Bestand — was gesichert wird, und warum genau das.

Dieses Modul ist eine LISTE, keine Mechanik. Es beantwortet eine einzige Frage:
welche Dinge muss man haben, um SOLVIO nach einem Totalverlust der internen
Platte wieder aufzubauen, und welche ausdruecklich nicht?

Drei Leitsaetze, die die Liste erklaeren
---------------------------------------

**Git ist keine Sicherung.** Quelltext und Verlauf liegen bei GitHub und im
Arbeitsbaum. Was NICHT dort liegt, ist der Betriebszustand — und genau der ist
SOLVIO. Ein Buendel der Repositories kommt trotzdem mit, weil ein GitHub-Konto
auch verloren gehen kann.

**Nicht alles im Heimverzeichnis verdient eine Sicherung.** Ein `venv`, ein
`__pycache__`, ein 207-MB-Xcode-Bauverzeichnis und eine `.xcodeproj`, die aus
`project.yml` erzeugt wird, sind neu herstellbar. Sie zu sichern kostet Platz
und erzeugt den falschen Eindruck, man haette etwas gerettet.

**Autoritaet wird nicht kopiert.** Das ist die schaerfste Regel hier, und sie
kostet Bequemlichkeit. Die privaten Schluessel, mit denen sich der Core als der
Core ausweist (`core_signing_key.pem`, `gateway_key.pem`), gehen NICHT auf die
externe Platte. Wer sie mitnimmt, hat SOLVIOs Ausweis zweimal, und einer der
beiden liegt in einer Schublade. Ein Anbieterschluessel in `.env` ist etwas
anderes: er gehoert nicht SOLVIO, er ist widerrufbar, und der Anbieter kann ihn
tauschen. Die Folge steht ehrlich im Wiederherstellungsplan: nach einem
Mac-Verlust muss das iPhone neu angemeldet werden. Das ist ein Face-ID-Vorgang,
kein Datenverlust.

Vier Klassen fuer alles Geheimnisbehaftete
------------------------------------------
* **A — in die verschluesselte Sicherung.** Anbieterzugaenge, widerrufbar.
* **B — betriebssystemgebunden, neu zu erzeugen.** Schluesselbund, Secure
  Enclave, App-Attest. Eine Datei kann Hardware-Identitaet nicht sichern.
* **C — gehoert spaeter in den Geheimnistresor.** Heute Datei, morgen Alias.
* **D — wird nie exportiert.** SOLVIOs eigene Autoritaet.
"""
from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass, field
from typing import Iterable

#: **Die Wurzel, unter der der Bestand gesucht wird. Normalerweise keine.**
#:
#: DEBT-0228: die Offsite-Suiten bauten ihren Sicherungssatz aus DIESER Liste —
#: und damit aus dem produktiven Bestand. Sie hatten nur zwei Ausgaenge: die
#: laufenden Datenbanken des Cores als Fixture lesen, oder durchfallen. Ein
#: angehaltener Dienst haette daran nichts geaendert; eine reale Datenbank wird
#: nicht dadurch zur Testdatei, dass gerade niemand sie benutzt.
#:
#: Diese Wurzel ist der Ausweg, und sie ist bewusst die kleinste moegliche
#: Naht: dieselbe Liste, dieselben Pfade, nur ein anderer Anfang. Sie ersetzt
#: keinen Pfad und blendet keinen aus — auch die absoluten
#: (`/Users/solvio/solvio-core/.env`) wandern mit, denn genau die waren unter
#: einem umgeleiteten `HOME` noch produktiv.
#:
#: **Ein falsch gesetzter Schalter kann keine stille Luecke erzeugen.** Zeigt er
#: ins Leere, fehlen die Pflichtstuecke, und der Sicherungslauf endet rot — die
#: Vollstaendigkeitspruefung ist genau dafuer da. Der Warnhinweis unten macht
#: es zusaetzlich sichtbar.
ROOT_ENV = "SOLVIO_INVENTORY_ROOT"

#: Die Wurzel des produktiven Core-Arbeitsbaums. Der Core laeuft direkt daraus.
CORE_REPO = os.path.expanduser("~/solvio-core")
IOS_REPO = os.path.expanduser("~/solvio-ios")

#: Die plist des laufenden Cores. Sie ist die Wahrheit darueber, welches
#: Verzeichnis produktiv ist — nicht ein Modulstandard und nicht ein Dokument.
CORE_AGENT_PLIST = os.path.expanduser(
    "~/Library/LaunchAgents/com.solvio.core.plist")

#: Wohin gegriffen wird, wenn die plist nichts sagt. Bewusst NICHT
#: `identity.DEFAULT_STATE_DIR`: dessen Standard zeigt auf ~/.solvio-approvals,
#: und das traegt eine ANDERE Kryptoidentitaet und ein aelteres Schema. Ein
#: Sicherungswerkzeug, das dort greift, sichert eine fremde Identitaet und
#: merkt es nie.
FALLBACK_APPROVAL_STATE_DIR = "~/.solvio-approvals-production"

SQLITE = "sqlite"
FILE = "file"
TREE = "tree"

#: Geheimnisklassen. Siehe Modulkopf.
CLASS_BACKUP = "A"          # in die verschluesselte Sicherung
CLASS_OS_BOUND = "B"        # betriebssystemgebunden, neu zu erzeugen
CLASS_FUTURE_VAULT = "C"    # gehoert in den kuenftigen Geheimnistresor
CLASS_NEVER = "D"           # wird nie exportiert


def approval_state_dir() -> str:
    """Welches Freigabeverzeichnis produktiv ist — aus dem, was wirklich laeuft.

    Gelesen wird die launchd-Datei des Cores, weil dort steht, womit der Prozess
    gestartet wurde. Das ist derselbe Grund, aus dem dieses Projekt Laufzeit vor
    Dokumentation stellt: die plist ist die Konfiguration, alles andere ist eine
    Beschreibung davon.
    """
    env = os.environ.get("SOLVIO_APPROVAL_STATE_DIR")
    if env:
        return os.path.expanduser(env)
    try:
        with open(CORE_AGENT_PLIST, "rb") as fh:
            data = plistlib.load(fh)
        value = (data.get("EnvironmentVariables") or {}).get(
            "SOLVIO_APPROVAL_STATE_DIR")
        if value:
            return os.path.expanduser(str(value))
    except (OSError, ValueError):
        pass
    return os.path.expanduser(FALLBACK_APPROVAL_STATE_DIR)


def core_env(name: str, fallback: str) -> str:
    """Der Wert von `name` — aus der eigenen Umgebung, sonst aus der Core-plist.

    Die Reihenfolge ist ausdruecklich diese und nicht die umgekehrte: wer die
    Variable dem laufenden Prozess mitgibt, meint sie auch (so laesst sich ein
    Sicherungslauf gezielt umlenken, und so testet man ihn). Erst wenn sie dort
    fehlt, wird die plist des Cores gefragt. `approval_state_dir()` macht es
    seit jeher genauso.

    Die erste Fassung dieses Satzes behauptete das Gegenteil — „nicht dieser
    Prozess" — und beschrieb damit eine Funktion, die es nicht gibt. Wer ihr
    geglaubt haette, haette sich auf einen Vorrang verlassen, den der Code nie
    hatte.

    Dieselbe Begruendung wie bei `approval_state_dir()`: die launchd-Datei des
    Cores ist die Konfiguration, alles andere ist eine Beschreibung davon.

    Warum das hier nicht optional ist: die Sicherung laeuft NICHT im
    Core-Prozess. `de.solvio.backup` ist ein eigener launchd-Agent, und der hat
    ueberhaupt keine `EnvironmentVariables`. Ein `os.environ.get()` in diesem
    Modul sieht daher nie, was dem Core gesetzt wurde — es liest die Umgebung
    des Sicherungslaufs. Genau so entstand der lautlose Verlust, den ein
    frueherer Kommentar an dieser Stelle auszuschliessen behauptete: das
    Inventar haette `~/.solvio/telephony.sqlite3` gesichert (nicht vorhanden,
    `required=False`, also stilles Achselzucken), waehrend der Core sein
    Anrufbuch ganz woanders fuehrt.
    """
    env = os.environ.get(name)
    if env:
        return os.path.expanduser(env)
    try:
        with open(CORE_AGENT_PLIST, "rb") as fh:
            data = plistlib.load(fh)
        value = (data.get("EnvironmentVariables") or {}).get(name)
        if value:
            return os.path.expanduser(str(value))
    except (OSError, ValueError):
        pass
    return os.path.expanduser(fallback)


@dataclass(frozen=True)
class Item:
    """Ein Ding im Bestand."""

    name: str
    kind: str
    source: str
    #: Zielpfad RELATIV zum Wurzelverzeichnis des Sicherungssatzes.
    dest: str
    category: str
    why: str
    #: Braucht dieses Ding eine nachgewiesen verschluesselte Platte?
    needs_encryption: bool = False
    #: Fehlt es, ist die Sicherung kaputt (True) oder nur unvollstaendig (False)?
    required: bool = True
    #: Verzeichnisnamen, die in einem Baum uebersprungen werden.
    exclude: tuple[str, ...] = ()
    secret_class: str | None = None

    @property
    def expanded(self) -> str:
        return under_root(os.path.expanduser(os.path.expandvars(self.source)))


def inventory_root() -> str:
    """Die gesetzte Wurzel — oder leer. Wird bei jedem Zugriff frisch gelesen,
    damit ein Test sie setzen kann, nachdem das Modul geladen ist."""
    return (os.environ.get(ROOT_ENV, "") or "").strip()


_ROOT_WARNED = False


def under_root(path: str) -> str:
    """Einen aufgeloesten Pfad unter die gesetzte Wurzel legen.

    Ohne Wurzel unveraendert. Mit Wurzel wird der ABSOLUTE Pfad angehaengt, so
    dass `/Users/solvio/.solvio/x` zu `<wurzel>/Users/solvio/.solvio/x` wird —
    die Struktur bleibt erhalten, und nichts kann versehentlich ausserhalb
    landen.
    """
    global _ROOT_WARNED
    wurzel = inventory_root()
    if not wurzel:
        return path
    if not _ROOT_WARNED:
        _ROOT_WARNED = True
        from solvio.logging_setup import get_logger
        get_logger("storage").warning("storage.inventory_root_redirected",
                                      root=wurzel)
    return os.path.join(wurzel, os.path.abspath(path).lstrip(os.sep))


@dataclass(frozen=True)
class Excluded:
    """Etwas, das bewusst NICHT gesichert wird. Der Grund gehoert dazu."""

    what: str
    secret_class: str
    reason: str
    recovery: str


# ---------------------------------------------------------------- ausgeschlossen
#: Was NICHT auf die Platte geht. Diese Liste ist so wichtig wie der Bestand
#: selbst — sie ist der Grund, warum der Wiederherstellungsplan nicht luegt.
EXCLUDED: tuple[Excluded, ...] = (
    Excluded(
        what="~/.solvio-approvals-production/core_signing_key.pem",
        secret_class=CLASS_NEVER,
        reason="SOLVIOs eigener Ausweis. Eine zweite Kopie in einer Schublade "
               "ist eine zweite Autoritaet.",
        recovery="Wird beim ersten Start neu erzeugt. Geraete melden sich neu an.",
    ),
    Excluded(
        what="~/.solvio-approvals-production/gateway_key.pem",
        secret_class=CLASS_NEVER,
        reason="Privater Schluessel des Freigabe-Gateways; das iPhone pinnt das "
               "zugehoerige Zertifikat.",
        recovery="Neu erzeugt; das iPhone nimmt das neue Zertifikat bei der "
                 "Neuanmeldung an.",
    ),
    Excluded(
        what="~/.ssh/* (private Schluessel)",
        secret_class=CLASS_FUTURE_VAULT,
        reason="Zugang zum Pi und zu Deploy-Zielen. In zwei Minuten neu erzeugt, "
               "und der Pi hat ohnehin Schluesselzugang zum Mac (DEBT-0003).",
        recovery="Neues Schluesselpaar erzeugen, oeffentlichen Teil auf dem Pi "
                 "hinterlegen.",
    ),
    Excluded(
        what="Schluesselbund-Eintrag de.solvio.vault/kek-v1 (Tresor-Hauptschluessel)",
        secret_class=CLASS_OS_BOUND,
        reason="Der Schluessel zum Tresor gehoert nicht in den Tresor — und nicht "
               "neben ihn auf dieselbe Platte. Er liegt im macOS-Schluesselbund.",
        recovery="Ueber den Wiederherstellungsumschlag `Vault/recovery.json` und "
                 "die Passphrase des Besitzers. Das ist der einzige zweite Weg.",
    ),
    Excluded(
        what="Wiederherstellungs-Passphrase des Tresors",
        secret_class=CLASS_NEVER,
        reason="Sie ist der zweite Faktor der Sicherung. Sie mitzusichern hiesse, "
               "beide Haelften in dieselbe Schublade zu legen.",
        recovery="Steht im Passwortmanager des Menschen. SOLVIO kennt sie nicht.",
    ),
    Excluded(
        what="Schluesselbund-Eintrag de.solvio.portal-vault/master-key",
        secret_class=CLASS_OS_BOUND,
        reason="Liegt im macOS-Schluesselbund, nicht als Datei. Genau das ist "
               "die Absicht (siehe portal/vault.py).",
        recovery="Portalzugaenge nach einem Mac-Verlust neu eingeben.",
    ),
    Excluded(
        what="App-Attest-Material auf dem iPhone (Secure Enclave)",
        secret_class=CLASS_OS_BOUND,
        reason="Hardware-Identitaet. Eine Datei kann sie nicht sichern.",
        recovery="Neuanmeldung des Geraets mit Face ID.",
    ),
    Excluded(
        what="Signaturzertifikate / Provisioning-Profile fuer iOS",
        secret_class=CLASS_OS_BOUND,
        reason="Gehoeren dem Entwicklerkonto und dem Schluesselbund, nicht "
               "diesem Repository.",
        recovery="Aus dem Apple-Developer-Konto neu laden.",
    ),
    Excluded(
        what="data/ha_entities.json",
        secret_class="",
        reason="Tot. Kein Codepfad liest die Datei; SOLVIO holt die Freigabeliste "
               "bei jedem Bedarf live aus Home Assistant. Ein Abzug vom 16.08. "
               "mit 119 Entitaeten, waehrend HA heute 126 fuehrt.",
        recovery="entfaellt — die Liste gehoert HA, nicht SOLVIO.",
    ),
    Excluded(
        what="~/.solvio-hermes/{src,venv,home/state.db,home/kanban.db}",
        secret_class="",
        reason="381 MB Installation und Arbeitszustand eines untrusted_executor. "
               "Sein Zustand ist ausdruecklich nicht kanonisch.",
        recovery="Hermes in der angehefteten Fassung neu installieren.",
    ),
    Excluded(
        what="~/solvio-ios/.build (207 MB) und SolvioApprovals.xcodeproj",
        secret_class="",
        reason="Bauprodukte. Das Projekt wird von XcodeGen aus project.yml "
               "erzeugt, und project.yml liegt in git.",
        recovery="xcodegen generate; swift build.",
    ),
    Excluded(
        what="~/solvio-core/.venv, __pycache__, ~/.cache",
        secret_class="",
        reason="Neu herstellbar aus uv.lock. Eine Sicherung davon taeuscht "
               "Wert vor.",
        recovery="uv sync.",
    ),
    Excluded(
        what="logs/core.out.log",
        secret_class="",
        reason="Waechst unbegrenzt (DEBT-0056) und enthaelt Betriebsspuren, "
               "keine Wahrheit, die man wiederherstellen muesste.",
        recovery="entfaellt",
    ),
    Excluded(
        what="das Zahlungsmittel beim Anbieter (Kartennummer, Pruefziffer, "
             "Wallet-Kryptogramm)",
        secret_class=CLASS_NEVER,
        reason="SOLVIO ist nicht der Kartentresor und will es nicht sein. Diese "
               "Werte existieren in keiner Datei dieses Rechners — sie liegen "
               "beim Anbieter bzw. beim Haendler. Was hier gesichert wird, sind "
               "undurchsichtige Verweise, die ohne den Anbieterzugang nichts "
               "koennen.",
        recovery="Der Eigentuemer hinterlegt die Karte erneut beim Anbieter. "
                 "SOLVIO bekommt danach einen neuen Token — nie die Karte.",
    ),
    # ---- N8/C4 (18.09.2026): die Arbeitsflaechen der nativen Auftragsarbeiter
    # ---- und der Nexus-Betriebsordner bekommen ihre Entscheidung.
    Excluded(
        what="~/.solvio-tasks (workspaces und claude-jails)",
        secret_class="",
        reason="Arbeitsraeume und Kaefige der nativen Auftragsarbeiter (Codex, "
               "Claude) — Arbeitsflaeche eines untrusted_executor. Was zaehlt, "
               "publiziert der Core in den Auftragsbestand (Item "
               "agent-run-artifacts); ein offener nativer Turn ist nach Verlust "
               "des Arbeitsraums nicht fortsetzbar und endet fail-closed.",
        recovery="entfaellt — der naechste Auftrag legt seinen Arbeitsraum neu an.",
    ),
    Excluded(
        what="~/.solvio-nexus (codex, runtimes, proofs, worktrees)",
        secret_class=CLASS_FUTURE_VAULT,
        reason="Nexus-Betriebsordner: das native Codex-Home (Anmeldung des "
               "offiziellen CLI, OAuth-Datei — kontogebunden, vom CLI selbst "
               "erneuert), aus Bauzetteln neu erzeugbare Test-/Browser-Laufzeiten, "
               "Nachweislaeufe und git-Arbeitsbaeume. Keine kanonische Wahrheit; "
               "die Anmeldung gehoert perspektivisch in den Tresor.",
        recovery="Laufzeiten aus BUILD.json/uv.lock neu bauen; Arbeitsbaeume aus "
                 "git; der Owner meldet das native Codex-Home mit `codex login` "
                 "neu an.",
    ),
    Excluded(
        what="~/.solvio-hermes/home, work, src, venv",
        secret_class="",
        reason="Der Kaefig ist Arbeitsflaeche eines untrusted_executor. Sein "
               "Zustand ist ausdruecklich nicht kanonisch.",
        recovery="Hermes neu installieren (angeheftete Fassung), sandbox.sb "
                 "aus der Sicherung.",
    ),
    # ---- B1 von Offsite Encrypted Backup V1 (DEBT-0159): die fuenf Pfade,
    # ---- die in keiner Liste standen, bekommen ihre Entscheidung.
    Excluded(
        what="~/.solvio/payment-sandbox.env",
        secret_class=CLASS_FUTURE_VAULT,
        reason="Zugang zum Pruefanbieter der Zahlungs-Sandbox "
               "(scripts/payment_admin.py legt ihn an). Ein Sandbox-"
               "Geheimnis ohne echtes Geld dahinter.",
        recovery="Mit scripts/payment_admin.py neu erzeugen.",
    ),
    Excluded(
        what="~/.solvio-hermes/api_key",
        secret_class=CLASS_OS_BOUND,
        reason="Vom Core je Start gepraegter Zugang zum Rueckschleifen-"
               "Gateway des Kaefigs. Eine Sicherung truege einen Wert "
               "zurueck, den der naechste Start ohnehin neu praegt.",
        recovery="Entsteht beim naechsten Core-Start von selbst.",
    ),
    Excluded(
        what="~/.solvio/memory/backups/",
        secret_class="",
        reason="Abgeleitete Kopien von memory.sqlite3 (memory/backup.py). "
               "Das Original ist Bestand; eine Kopie der Kopie taeuscht "
               "Wert vor.",
        recovery="entfaellt — das Original liegt im Satz.",
    ),
    Excluded(
        what="~/.solvio/agent_workspaces und ~/.solvio/githome",
        secret_class="",
        reason="Arbeitsflaechen der Agentenlaufzeit (worktrees je Lauf) und "
               "das Wegwerf-HOME ihrer git-Unterprozesse. Das Ergebnis eines "
               "Laufs liegt im Harvest-Repo und in den Run-Artefakten — "
               "beides ist Bestand; die Werkbank ist es nicht.",
        recovery="entfaellt — entsteht je Lauf neu.",
    ),
    Excluded(
        what="/opt/homebrew/var/postgresql@16",
        secret_class="",
        reason="Kein SOLVIO-Codepfad benutzt diese Datenbank (einzige "
               "Erwaehnung: ein abgewiesenes Ziel im Browser-Test). Sie "
               "gehoert nicht zu SOLVIOs Bestand; ob der Dienst ueberhaupt "
               "laufen muss, ist eine offene Eigentuemerfrage (DEBT-0159).",
        recovery="entfaellt — nicht SOLVIOs Daten.",
    ),
)


# -------------------------------------------------------------------- der Bestand
def items() -> tuple[Item, ...]:
    """Der kanonische Bestand.

    Die Reihenfolge ist die Reihenfolge der Sicherung: erst das Kleine und
    Kritische, dann das Grosse. Wer bei voller Platte abbricht, hat dann das
    Wichtige schon.
    """
    return (
        # ------------------------------------------------------------- Gedaechtnis
        Item("memory", SQLITE, "~/.solvio/memory/memory.sqlite3",
             "Memory/memory.sqlite3", "memory",
             "Das kanonische Gedaechtnis. Bleibt intern; hier liegt nur die Kopie.",
             needs_encryption=True),
        Item("memory-candidates", SQLITE, "~/.solvio/memory/candidates.sqlite3",
             "Memory/candidates.sqlite3", "memory",
             "Adaptive Memory: Vorschlaege, die kein Abruf je sieht.",
             needs_encryption=True),
        Item("memory-privacy-ledger", SQLITE, "~/.solvio/memory/privacy_ledger.sqlite3",
             "Memory/privacy_ledger.sqlite3", "memory",
             "Das Vergessen. Ohne ihn brueche ein Restore geloeschte Saetze zurueck.",
             needs_encryption=True),
        Item("memory-semantic-index", SQLITE, "~/.solvio/memory/semantic_index.sqlite3",
             "Memory/semantic_index.sqlite3", "memory",
             "Einbettungen. Neu berechenbar, aber teuer.",
             needs_encryption=True),

        # ------------------------------------------------------------ Laufzeitzustand
        Item("conversations", SQLITE, "~/.solvio/conversations.sqlite3",
             "RuntimeState/conversations.sqlite3", "runtime",
             "Gespraechsbesitz des Cores.", needs_encryption=True),
        # Bestaetigte Empfaengerbindungen. Kein Kontaktbestand — nur das,
        # was der Eigentuemer ausdruecklich bestaetigt hat („mein Sohn" ist
        # DIESE Adresse). Genau deshalb gehoert es in die Sicherung: es ist
        # nirgends sonst ableitbar, und ohne es faellt SOLVIO auf Vorschlaege
        # zurueck, die niemand bestaetigt hat.
        # `required=False` aus demselben Grund wie beim Autopiloten: auf einem
        # Rechner, auf dem noch niemand eine Bindung bestaetigt hat, gibt es
        # die Datei nicht — das ist kein roter Sicherungssatz, sondern ein
        # leerer. Sobald die erste Bindung steht, ist sie da und wird
        # mitgenommen.
        Item("contacts", SQLITE, "~/.solvio/contacts.sqlite3",
             "RuntimeState/contacts.sqlite3", "runtime",
             "Bestaetigte Empfaengerbindungen der Kommunikationsschicht.",
             needs_encryption=True, required=False),
        Item("proactive", SQLITE, "~/.solvio/proactive.sqlite3",
             "RuntimeState/proactive.sqlite3", "runtime",
             "Hintergrundaufgaben, Vorabgenehmigungen und der proaktive Eingang.",
             needs_encryption=True),
        Item("doctor", SQLITE, "~/.solvio/doctor.sqlite3",
             "RuntimeState/doctor.sqlite3", "runtime",
             "Befundverlauf des Diagnostikers."),
        Item("deep-tasks", SQLITE, "~/.solvio-deep/deep_tasks.sqlite3",
             "RuntimeState/deep_tasks.sqlite3", "runtime",
             "Auftragsjournal der tiefen Recherche.", needs_encryption=True),
        # DEBT-0130: das Buch des Brokers stand in keiner Sicherungsliste — weder
        # unter `items()` noch als EXCLUDED. Es ist die einzige Antwort auf „was
        # hat ein Tag wirklich gekostet" und traegt die Lease-Zuordnung jedes
        # Anbieteraufrufs. Eine Sicherung, die ihre Luecken verschweigt, ist die
        # gefaehrlichste Art von gruen.
        Item("broker", SQLITE, "~/.solvio/broker.sqlite3",
             "RuntimeState/broker.sqlite3", "runtime",
             "Verbrauch und Lease-Zuordnung jedes Anbieteraufrufs.",
             needs_encryption=True, required=False),
        # Auf einem Rechner, auf dem nie ein Agentenauftrag lief, fehlen beide —
        # das ist kein roter Sicherungssatz, sondern ein leerer.
        Item("agent-runs", SQLITE, "~/.solvio/agent_runs.sqlite3",
             "RuntimeState/agent_runs.sqlite3", "runtime",
             "Aufgaben, Laeufe, Schritte und Ereignisse der Agentenlaufzeit.",
             needs_encryption=True, required=False),
        Item("agent-harvest", TREE, "~/.solvio/agent_harvest.git",
             "RuntimeState/agent_harvest.git", "runtime",
             "Die geernteten Agentenzweige. Das Ergebnis eines Bau-Laufs liegt "
             "hier und nirgends sonst.",
             needs_encryption=True, required=False),
        # Das Autopilot-Buch traegt die Wahrheit ueber jeden
        # Entwicklungs-Milestone: gepinnter Contract, Zustand, Evidence,
        # Findings, Urteile, Verbrauch. Ein Milestone, dessen Buch fehlt,
        # verliert seine ganze Geschichte — der Arbeitsbereich allein sagt
        # nicht, WARUM etwas so gebaut wurde.
        Item("autopilot", SQLITE, "~/.solvio/autopilot.sqlite3",
             "RuntimeState/autopilot.sqlite3", "runtime",
             "Milestones, Phasen, Evidence, Findings und Urteile des "
             "Entwicklungs-Autopiloten.",
             needs_encryption=True, required=False),

    # Auf einem Rechner, auf dem der Router nie lief, fehlt sie — das ist kein
    # roter Sicherungssatz, sondern ein leerer.
    Item("cognition", SQLITE, "~/.solvio/cognition.sqlite3",
         "RuntimeState/cognition.sqlite3", "runtime",
         "Gewaehlte Routen und Stufen des kognitiven Routers. Kein "
         "Aeusserungstext, nur Verweise.",
         needs_encryption=True, required=False),

        # DEBT-0158: das Ledger referenziert diese Befunde mit Pfad+SHA256 —
        # ohne sie zeigen die Zeilen nach einem Restore ins Leere. Das
        # Ergebnis eines Laufs liegt hier und nirgends sonst.
        Item("agent-run-artifacts", TREE, "~/.solvio/agent_runs",
             "RuntimeState/agent_runs", "runtime",
             "Die Befund-Artefakte der Agentenlaufzeit, je Lauf ein "
             "Verzeichnis. Das Ledger verweist per Pfad und Pruefsumme "
             "hierher.",
             needs_encryption=True, required=False),

        # N7: der Seed-Arbeitsbaum enthaelt nur README.md. Die veroeffentlichten
        # Adapter liegen in .git/objects und refs/autopilot; .git darf hier
        # deshalb NICHT wie bei einem wegwerfbaren Builder-Klon entfallen.
        # Vor N7 fehlt der ganze Speicher. Er benutzt dieselbe State-Wurzel
        # wie extension_runtime, auch wenn nur die Core-plist sie festlegt.
        Item("document-adapter-store", TREE,
             os.path.join(core_env("SOLVIO_STATE_DIR", "~/.solvio"),
                          "document-adapter-store"),
             "RuntimeState/document-adapter-store", "runtime",
             "Kanonisches Seedrepo samt Git-Objekten und unveraenderlichen "
             "Dokumentadapter-Checkpoints; die Agenten- und Autopilot-Buecher "
             "verweisen auf diese Commits.",
             needs_encryption=True, required=False),

        # Der Wirkungsordner: was Faehigkeiten dem Eigentuemer hinterlassen.
        # Heute sind das die Notizen (`effects/notes`, `capabilities/notes.py`).
        # Es ist INHALT, kein Betriebszustand — eine Notiz, die eine Sicherung
        # nicht mitnimmt, ist nach einem Plattenverlust fort, und niemand
        # koennte sie neu berechnen. `required=False`, weil der Ordner erst
        # entsteht, wenn zum ersten Mal etwas darin abgelegt wird.
        Item("capability-effects", TREE, "~/.solvio/effects",
             "RuntimeState/effects", "runtime",
             "Was Faehigkeiten dem Eigentuemer hinterlassen — heute die "
             "Notizen. Inhalt, nicht Betriebszustand: neu berechenbar ist "
             "davon nichts.",
             needs_encryption=True, required=False),

        # Offsite Encrypted Backup V1: das Buch der Offsite-Generationen —
        # die DEBT-0130-Lektion, beim Bau gleich richtig. Es entsteht erst
        # mit der Offsite-Implementierung; bis dahin ist sein Fehlen ein
        # leerer Eintrag, kein roter.
        Item("offsite-ledger", SQLITE, "~/.solvio/offsite.sqlite3",
             "RuntimeState/offsite.sqlite3", "runtime",
             "Offsite-Generationen, Verifikationen und Retention-Abgleich. "
             "Kein Schluessel, kein Inhalt — nur Buchhaltung.",
             needs_encryption=True, required=False),

        # Der Wiederherstellungsumschlag der Offsite-Identitaet: die
        # age-Identitaet unter der Passphrase des Besitzers (age-scrypt).
        # Ohne die Passphrase wertlos — genau das macht ihn sicherbar,
        # dasselbe Muster wie Vault/recovery.json. Er liegt ZUSAETZLICH
        # offsite (v1/recovery/) und auf der externen Platte; die Kopie im
        # Satz ist beilaeufige Redundanz, nie der Recovery-Pfad.
        Item("offsite-identity-envelope", FILE,
             "~/.solvio/offsite/offsite-identity-v1.age",
             "Config/offsite-identity-v1.age", "config",
             "Die Offsite-age-Identitaet unter der Besitzer-Passphrase. "
             "Ohne sie Zufallsbytes; mit ihr der Katastropheneinstieg.",
             needs_encryption=True, required=False,
             secret_class=CLASS_BACKUP),

        # ---------------------------------------------------------------- Freigabe
        Item("approval-control", SQLITE,
             approval_state_dir() + "/approval_control.sqlite3",
             "Approval/approval_control.sqlite3", "approval",
             "Geraeteregistrierung, Freigabeentscheidungen, Widerrufe. "
             "Beweiswert — ohne die privaten Schluessel KEINE Wiederinbetriebnahme.",
             needs_encryption=True),
        Item("approval-instance-id", FILE,
             approval_state_dir() + "/core_instance_id",
             "Approval/core_instance_id", "approval",
             "Welche Core-Instanz die Registrierungen ausgestellt hat.",
             needs_encryption=True),
        Item("approval-gateway-cert", FILE,
             approval_state_dir() + "/gateway_cert.pem",
             "Approval/gateway_cert.pem", "approval",
             "Oeffentliches Zertifikat. Kein Geheimnis, aber es belegt, welches "
             "Zertifikat das iPhone gepinnt hatte."),

        # --------------------------------------------------------------- Konfiguration
        Item("core-env", FILE, f"{CORE_REPO}/.env", "Config/solvio-core.env", "config",
             "Anbieterzugaenge: OpenAI, Home Assistant, Google. Klasse A — "
             "widerrufbar und vom Anbieter tauschbar.",
             needs_encryption=True, secret_class=CLASS_BACKUP),
        Item("core-nodes", FILE, f"{CORE_REPO}/config/nodes.json",
             "Config/nodes.json", "config",
             "Knotenkonfiguration; nicht in git.", required=False),
        # DEBT-0159: stand in keiner Liste. Nur Anbieteradressen, nie
        # Geheimnisse (payment/config.py) — aber fail-closed restore-
        # relevant: ohne die Datei gibt es nach einem Restore keinen
        # Zahlungsanbieter.
        Item("payment-config", FILE, "~/.solvio/payment.json",
             "Config/payment.json", "config",
             "Zahlungsanbieter-Adressen. Keine Geheimnisse — aber ohne sie "
             "ist die Zahlung nach einem Restore stumm.", required=False),
        Item("launchagents", TREE, "~/Library/LaunchAgents",
             "Config/LaunchAgents", "config",
             "Wie SOLVIO startet. Ohne com.solvio.core.plist weiss ein neuer Mac "
             "nicht, dass es SOLVIO gibt.",
             exclude=("__pycache__",)),
        Item("portal-vault", FILE, "~/.solvio-portal/vault.bin",
             "Config/portal-vault.bin", "config",
             "AES-GCM-Geheimtext. Der Hauptschluessel liegt im Schluesselbund und "
             "geht NICHT mit — die Datei allein ist wertlos (Klasse B).",
             needs_encryption=True, required=False, secret_class=CLASS_OS_BOUND),

        # ---------------------------------------------------------- Zahlungen
        #
        # Absichten, Grenzen, das Zahlungsbuch und die UNDURCHSICHTIGEN Verweise
        # auf hinterlegte Zahlungsmittel. Kein Kartenwert liegt darin — die
        # Karte haelt der Anbieter, und der Anbieterzugang liegt im Tresor.
        #
        # `required=False`, weil auf einem Mac, auf dem nie eine Zahlung
        # eingerichtet wurde, sonst jeder taegliche Lauf `ok=False` meldete.
        Item("payment", SQLITE, "~/.solvio/payments.sqlite3",
             "RuntimeState/payments.sqlite3", "runtime",
             "Zahlungsabsichten, Grenzen und das Zahlungsbuch. Kein Kartenwert, "
             "keine Pruefziffer, kein Anbieterschluessel — nur Verweise.",
             needs_encryption=True, required=False),

        # ------------------------------------------------------------ Telefonie
        #
        # Das Anrufbuch: je Freigabe eine Zeile, darin der Faden zum Anbieter
        # (`conversation_id`), die gewaehlte Rufnummer, die ausgerichtete
        # Nachricht und der Ausgang. Es ist Bestand und keine Maschinerie — nach
        # einem Verlust waere nicht mehr feststellbar, WEN SOLVIO angerufen hat
        # und was dabei herauskam.
        #
        # Verschluesselt, weil Rufnummern und Gespraechszusammenfassungen darin
        # stehen. Kein Anbieterschluessel: der liegt im Tresor, und die
        # Twilio-Zugangsdaten liegen ueberhaupt nicht auf diesem Rechner.
        #
        # `required=False` aus demselben Grund wie bei der Zahlung: auf einem
        # Mac ohne eingerichtete Telefonie meldete sonst jeder taegliche Lauf
        # `ok=False`.
        # Der Ledger folgt `SOLVIO_TELEPHONY_DB`. Gefragt wird die plist des
        # CORES und nicht die eigene Umgebung — die Sicherung laeuft in einem
        # anderen Prozess, der die Variable nie zu sehen bekommt. Siehe
        # `core_env()`; `approval_state_dir()` macht es seit jeher genauso.
        Item("telephony", SQLITE,
             core_env("SOLVIO_TELEPHONY_DB", "~/.solvio/telephony.sqlite3"),
             "RuntimeState/telephony.sqlite3", "runtime",
             "Anrufbuch: je Freigabe ein Anruf, mit Faden zum Anbieter, "
             "Rufnummer, Nachricht und Ausgang. Kein Anbieterschluessel.",
             needs_encryption=True, required=False),

        # ------------------------------------------------------ Geheimnistresor
        #
        # Zwei Dinge, und sie gehoeren zusammen: der Geheimtext und der
        # Umschlag, der seinen Schluessel traegt. Die Datei allein ist wertlos —
        # der Hauptschluessel liegt im Schluesselbund und geht NICHT mit.
        #
        # Und genau deshalb gibt es den zweiten Eintrag. Ohne ihn waere die
        # Sicherung nach einem Verlust der internen Platte nicht beschaedigt,
        # sondern WERTLOS: niemand koennte sie je wieder oeffnen. Der Umschlag
        # traegt den Hauptschluessel, verpackt unter einer Passphrase, die nur
        # der Besitzer kennt. Die Platte allein oeffnet ihn nicht.
        Item("secret-vault", SQLITE, "~/.solvio-vault/vault.sqlite3",
             "Vault/vault.sqlite3", "vault",
             "Der Geheimnistresor: Geheimtext, Berechtigungen und Zugriffsspur. "
             "Ohne den Hauptschluessel unlesbar — und der geht nicht mit.",
             needs_encryption=True, required=False, secret_class=CLASS_BACKUP),
        Item("secret-vault-recovery", FILE, "~/.solvio-vault/recovery.json",
             "Vault/recovery.json", "vault",
             "Der Hauptschluessel des Tresors, verpackt unter der "
             "Wiederherstellungs-Passphrase des Besitzers (Argon2id + AES-256-GCM). "
             "OHNE die Passphrase oeffnet ihn niemand; MIT ihr ist der Tresor nach "
             "einem Mac-Verlust wieder da.",
             needs_encryption=True, required=False, secret_class=CLASS_BACKUP),
        Item("ha-vm-scripts", TREE, "~/Library/Scripts",
             "Config/Library-Scripts", "config",
             "Die Skripte, die die Home-Assistant-VM am Leben halten "
             "(Wachhund, Abschalthaken, EFI-Pflege). Sie liegen in KEINEM "
             "Repository — ohne sie startet die VM nach einem Neuaufbau nicht.",
             required=False),
        Item("node-pki", TREE, "~/solvio-node-pki",
             "Config/node-pki", "node",
             "CA und Clientzertifikate fuer die entfernten Knoten. Ohne sie ist "
             "config/nodes.json eine Liste von Pfaden ins Leere.",
             needs_encryption=True, required=False, secret_class=CLASS_FUTURE_VAULT),
        Item("satellite-auth", FILE, "~/.solvio/satellite_auth.json",
             "Config/satellite_auth.json", "config",
             "Der Zugangsnachweis des Sprachsatelliten. Neu erzeugbar, aber "
             "dann muss der Pi angefasst werden.",
             needs_encryption=True, required=False, secret_class=CLASS_FUTURE_VAULT),
        Item("hermes-profiles", TREE, "~/.solvio-hermes/home/profiles",
             "Config/hermes-profiles", "config",
             "Die belegten Laufzeitprofile der drei Fachbots. Der Bauplan liegt "
             "in git, diese Haelfte nicht.",
             needs_encryption=True, required=False),
        Item("hermes-sandbox", FILE, "~/.solvio-hermes/sandbox.sb",
             "Config/hermes-sandbox.sb", "config",
             "Das Seatbelt-Profil des Hermes-Kaefigs. Sicherheitsrelevante "
             "Konfiguration, kein Geheimnis.", required=False),

        # -------------------------------------------------------------------- Wissen
        Item("knowledge-vault", TREE, "~/SOLVIO Knowledge",
             "Knowledge/Vault", "knowledge",
             "Der menschenlesbare Vault. Eine PROJEKTION, keine Quelle — aber "
             "eine, in der ein Mensch geschrieben haben kann.",
             needs_encryption=True,
             exclude=(".Trash", "workspace.json", ".DS_Store")),

        # ------------------------------------------------------------------ Satellit
        Item("satellite-mirror", TREE, "~/solvio-satellite-backup",
             "Satellite/mirror", "satellite",
             "Bare-Repo, Buendel und Baumarchiv des Pi, samt der DSP-Befunde "
             "vor und nach der Wake-Arbeit. Der Pi hat sonst nur seine SD-Karte.",
             required=False),
    )


def secret_inventory() -> tuple[dict[str, str], ...]:
    """Wo ueberhaupt Geheimnisse liegen — Orte und Klassen, NIE Werte.

    Diese Tabelle ist die Vorarbeit fuer den kuenftigen Geheimnistresor. Sie
    nennt Pfade und Schluesselnamen, weil das erlaubt ist, und keinen einzigen
    Wert, weil das nie erlaubt ist.
    """
    return (
        {"ort": "~/.solvio-vault/vault.sqlite3",
         "inhalt": "Geheimnistresor (AES-256-GCM je Eintrag)",
         "klasse": CLASS_BACKUP,
         "hinweis": "Der Hauptschluessel liegt im Schluesselbund, nicht hier."},
        {"ort": "~/.solvio-vault/recovery.json",
         "inhalt": "Hauptschluessel unter der Wiederherstellungs-Passphrase",
         "klasse": CLASS_BACKUP,
         "hinweis": "Ohne die Passphrase wertlos. Genau das macht ihn sicherbar."},
        {"ort": "~/.solvio/payments.sqlite3",
         "inhalt": "Zahlungsverweise und Anbieter-Token (Kennungen, keine Werte)",
         "klasse": CLASS_BACKUP,
         "hinweis": "Kein Kartenwert und keine Pruefziffer. Ein Token allein "
                    "bewegt nichts — der Anbieterzugang liegt im Tresor."},
        {"ort": f"{CORE_REPO}/.env", "inhalt": "OPENAI_API_KEY, HOME_ASSISTANT_TOKEN, "
         "GOOGLE_CALENDAR_CLIENT_ID/SECRET/REFRESH_TOKEN",
         "klasse": CLASS_BACKUP,
         "grund": "Anbieterzugaenge, widerrufbar und tauschbar. Seit Provider "
                  "Broker V1 ist dies der EINZIGE Ort, an dem der echte "
                  "Anbieterschluessel liegt; der Kaefig bekommt ihn nicht mehr. "
                  "Gehoert spaeter in den Tresor (dann Klasse C)."},
        {"ort": "~/.solvio-approvals-production/core_signing_key.pem",
         "inhalt": "privater Schluessel der Core-Identitaet", "klasse": CLASS_NEVER,
         "grund": "SOLVIOs eigene Autoritaet."},
        {"ort": "~/.solvio-approvals-production/gateway_key.pem",
         "inhalt": "privater Schluessel des Freigabe-Gateways", "klasse": CLASS_NEVER,
         "grund": "SOLVIOs eigene Autoritaet."},
        {"ort": "~/.solvio-portal/vault.bin", "inhalt": "Portalzugaenge, AES-256-GCM",
         "klasse": CLASS_OS_BOUND,
         "grund": "Geheimtext ohne Schluessel. Der Hauptschluessel liegt im "
                  "Schluesselbund (de.solvio.portal-vault/master-key)."},
        # KORREKTUR (Provider Broker V1): diese Datei war hier als
        # „Anbieterschluessel des Executors" gefuehrt, und das war schlicht
        # falsch. Sie haelt den vom Core selbst gepraegten Zugang, mit dem der
        # CORE das Gateway im Kaefig anspricht (`API_SERVER_KEY`,
        # `deep/service._api_key`, `secrets.token_hex(32)`) — kein
        # Anbieterzugang, nichts, was ausserhalb dieses Rechners etwas oeffnet.
        # Der echte Anbieterschluessel lag woanders, naemlich in den `.env` des
        # Kaefigs, und die standen in dieser Tabelle ueberhaupt nicht.
        {"ort": "~/.solvio-hermes/api_key",
         "inhalt": "lokaler Core->Gateway-Zugang (API_SERVER_KEY)",
         "klasse": CLASS_OS_BOUND,
         "grund": "Vom Core gepraegt und jederzeit neu praegbar; oeffnet nur den "
                  "Rueckschleifen-Port des Kaefigs. Kein Anbieterzugang."},
        {"ort": "~/.solvio-hermes/home/.env und home/profiles/<profil>/.env",
         "inhalt": "kurzlebiger Broker-Token und OPENAI_BASE_URL",
         "klasse": CLASS_NEVER,
         "grund": "Wird NICHT gesichert und ist keine dauerhafte Autoritaet. Der "
                  "Wert lebt nur bis zum Ende des Auftrags und stirbt mit dem "
                  "Core; eine Sicherung davon truege einen Zugang zurueck, den "
                  "es nicht mehr gibt. BIS Provider Broker V1 stand hier der "
                  "echte Anbieterschluessel des Cores — deshalb gilt der als "
                  "historisch belichtet (DEBT-0129)."},
        {"ort": "~/.ssh/{raspi_claude,solvio_satellite,solvio_deploy}",
         "inhalt": "private SSH-Schluessel", "klasse": CLASS_FUTURE_VAULT,
         "grund": "Zugang zum Pi. Neu erzeugbar; siehe DEBT-0003."},
        {"ort": "~/.solvio/satellite_auth.json", "inhalt": "Satelliten-Zugangsnachweis",
         "klasse": CLASS_FUTURE_VAULT,
         "grund": "Mit scripts/provision_satellite_credential.py neu erzeugbar."},
        {"ort": "macOS-Schluesselbund", "inhalt": "de.solvio.portal-vault/master-key, "
         "Passphrase der Speicherplatte (falls der Nutzer sie merken laesst)",
         "klasse": CLASS_OS_BOUND,
         "grund": "Betriebssystemgebunden. Kein Dateibackup kann das ersetzen."},
        {"ort": "iPhone Secure Enclave", "inhalt": "App-Attest-Schluessel, Face-ID-Bindung",
         "klasse": CLASS_OS_BOUND,
         "grund": "Hardware. Eine Datei kann keine Geraeteidentitaet sichern."},
    )


def by_category(selected: Iterable[str] | None = None) -> tuple[Item, ...]:
    if selected is None:
        return items()
    wanted = set(selected)
    return tuple(i for i in items() if i.category in wanted)
