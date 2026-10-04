"""Was ein Bot zurueckgibt — und was ausdruecklich nicht.

Drei Dinge fehlen hier mit Absicht, und es sind dieselben drei wie beim
Fachteam:

* **Kein Gedankengang.** Aufgehoben werden Schluesse und Belege. Der Weg dorthin
  ist weder nachpruefbar noch verlaesslich, und wer ihn aufhebt, hebt vor allem
  Text auf, der spaeter wie eine Begruendung gelesen wird.
* **Kein Anmeldedatum.** Der Rohtext eines fremden Prozesses kann einen
  Schluessel in einer Fehlermeldung tragen; er wird entschaerft, bevor er
  irgendwo landet.
* **Keine Autoritaet.** Es gibt kein Feld `risk`, kein `approved`, kein
  `needs_approval`. Ein Bot kann „das ist harmlos" in einen Schluss schreiben —
  als Notiz, die SOLVIO liest. Ein Feld, das der Vertragsschicht aehnlich
  saehe, wuerde frueher oder spaeter mit ihr verwechselt.

`confidence` ist eine **Selbsteinschaetzung** und wiegt weniger als ein Beleg.
Ein Modell, das sich sicher ist, ist nicht haeufiger richtig — es ist haeufiger
ueberzeugend.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from solvio.bots.redaction import redact
from solvio.contracts.untrusted import neutralize

#: Der Rang jeder Botantwort. Derselbe Gedanke wie bei Hermes und beim Fachteam:
#: hilfreich, fremd, niemals Autoritaet.
CONTENT_TRUST = "untrusted_executor"

#: Die verlangte Form. Geht woertlich in die Frage — und wird auf SOLVIOs Seite
#: geprueft, nicht geglaubt.
ANSWER_SCHEMA = """{
  "conclusions": ["kurze, pruefbare Feststellungen"],
  "evidence": ["Quelle je Feststellung: URL, Dateiname oder Befund"],
  "unknowns": ["was du NICHT beantworten kannst und warum"],
  "next_step": "ein Satz: der naechste sinnvolle Schritt, oder leer",
  "confidence": "hoch | mittel | niedrig"
}"""

_CONFIDENCE = ("hoch", "mittel", "niedrig")
_MAX_ITEMS = 12
_MAX_ITEM = 600
_MAX_EXCERPT = 2000


@dataclass
class BotAnswer:
    """Die Antwort eines Bots, in vergleichbarer Form."""

    role: str
    profile: str
    question: str
    #: `ok=False` heisst nicht „falsch", sondern „hat nicht geantwortet".
    ok: bool = False
    #: Warum nicht — `timeout`, `provider_auth`, `provider_quota`,
    #: `posture_violation`, `executor_unavailable`, `malformed`, …
    reason: str = ""
    conclusions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    next_step: str = ""
    confidence: str = ""
    elapsed: float = 0.0
    #: Was der Prozess ueber seine eigene Werkzeugflaeche gesagt hat.
    tools: list[str] = field(default_factory=list)
    #: Der Rohtext, entschaerft und gedeckelt — falls die Gliederung misslang.
    raw_excerpt: str = ""
    #: Ob die Antwort dem vereinbarten Schema folgte.
    structured: bool = False

    @property
    def usable(self) -> bool:
        return self.ok and bool(self.conclusions or self.unknowns)

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "rolle": self.role,
            "bot": self.profile,
            "frage": self.question[:400],
            "erreichbar": self.ok,
            "schluesse": self.conclusions[:_MAX_ITEMS],
            "belege": self.evidence[:_MAX_ITEMS],
            "unbekannt": self.unknowns[:_MAX_ITEMS],
            "naechster_schritt": self.next_step[:800],
            "selbsteinschaetzung": self.confidence or "unbekannt",
            "dauer_s": round(self.elapsed, 1),
            "werkzeuge": sorted(self.tools),
            "strukturiert": self.structured,
            "content_trust": CONTENT_TRUST,
        }
        if not self.ok:
            entry["grund"] = self.reason
        if not self.structured and self.raw_excerpt:
            entry["rohtext"] = self.raw_excerpt
        return entry


def failed(role: str, profile: str, question: str, reason: str, *,
           elapsed: float = 0.0, excerpt: str = "") -> BotAnswer:
    """Eine ehrliche Nichtantwort. Kein leeres Ergebnis, das nach Erfolg aussieht."""
    return BotAnswer(role=role, profile=profile, question=question, ok=False,
                     reason=reason, elapsed=elapsed,
                     raw_excerpt=neutralize(redact(excerpt), limit=_MAX_EXCERPT))


def parse(role: str, profile: str, question: str, text: str, *,
          elapsed: float = 0.0, tools: list[str] | None = None) -> BotAnswer:
    """Liest die Antwort — und bleibt heil, wenn sie dem Schema nicht folgt.

    Ein Bot ist ein fremdes Programm mit einem eigenen Modell. Dass es sauberes
    JSON liefert, ist eine Hoffnung, keine Zusage; und der Strom, in dem es
    steht, traegt zusaetzlich Hermes' Anlaufmeldungen und die Ergebnisse seiner
    Werkzeuge. Gesucht wird deshalb das LETZTE Objekt mit der Antwortform (siehe
    `_object`). Misslingt auch das, entsteht trotzdem ein Ergebnis — als
    unstrukturierte Antwort, ausdruecklich als solche markiert.
    """
    answer = BotAnswer(role=role, profile=profile, question=question, ok=True,
                       elapsed=elapsed, tools=list(tools or []))
    # Entschaerfen kommt VOR dem Lesen, nicht danach. Der Laeufer tut es schon;
    # hier noch einmal, damit die Zusage auch dann gilt, wenn dieser Parser
    # einmal an einem anderen Strom haengt.
    body = redact(text or "").strip()
    answer.raw_excerpt = neutralize(_tail(body), limit=_MAX_EXCERPT)

    data = _object(body)
    if data is None:
        answer.conclusions = [neutralize(_tail(body, 1200), limit=1200)] if body else []
        answer.unknowns = ["Antwort folgte nicht dem vereinbarten Schema"]
        answer.confidence = "niedrig"
        return answer

    answer.structured = True
    answer.conclusions = _strings(data, "conclusions")
    answer.evidence = _strings(data, "evidence")
    answer.unknowns = _strings(data, "unknowns")
    answer.next_step = neutralize(str(data.get("next_step", "")), limit=800)
    confidence = str(data.get("confidence", "")).strip().lower()
    answer.confidence = confidence if confidence in _CONFIDENCE else ""
    return answer


def _strings(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if isinstance(value, str):
        return [neutralize(value, limit=_MAX_ITEM)]
    if isinstance(value, list):
        return [neutralize(str(item), limit=_MAX_ITEM) for item in value[:_MAX_ITEMS]]
    return []


def _tail(text: str, limit: int = _MAX_EXCERPT) -> str:
    """Der Schluss, nicht der Anfang: dort steht die Antwort."""
    return text[-limit:] if len(text) > limit else text


#: Die Felder, an denen eine Antwort als Antwort erkennbar ist.
_KEYS = frozenset({"conclusions", "evidence", "unknowns", "next_step", "confidence"})

#: Wie viele Objektanfaenge hoechstens probiert werden. Die Antwort steht am
#: Ende; wer sie in so vielen Versuchen von hinten nicht findet, findet sie
#: nicht — und ein unbegrenzter Lauf ueber einen fremden Strom ist ein
#: Angriffsziel.
MAX_CANDIDATES = 600


def _object(text: str) -> dict[str, Any] | None:
    """Sucht die Antwort im Strom — von hinten, und mit einem echten Parser.

    Drei Entwuerfe, drei gemessene Fehlschlaege, und jeder hat etwas beigebracht:

    1. **Das groesste Objekt nehmen** liegt zuverlaessig daneben. Im
       ausfuehrlichen Modus steht das Ergebnis von `web_search` mit ein paar
       Kilobyte Suchtreffern im selben Strom, und das ist immer groesser als
       eine Antwort.
    2. **Auf „irgendein Objekt" zurueckfallen** ist schlimmer als kein Ergebnis:
       bei einer formlosen Antwort wurde das naechstbeste Woerterbuch genommen —
       meist ein Werkzeugergebnis — und daraus eine Antwort, die `strukturiert`
       hiess und in jedem Feld leer war. Ein Fehlschlag, der wie ein Erfolg
       aussieht, ist der teuerste.
    3. **Klammern selbst zaehlen** scheitert am Strom. Hermes kuerzt seine
       Protokollzeilen (`Tool call: … {"query": "…", "limit": 5}...`), und die
       Kuerzung faellt manchmal MITTEN in ein Objekt. Danach steht der Zaehler
       dauerhaft falsch, und die Antwort weiter hinten wird verschluckt — im
       laufenden Core genau so passiert.

    Was uebrig bleibt und traegt: von hinten jede Stelle probieren, an der ein
    Objekt beginnen koennte, und `raw_decode` entscheiden lassen. Ein echter
    Parser laesst sich von unbalanciertem Text davor nicht beirren, und „von
    hinten" ist zugleich die Abwehr — ein Werkzeugergebnis, das eine Antwort
    vortaeuscht (eine Webseite darf so etwas schreiben), steht zwangslaeufig
    VOR der echten.
    """
    decoder = json.JSONDecoder()
    tried = 0
    for start in _candidates(text):
        tried += 1
        if tried > MAX_CANDIDATES:
            break
        try:
            parsed, _end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(parsed, dict) and _KEYS & set(parsed):
            return parsed
    return None


def _candidates(text: str):
    """Die Positionen moeglicher Objektanfaenge — von hinten nach vorn."""
    index = text.rfind("{")
    while index >= 0:
        yield index
        index = text.rfind("{", 0, index)
