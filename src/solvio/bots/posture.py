"""Die Haltung eines Bots — geschrieben, und danach beim laufenden Prozess nachgefragt.

Dieselbe Lehre wie beim tiefen Executor, und sie ist hier zum zweiten Mal
bezahlt worden. Die Konfiguration sagt das eine, der Prozess tut das andere:

* Ein **frisch angelegtes** Hermes-Profil hat `terminal`, `file`,
  `code_execution`, `computer_use`, `browser`, `memory`, `delegation` und
  `cronjob` **eingeschaltet**. Wer ein Profil anlegt und es benutzt, hat einem
  Modell eine Shell gegeben, ohne es zu merken.
* Eine **Sperrliste** loeschte in der ersten Fassung `web_search` gleich mit:
  das Werkzeug liegt zusaetzlich in `browser`, `debugging`, `safe` und `search`,
  und die Subtraktion trifft den Namen, nicht die Gruppe. `hermes tools list`
  meldete `web ✓ enabled`, das Modell bekam null Werkzeuge, und der Bot
  antwortete „ich kann nicht suchen" — plausibel, hilfsbereit und falsch.

Also drei Ebenen, und die dritte ist die, die zaehlt:

1. `platform_toolsets.cli` — die Erlaubnisliste in der Konfiguration.
2. `-t` beim Aufruf — die Erlaubnisliste, die SOLVIO selbst uebergibt.
3. **Die Selbstauskunft des laufenden Prozesses.** Hermes schreibt im
   ausfuehrlichen Modus, welche Werkzeuge es tatsaechlich geladen hat. Steht
   dort auch nur ein Name, der nicht in der Erlaubnisliste der Rolle steht,
   ist die Antwort ungueltig — nicht „etwas grosszuegig eingestellt".

Was hier ausdruecklich NICHT steht, ist eine Sperrliste. Sie war der Fehler.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from solvio.bots import soul
from solvio.bots.registry import BotSpec
from solvio.bots.runner import Jail, Outcome, run
from solvio.deep import isolation
from solvio.logging_setup import get_logger

log = get_logger("bots")

#: Wie Hermes seine tatsaechlich geladene Werkzeugflaeche meldet.
_LOADED = re.compile(r"Loaded\s+(\d+)\s+tools?:\s*(.+)")
_NO_TOOLS = re.compile(r"No tools loaded")

#: Wie lange das Anlegen eines Profils dauern darf.
CREATE_TIMEOUT = 90.0

#: Die Konfiguration, die SOLVIO in jedes Botprofil schreibt.
#:
#: `platform_toolsets.cli` ist die Erlaubnisliste; eine leere Liste heisst
#: wirklich null Werkzeuge (gemessen, nicht vermutet). `bot_mode_protocol`
#: bleibt aus: der Abschnitt, den Hermes sonst in eine kanonische Bot-Chat-
#: Sitzung schreibt, erklaert dem Modell, wie es mit `terminal` und `file` einen
#: anderen Bot anruft — also genau die zwei Werkzeuge, die es hier nicht gibt.
#: `tirith_enabled` aus, weil der Scanner beim ersten Lauf ein 19-MB-Binaerpaket
#: aus dem Netz nachlaedt und im Profil ablegt; ein Bot ohne Terminal hat nichts
#: zu scannen. `auxiliary.free_only` ist eine zusaetzliche Sicherung gegen eine
#: ueberraschende Abrechnung — sie traegt aber weniger, als sie aussieht: sie
#: beschraenkt nur die OpenRouter-Rueckfallware, waehrend Hilfsaufgaben zuerst
#: auf das Hauptmodell gehen. Die eigentliche Schranke ist seit Provider Broker
#: V1 eine andere und liegt ausserhalb des Kaefigs: in dieser `.env` steht kein
#: Anbieterzugang mehr, sondern ein Broker-Token ohne Lebensdauer ueber die
#: Frage hinaus, und die Tokenkappe des Brokers zaehlt mit.
_CONFIG = """# Von SOLVIO geschrieben. Haendische Aenderungen werden beim Start ueberschrieben.
# Dieser Bot ist Fachauskunft, nicht Entscheider.
approvals:
  mode: manual
  timeout: 30
  cron_mode: deny
  single_query_mode: deny
platform_toolsets:
  cli:{allowed}
agent:
  bot_mode_protocol: false
auxiliary:
  free_only: true
  title_generation:
    enabled: false
memory:
  memory_enabled: false
  user_profile_enabled: false
security:
  tirith_enabled: false
  allow_private_urls: false
  redact_secrets: true
"""


class PostureViolation(RuntimeError):
    """Der Bot bietet mehr an, als er darf. Dann gilt seine Antwort nicht."""

    def __init__(self, profile: str, extra: list[str]) -> None:
        super().__init__(f"{profile}: {','.join(extra) or 'keine Selbstauskunft'}")
        self.profile = profile
        self.extra = extra


@dataclass(frozen=True)
class Provisioned:
    """Was beim Einrichten eines Bots wirklich passiert ist."""

    profile: str
    created: bool
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def render_config(spec: BotSpec) -> str:
    """Die `config.yaml` genau dieses Bots."""
    if spec.toolsets:
        allowed = "\n" + "\n".join(f"    - {name}" for name in sorted(spec.toolsets))
    else:
        allowed = " []"
    return _CONFIG.format(allowed=allowed)


def loaded_tools(text: str) -> frozenset[str] | None:
    """Was der Prozess ueber seine eigene Werkzeugflaeche gesagt hat.

    `None` heisst: er hat gar nichts gesagt. Das ist kein Freibrief — ohne
    Selbstauskunft gilt die Haltung als unbelegt, und unbelegt ist verletzt.
    """
    found: set[str] | None = None
    for line in (text or "").splitlines():
        if _NO_TOOLS.search(line):
            found = set()
            continue
        match = _LOADED.search(line)
        if not match:
            continue
        names = {part.strip() for part in match.group(2).split(",")}
        found = {name for name in names if name}
    return frozenset(found) if found is not None else None


def assert_posture(spec: BotSpec, text: str) -> frozenset[str]:
    """Haelt den Prozess bei seinem Wort. Wirft, statt zu warnen."""
    reported = loaded_tools(text)
    if reported is None:
        raise PostureViolation(spec.profile, [])
    extra = sorted(reported - spec.tools)
    if extra:
        log.error("bots.posture_violation", profile=spec.profile,
                  extra=",".join(extra))
        raise PostureViolation(spec.profile, extra)
    return reported


async def provision(jail: Jail, spec: BotSpec, *, broker_token: str,
                    broker_port: int = isolation.BROKER_PORT) -> Provisioned:
    """Legt das Profil an, falls noetig, und schreibt SOLVIOs Haltung hinein.

    Idempotent und bei jedem Core-Start erneut: dieselbe Haltung wie
    `deep/executor.provision`. Was jemand von Hand im Profil geaendert hat, ist
    danach wieder das, was hier steht — eine Konfiguration, die zwischen zwei
    Starts driften darf, ist keine.
    """
    directory = jail.profile_dir(spec.profile)
    created = False
    if not os.path.isdir(directory):
        outcome: Outcome = await run(
            jail,
            ["profile", "create", spec.profile, "--no-alias", "--no-skills"],
            timeout=CREATE_TIMEOUT)
        if not outcome.ok or not os.path.isdir(directory):
            log.error("bots.profile_create_failed", profile=spec.profile,
                      reason=outcome.reason or "no_directory")
            return Provisioned(spec.profile, False,
                               reason=outcome.reason or "create_failed")
        created = True

    with open(os.path.join(directory, "config.yaml"), "w", encoding="utf-8") as handle:
        handle.write(render_config(spec))
    with open(os.path.join(directory, "SOUL.md"), "w", encoding="utf-8") as handle:
        handle.write(soul.render(spec))
    _write_env(directory, broker_token, broker_port=broker_port)

    log.info("bots.provisioned", profile=spec.profile, created=created,
             toolsets="+".join(spec.toolsets) or "keine")
    return Provisioned(spec.profile, created)


def write_credential(directory: str, broker_token: str, *,
                     broker_port: int = isolation.BROKER_PORT) -> None:
    """Der oeffentliche Name fuer die Rotation.

    Der Broker praegt bei Lease-Null neu und meldet den frischen Token an den
    Eigentuemer der Datei — hier. Es ist bewusst dieselbe Schreibfunktion wie
    beim Provisionieren: zwei Schreibwege fuer dieselbe Datei laufen
    irgendwann auseinander.
    """
    _write_env(directory, broker_token, broker_port=broker_port)


def _write_env(directory: str, broker_token: str, *,
               broker_port: int = isolation.BROKER_PORT) -> None:
    """Der Zugang des Profils — und das ist **kein** Anbieterschluessel.

    Bis Provider Broker V1 stand hier der echte Schluessel des Cores, derselbe,
    den auch der tiefe Executor bekam und den jeder Prozess im Gefaengnis lesen
    konnte. Jetzt steht hier ein Broker-Token: er oeffnet nur den Broker des
    Cores auf der Rueckschleife, nur mit offenem Lease, und er wird neu
    gepraegt, sobald die Frage beantwortet ist.

    Die Zeile heisst weiterhin `OPENAI_API_KEY`, weil der Aufloeser des
    angehefteten Hermes einen gewoehnlichen Schluesselplatz verlangt — der
    **Name** der Zeile ist eine Hermes-Konvention, nicht eine Aussage ueber den
    Wert. `OPENAI_BASE_URL` daneben ist das, was den Aufruf umlenkt; der
    Anbietername bleibt `openai-api`, weil nur dieser Aufloeser die Basis-URL
    ueberhaupt ansieht.

    Hermes liest `.env` ausschliesslich aus dem eigenen Profilordner — es gibt
    keinen Rueckfall auf die Wurzel, also muss beides hier stehen. Die Datei
    wird vollstaendig neu geschrieben; genau das braucht die Rotation.
    """
    path = os.path.join(directory, ".env")
    previous = os.umask(0o077)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Von SOLVIO geschrieben. Kein Anbieterzugang:\n"
                         "# ein Broker-Token des Cores, der ohne offenes Lease\n"
                         "# nichts oeffnet und nach der Frage neu gepraegt wird.\n")
            if broker_token:
                handle.write(f"OPENAI_API_KEY={broker_token}\n")
            handle.write(f"OPENAI_BASE_URL=http://127.0.0.1:{int(broker_port)}/v1\n")
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
