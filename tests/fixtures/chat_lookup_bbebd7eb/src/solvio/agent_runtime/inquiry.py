"""Die Auftragsauskunft — ein neues Gespraech findet einen Auftrag ohne Kennung.

**Der Anlass.** Ein Auftrag wird im Gespraech erteilt, das Gespraech endet, die
Freigabe kommt spaeter, der Lauf arbeitet stundenlang. Irgendwann fragt der
Mensch in einer frischen Gespraechsinstanz: „Was ist aus meinem Auftrag
geworden?" Diese Instanz kennt keine `ar-`-Kennung und keine Freigabekennung —
und sie soll auch keine von einem Pruefer oder vom Menschen ueberreicht
bekommen.

Die bisherige Laufansicht lieferte Kennungen, Zustand, Ergebnissatz und einen
Branch-Verweis. Es fehlten der **Wortlaut des Auftrags** (ohne ihn kann kein
Mensch sagen, WELCHER Lauf gemeint ist), die **strukturierte Nutzergrenze**
(was genau ist zu tun, und was passiert danach), die **Belege** (Befunde und
Quellen liegen als Artefakt, nicht in der Ansicht) — und der ganze Zustand
**vor** dem Lauf: eine Startfreigabe, die offen, abgelehnt, abgelaufen oder im
Ausgang ungewiss ist, ist noch kein Lauf und war bisher unsichtbar.

**Was hier nicht geschieht.** Nichts wird angelegt, gestartet, fortgesetzt oder
abgebrochen; die Auskunft ist rein lesend. Bei mehreren passenden Auftraegen
wird KEINER gewaehlt — die Kandidaten gehen zurueck, der Mensch entscheidet.
Ein „juengster Zeitstempel gewinnt" gibt es hier nicht. Und die Zuordnung
Freigabe → Lauf wird GELESEN (`pending_starts.run_id`, gebunden vom Poller aus
dem Ergebnis derselben Ausfuehrung), nie aus aehnlichem Text erschlossen: eine
Zeile ohne Verbindung heisst „Ausgang ungewiss", nicht „vermutlich jener Lauf".

Die Zustandsworte fuer Menschen wohnen hier und werden vom Nexus-Endpunkt
mitbenutzt — eine Liste, nicht zwei.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Awaitable, Callable

from solvio.agent_runtime import store as S
from solvio.agent_runtime.boundaries import UserBoundary
from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

#: Zustaende in Worte. Ein Mensch liest „wartet auf deine Freigabe", nicht
#: `WAITING_APPROVAL`.
STATE_WORDS = {
    "CREATED": "angenommen", "PLANNING": "plant", "RUNNING": "arbeitet",
    "WAITING_SPECIALIST": "wartet auf einen Spezialisten",
    "WAITING_CAPABILITY": "fuehrt etwas aus",
    "WAITING_APPROVAL": "wartet auf deine Freigabe",
    "WAITING_USER": "wartet auf dich",
    "VERIFYING": "prueft das Ergebnis",
    "SUCCEEDED": "fertig", "FAILED": "fehlgeschlagen",
    "CANCELLED": "abgebrochen", "INTERRUPTED": "unterbrochen",
}

REASON_WORDS = {
    "plan_invalid": "kein brauchbarer Plan",
    "specialist_unavailable": "kein Spezialist erreichbar",
    "specialist_failed": "der Spezialist kam nicht durch",
    "quota": "das Kontingent ist erschoepft",
    "capability_failed": "eine Handlung ist fehlgeschlagen",
    "approval_denied": "du hast es abgelehnt",
    "approval_expired": "die Freigabefrage ist verfallen",
    "policy_denied": "aus dem Hintergrund nicht erlaubt",
    "recovery_required": "der Ausgang ist ungewiss — bitte pruefen",
    "budget_exhausted": "die Grenze war erreicht",
    "loop_detected": "es drehte sich im Kreis",
    "timeout": "es hat zu lange gedauert",
    "interrupted": "ein Neustart kam dazwischen",
    "workspace_conflict": "die Arbeitskopie liess sich nicht anlegen",
    "cancelled_by_user": "du hast abgebrochen",
    "no_result": "es kam kein Arbeitsergebnis heraus",
    "planner_invalid_step": "ich habe denselben unvollstaendigen Schritt "
                            "zweimal geplant",
    "plan_unrecoverable": "nach der Unterbrechung war der Plan nicht mehr "
                          "rekonstruierbar",
    "goal_unverified": "ich habe ein Ergebnis, kann aber nicht feststellen, ob "
                       "dein Ziel damit erfuellt ist",
}

#: Welche Startfaehigkeit welchen Auftragsumfang erzeugt. Dieselben zwei
#: Namen wie `tools.agent_capability_tools.CREATION_CAPABILITIES`; ein Test
#: haelt die Gleichheit fest.
SCOPE_OF = {"agent_task_research": S.SCOPE_RESEARCH,
            "agent_task_build": S.SCOPE_BUILD}
KIND_WORDS = {S.SCOPE_RESEARCH: "Recherche", S.SCOPE_BUILD: "Codearbeit",
              S.SCOPE_ACTION: "Alltagsauftrag"}

#: Die Wiederaufnahme, die ebenfalls im Merkzettel liegen kann — sie erzeugt
#: keinen Lauf, sie gehoert zu einem.
RESUME_CAPABILITY = "agent_run_resume"

PHASE_BEFORE_RUN = "vor_dem_lauf"
PHASE_RUN = "lauf"

#: Die Zustaende VOR dem Lauf. Geschlossen, wie jede Vokabel im Buch.
FREIGABE_OFFEN = "freigabe_offen"
FREIGABE_ERTEILT = "freigabe_erteilt"
GESTARTET = "gestartet"
ABGELEHNT = "abgelehnt"
ABGELAUFEN = "abgelaufen"
START_UNGEWISS = "start_ungewiss"
START_GESCHEITERT = "start_gescheitert"
UNBEKANNT = "unbekannt"
START_STATES = frozenset({FREIGABE_OFFEN, FREIGABE_ERTEILT, GESTARTET, ABGELEHNT,
                          ABGELAUFEN, START_UNGEWISS, START_GESCHEITERT, UNBEKANNT})
#: Was davon noch nicht erledigt ist — fuer die Reihung der Kandidaten.
OPEN_START_STATES = frozenset({FREIGABE_OFFEN, FREIGABE_ERTEILT, START_UNGEWISS,
                               UNBEKANNT})

START_WORDS = {
    FREIGABE_OFFEN: "wartet auf deine Startfreigabe",
    FREIGABE_ERTEILT: "freigegeben — der Start folgt im naechsten Takt",
    GESTARTET: "gestartet",
    ABGELEHNT: "du hast den Start abgelehnt",
    ABGELAUFEN: "die Startfreigabe ist verfallen",
    START_UNGEWISS: "die Freigabe wurde eingeloest, aber welcher Lauf daraus "
                    "entstand, ist nicht verzeichnet",
    START_GESCHEITERT: "die Freigabe war da, aber der Start ist gescheitert",
    UNBEKANNT: "der Stand der Freigabe ist gerade nicht lesbar",
}

#: Wie viele Laeufe die Suche ueberhaupt betrachtet, und wie viele Kandidaten
#: bei Mehrdeutigkeit genannt werden. Beides sind Kappen, keine Auswahl: was
#: darueber liegt, wird als `gesamt` gezaehlt, nicht verschwiegen.
RECENT_LIMIT = 25
CANDIDATE_LIMIT = 5
#: Wie viel eines Berichtsartefakts gelesen wird. Ein Beleg ist ein Auszug.
MAX_REPORT_BYTES = 200_000
MAX_FINDINGS = 8
MAX_SOURCES = 12
MAX_OBJECTIVE_IN_CANDIDATE = 160

#: Ab welcher Wortlaenge ein Wort der Frage zaehlt, welcher Anteil der
#: zaehlenden Woerter im Auftrag vorkommen muss, damit er als KANDIDAT gilt,
#: und wie lang der Wortstamm ist, ueber den verglichen wird („Hotelsuche"
#: trifft „Hotels"). Absichtlich grob — und deshalb NIE eine Identitaet:
#: „Vergleiche Hotels in Hamburg" trifft so auch „Vergleiche Hotels in
#: Berlin". Gemessen am Review vom 09.09.2026: ein einziger solcher
#: Kandidat wurde als gefunden zurueckgegeben, und der Berliner Auftrag
#: galt als der Hamburger. Aehnliche Woerter liefern Kandidaten zur
#: Bestaetigung; gefunden ist nur, was Kennung oder exakter Wortlaut belegt.
MIN_TOKEN = 4
MATCH_SHARE = 0.6
STEM = 5

ApprovalReader = Callable[[str], Awaitable[dict[str, Any]]]


class InquiryUnreadable(RuntimeError):
    """Der Bestand liess sich nicht lesen. Das ist eine Auskunft ueber die
    Auskunft — und ausdruecklich KEIN leerer Bestand."""


# =====================================================================
# Die Sicht auf einen Lauf
# =====================================================================

def normalise(text: str) -> str:
    """Wortlaut vergleichbar machen — Gross-/Kleinschreibung, Satzzeichen,
    Leerraum. Mehr nicht; keine Aehnlichkeitsrechnung."""
    return " ".join(re.sub(r"[^\w\s]+", " ", (text or "").lower()).split())


def _report(ledger: S.AgentRunLedger, run_id: str) -> tuple[list[str], list[str]]:
    """Befunde und Quellen aus dem Berichtsartefakt des Laufs — falls es eins gibt.

    Das Artefakt entsteht beim Abschluss (`_write_report`) und ist STRUKTUR:
    zwei Listen, kein Transkript. Waehrend der Lauf noch arbeitet, gibt es
    nur die Zusammenfassungen der erledigten Spezialistenschritte.
    """
    try:
        artefakte = ledger.artifacts_for_run(run_id)
    except Exception:  # noqa: BLE001 - ein unlesbares Buch ist keine Auskunft
        return [], []
    for artefakt in artefakte:
        if artefakt.kind != "report":
            continue
        try:
            if os.path.getsize(artefakt.path) > MAX_REPORT_BYTES:
                continue
            with open(artefakt.path, encoding="utf-8") as handle:
                body = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        befunde = [str(b) for b in (body.get("befunde") or []) if str(b).strip()]
        quellen = [str(q) for q in (body.get("quellen") or []) if str(q).strip()]
        return befunde[:MAX_FINDINGS], quellen[:MAX_SOURCES]
    befunde = []
    with_steps = []
    try:
        with_steps = ledger.steps_for_run(run_id)
    except Exception:  # noqa: BLE001
        with_steps = []
    for step in with_steps:
        if step.kind == "specialist" and step.state == "succeeded":
            text = (step.summary or "").strip()
            if text and text not in befunde:
                befunde.append(text)
    return befunde[:MAX_FINDINGS], []


def _verification(ledger: S.AgentRunLedger, run_id: str) -> dict[str, Any] | None:
    """Der tatsaechliche Pruefstand eines Laufs: der letzte Verify-Schritt."""
    try:
        steps = ledger.steps_for_run(run_id)
    except Exception:  # noqa: BLE001
        return None
    pruefungen = [s for s in steps if s.kind == "verify"]
    if not pruefungen:
        return None
    letzte = pruefungen[-1]
    return {"zustand": letzte.state, "zusammenfassung": letzte.summary}


def _resume_start(ledger: S.AgentRunLedger, run_id: str) -> dict[str, Any] | None:
    """Eine offene Wiederaufnahme-Freigabe zu diesem Lauf — falls eine liegt."""
    try:
        zettel = ledger.starts_for_capability(RESUME_CAPABILITY, limit=RECENT_LIMIT)
    except Exception:  # noqa: BLE001
        return None
    for eintrag in zettel:
        if str((eintrag.get("arguments") or {}).get("run_id") or "") != run_id:
            continue
        if eintrag.get("state") != S.START_WAITING:
            continue
        return {"kennung": eintrag["request_id"], "zustand_code": FREIGABE_OFFEN,
                "zustand": "die Freigabe zum Weitermachen liegt auf deinem iPhone"}
    return None


def run_view(ledger: S.AgentRunLedger, run: S.AgentRun, *,
             task: S.AgentTask | None = None,
             start: dict[str, Any] | None = None) -> dict[str, Any]:
    """Die sichere, VOLLSTAENDIGE Betriebssicht auf einen Lauf.

    Deutsch, mit Wortlaut, Grenze und Belegen — und ohne Gedankengang, Prompt,
    Rohausgabe oder Geheimnis. Verweise auf Artefakte, nie deren Material.
    """
    if task is None:
        try:
            task = ledger.get_task(run.task_id)
        except Exception:  # noqa: BLE001
            task = None
    if start is None:
        try:
            start = ledger.start_for_run(run.run_id)
        except Exception:  # noqa: BLE001
            start = None
    grenze = UserBoundary.from_json(run.boundary or "")
    try:
        provider_wait = json.loads(run.boundary or "{}").get("provider_wait", {})
    except (ValueError, AttributeError):
        provider_wait = {}
    if not isinstance(provider_wait, dict):
        provider_wait = {}
    bound_route = ledger.provider_route_for_run(run.run_id)
    route = bound_route
    for event in reversed(ledger.events_for_run(run.run_id)):
        if event.kind != "provider_route":
            continue
        try:
            latest = json.loads(event.ref)
        except (ValueError, TypeError):
            continue
        if isinstance(latest, dict) and latest.get("phase") in ("plan", "assessment", "action_compose"):
            route = latest
            break
    if provider_wait and run.state != S.WAITING_USER:
        grenze = None
    befunde, quellen = _report(ledger, run.run_id)
    scope = task.scope if task is not None else ""
    view: dict[str, Any] = {
        "phase": PHASE_RUN,
        "kennung": run.run_id,
        "aufgabe": run.task_id,
        "auftrag": task.objective if task is not None else "",
        "art": KIND_WORDS.get(scope, scope),
        "umfang": scope,
        "projekt": task.target_repo if task is not None else "",
        "herkunft": task.created_origin if task is not None else "",
        "zustand": STATE_WORDS.get(run.state, run.state),
        "zustand_code": run.state,
        "offen": not run.terminal,
        "angelegt": run.created_at,
        "begonnen": run.started_at,
        "beendet": run.finished_at,
        "ergebnis": run.result_summary,
        "grund": REASON_WORDS.get(run.failure_category, run.failure_category),
        "grund_code": run.failure_category,
        "spezialisten": run.specialist_count,
        "arbeitsergebnis": run.branch_ref,
        # **Ein Verweis auf einen Zweig heisst nicht, dass etwas produktiv
        # laeuft.** Die Uebernahme ist eine eigene Entscheidung des Menschen.
        "vorbereitet": bool(run.branch_ref),
        "uebernommen": False,
        "wartet_auf": grenze.as_dict() if grenze else None,
        "befunde": befunde,
        "quellen": quellen,
        "pruefung": _verification(ledger, run.run_id),
        "freigabe": str(start.get("request_id") or "") if start else "",
        "fortsetzung": _resume_start(ledger, run.run_id),
        "anbieter": route.get("provider", ""),
        "abrechnung": route.get("billing_mode", "unknown"),
        "angeforderte_abrechnung": bound_route.get("billing_mode", "unknown"),
        "anbieter_aufgerufen": bool(route.get("dispatch_started", False)),
        "anbieternutzung_gemeldet": bool(route.get("usage_reported", False)),
        "anbieterwartezeit_s": float(getattr(run, "provider_wait_seconds", 0.0) or 0.0),
    }
    if provider_wait and run.state == S.WAITING_USER:
        from solvio.agent_runtime import provider_switch as PS
        view["anbietergrenze"] = {
            "anbieter": provider_wait.get("provider", ""),
            "abrechnung": provider_wait.get("billing_mode", "unknown"),
            "angeforderte_abrechnung": provider_wait.get("requested_billing_mode", "subscription"),
            "grund": provider_wait.get("reason", "provider_unavailable"),
            "phase": provider_wait.get("phase", ""),
            "fortsetzbar": bool(provider_wait.get("resume_allowed", False)),
            "anbieterwechsel": "nur nach deiner Entscheidung",
            "wartet_seit": provider_wait.get("since", 0),
            "boundary_ref": PS.reference(run),
            "wechseloptionen": PS.offers(ledger, run),
        }
        view["anbieterwartezeit_s"] += max(0.0, time.time() - float(provider_wait.get("since") or time.time()))
    try:
        view["artefakte"] = [{"art": a.kind, "pfad": a.path}
                             for a in ledger.artifacts_for_run(run.run_id)]
    except Exception:  # noqa: BLE001
        view["artefakte"] = []
    return view


# =====================================================================
# Die Sicht auf eine Startfreigabe, aus der (noch) kein Lauf entstand
# =====================================================================

def start_state(eintrag: dict[str, Any], approval: dict[str, Any] | None) -> str:
    """Der Zustand einer Startfreigabe — aus Merkzettel UND Freigabespeicher.

    Die Reihenfolge ist die Beweislast: ein verzeichneter Lauf ist ein Lauf;
    Ablehnung und Verfall sind endgueltig; eine noch wartende Zeile richtet
    sich nach der Freigabe; und alles, was eingeloest wurde, ohne dass ein
    Lauf verzeichnet ist, heisst UNGEWISS — nicht „vermutlich gestartet".
    """
    if str(eintrag.get("run_id") or ""):
        return GESTARTET
    if not approval or not approval.get("ok"):
        return UNBEKANNT
    zustand = str(approval.get("state") or "")
    if zustand == "DENIED":
        return ABGELEHNT
    if zustand == "EXPIRED":
        return ABGELAUFEN
    # **Der Beleg schlaegt die Zustandsspalte.** Ein Versuch mit ungewissem
    # Ausgang bleibt ungewiss, auch wenn die Zeile noch „wartet" — und ein
    # eingeloester Versuch ohne verzeichneten Lauf ist kein Lauf.
    if approval.get("uncertain") or approval.get("executed"):
        return START_UNGEWISS
    versuche = [str(v) for v in (approval.get("attempts") or [])]
    if zustand == "FAILED" or "FAILED_SAFE" in versuche or "FAILED" in versuche:
        return START_GESCHEITERT
    if eintrag.get("state") == S.START_WAITING:
        if zustand == "PENDING":
            return FREIGABE_OFFEN
        if zustand == "APPROVED":
            return FREIGABE_ERTEILT
        return START_UNGEWISS
    return START_UNGEWISS


def start_view(eintrag: dict[str, Any], approval: dict[str, Any] | None) -> dict[str, Any]:
    argumente = eintrag.get("arguments") or {}
    scope = SCOPE_OF.get(str(eintrag.get("capability") or ""), "")
    code = start_state(eintrag, approval)
    return {
        "phase": PHASE_BEFORE_RUN,
        "kennung": eintrag["request_id"],
        "auftrag": str(argumente.get("objective") or ""),
        "art": KIND_WORDS.get(scope, scope),
        "umfang": scope,
        "projekt": str(argumente.get("repository") or ""),
        "herkunft": str(eintrag.get("origin") or ""),
        "zustand": START_WORDS.get(code, code),
        "zustand_code": code,
        "offen": code in OPEN_START_STATES,
        "angelegt": eintrag.get("created_at"),
        "freigabe": eintrag["request_id"],
        "freigabe_zustand": str((approval or {}).get("state") or ""),
        "lauf": str(eintrag.get("run_id") or ""),
        "wartet_auf": None,
        "ergebnis": "",
        "befunde": [],
        "quellen": [],
    }


# =====================================================================
# Die Suche
# =====================================================================

def _short(view: dict[str, Any]) -> dict[str, Any]:
    """Kurztext fuer die Liste, Original zum Unterscheiden, Kennung zur Wahl."""
    return {"kennung": view["kennung"],
            "auftrag": str(view.get("auftrag") or "")[:MAX_OBJECTIVE_IN_CANDIDATE],
            "auftrag_vollstaendig": str(view.get("auftrag") or "")[:S.MAX_OBJECTIVE],
            "art": view.get("art", ""),
            "phase": view.get("phase", ""),
            "zustand": view.get("zustand", ""),
            "zustand_code": view.get("zustand_code", ""),
            "offen": bool(view.get("offen"))}


def _matches(query: str, objective: str) -> bool:
    tokens = [t for t in query.split() if len(t) >= MIN_TOKEN]
    if not tokens or not objective:
        return False
    hits = sum(1 for t in tokens
               if t in objective or (len(t) >= STEM and t[:STEM] in objective))
    return hits / len(tokens) >= MATCH_SHARE


def _prioritised(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Der juengste Auftrag bleibt sichtbar, danach offene nach Aktualitaet.

    Alte ungewisse Starts behalten ihren Zustand, koennen aber den gerade
    abgeschlossenen Auftrag nicht aus dem begrenzten Ausschnitt verdraengen.
    Das ist nur die Darstellung: auch der erste Kandidat ist nicht gewaehlt.
    """
    recent = sorted(entries, key=lambda e: -float(e.get("angelegt") or 0.0))
    if not recent:
        return []
    return recent[:1] + sorted(recent[1:], key=lambda e: (
        not e.get("offen"), -float(e.get("angelegt") or 0.0)))


async def _entries(ledger: S.AgentRunLedger, approval_status: ApprovalReader,
                   limit: int) -> list[dict[str, Any]]:
    """Alles, was ein Auftrag sein kann: die juengsten Laeufe (einer je
    Aufgabe) und die Startfreigaben, aus denen KEIN Lauf verzeichnet ist."""
    views: list[dict[str, Any]] = []
    gesehen: set[str] = set()
    # **Ein Lesefehler ist kein leerer Bestand.** Frueher stand hier eine leere
    # Liste, und die Antwort hiess „keine Auftraege" — ein Aufrufer haette
    # daraus geschlossen, dass sein ungewisser Start nie ankam. Gemessen am
    # Review vom 09.09.2026. Jetzt faellt die ganze Auskunft mit einem Grund.
    try:
        laeufe = ledger.recent_runs(limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_runtime.inquiry_runs_unreadable", kind=type(exc).__name__)
        raise InquiryUnreadable(f"runs:{type(exc).__name__}") from exc
    for run in laeufe:
        if run.task_id in gesehen:
            continue                    # der juengste Lauf je Aufgabe
        gesehen.add(run.task_id)
        view = run_view(ledger, run)
        view["kennungen"] = [run.run_id, run.task_id] + \
            ([view["freigabe"]] if view["freigabe"] else [])
        view["_norm"] = normalise(view["auftrag"])
        views.append(view)
    for capability in SCOPE_OF:
        try:
            zettel = ledger.starts_for_capability(capability, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_runtime.inquiry_starts_unreadable",
                        kind=type(exc).__name__)
            raise InquiryUnreadable(f"starts:{type(exc).__name__}") from exc
        for eintrag in zettel:
            if str(eintrag.get("run_id") or ""):
                continue                # der Lauf steht oben
            try:
                approval = await approval_status(eintrag["request_id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("agent_runtime.inquiry_approval_unreadable",
                            kind=type(exc).__name__)
                approval = None
            view = start_view(eintrag, approval)
            view["kennungen"] = [eintrag["request_id"]]
            view["_norm"] = normalise(view["auftrag"])
            views.append(view)
    views.sort(key=lambda v: -float(v.get("angelegt") or 0.0))
    return views


def _found(view: dict[str, Any], treffer: str, gesamt: int) -> dict[str, Any]:
    clean = {k: v for k, v in view.items() if not k.startswith("_")}
    return {"ok": True, "found": True, "ambiguous": False, "treffer": treffer,
            "auftrag": clean, "candidates": [], "gesamt": gesamt}


def _ambiguous(entries: list[dict[str, Any]], gesamt: int) -> dict[str, Any]:
    reihe = _prioritised(entries)
    return {"ok": True, "found": False, "ambiguous": True, "treffer": "",
            "auftrag": None,
            "candidates": [_short(e) for e in reihe[:CANDIDATE_LIMIT]],
            "gesamt": gesamt,
            "weitere": max(0, len(reihe) - CANDIDATE_LIMIT)}


def _unconfirmed(entries: list[dict[str, Any]], gesamt: int) -> dict[str, Any]:
    """Aehnliche Woerter: Kandidaten zur BESTAETIGUNG, keine Wahl.

    `found` bleibt falsch, `bestaetigung_noetig` sagt, was zu tun ist: den
    Kandidaten nennen und fragen, ob er gemeint ist. Erst die Antwort des
    Menschen — dann ueber die Kennung — bindet.
    """
    reihe = _prioritised(entries)
    return {"ok": True, "found": False, "ambiguous": False, "treffer": "",
            "reason": "aehnlich", "bestaetigung_noetig": True, "auftrag": None,
            "candidates": [_short(e) for e in reihe[:CANDIDATE_LIMIT]],
            "gesamt": gesamt,
            "weitere": max(0, len(reihe) - CANDIDATE_LIMIT)}


def _none(reason: str, entries: list[dict[str, Any]], gesamt: int) -> dict[str, Any]:
    reihe = _prioritised(entries)
    return {"ok": True, "found": False, "ambiguous": False, "treffer": "",
            "reason": reason, "auftrag": None,
            "candidates": [_short(e) for e in reihe[:CANDIDATE_LIMIT]],
            "gesamt": gesamt,
            "weitere": max(0, len(reihe) - CANDIDATE_LIMIT)}


async def find(ledger: S.AgentRunLedger, *, text: str = "", key: str = "",
               approval_status: ApprovalReader,
               limit: int = RECENT_LIMIT) -> dict[str, Any]:
    """Einen Auftrag wiederfinden — nur lesend, und ohne zu raten.

    * `key` — eine Kennung, die der Anrufer IN DIESEM Gespraech vom Core
      bekommen hat (Lauf, Aufgabe oder Freigabe). Genau ein Treffer oder
      keiner.
    * `text` — der Wortlaut oder ein Teil davon. Exakt gleicher Wortlaut
      trifft (mehrere gleiche werden GENANNT, nicht gewaehlt). Aehnliche
      Woerter treffen NICHT: sie liefern Kandidaten zur Bestaetigung
      (`bestaetigung_noetig`), auch wenn es nur einer ist — eine andere Stadt
      ist ein anderer Auftrag.
    * nichts — gibt es genau einen Auftrag, ist er es. Sonst die Kandidaten.
    """
    try:
        entries = await _entries(ledger, approval_status, limit)
    except InquiryUnreadable as exc:
        return {"ok": False, "reason": "ledger_unreadable", "detail": str(exc)[:80],
                "found": False, "ambiguous": False, "treffer": "", "auftrag": None,
                "candidates": [], "gesamt": 0}
    return _select(entries, text=text, key=key)


def find_linked(ledger: S.AgentRunLedger, links: list[dict[str, Any]], *,
                principal: str, text: str = "", key: str = "") -> dict[str, Any]:
    """Read only exact chat links, including old results outside RECENT_LIMIT.

    The caller owns the authenticated conversation check. A link alone never
    substitutes for task ownership, and multiple tasks remain ambiguous.
    """
    entries = []
    seen = set()
    for link in reversed(links):
        run = ledger.get_run(link['run_id'])
        if run is None or run.task_id != link['task_id'] or run.task_id in seen:
            continue
        task = ledger.get_task(run.task_id)
        if task is None or task.created_principal != principal:
            continue
        # A stale linked revision must not hide a newer run in another chat.
        latest = ledger.runs_for_task(task.task_id)
        if not latest or latest[-1].run_id != run.run_id:
            continue
        seen.add(run.task_id)
        view = run_view(ledger, run, task=task)
        view['kennungen'] = [run.run_id, run.task_id]
        view['_norm'] = normalise(view['auftrag'])
        entries.append(view)
    return _select(entries, text=text, key=key)


def _select(entries: list[dict[str, Any]], *, text: str, key: str) -> dict[str, Any]:
    gesamt = len(entries)
    key = (key or "").strip()
    query = normalise(text)
    if key:
        hits = [e for e in entries if key in e.get("kennungen", [])]
        if not hits:
            return _none("unbekannte_kennung", entries, gesamt)
        return _found(hits[0], "kennung", gesamt)
    if query:
        exact = [e for e in entries if e["_norm"] == query]
        if len(exact) == 1:
            return _found(exact[0], "wortlaut", gesamt)
        if len(exact) > 1:
            return _ambiguous(exact, gesamt)
        similar = [e for e in entries if _matches(query, e["_norm"])]
        if similar:
            return _unconfirmed(similar, gesamt)
        return _none("nicht_gefunden", entries, gesamt)
    if gesamt == 1:
        return _found(entries[0], "einziger", gesamt)
    if gesamt == 0:
        return _none("keine_auftraege", entries, gesamt)
    return _ambiguous(entries, gesamt)


__all__ = ["ABGELAUFEN", "ABGELEHNT", "CANDIDATE_LIMIT", "FREIGABE_ERTEILT",
           "FREIGABE_OFFEN", "GESTARTET", "InquiryUnreadable", "KIND_WORDS",
           "OPEN_START_STATES",
           "PHASE_BEFORE_RUN", "PHASE_RUN", "REASON_WORDS", "RECENT_LIMIT",
           "SCOPE_OF", "START_GESCHEITERT", "START_STATES", "START_UNGEWISS",
           "START_WORDS", "STATE_WORDS", "UNBEKANNT", "find", "normalise",
           "run_view", "start_state", "start_view"]
