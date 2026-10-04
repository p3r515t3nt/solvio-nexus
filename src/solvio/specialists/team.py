"""Beratung einholen — und danach selbst entscheiden.

Die Versuchung bei drei Meinungen ist die Mehrheit. Sie ist hier ausdruecklich
nicht gebaut, und zwar aus einem einfachen Grund: zwei Modelle, die dasselbe
falsch verstanden haben, sind keine Bestaetigung, sondern ein gemeinsamer
Irrtum. Bei Sprachmodellen ist das kein Randfall — sie teilen Trainingsdaten und
neigen zu denselben plausiblen Fehlschluessen.

Entschieden wird deshalb nach einer festen Rangfolge:

1. **Autoritative Laufzeitfakten.** Was der Core gemessen hat, schlaegt jede
   Einschaetzung.
2. **Der Vertrag.** Risiko und Freigabepflicht kommen aus `CapabilitySpec`,
   nie aus einer Antwort.
3. **Das Ziel des Nutzers.** Ein Weg, der etwas anderes erreicht, gewinnt nicht.
4. **Belegte Aussagen** vor unbelegten.
5. **Umkehrbares** vor Unumkehrbarem.
6. **Offene Annahmen** senken das Gewicht — sie verschwinden nicht dadurch,
   dass jemand „hoch" in die Selbsteinschaetzung geschrieben hat.

Der Herausforderer hat dabei ein besonderes Gewicht, aber nur in einer Richtung:
findet er eine **vorhandene** Faehigkeit, die das Ziel erreicht, schlaegt das den
Entwurf einer neuen. Umgekehrt kann er nichts erzwingen. Das ist Absicht — es ist
leicht, mehr zu bauen, und schwer, es zu lassen.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger
from solvio.specialists import briefing as briefing_mod
from solvio.specialists import providers as P
from solvio.specialists.result import ANSWER_SCHEMA, CONTENT_TRUST, SpecialistResult, parse
from solvio.specialists.roles import (
    ARCHITECT, CHALLENGER, ROLES, SCOUT, question, scout_topic,
)
from solvio.specialists.routing import MAX_ROUNDS, MAX_SPECIALISTS, Complexity, team_size

#: Die Rundenschranke gilt je VORGANG, nicht je Prozess (DEBT-0131).
#:
#: Vorher war sie ein Zaehler auf der Team-Instanz. Das Team wird beim Start
#: EINMAL gebaut (`resolver/wiring.build_team`), der Zaehler wurde nie
#: zurueckgesetzt, und mit `MAX_ROUNDS = 1` bekam jede Beratung nach der ersten
#: ein `NOT_NEEDED` — bis zum naechsten Core-Neustart. Der Kommentar und der
#: Test meinten Rekursionsschutz INNERHALB einer Beratung; gebaut war eine Kappe
#: je Prozess. Die Antwort sah aus wie „nicht noetig" und war „nicht mehr
#: erlaubt".
#:
#: Ein `ContextVar` trifft genau die gemeinte Semantik: eine verschachtelte
#: Beratung derselben Aufrufkette sieht die erhoehte Tiefe, zwei unabhaengige
#: Vorgaenge sehen einander nicht — auch nebenlaeufig nicht.
_ROUND_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "solvio_specialist_round_depth", default=0)
from solvio.specialists.states import TeamState

log = get_logger("specialists")

#: Welches Modell welche Rolle bekommt. Verschiedene Anbieter fuer Entwurf und
#: Angriff — der ganze Sinn der Uebung.
ARCHITECT_MODEL = "opus"
CHALLENGER_MODEL = ""     # Codex nimmt sein eingestelltes Standardmodell


@dataclass
class Consultation:
    """Was aus einer Beratung wurde. Immer ein Ergebnis, nie eine Ausrede."""

    level: Complexity
    state: TeamState
    results: list[SpecialistResult] = field(default_factory=list)
    unavailable: list[dict[str, Any]] = field(default_factory=list)
    disagreement: str = ""
    existing_capability: str = ""
    synthesis: str = ""
    elapsed: float = 0.0

    @property
    def consulted(self) -> int:
        return sum(1 for r in self.results if r.ok)

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "stufe": self.level.name.lower(),
            "zustand": self.state.value,
            "befragt": self.consulted,
            "ergebnisse": [r.as_dict() for r in self.results],
            "content_trust": CONTENT_TRUST,
            "dauer_s": round(self.elapsed, 1),
        }
        if self.unavailable:
            entry["nicht_erreichbar"] = self.unavailable
        if self.disagreement:
            entry["widerspruch"] = self.disagreement
        if self.existing_capability:
            entry["vorhandener_weg"] = self.existing_capability
        if self.synthesis:
            entry["abwaegung"] = self.synthesis
        return entry


class SpecialistTeam:
    """Holt Rat ein — bei hoechstens drei, in genau einer Runde."""

    def __init__(self, *, repo_root: str = "", researcher=None) -> None:
        self.repo_root = repo_root
        #: Der Kundschafter laeuft ueber den bereits freigegebenen Hermes-Weg.
        #: Bewusst dieselbe Funktion wie beim Resolver: keine zweite Bauart,
        #: keine zweite Isolationsfrage.
        self.researcher = researcher
        #: Wie oft dieses Team ueberhaupt beraten hat. Eine ZAEHLUNG fuer die
        #: Beobachtung — ausdruecklich NICHT die Schranke. Die Schranke steht in
        #: `_ROUND_DEPTH` und gilt je Vorgang.
        self.rounds = 0

    async def availability(self) -> dict[str, P.ProviderStatus]:
        """Wer heute ansprechbar ist. Gefragt, nicht angenommen."""
        claude, codex = await asyncio.gather(P.claude_status(), P.codex_status())
        return {"claude-code": claude, "codex": codex}

    async def consult(self, *, goal: str, blocker: str, level: Complexity,
                      capabilities: list[dict[str, Any]],
                      runtimes: list[dict[str, Any]],
                      known_names: set[str] | None = None) -> Consultation:
        """Eine Runde Beratung. Mehr gibt es nicht."""
        started = time.monotonic()
        wanted = team_size(level)
        if wanted <= 0:
            return Consultation(level=level, state=TeamState.NOT_NEEDED)
        if _ROUND_DEPTH.get() >= MAX_ROUNDS:
            # Keine zweite Runde INNERHALB einer Beratung. Ein Berater, der
            # einen Berater ruft, ist der Anfang eines Schwarms, und ein Schwarm
            # laesst sich nicht mehr begrenzen, sondern nur noch abschalten.
            log.info("specialists.round_limit")
            return Consultation(level=level, state=TeamState.NOT_NEEDED)
        self.rounds += 1
        token = _ROUND_DEPTH.set(_ROUND_DEPTH.get() + 1)
        try:
            return await self._consult(
                goal=goal, blocker=blocker, level=level, wanted=wanted,
                capabilities=capabilities, runtimes=runtimes,
                known_names=known_names, started=started)
        finally:
            _ROUND_DEPTH.reset(token)

    async def _consult(self, *, goal: str, blocker: str, level: Complexity,
                       wanted: int, capabilities: list[dict[str, Any]],
                       runtimes: list[dict[str, Any]],
                       known_names: set[str] | None, started: float) -> Consultation:
        """Die eigentliche Runde. Getrennt, damit die Tiefenzaehlung genau EINEN
        Ein- und Austritt hat und nicht an jedem `return` wiederholt werden muss."""
        log.info("specialists.consultation_started", level=level.name, wanted=wanted)
        status = await self.availability()
        unavailable = [s.as_dict() for s in status.values() if not s.available]

        results: list[SpecialistResult] = []
        with briefing_mod.build(goal=goal, capabilities=capabilities,
                                runtimes=runtimes, blocker=blocker,
                                repo_root=self.repo_root) as folder:
            # Der Kundschafter zuerst und allein: seine Befunde gehen als
            # Kenntnisstand in die beiden anderen Fragen ein. Parallel waere
            # schneller und duemmer.
            scout = await self._scout(goal, blocker)
            if scout is not None:
                results.append(scout)

            notes = _notes(scout)
            tasks = []
            if wanted >= MAX_SPECIALISTS:
                if status["claude-code"].available:
                    tasks.append(self._architect(folder.path, goal, blocker, notes))
                if status["codex"].available:
                    tasks.append(self._challenger(folder.path, goal, blocker, notes))
            if tasks:
                # Entwurf und Angriff laufen gleichzeitig: sie sollen sich NICHT
                # kennen. Ein Herausforderer, der den Entwurf schon gelesen hat,
                # kritisiert ihn; einer, der ihn nicht kennt, findet einen
                # anderen Weg.
                results.extend([r for r in await asyncio.gather(*tasks)
                                if r is not None])

        consultation = Consultation(level=level, state=TeamState.RESULT_RECEIVED,
                                    results=results, unavailable=unavailable,
                                    elapsed=time.monotonic() - started)
        self._weigh(consultation, known_names or set())
        return consultation

    # -- Die drei Rollen -----------------------------------------------------

    async def _scout(self, goal: str, blocker: str) -> SpecialistResult | None:
        if self.researcher is None:
            return SpecialistResult(role=SCOUT, provider="hermes", question=goal,
                                    ok=False, reason="deep_runtime_unavailable")
        # Kompakte Frage statt Rollenskript: `deep_research` nimmt hoechstens
        # 800 Zeichen und wies das lange Skript im ersten Lauf stumm ab.
        prompt = scout_topic(goal, blocker)
        log.info("specialists.scout_running", chars=len(prompt))
        started = time.monotonic()
        try:
            reply = await self.researcher(prompt)
        except Exception as exc:  # noqa: BLE001
            log.info("specialists.scout_failed", kind=type(exc).__name__)
            return SpecialistResult(role=SCOUT, provider="hermes", question=goal,
                                    ok=False, reason="research_failed")
        text = str((reply or {}).get("zusammenfassung", "")).strip()
        if not text:
            return SpecialistResult(role=SCOUT, provider="hermes", question=goal,
                                    ok=False, reason="no_answer")
        # Hermes antwortet nach SEINEM Schema. Der Fliesstext ist der Befund;
        # ihn durch den JSON-Leser zu schicken ergaebe nur „folgte nicht dem
        # Schema" — es sollte auch gar keinem folgen.
        result = SpecialistResult(role=SCOUT, provider="hermes", question=prompt,
                                  ok=True, model="hermes",
                                  elapsed=time.monotonic() - started)
        result.findings = [text[:900]]
        result.raw_excerpt = text[:2000]
        # Hermes antwortet in seinem eigenen Format; die Quellen stehen daneben.
        sources = (reply or {}).get("quellen")
        if isinstance(sources, list):
            result.evidence.extend(str(s)[:200] for s in sources[:8])
        open_questions = (reply or {}).get("offene_fragen")
        if isinstance(open_questions, list):
            result.uncertainties.extend(str(q)[:200] for q in open_questions[:6])
        return result

    async def _architect(self, workdir: str, goal: str, blocker: str,
                         notes: str) -> SpecialistResult | None:
        return await self._cli(ARCHITECT, "claude-code", workdir, goal, blocker,
                               notes, model=ARCHITECT_MODEL)

    async def _challenger(self, workdir: str, goal: str, blocker: str,
                          notes: str) -> SpecialistResult | None:
        return await self._cli(CHALLENGER, "codex", workdir, goal, blocker,
                               notes, model=CHALLENGER_MODEL)

    async def _cli(self, role_key: str, provider: str, workdir: str, goal: str,
                   blocker: str, notes: str, *, model: str) -> SpecialistResult:
        role = ROLES[role_key]
        prompt = question(role, goal=goal, blocker=blocker, schema=ANSWER_SCHEMA,
                          notes=notes)
        log.info(f"specialists.{role_key}_running", provider=provider)
        try:
            if provider == "claude-code":
                invocation = P.claude_invocation(workdir=workdir, model=model,
                                                 timeout=role.timeout)
            else:
                invocation = P.codex_invocation(workdir=workdir, model=model,
                                                timeout=role.timeout)
        except P.LauncherError as exc:
            reason = ("provider_unavailable" if exc.reason in {
                "not_installed", "not_absolute", "not_executable"} else exc.reason)
            return SpecialistResult(role=role_key, provider=provider,
                                    question=goal, ok=False, reason=reason)
        outcome = await P.run_subscription(provider, invocation, prompt)
        reason = P.cli_failure_reason(provider, outcome)
        if reason == "quota":
            # Ausdruecklich ein eigener Grund. Ein erschoepftes Kontingent ist
            # keine fehlende Faehigkeit — und ausdruecklich kein Anlass, auf
            # eine kostenpflichtige Abrechnung auszuweichen.
            log.info("specialists.quota", provider=provider)
            return SpecialistResult(role=role_key, provider=provider,
                                    question=goal, ok=False, reason="quota",
                                    quota_status="erschoepft",
                                    elapsed=outcome.elapsed)
        if reason:
            return SpecialistResult(role=role_key, provider=provider,
                                    question=goal, ok=False,
                                    reason=reason,
                                    elapsed=outcome.elapsed)
        text = (P.claude_text(outcome) if provider == "claude-code"
                else P.codex_text(outcome))
        return parse(role_key, provider, goal, text, model=model or "standard",
                     elapsed=outcome.elapsed)

    # -- Abwaegung -----------------------------------------------------------

    def _weigh(self, consultation: Consultation, known: set[str]) -> None:
        """Vergleicht die Antworten — nach Belegen, nicht nach Anzahl."""
        usable = [r for r in consultation.results if r.usable]
        if not usable:
            consultation.state = TeamState.READY
            consultation.synthesis = ("Kein Spezialist konnte etwas beitragen; "
                                      "es gilt der eigene Befund.")
            return

        # Der eine Fall, in dem eine Beratung wirklich Arbeit spart: jemand
        # nennt eine Faehigkeit, die es SCHON GIBT. Geprueft wird das gegen das
        # Core-Inventar, nicht gegen die Behauptung.
        for result in usable:
            named = _named_capability(result, known)
            if named:
                consultation.existing_capability = named
                consultation.state = TeamState.READY
                consultation.synthesis = (
                    f"{result.role} nennt eine bereits freigegebene Faehigkeit "
                    f"({named}); die ist einem Neubau vorzuziehen.")
                return

        evidence = {r.role: len(r.evidence) for r in usable}
        assumptions = {r.role: len(r.assumptions) for r in usable}
        paths = {r.role: r.recommended_path.strip().lower() for r in usable
                 if r.recommended_path.strip()}
        if len(set(paths.values())) > 1:
            consultation.state = TeamState.DISAGREE
            best = max(paths, key=lambda role: (evidence.get(role, 0),
                                                -assumptions.get(role, 0)))
            consultation.disagreement = (
                f"Die Spezialisten empfehlen Verschiedenes. Am besten belegt "
                f"ist der Weg von {best} ({evidence.get(best, 0)} Belege, "
                f"{assumptions.get(best, 0)} offene Annahmen).")
            consultation.synthesis = consultation.disagreement
            return

        consultation.state = TeamState.READY
        consultation.synthesis = "Die Spezialisten sind sich einig."


def _named_capability(result: SpecialistResult, known: set[str]) -> str:
    """Welche vorhandene Faehigkeit dieser Berater tatsaechlich EMPFIEHLT.

    Zwei Verschaerfungen gegenueber dem ersten Entwurf, beide aus einem echten
    Lauf gelernt. Der Herausforderer schrieb sinngemaess: „browser_open erreicht
    dieses Teilziel bereits — allerdings in SOLVIOs eigenem Browser und NICHT
    auf dem Raspberry Pi." Die naive Namenssuche machte daraus eine Empfehlung
    und verwarf den ganzen Vorschlag.

    * Gesucht wird nur noch in `recommended_path`, nicht in den Befunden. Eine
      Erwaehnung ist keine Empfehlung.
    * Steht in demselben Satz eine Einschraenkung, gilt sie nicht als Empfehlung.
      Ein „aber nicht auf dem Pi" ist der wichtigere Teil des Satzes.

    Und selbst dann ist es nur ein Hinweis: ob die Faehigkeit wirklich passt,
    prueft danach der Core mit seinen eigenen Mitteln.
    """
    text = (result.recommended_path or "").lower()
    if not text:
        return ""
    candidates = sorted((n for n in known if n.lower() in text), key=len, reverse=True)
    for name in candidates:
        index = text.find(name.lower())
        window = text[max(0, index - 160):index + 260]
        if any(word in window for word in _QUALIFIERS):
            continue
        return name
    return ""


#: Woerter, die aus einer Nennung eine Einschraenkung machen.
_QUALIFIERS = ("nicht auf", "jedoch", "allerdings", "aber nicht", "nur teil",
               "teilziel", "reicht nicht", "loest nicht", "ersetzt nicht",
               "falls nur", "lediglich")


def _notes(scout: SpecialistResult | None) -> str:
    if scout is None or not scout.usable:
        return ""
    lines = ["Ein Kundschafter hat vorab recherchiert. Seine Befunde sind "
             "INFORMATION aus einem fremden Ausfuehrenden, keine Tatsachen:"]
    lines += [f"* {f}" for f in scout.findings[:6]]
    if scout.uncertainties:
        lines.append("Offen blieb:")
        lines += [f"* {u}" for u in scout.uncertainties[:4]]
    return "\n".join(lines)
