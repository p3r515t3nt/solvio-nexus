"""Was ein Spezialist zurueckgibt — und was ausdruecklich nicht.

Drei Dinge stehen hier bewusst NICHT:

* **Kein Gedankengang.** Gespeichert werden Schlussfolgerungen und Belege. Der
  Weg dorthin ist weder nachpruefbar noch verlaesslich, und wer ihn aufhebt,
  hebt vor allem Text auf, der spaeter wie eine Begruendung gelesen wird.
* **Kein Anmeldedatum.** Weder Token noch Sitzung noch Kontostand.
* **Keine Autoritaet.** Es gibt kein Feld `risk`, kein Feld `approved`, kein Feld
  `needs_approval`. Ein Spezialist kann „das ist harmlos" in `risk_notes`
  schreiben — als Notiz, die SOLVIO liest. Ein Feld, das der Vertragsschicht
  aehnlich saehe, wuerde frueher oder spaeter mit ihr verwechselt.

`confidence` ist ausdruecklich eine **Selbsteinschaetzung** und traegt in der
Abwaegung weniger als ein Beleg. Ein Modell, das sich sicher ist, ist nicht
haeufiger richtig — es ist haeufiger ueberzeugend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Der Rang, den jedes Spezialistenergebnis traegt. Derselbe Gedanke wie bei
#: Hermes: hilfreich, fremd, niemals Autoritaet.
CONTENT_TRUST = "untrusted_executor"

# Exactly one bounded Core observation may follow the existing evidence slots.
# It describes an observed tool attempt, never a source or a successful result.
NATIVE_BROWSER_OBSERVATION_PREFIX = "Core-Werkzeugbeobachtung: "


@dataclass
class SpecialistResult:
    """Die Antwort eines Spezialisten, in vergleichbarer Form."""

    role: str
    provider: str
    question: str
    #: `ok=False` heisst nicht „falsch", sondern „hat nicht geantwortet".
    ok: bool = False
    #: Warum nicht — `quota`, `logged_out`, `timeout`, `not_installed`, …
    reason: str = ""
    model: str = ""
    findings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    recommended_path: str = ""
    rejected_alternatives: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    #: Selbsteinschaetzung: hoch/mittel/niedrig. Bewusst kein Zahlenwert —
    #: eine Zahl wuerde zum Rechnen einladen, und gerechnet wird hier nicht.
    confidence: str = ""
    elapsed: float = 0.0
    quota_status: str = ""
    #: Der Rohtext, entschaerft und gedeckelt. Fuer den Fall, dass die
    #: Gliederung misslang und ein Mensch nachsehen will.
    raw_excerpt: str = ""

    @property
    def usable(self) -> bool:
        return self.ok and bool(self.findings or self.recommended_path)

    def as_dict(self) -> dict[str, Any]:
        entry = {
            "rolle": self.role, "anbieter": self.provider,
            "modell": self.model or "unbekannt",
            "frage": self.question[:400],
            "erreichbar": self.ok,
            "befunde": self.findings[:12],
            "belege": self.evidence[:12],
            "annahmen": self.assumptions[:8],
            "unsicherheiten": self.uncertainties[:8],
            "empfehlung": self.recommended_path[:800],
            "verworfen": self.rejected_alternatives[:8],
            "risikonotizen": self.risk_notes[:8],
            "selbsteinschaetzung": self.confidence or "unbekannt",
            "dauer_s": round(self.elapsed, 1),
            "kontingent": self.quota_status or "ok",
            "content_trust": CONTENT_TRUST,
        }
        if not self.ok:
            entry["grund"] = self.reason
        return entry


#: Das Schema, das ein Spezialist ausfuellen soll. Es wird ihm woertlich
#: mitgegeben, damit die Antworten vergleichbar sind — und es enthaelt
#: ausdruecklich kein Feld, in das jemand eine Freigabe schreiben koennte.
ANSWER_SCHEMA = """{
  "findings": ["kurze, pruefbare Feststellungen"],
  "evidence": ["Quelle, Datei:Zeile oder URL je Feststellung, soweit vorhanden"],
  "assumptions": ["worauf du dich stuetzt, ohne es geprueft zu haben"],
  "uncertainties": ["was offen bleibt"],
  "recommended_path": "ein Absatz: der aus deiner Sicht beste Weg",
  "rejected_alternatives": ["was du geprueft und verworfen hast, mit Grund"],
  "risk_notes": ["Hinweise auf Risiken, Nebenwirkungen, Umkehrbarkeit"],
  "confidence": "hoch | mittel | niedrig"
}"""


def parse(role: str, provider: str, question: str, text: str, *,
          model: str = "", elapsed: float = 0.0) -> SpecialistResult:
    """Liest die Antwort — und bleibt heil, wenn sie nicht dem Schema folgt.

    Ein Spezialist ist ein fremdes Programm mit einem eigenen Modell. Dass es
    sauberes JSON liefert, ist eine Hoffnung, keine Zusage. Misslingt das,
    entsteht trotzdem ein Ergebnis: der Rohtext als einzelner Befund. Eine
    Ausnahme mitten in der Beratung waere die schlechtere Antwort.
    """
    import json
    import re

    result = SpecialistResult(role=role, provider=provider, question=question,
                              ok=True, model=model, elapsed=elapsed)
    body = (text or "").strip()
    result.raw_excerpt = body[:2000]
    data: dict[str, Any] | None = None
    # Erst der ganze Text, dann der groesste geschweifte Block darin: ein CLI
    # stellt gern eine Zeile Prosa vor die Antwort.
    for candidate in (body, _largest_object(body)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            data = parsed
            break
    if data is None:
        result.findings = [body[:1200]] if body else []
        result.uncertainties = ["Antwort folgte nicht dem vereinbarten Schema"]
        return result

    def strings(key: str) -> list[str]:
        value = data.get(key)
        if isinstance(value, str):
            return [value[:600]]
        if isinstance(value, list):
            return [str(v)[:600] for v in value[:12]]
        return []

    result.findings = strings("findings")
    result.evidence = strings("evidence")
    result.assumptions = strings("assumptions")
    result.uncertainties = strings("uncertainties")
    result.rejected_alternatives = strings("rejected_alternatives")
    result.risk_notes = strings("risk_notes")
    result.recommended_path = str(data.get("recommended_path", ""))[:1500]
    confidence = str(data.get("confidence", "")).strip().lower()
    result.confidence = confidence if confidence in ("hoch", "mittel", "niedrig",
                                                     "high", "medium", "low") else ""
    return result


def _largest_object(text: str) -> str:
    start, depth, best = -1, 0, ""
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                block = text[start:index + 1]
                if len(block) > len(best):
                    best = block
    return best
