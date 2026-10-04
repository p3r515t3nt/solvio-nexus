"""Der Fortsetzungspunkt eines Laufs — dauerhaft, gedeckelt, und ohne Autoritaet.

Reproduziert am 5. September 2026 (`objective_probe.py`, sechs Beobachtungen):
`RunContext.plan` lebte ausschliesslich im Arbeitsspeicher. Nach einem
Prozessverlust rekonstruierte `_rebuild_context()` keinen Plan, `_do_next_step()`
las „kein Plan" als „Plan zu Ende" und `_do_verify()` fand eine leere
Schrittliste ohne offenen Rest. Drei unerledigte Auftraege endeten so auf
`SUCCEEDED` — einer davon mit einem nie ausgefuehrten zweiten Schritt.

Dieses Modul haelt genau das fest, was **fehlte**, und ausdruecklich nicht mehr:

* den validierten Plan mit seiner Revision — sonst gibt es nach dem Neustart
  keine Restarbeit, an der ein Abschluss scheitern koennte;
* den Fortschritt im Plan, die verbrauchten Budgets und die Wiederholungs-
  Digeste — sonst schenkt ein Neustart dem Lauf ein frisches Versuchsbudget;
* die bereits erarbeiteten Befunde und Quellen — sonst geht eine gelungene
  Teilrecherche verloren, weil der Rest offen blieb.

## Drei Regeln, die dieses Modul zu einem Merkzettel machen und nicht zu einer Quelle

**Der Plan wird beim LESEN neu validiert.** Eine Zeile in der Datenbank ist
Eingabe, kein Urteil. Beim Wiederherstellen laeuft der gespeicherte Plan durch
dieselbe `planner.validate()`, durch die er beim Planen lief — mit den JETZT
erlaubten Profilen und den JETZT bekannten Faehigkeiten. Ein Plan, der eine
inzwischen gesperrte Faehigkeit nennt, kommt nicht zurueck. Es gibt keine
zweite Policy; es gibt dieselbe, zweimal angewandt.

**Ziel und Scope kommen NIE aus dem Checkpoint.** Sie stehen in `agent_tasks`,
gesetzt bei der Erzeugung unter geprueftem Prinzipal und gepruefter Herkunft.
Der Checkpoint traegt sie nicht bei — wer sie von hier naehme, koennte durch
eine Zeile den Auftrag umschreiben, den ein Mensch gegeben hat.

**Kein Transkript und kein Geheimnis.** Jedes Textfeld ist einzeln redigiert
und gedeckelt, die Feldmenge ist geschlossen, und der fertige Satz laeuft
zusaetzlich durch den Zaun des Buchs (`store._safe_text`). Passt er trotzdem
nicht in den Deckel, fallen zuerst die weichen Felder — der Plan faellt nie,
denn ohne ihn ist der Lauf nicht fortsetzbar, und das waere der teuerste
Verlust von allen.

Was hier NICHT steht, ist ebenso Absicht: kein Freigabezustand, keine
Ausfuehrungskennung, kein Ergebnis einer Aussenhandlung. Ob eine Wirkung
eingetreten ist, weiss das Freigabe- und Ausfuehrungsjournal und sonst
niemand — ein Checkpoint ersetzt keine Einmaligkeit.
"""
from __future__ import annotations

import json

from solvio.agent_runtime import planner as PL
from solvio.specialists.result import NATIVE_BROWSER_OBSERVATION_PREFIX
from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

#: Die Fassung des Satzes. Eine unbekannte Fassung wird nicht geraten.
VERSION = 1

#: Deckel je Feld. Sie sind der Grund, warum kein Transkript hineinpasst: die
#: Feldmenge ist geschlossen, und jedes einzelne Feld ist kuerzer als das, was
#: ein Modell zu erzaehlen haette.
MAX_NOTES = 5
MAX_NOTE_TEXT = 600
MAX_FINDINGS = 8
MAX_FINDING_TEXT = 600
MAX_SOURCES = 12
MAX_SOURCE_TEXT = 300
RESULT_SECTION_LISTS = ("findings", "evidence", "assumptions", "uncertainties",
                        "rejected_alternatives", "risk_notes")
MAX_SIGNATURES = 24
MAX_DIGESTS = 64
MAX_DIGEST_KEY = 40

#: Der aeussere Deckel des ganzen Satzes. Bemessen am strukturellen Maximum
#: eines Plans (`MAX_PLAN_STEPS` × `MAX_TEXT` plus Argumente) und den weichen
#: Feldern oben — nicht an einem Wunsch.
MAX_CHECKPOINT = 32_000

#: Die weichen Felder, in der Reihenfolge, in der sie fallen duerfen. Der Plan
#: steht nicht darin.
_SHEDDABLE = ("notizen", "quellen", "befunde")


def _redact(text: str) -> str:
    from solvio.agent_runtime import specialists as SP
    return SP.redact_specialist_output(str(text or ""))


def _texts(values, *, limit: int, chars: int) -> list[str]:
    out: list[str] = []
    for value in list(values or [])[:limit]:
        cleaned = _redact(value).strip()[:chars]
        if cleaned:
            out.append(cleaned)
    return out


def _result_sections(raw):
    """Preserve complete structured conclusions, or explicitly decline them.

    These are the existing SpecialistResult bounds, not raw provider output.
    Unlike the short display fields, silently slicing a qualification here
    would change the material on which the completion assessment is based.
    """
    if not isinstance(raw, list):
        return [], False
    result = []
    for section in raw:
        if (not isinstance(section, dict) or set(section) !=
                {"step_id", "recommended_path", "confidence", *RESULT_SECTION_LISTS}
                or not isinstance(section["step_id"], str)
                or len(section["step_id"]) > 64
                or not isinstance(section["recommended_path"], str)
                or len(section["recommended_path"]) > 1500
                or not isinstance(section["confidence"], str)
                or section["confidence"] not in {"", "hoch", "mittel", "niedrig", "high", "medium", "low"}):
            return [], False
        item = {"step_id": section["step_id"],
                "recommended_path": _redact(section["recommended_path"]).strip(),
                "confidence": section["confidence"]}
        for name in RESULT_SECTION_LISTS:
            values = section[name]
            # Native web and browser observations each add at most twelve
            # bounded URLs to twelve model evidence entries. The non-JSON
            # legacy fallback has one 1200-char finding. The total checkpoint
            # size remains bounded below without silently clipping qualifiers.
            limit, chars = ((36, 720) if name == "evidence" else
                            (12, 1200) if name == "findings" else (12, 600))
            # Only the caller's bounded observation may occupy the final
            # overflow slot; this is not an extra model evidence allowance.
            native_observation = (name == "evidence" and isinstance(values, list)
                and len(values) == 37 and isinstance(values[-1], str)
                and values[-1].startswith(NATIVE_BROWSER_OBSERVATION_PREFIX)
                and len(values[-1]) <= 600)
            if (not isinstance(values, list) or (len(values) > limit and not native_observation)
                    or any(not isinstance(v, str) or len(v) > chars for v in values)):
                return [], False
            item[name] = [_redact(v).strip() for v in values if v.strip()]
        result.append(item)
    return result, True


def _result_material_retained(body, material, journal_material):
    """Every original finding/source must remain exact after redaction.

    Complete sections can replace their duplicate short views. An uncovered
    prefix cannot replace its missing qualification, even before shedding.
    Context notes are planner context, not findings or source evidence.
    """
    for field, section_field in (("befunde", "findings"), ("quellen", "evidence")):
        retained = (set(body[field]) | journal_material[field]
                    | {text for section in body["result_sections"]
                       for text in section[section_field]})
        if not material[field] <= retained:
            return False
    return True


def _counters(raw) -> dict:
    """Digest → Zahl. Schluessel sind Hexziffern, Werte kleine Ganzzahlen.

    Streng gefiltert, weil dieses Feld sonst der bequemste Weg waere, beliebigen
    Text in einer Zahlenspalte zu transportieren.
    """
    out: dict[str, int] = {}
    for key, value in list((raw or {}).items())[:MAX_DIGESTS]:
        name = str(key)
        # Nicht kappen, sondern ABLEHNEN. Ein gekuerzter Schluessel ist ein
        # ANDERER Digest — die Schleifenbremse zaehlte dann etwas mit, das nie
        # versucht wurde, oder verlore den Zaehler des echten Versuchs.
        if not name or len(name) > MAX_DIGEST_KEY:
            continue
        if not all(c in "0123456789abcdef" for c in name.lower()):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[name] = min(count, 999)
    return out


def _step_to_raw(step) -> dict:
    """Ein Planschritt in der Form, die `planner.validate()` liest.

    Bewusst dieselbe Form und nicht eine eigene: was gespeichert wird, muss
    durch dieselbe Pruefung zurueckkommen koennen, sonst gaebe es zwei
    Vorstellungen davon, was ein gueltiger Schritt ist.
    """
    return {"art": step.kind, "profil": step.profile,
            "faehigkeit": step.capability,
            "auftrag": _redact(step.instruction)[:PL.MAX_TEXT],
            "argumente": step.arguments or {},
            "verzichtbar": bool(step.optional),
            # Zuordnung zum gebundenen Auftrag, kein Erfuellungsbeleg.
            "erfuellt": list(step.requirements) if step.requirements else step.requirement,
            "vertrag": step.contract, "ressource": step.resource}


def encode(*, plan, revision: int, cursor: int, goal_met: str,
           approval_attempts: int, pending_step_id: str, notes, findings,
           sources, invalid_signatures, attempts, low_value,
           result_sections=None, result_sections_complete=True,
           research_result_material=False, journal_findings=(), journal_sources=()) -> str:
    """Der Fortsetzungspunkt als JSON — oder `""`, wenn es keinen Plan gibt.

    Ohne Plan gibt es nichts fortzusetzen, und ein Satz ohne Plan waere genau
    die stille Zusage („da war nichts zu tun"), die dieser Milestone abschafft.
    """
    if plan is None:
        return ""
    findings, sources = list(findings or []), list(sources or [])
    body = {
        "v": VERSION,
        "revision": int(revision),
        "cursor": max(0, int(cursor)),
        "hinweis": _redact(plan.note)[:PL.MAX_TEXT],
        "rueckfrage": plan.question,
        "schritte": [_step_to_raw(s) for s in plan.steps[:PL.MAX_PLAN_STEPS]],
        "ziel_erfuellt": str(goal_met or "")[:64],
        "freigabeversuche": max(0, int(approval_attempts)),
        "offener_schritt": str(pending_step_id or "")[:64],
        "notizen": _texts(notes, limit=MAX_NOTES, chars=MAX_NOTE_TEXT),
        "befunde": _texts(findings, limit=MAX_FINDINGS, chars=MAX_FINDING_TEXT),
        "quellen": _texts(sources, limit=MAX_SOURCES, chars=MAX_SOURCE_TEXT),
        "ungueltig": [str(s)[:120] for s in list(invalid_signatures or [])[:MAX_SIGNATURES]],
        "versuche": _counters(attempts),
        "geringwertig": _counters(low_value),
    }
    if result_sections is not None:
        sections, valid = _result_sections(result_sections)
        body["result_sections"] = sections
        material = {field: {cleaned for value in values
                            if (cleaned := _redact(value).strip())}
                    for field, values in (("befunde", findings), ("quellen", sources))}
        # Only the caller's freshly verified canonical native file material
        # may stand outside this projection. It is never persisted here or
        # accepted as an execution/authority proof from checkpoint input.
        journal_material = {field: {cleaned for value in values
                                    if (cleaned := _redact(value).strip())}
                            for field, values in (("befunde", journal_findings),
                                                  ("quellen", journal_sources))}
        # Research findings live in these sections and must remain exact.
        # Native action/file previews are rehydrated from their canonical
        # execution journals for assessment; the short view is not a receipt.
        # The caller selects this policy from the bound task, never this JSON.
        body["result_sections_complete"] = bool(valid and result_sections_complete is True
            and (not research_result_material
                 or _result_material_retained(body, material, journal_material)))
    text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    for field in _SHEDDABLE:
        if len(text) <= MAX_CHECKPOINT:
            break
        body[field] = []
        if "result_sections" in body:
            # Keep a prior False sticky. Removing a duplicate display view or
            # non-evidentiary planning notes does not lose complete material;
            # removing an uncovered finding/source always does.
            body["result_sections_complete"] = bool(body["result_sections_complete"]
                and research_result_material
                and _result_material_retained(body, material, journal_material))
        text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if len(text) > MAX_CHECKPOINT and "result_sections" in body:
        body["result_sections"] = []
        body["result_sections_complete"] = False
        text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if len(text) > MAX_CHECKPOINT:
        # Der Plan allein sprengt den Deckel. Das ist strukturell unmoeglich,
        # solange `planner.validate` gilt — passiert es doch, wird KEIN halber
        # Satz geschrieben. Ein halber Checkpoint waere schlimmer als keiner.
        log.warning("agent_runtime.checkpoint_oversized", size=len(text))
        return ""
    return text


def decode(raw: str) -> dict | None:
    """Der rohe Satz, strukturell geprueft. Fail-closed: im Zweifel `None`.

    Hier faellt noch keine Entscheidung ueber den Plan — nur darueber, ob
    ueberhaupt ein lesbarer Satz vorliegt.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) > MAX_CHECKPOINT:
        log.warning("agent_runtime.checkpoint_too_long", size=len(text))
        return None
    try:
        body = json.loads(text)
    except ValueError:
        log.warning("agent_runtime.checkpoint_unreadable")
        return None
    if not isinstance(body, dict) or body.get("v") != VERSION:
        log.warning("agent_runtime.checkpoint_unknown_version")
        return None
    if not isinstance(body.get("schritte"), list) or not body["schritte"]:
        return None
    return body


class Restored:
    """Was aus einem Checkpoint zurueckkam — Plan plus weiche Fortsetzung."""

    __slots__ = ("plan", "revision", "cursor", "goal_met", "approval_attempts",
                 "pending_step_id", "notes", "findings", "sources",
                 "invalid_signatures", "attempts", "low_value",
                 "result_sections", "result_sections_complete")

    def __init__(self, **fields) -> None:
        for name in self.__slots__:
            setattr(self, name, fields[name])


def restore(raw: str, *, goal: str, scope: str, allowed_profiles: set[str],
            known_capabilities: set[str],
            allowed_needs: dict[str, str] | None = None) -> tuple[Restored | None, str]:
    """Den Fortsetzungspunkt wiederherstellen — oder ehrlich sagen, warum nicht.

    `goal` und `scope` sind die des AUFTRAGS aus dem Buch, nie die des
    Checkpoints. Der Plan laeuft durch `planner.validate()`, also durch genau
    die Policy, die beim Planen galt: gesperrte Faehigkeit, fremdes Profil,
    Autoritaetsfeld im Argument, Builder im Rechercheauftrag — alles wird beim
    Lesen erneut abgelehnt.

    Rueckgabe: `(Restored | None, grund)` mit `grund` aus einer geschlossenen
    Vokabel: `restored`, `absent`, `unreadable`, `rejected`.
    """
    body = decode(raw)
    if body is None:
        return None, ("absent" if not str(raw or "").strip() else "unreadable")
    try:
        plan = PL.validate({"schritte": body["schritte"],
                            "hinweis": str(body.get("hinweis", "")),
                            "rueckfrage": body.get("rueckfrage", "")},
                           scope=scope, allowed_profiles=set(allowed_profiles),
                           known_capabilities=set(known_capabilities), goal=goal,
                           allowed_needs=allowed_needs)
    except PL.PlanInvalid as exc:
        log.warning("agent_runtime.checkpoint_rejected", reason=exc.reason)
        return None, "rejected"
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_runtime.checkpoint_rejected",
                    reason=type(exc).__name__)
        return None, "rejected"

    def _list(name, limit, chars):
        return _texts(body.get(name), limit=limit, chars=chars)

    try:
        cursor = max(0, int(body.get("cursor", 0)))
        revision = max(0, int(body.get("revision", 0)))
        attempts = max(0, int(body.get("freigabeversuche", 0)))
    except (TypeError, ValueError):
        return None, "unreadable"
    sections, valid = _result_sections(body.get("result_sections", []))
    return Restored(
        plan=plan, revision=revision, cursor=min(cursor, len(plan.steps)),
        goal_met=str(body.get("ziel_erfuellt", ""))[:64],
        approval_attempts=attempts,
        pending_step_id=str(body.get("offener_schritt", ""))[:64],
        notes=_list("notizen", MAX_NOTES, MAX_NOTE_TEXT),
        findings=_list("befunde", MAX_FINDINGS, MAX_FINDING_TEXT),
        sources=_list("quellen", MAX_SOURCES, MAX_SOURCE_TEXT),
        invalid_signatures={str(s)[:120] for s in
                            list(body.get("ungueltig") or [])[:MAX_SIGNATURES]},
        attempts=_counters(body.get("versuche")),
        low_value=_counters(body.get("geringwertig")),
        result_sections=sections,
        result_sections_complete=valid and body.get("result_sections_complete", True) is True), "restored"


__all__ = ["MAX_CHECKPOINT", "Restored", "VERSION", "decode", "encode", "restore"]
