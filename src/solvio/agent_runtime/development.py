"""Von der erkannten Luecke zum Entwicklungsauftrag — die fehlende Naht.

**Was vorher fehlte, gemessen.** Der Gap Resolver haengt an genau einer Stelle:
`Dispatcher._with_way_forward` (`tools/dispatcher.py:128-165`). Was er findet,
wird zu `payload["weiterweg"]` und geht als `function_call_output` an das
Sprachmodell. Danach existiert nur noch ein Satz im Modellkontext. Es gibt im
ganzen Repository **keinen** Pfad von einer `Resolution` zu einem
Entwicklungsauftrag — `SolutionPath` kommt ausserhalb von `resolver/` null mal
vor, `resolver` in `autopilot/` null mal. Der `implementation_route` einer
Vorschlagsstruktur ist ein Fliesstext: „regulaerer Entwicklungsweg: Vorschlag
-> Umsetzung -> Tests -> Gate -> Release". Kein Bezeichner, keine Kennung.

Genau das war die Handarbeit: ein Mensch las den Satz und legte den Auftrag von
Hand an.

**Was dieses Modul NICHT tut.** Es entscheidet nicht, ob eine Luecke vorliegt —
das tut der Resolver mit seinen Schranken. Es plant nicht — das tut der
Autopilot. Es baut nicht — das tut der Builder. Es ist die Verbindung, und sie
besteht aus drei Dingen: einer **deterministischen Kennung**, einem
**Vertragstext**, und einer **Feststellung, ob die Faehigkeit wirklich da ist**.

**Die Kennung traegt die Idempotenz.** Sie entsteht aus Lauf, Faehigkeit und
Luckenart — nicht aus der Zeit und nicht aus einem Zaehler. Zweimal dasselbe
Ereignis ergibt dieselbe Kennung, und `create_milestone` wirft dann
`milestone_exists`. Ein Neustart mitten in der Entwicklung erzeugt keinen
zweiten Auftrag; er findet den ersten.

**Ein gelungener Bau ist keine Bereitstellung.** Der Autopilot endet bei Commit
und Ref: `READY` hat eine leere Kantenmenge, `NEVER_PERMITTED` verbietet einem
Contract ausdruecklich `merge_to_main`, `deploy` und `restart_service`, und
nichts liest `refs/autopilot/*` weiter. Eine Faehigkeit entsteht ausschliesslich
beim Prozessstart des Cores (`build_dispatcher`). Deshalb fragt `is_available()`
den ROUTER und nicht den Autopilot — und deshalb ist die Bereitstellung eine
Owner-Entscheidung, die dieses Modul weder trifft noch umgeht.
"""
from __future__ import annotations

import hashlib
import os

#: Praefix der Kennung. Kurz, damit `_ID` in `autopilot/contract.py`
#: (`^[a-z0-9][a-z0-9-]{2,63}$`) sicher traegt.
PREFIX = "gap-"

#: Wie viele Hexstellen des Digests die Kennung traegt. 16 sind 64 Bit — genug,
#: dass zwei verschiedene Luecken nicht kollidieren, und kurz genug, dass ein
#: Mensch die Kennung noch vorlesen kann.
DIGEST_CHARS = 16


def milestone_id_for(*, run_id: str, capability: str, kind: str) -> str:
    """Die Kennung des Entwicklungsauftrags — deterministisch.

    Sie haengt am LAUF, nicht an der Aufgabe: zwei Laeufe desselben Auftrags
    sind zwei Versuche, und der zweite soll nicht auf dem Auftrag des ersten
    sitzen bleiben. Sie haengt an der FAEHIGKEIT und der ART, weil derselbe Lauf
    an zwei verschiedenen Luecken haengenbleiben kann.

    Nicht an der Zeit und nicht an einem Zaehler: sonst waere jedes wiederholte
    Ereignis ein neuer Auftrag, und genau das soll nicht passieren.
    """
    roh = f"{run_id}|{capability}|{kind}".encode("utf-8")
    return PREFIX + hashlib.sha256(roh).hexdigest()[:DIGEST_CHARS]


#: Hoechstens so viele Gedaechtnistreffer wandern in einen Auftrag, und so
#: lang darf jeder sein. Ein Entwicklungsvertrag ist kein Gedaechtnisauszug.
MAX_KNOWLEDGE = 3
MAX_KNOWLEDGE_CHARS = 240


async def known_solutions(knowledge, *, capability: str, goal: str) -> list[str]:
    """Was das Gedaechtnis ueber diese Luecke schon weiss — als INFORMATION.

    **Es ist der kanonische Leseweg, kein zweiter.** `MemoryService.search`
    geht ueber denselben Aktiv-Filter wie `active_records()`: superseierte,
    vergessene und ausserhalb ihres Gueltigkeitsfensters liegende Eintraege
    kommen nicht zurueck. Diese Funktion umgeht daran nichts — sie fragt und
    kuerzt.

    **Und sie autorisiert nichts.** Was hier herauskommt, steht spaeter im
    Entwicklungsvertrag unter „bekannt aus dem Gedaechtnis" — es beschreibt,
    was schon einmal half. Es entscheidet nicht, ob entwickelt wird (das tut
    der Resolver), und es ersetzt keine Pruefung. Gedaechtnis ist Information,
    nie Autoritaet.

    Herkunft und Alter reisen mit: ohne sie waere ein Satz von gestern
    ununterscheidbar von einem, der seit einem Jahr nicht mehr stimmt.

    Faellt der Abruf aus, ist das kein Fehler dieses Wegs — der Auftrag
    entsteht dann ohne Vorwissen. Ein Gedaechtnis, das schweigt, darf keine
    Entwicklung aufhalten.
    """
    if knowledge is None:
        return []
    frage = f"{capability} {goal}".strip()
    try:
        treffer = await knowledge.search(frage, top_k=MAX_KNOWLEDGE)
    except Exception:  # noqa: BLE001 - ein stummes Gedaechtnis ist kein Fehler
        return []
    zeilen = []
    for hit in list(treffer)[:MAX_KNOWLEDGE]:
        text = str(getattr(hit, "content", "") or "").strip()
        if not text:
            continue
        wann = str(getattr(hit, "created_at", "") or "")[:10]
        # Die Art des Eintrags ueber Attributzugriff, nicht ueber eine
        # Zeichenkette: eine Zusicherung der Laufzeit verbietet Konstanten, die
        # wie ein Faehigkeitsname der Gedaechtnis- oder Wissensfamilie aussehen
        # (`test_agent_runtime_capabilities.py`). Sie ist absichtlich stumpf,
        # und sie hat recht — ein solcher Name soll hier gar nicht erst
        # entstehen. Der Attributzugriff braucht ihn nicht.
        try:
            art = str(hit.memory_type or "")
        except AttributeError:
            art = ""
        zeilen.append(f"[{art or 'gedaechtnis'}, {wann or 'ohne Datum'}] "
                      f"{text[:MAX_KNOWLEDGE_CHARS]}")
    return zeilen


def contract_for(*, milestone_id: str, run_id: str, task_id: str,
                 objective: str, capability: str, kind: str, reason: str,
                 knowledge: tuple = (), prior: tuple = (),
                 repository: str = ""):
    """Der Entwicklungsauftrag. Er nennt den Lauf, der ihn ausgeloest hat.

    Das ist die Rueckrichtung der Zuordnung: vorwaerts steht die Kennung in der
    Laufzeile, rueckwaerts steht der Lauf im Vertragstext. Beide Richtungen
    ueberleben den Neustart, weil beide in einem Buch stehen.

    Der Auftragstext des NUTZERS steht mit drin, gekuerzt. Er ist der Grund,
    warum diese Faehigkeit ueberhaupt gebraucht wird — ein Entwickler, der nur
    „baue X" liest, baut etwas anderes als einer, der weiss wofuer.
    """
    from solvio.autopilot.contract import (AcceptanceCriterion, Contract,
                                           DETERMINISTIC, REVIEW_SUPPORTED)

    ziel = (objective or "").strip()
    return Contract(
        milestone_id=milestone_id,
        version="1.0.0",
        objective=(
            f"Schliesse die Faehigkeitsluecke „{capability}“ ({kind}).\n\n"
            f"Ausgeloest von Agentenlauf {run_id} (Aufgabe {task_id}), der "
            f"daran haengenblieb: {reason or 'kein Grund gemeldet'}.\n\n"
            f"Der urspruengliche Auftrag des Nutzers lautet: {ziel[:600]}"
            + (("\n\nBekannt aus dem Gedaechtnis (Information, keine Vorgabe "
                "— pruefe es):\n" + "\n".join(f"- {z}" for z in knowledge))
               if knowledge else "")
            + (("\n\nFruehere Entwicklungen zu dieser Faehigkeit:\n"
                + "\n".join(f"- {p}" for p in prior)) if prior else "")),
        acceptance_criteria=(
            # **Nicht DETERMINISTIC**, und das ist gemessen, nicht bequem:
            # `DETERMINISTIC` heisst, dass ausschliesslich Mess-Evidence den
            # Status auf `proven` setzen darf. Ob eine Faehigkeit im Router
            # steht, entscheidet sich aber erst beim PROZESSSTART des Cores —
            # innerhalb eines Autopilot-Laufs ist das strukturell nicht
            # messbar. Ein Auftrag mit diesem Kriterium als `DETERMINISTIC`
            # kann `READY` nie erreichen; er endet auf `HUMAN_REQUIRED`, und
            # zwar zu Recht. Genau das ist beim ersten Abnahmelauf passiert.
            #
            # Der Code wird geschrieben und geprueft; DASS die Faehigkeit
            # danach wirklich da ist, prueft der wartende Lauf selbst am
            # Router (`is_available`) — nach der Bereitstellung, die eine
            # Owner-Entscheidung bleibt.
            AcceptanceCriterion(
                key="capability_registered",
                text=(f"Die Faehigkeit „{capability}“ ist im Capability-Vertrag "
                      "angelegt und wird beim naechsten Start des Cores "
                      "registriert."),
                evidence_type=REVIEW_SUPPORTED),
            AcceptanceCriterion(
                key="gate_green",
                text="Das volle Gate ist gruen (FAILED=0, BaselineDrift=0).",
                evidence_type=DETERMINISTIC),
        ),
        non_goals=(
            "Den urspruenglichen Nutzerauftrag selbst ausfuehren — das tut der "
            "Agentenlauf, sobald die Faehigkeit da ist.",
            "Die Freigabe-, Herkunfts- oder Ausfuehrungssemantik aendern.",
        ),
        security_boundaries=(
            "Keine Abschwaechung der Freigabematrix.",
            "Keine neue Autoritaet fuer Hintergrundlaeufe.",
        ),
        repository=repository)


#: Hoechstens so viele fruehere Entwicklungen wandern in einen neuen Auftrag.
MAX_PRIOR = 3


def prior_developments(store, *, capability: str) -> list[str]:
    """Was fuer DIESE Faehigkeit schon einmal gebaut wurde.

    **Die Antwort auf „wie wird technische Historie wiederauffindbar" — und sie
    braucht keine neue Ablage.** Der Autopilot-Buch traegt bereits alles: den
    Vertrag im Klartext (`contract_json`, und dort steht der Faehigkeitsname),
    den Zustand, den ausfuehrenden Commit, die Belege und die Phasen. Gesucht
    wird also im vorhandenen Buch, nicht in einem neuen.

    **Und ausdruecklich nicht im Gedaechtnis.** Ein Entwicklungsauftrag ist
    technische Historie; eine persoenliche Erinnerung ist etwas anderes. Sie zu
    mischen hiesse, dem Menschen seine eigene Ablage mit Bauprotokollen zu
    fuellen — und der Automatik eine Autoritaet zu geben, die sie dort nicht
    hat. Die beiden Buecher bleiben getrennt.

    Warum das ueberhaupt noetig ist: `milestone_id_for` haengt am LAUF, damit
    zwei Versuche zwei Auftraege sind. Ein zweiter Lauf an derselben Luecke
    findet den ersten deshalb nicht ueber die Kennung — sondern nur so.
    """
    if store is None or not capability:
        return []
    try:
        alle = store.milestones()
    except Exception:  # noqa: BLE001 - ein stummes Buch haelt nichts auf
        return []
    treffer = []
    for stein in alle:
        vertrag = str(getattr(stein, "contract_json", "") or "")
        if capability not in vertrag:
            continue
        commit = str(getattr(stein, "last_commit", "") or "")[:12]
        treffer.append(f"{stein.milestone_id} steht auf {stein.state}"
                       + (f", Commit {commit}" if commit else ""))
    return treffer[-MAX_PRIOR:]


def build_driver_factory(*, ledger, workspaces, repo: str = "",
                         python: str = "", adapters=None, lead=None):
    """Dieselbe Treiberkonfiguration wie `scripts/autopilot.py run`.

    **Hier lag der Anschlussfehler.** Der Orchestrator erwartete eine Fabrik,
    die Abnahme setzte sie am Objekt, und der Core uebergab keine — der
    automatische Start funktionierte damit ausschliesslich in der
    Testanordnung. Die behauptete Produktwirkung gab es nicht.

    Damit das nicht ein zweites Mal auseinanderlaeuft, baut diese EINE Funktion
    die Konfiguration, und sowohl der Core als auch die Abnahme rufen sie. Was
    hier steht, ist Zeile fuer Zeile das, was das Bedienskript tut:

    * `Driver(ledger, workspace=…, python=…)` — dasselbe Entwicklungsbuch,
      das auch `commission()` beschreibt; zwei Buecher waeren zwei Wahrheiten;
    * `lead = TechnicalLead()` — der echte Technical Lead;
    * `publisher = CheckpointPublisher()` — ohne ihn bleibt jeder Checkpoint im
      Klon, und das Offsite-Herkunftstor findet den ausfuehrenden Commit nicht
      (gemessen am 2026-09-03: 18 rote Zusicherungen);
    * die Builder-Adapter bleiben die Vorgabe des Treibers — hier wird KEINE
      Allowlist erweitert und kein Auftraggeber neu gepraegt.

    `adapters` und `lead` sind die beiden AEUSSEREN Abhaengigkeiten: ein
    echter Schreiber und ein echter Modellaufruf. Beide bleiben in der
    Produktion die Vorgabe (`None` → `default_adapters()` bzw.
    `TechnicalLead()`); eine isolierte Abnahme darf sie kontrollieren, ohne
    dafuer an dieser Verdrahtung vorbeizugehen. Genau das ist der Unterschied
    zwischen „derselbe Aufbauweg mit gestellter Aussenwelt" und „ein zweiter
    Aufbauweg".

    Der Arbeitsbereich ist der vorhandene isolierte: `WorkspaceManager.clone`
    legt je Auftrag einen eigenen Klon unter `agent_workspaces/<id>/repo` an,
    ohne `origin`, mit Isolationspruefung. Existiert er schon — zweiter Anlauf
    nach einem Neustart —, wird er wiederverwendet statt neu geklont.
    """
    def fabrik(milestone_id: str = ""):
        from solvio.agent_runtime import workspace as W
        from solvio.autopilot import driver as D
        from solvio.autopilot import lead as LEAD
        from solvio.autopilot import publisher as PUB

        kennung = milestone_id or "entwicklung"
        vorhanden = os.path.join(W.workspace_root(kennung), "repo")
        if os.path.isdir(vorhanden):
            pfad = vorhanden
        else:
            pfad = workspaces.clone(kennung, repo, slug="luecke").path
        treiber = D.Driver(ledger, workspace=pfad, python=python,
                           adapters=adapters)
        treiber.lead = lead if lead is not None else LEAD.TechnicalLead()
        treiber.publisher = PUB.CheckpointPublisher()
        return treiber

    return fabrik


def commission(store, contract) -> tuple[str, bool]:
    """Den Auftrag anlegen — oder feststellen, dass es ihn schon gibt.

    Rueckgabe `(milestone_id, neu)`. `neu=False` heisst ausdruecklich nicht
    „Fehler": es heisst, dass dasselbe Ereignis schon einmal ankam. Ein
    Neustart, eine doppelte Rueckmeldung und ein zweiter Takt landen alle hier.

    Die Einmaligkeit kommt aus dem Buch, nicht aus einer Pruefung davor: der
    Primaerschluessel wirft, und das ist der einzige Zeitpunkt, zu dem die
    Aussage stimmt. Ein `SELECT` davor waere ein Wettlauf.
    """
    from solvio.autopilot.store import LedgerError

    try:
        store.create_milestone(contract)
        return contract.milestone_id, True
    except LedgerError as exc:
        if str(getattr(exc, "code", "") or exc).startswith("milestone_exists") \
                or "milestone_exists" in str(exc):
            return contract.milestone_id, False
        raise


#: Wie lange ein Lauf hoechstens auf eine Entwicklung wartet.
#:
#: Der Treiber selbst ist durch `MAX_ROUNDS` begrenzt und kehrt zurueck — er
#: haengt nicht. Diese Grenze gilt dem ANDEREN Fall: es faehrt gar niemand.
#: Ein abgestuerzter Treiber, ein nie erschienener Arbeiter, ein Lock, das ein
#: fremder Prozess dauerhaft haelt. Ohne sie parkte der Auftrag des Nutzers
#: fuer immer, und „wartet noch" saehe genauso aus wie „kommt nie".
#:
#: Gemessen wird an `created_at` des Auftrags, nicht an einer Laufzeitvariable:
#: das ueberlebt den Neustart. Sechs Stunden sind grosszuegig fuer einen Bau
#: und kurz genug, dass ein Mensch am selben Tag erfaehrt, dass nichts geschah.
MAX_WAIT_SECONDS = 6 * 3600.0


#: Wie oft fuer EINEN Auftrag hoechstens gebaut werden darf.
#:
#: `Driver.run` haelt `MAX_ROUNDS = 12` — aber es kehrt danach zurueck, auch
#: wenn der Auftrag noch nicht fertig ist, und der Takt startet es dann erneut.
#: Gemessen am echten Notizlauf: ZWEI Treiberstarts, zusammen 16 Bauphasen und
#: 17 Modellurteile in 23 Minuten. Fuer eine Notiz. Ohne diese Grenze haette
#: nur die Sechs-Stunden-Uhr gestoppt — „unbemerkt beliebig viele Runden" ist
#: genau die Lage, die dabei entsteht.
#:
#: Vierundzwanzig sind zwei volle Treiberlaeufe: grosszuegig gegenueber dem
#: gemessenen Bedarf und trotzdem eine Zahl, die dasteht.
MAX_BUILD_PHASES = 24


def build_phases(store, milestone_id: str) -> int:
    """Wie oft fuer diesen Auftrag schon gebaut wurde — aus dem Buch.

    Nicht aus einem Zaehler im Arbeitsspeicher: der ueberlebt keinen Neustart,
    und genau ueber Neustarts hinweg soll die Grenze halten.
    """
    if store is None or not milestone_id:
        return 0
    try:
        return sum(1 for p in store.phases(milestone_id, limit=200)
                   if p.get("kind") == "build")
    except Exception:  # noqa: BLE001 - ein unlesbares Buch ist keine Erlaubnis
        return MAX_BUILD_PHASES


def overdue(milestone, *, now: float) -> bool:
    """Wartet dieser Auftrag laenger, als ein Auftrag warten darf?"""
    begonnen = float(getattr(milestone, "created_at", 0.0) or 0.0)
    return bool(begonnen) and (now - begonnen) > MAX_WAIT_SECONDS


def state_of(store, milestone_id: str) -> str:
    """Der Zustand des Auftrags — leer, wenn es ihn nicht (mehr) gibt."""
    from solvio.autopilot.store import LedgerError

    try:
        return str(store.milestone(milestone_id).state or "")
    except LedgerError:
        return ""


def is_available(router, capability: str) -> bool:
    """Ist die Faehigkeit JETZT wirklich aufrufbar?

    **Die entscheidende Frage dieses Moduls**, und sie geht an den Router, nicht
    an den Autopilot. Ein `READY`-Milestone heisst: gebaut, getestet, geprueft.
    Er heisst NICHT: benutzbar. Zwischen beidem liegt eine Owner-Entscheidung —
    Merge, Deploy, Neustart —, die ein Contract nicht einmal erbitten darf
    (`NEVER_PERMITTED`).

    Gefragt wird ueber `spec()`, weil das der Weg ist, auf dem auch der
    Planer und `_structural_flaw` den Vertrag lesen. Wirft er oder liefert
    `None`, ist die Faehigkeit nicht da — Unwissenheit ist hier ein Nein.
    """
    if router is None or not capability:
        return False
    getter = getattr(router, "spec", None)
    if not callable(getter):
        return False
    try:
        return getter(capability) is not None
    except Exception:  # noqa: BLE001 - ein Lesefehler ist kein „vorhanden"
        return False


__all__ = ["MAX_KNOWLEDGE", "PREFIX", "build_driver_factory",
           "commission", "contract_for",
           "MAX_BUILD_PHASES", "build_phases", "is_available",
           "known_solutions", "milestone_id_for",
           "overdue", "prior_developments", "state_of"]
