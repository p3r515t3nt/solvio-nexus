"""Was der Diagnostiker sieht — Befunde, keine Maschine.

Der Diagnostiker ist ausdruecklich **nicht** der Arzt. Der Arzt gehoert dem
Core: er misst, er entscheidet, und er darf aus einer geschlossenen Liste von
Python-Funktionen reparieren. Ein Modell kann ihm helfen zu verstehen; es kann
keine Handlung erfinden. Diese Trennung ist freigegeben und wird hier nicht
angetastet.

Der Diagnostiker bekommt deshalb kein Werkzeug, keine Shell und keinen Zugang
zu irgendeiner Laufzeit — er bekommt einen **Befundbogen**: den letzten
bekannten Zustand jedes Teils, ein paar zurueckliegende Vorfaelle, und die
Topologie, aus der sich Zusammenhaenge ergeben. Das reicht, um zu korrelieren,
und reicht nicht, um etwas anzufassen. Genau so ist es gemeint.

Zwei Feinheiten, die sonst falsch gelesen werden:

* **`unknown` ist nicht `gesund`.** Es heisst „nicht gemessen". Der Bogen sagt
  das ausdruecklich, weil ein Modell sonst aus einem grauen Punkt einen gruenen
  macht.
* **Ein Befund hat ein Alter.** Ein Wert von vor einer Stunde ist eine Messung
  von damals. Also steht das Alter dabei, nicht nur der Wert.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from solvio.bots.redaction import redact
from solvio.logging_setup import get_logger

log = get_logger("bots")

#: Wie viele Teile und Vorfaelle hoechstens mitkommen. Ein Bogen ohne Grenze
#: waere ein Journal, und ein Journal beantwortet keine Frage.
MAX_COMPONENTS = 40
MAX_EVENTS = 20

#: Wie lang ein einzelner Grund sein darf.
MAX_REASON = 200

HEADER = """# Befundbogen (von SOLVIO gemessen)

Das hier sind strukturierte Befunde aus SOLVIOs eigener Messung. Sie sind
vollstaendig in dem Sinn, dass nichts weggelassen wurde — und unvollstaendig in
dem Sinn, dass nicht alles gemessen werden konnte.

* **`unknown` heisst „nicht gemessen", nicht „gesund".** Behandle es als Luecke.
* **Jeder Befund hat ein Alter.** Ein alter Wert ist eine Messung von damals.
* **Was hier nicht steht, kennst du nicht.** Erfinde keine Zahlen, keine
  Prozesse und keine Protokollzeilen.
* **Du reparierst nichts.** Nenne die wahrscheinlichste Ursache und den
  naechsten MESSSCHRITT. Ein Neustart ist keine Messung.
"""


@dataclass
class Evidence:
    """Der fertige Bogen und die Buchhaltung darueber."""

    text: str
    components: int = 0
    events: int = 0
    unknown: list[str] = field(default_factory=list)
    unhealthy: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.text)


def build(*, components: Iterable[dict[str, Any]] = (),
          events: Iterable[dict[str, Any]] = (),
          architecture: Iterable[str] = (),
          now: float | None = None) -> Evidence:
    """Baut den Bogen aus bereits strukturierten Angaben.

    Der Bogen nimmt ausdruecklich **Woerterbuecher** und keine lebenden Objekte:
    was hier hineingeht, hat der Core vorher in seine eigene Form gebracht. Ein
    Bot, der ein `HealthBoard` in der Hand haelt, koennte es auffordern zu
    messen — und dann waere die Messung seine.
    """
    moment = now if now is not None else time.time()
    evidence = Evidence(text="")
    parts: list[str] = [HEADER, "\n## Zustand der Teile\n\n"]

    seen = 0
    for entry in components:
        if seen >= MAX_COMPONENTS:
            parts.append(f"\n[weitere Teile ausgelassen — Obergrenze "
                         f"{MAX_COMPONENTS}]\n")
            break
        seen += 1
        parts.append(_component_line(entry, moment, evidence))
    if seen == 0:
        parts.append("Es liegt KEIN Zustand vor. Damit kannst du nichts "
                     "diagnostizieren — sage das.\n")
    evidence.components = seen

    parts.append("\n## Zurueckliegende Vorfaelle\n\n")
    count = 0
    for entry in events:
        if count >= MAX_EVENTS:
            parts.append(f"\n[weitere Vorfaelle ausgelassen — Obergrenze "
                         f"{MAX_EVENTS}]\n")
            break
        count += 1
        parts.append(_event_line(entry, moment))
    if count == 0:
        parts.append("Keine aufgezeichneten Vorfaelle im betrachteten Fenster.\n")
    evidence.events = count

    facts = [line for line in architecture if str(line).strip()]
    parts.append("\n## Bekannte Topologie\n\n")
    if facts:
        parts.extend(f"* {redact(str(line))[:240]}\n" for line in facts[:20])
    else:
        parts.append("Nicht verfuegbar. Schliesse nichts ueber Zusammenhaenge, "
                     "das hier nicht steht.\n")

    evidence.text = "".join(parts)
    log.info("bots.evidence_built", components=evidence.components,
             events=evidence.events, unknown=len(evidence.unknown),
             unhealthy=len(evidence.unhealthy))
    return evidence


def _component_line(entry: dict[str, Any], moment: float, evidence: Evidence) -> str:
    key = str(entry.get("komponente") or entry.get("key") or "?")[:60]
    label = str(entry.get("name") or entry.get("label") or key)[:80]
    state = str(entry.get("zustand") or entry.get("state") or "unknown")[:40]
    reason = redact(str(entry.get("grund") or entry.get("reason") or ""))[:MAX_REASON]
    checked = _age(entry.get("geprueft_um") or entry.get("last_checked_at"), moment)
    healthy = _age(entry.get("zuletzt_gesund") or entry.get("last_success_at"), moment)
    if state == "unknown":
        evidence.unknown.append(key)
    elif state != "healthy":
        evidence.unhealthy.append(key)
    return (f"* **{key}** ({label}) — Zustand `{state}`"
            f"{', Grund: ' + reason if reason else ''}"
            f" · gemessen {checked} · zuletzt gesund {healthy}\n")


def _event_line(entry: dict[str, Any], moment: float) -> str:
    component = str(entry.get("component") or entry.get("komponente") or "?")[:60]
    result = redact(str(entry.get("last_result") or entry.get("ergebnis") or ""))[:120]
    attempts = entry.get("attempts")
    at = _age(entry.get("resolved_at") or entry.get("last_seen") or entry.get("at"),
              moment)
    resolved = entry.get("resolved_at")
    return (f"* **{component}** — {result or 'ohne Ergebnisvermerk'}"
            f"{f' · {int(attempts)} Versuch(e)' if attempts else ''}"
            f" · {'abgeschlossen' if resolved else 'offen'} · vor {at}\n")


def _age(value: Any, moment: float) -> str:
    """Wie alt eine Angabe ist. `nie` ist eine Antwort, `0` waere eine Luege."""
    try:
        stamp = float(value or 0.0)
    except (TypeError, ValueError):
        return "unbekannt"
    if stamp <= 0:
        return "nie"
    delta = max(0.0, moment - stamp)
    if delta < 90:
        return f"{int(delta)} s"
    if delta < 5400:
        return f"{int(delta / 60)} min"
    return f"{int(delta / 3600)} h"


def architecture_facts(repo_root: str) -> list[str]:
    """Die Topologie — aus `project_state.yaml`, nicht aus einer zweiten Liste.

    Es waere bequemer, hier fuenf Saetze hinzuschreiben. Es waere auch die
    zweite Wahrheit, die irgendwann von der ersten abweicht. Also wird die
    maschinenlesbare Orientierungsschicht gelesen, die genau dafuer da ist —
    und wenn sie fehlt, gibt es eben keine Topologie.
    """
    path = os.path.join(repo_root, "docs", "project_state.yaml")
    if not os.path.isfile(path):
        return []
    try:
        import yaml
        with open(path, encoding="utf-8") as handle:
            state = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001 - ohne Topologie laeuft alles weiter
        log.info("bots.architecture_facts_failed", kind=type(exc).__name__)
        return []

    facts: list[str] = []
    ownership = state.get("ownership") or {}
    for key, value in list(ownership.items())[:6]:
        facts.append(f"{key}: {value}")
    runtimes = state.get("runtimes") or {}
    for name, entry in list(runtimes.items())[:10]:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role", "")).strip().replace("\n", " ")
        host = str(entry.get("host", "")).strip()
        ports = entry.get("ports")
        detail = ", ".join(part for part in (
            f"laeuft auf {host}" if host else "",
            f"Ports {ports}" if ports else "") if part)
        facts.append(f"{name}: {role}{' — ' + detail if detail else ''}")
    return facts
