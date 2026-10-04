"""Was der Auftrag verlangt, was das Ergebnis war, und was davon gedeckt ist.

Drei Saetze, die der Core bindet, und einer, den ein Modell vorschlaegt:

* **Anforderungen** (`agent_tasks.requirements`) — die Auslegung des
  Auftragstexts. Einmal gebunden, danach nur gelesen.
* **Snapshot** (`agent_artifacts`, Art `evaluation_snapshot`) — GENAU die
  Eingabe, ueber die geurteilt wurde. Unveraenderlich, mit eigenem sha256.
* **Urteil** (`agent_runs.completion_verdict`) — der Vorschlag des Modells,
  Core-seitig an Aufgabe, Lauf, Anforderungssatz und Snapshot gebunden.

## Die Korrektur, die diesem Modul zugrunde liegt

Ich hatte vorgeschlagen, der Core koenne aus leeren Listen ableiten, dass ein
Auftrag reine Information sei. **Das ist falsch.** Die Listen kommen auch vom
Modell — es kann eine verlangte Buchung schlicht auslassen, und dann sind sie
leer, ohne dass der Auftrag harmlos waere. Ein sha256 schuetzt gegen
nachtraegliche Aenderung, nicht gegen eine unvollstaendige Erstauslegung.

Deshalb bleibt der **urspruengliche Auftragstext die massgebliche Referenz**.
Die Bewertung bekommt ihn im Wortlaut mit und wird ausdruecklich gefragt, ob in
den gebundenen Anforderungen etwas FEHLT oder falsch eingeordnet ist. Meldet
sie etwas, blockiert das den Erfolg — und die erste Auslegung wird trotzdem
nicht still umgeschrieben.

Das ist modellgestuetzte Inhaltsbewertung. Sie kann irren, und ein zweiter
Modellaufruf macht sie nicht unabhaengig. Was sie NICHT kann: Rechte erzeugen,
Anforderungen erfinden oder eine Aussenhandlung als geschehen ausweisen.

## Warum der Snapshot nicht der Bericht ist

`_write_report()` schreibt `befunde-<run_id>.json` **genau einmal** und kehrt
bei vorhandener Datei zurueck; ausserdem deckelt er auf 8 Befunde und 12
Quellen. Sein Hash aendert sich also NICHT, wenn neue Befunde dazukommen — als
„aktueller Stand der Bewertungseingabe" waere er eine Luege. Der Snapshot hier
ist eine eigene, jedes Mal neu geschriebene Datei mit dem VOLLSTAENDIGEN
Material, und sein Hash ist die Ergebnisrevision.

Passt das Material nicht in den Bewertungsdeckel, wird nicht gekuerzt und
bewertet, sondern **gar nicht bewertet**: eine Vollstaendigkeitsaussage ueber
eine beschnittene Eingabe waere keine.
"""
from __future__ import annotations

import hashlib
import json
import os

from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

VERSION = 1

#: Deckel. Klein und geschlossen — hier passt kein Transkript hinein.
MAX_ITEMS = 5
# Each of the three requirement kinds can contain five distinct IDs. A
# judgement must be able to mention their complete union, not only one kind.
MAX_REQUIREMENTS = 3 * MAX_ITEMS
MAX_TEXT = 300
MAX_ID = 16
MAX_REFERENCES = 12

#: Wieviel Material die Bewertung hoechstens sehen darf. Passt der Snapshot
#: nicht hinein, gibt es KEIN Urteil — statt eines Urteils ueber die Haelfte.
#: 24 000 bis 19.09.2026: der dritte echte Durchstich zeigte, dass ein
#: echter nativer Auftrag (zwei Dateien, ein Helfer, sechs Befehle) mit einem
#: Drittel davon fuer Beobachtungen keinen entscheidenden Pruefbefehl mehr
#: unterbrachte; 36 000 Zeichen sind fuer den Abo-Bewerter kein Kostenfaktor.
MAX_EVALUATION_CHARS = 36_000

#: Die geschlossene Vokabel der Anforderungsarten.
ASK = "auskunft"
ACTION = "handlungen"
UNCLEAR = "unklar"


class RequirementsInvalid(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}:{detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def objective_digest(objective: str) -> str:
    """Die kanonische Bindung an den Auftragstext. Der CORE rechnet sie."""
    return hashlib.sha256((objective or "").encode("utf-8")).hexdigest()


def _entries(raw, feld: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RequirementsInvalid("not_a_list", feld)
    if len(raw) > MAX_ITEMS:
        raise RequirementsInvalid("too_many", feld)
    out = []
    for eintrag in raw:
        if not isinstance(eintrag, dict):
            raise RequirementsInvalid("entry_not_an_object", feld)
        kennung = str(eintrag.get("id", "")).strip()[:MAX_ID]
        text = str(eintrag.get("text", "")).strip()[:MAX_TEXT]
        if not kennung or not kennung.replace("_", "").isalnum():
            raise RequirementsInvalid("bad_id", kennung or feld)
        if not text:
            raise RequirementsInvalid("empty_text", kennung)
        value = {"id": kennung, "text": text}
        # Optional typed effect for the native task contract. Older bound
        # requirements keep their exact shape and digest. A local file can
        # never serve as proof of an explicitly external effect.
        if "effect" in eintrag:
            if feld != ACTION or type(eintrag["effect"]) is not str or eintrag["effect"] not in {"file", "local_execution", "external"}:
                raise RequirementsInvalid("invalid_effect", kennung)
            value["effect"] = eintrag["effect"]
        out.append(value)
    return out


def validate(raw: object, *, objective: str) -> dict:
    """Der Vorschlag des Modells, geprueft und an den Auftragstext gebunden.

    Verlangt wird, was ein Informationsvertrag mindestens braucht: mindestens
    EINE Auskunftsforderung, eindeutige Kennungen ueber ALLE Listen hinweg und
    ein sauber gedeckeltes Belegverlangen. Ein leerer Satz ist kein erfuellter
    Auftrag — er ist gar kein Vertrag.
    """
    if not isinstance(raw, dict):
        raise RequirementsInvalid("not_an_object")
    auskunft = _entries(raw.get(ASK), ASK)
    handlungen = _entries(raw.get(ACTION), ACTION)
    unklar = _entries(raw.get(UNCLEAR), UNCLEAR)
    # **Nicht leer — nicht „mindestens eine Auskunft".**
    #
    # Der Satz stand hier, als es nur den Informationsvertrag gab, und seine
    # Begruendung war „ein leerer Satz ist kein erfuellter Auftrag, er ist gar
    # kein Vertrag". Die Begruendung traegt weiter; die Bedingung traf zu eng.
    # Gemessen: ein reiner HANDLUNGSauftrag („Leg mir eine Notiz an") liess
    # sich damit nicht einmal binden — er fiel als `no_information_requirement`
    # durch, obwohl er ein vollstaendig beschriebener Auftrag ist.
    #
    # Geprueft wird deshalb, was gemeint war: dass ueberhaupt etwas verlangt
    # ist. Eine Handlung ist eine Forderung wie eine Frage.
    if not (auskunft or handlungen or unklar):
        raise RequirementsInvalid("no_requirement")

    kennungen = [e["id"] for e in auskunft + handlungen + unklar]
    if len(set(kennungen)) != len(kennungen):
        raise RequirementsInvalid("duplicate_id")

    belege = raw.get("belege") or {}
    if not isinstance(belege, dict):
        raise RequirementsInvalid("belege_not_an_object")
    try:
        mindestens = int(belege.get("mindestens", 0))
    except (TypeError, ValueError):
        raise RequirementsInvalid("belege_not_a_number") from None
    # Quellen sind keine Anforderungseintraege. Drei Angebote mit je zwei
    # Quellen muessen denselben Belegumfang binden koennen, den das Urteil
    # bereits aufnehmen kann; die verlangte Zahl wird niemals herabgesetzt.
    if not 0 <= mindestens <= MAX_REFERENCES:
        raise RequirementsInvalid("belege_out_of_range", str(mindestens))

    return {"v": VERSION, "ziel_digest": objective_digest(objective),
            ASK: auskunft, ACTION: handlungen, UNCLEAR: unklar,
            "belege": {"mindestens": mindestens}}


def load(payload: str, *, objective: str) -> dict | None:
    """Den gebundenen Satz zurueckholen — oder `None`.

    Die Bindung an den Auftragstext wird bei JEDEM Lesen nachgerechnet. Ein
    Satz, der zu einem anderen Text gehoert, ist keiner.
    """
    text = str(payload or "").strip()
    if not text:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        log.warning("agent_runtime.requirements_unreadable")
        return None
    if not isinstance(body, dict) or body.get("v") != VERSION:
        return None
    if body.get("ziel_digest") != objective_digest(objective):
        log.warning("agent_runtime.requirements_objective_mismatch")
        return None
    try:
        gepruft = validate(body, objective=objective)
    except RequirementsInvalid as exc:
        log.warning("agent_runtime.requirements_invalid", reason=exc.reason)
        return None
    return gepruft


def requirement_ids(bound: dict) -> set[str]:
    return {e["id"] for e in bound[ASK] + bound[ACTION] + bound[UNCLEAR]}


def digest_of(bound: dict) -> str:
    """Die Kennung des Anforderungssatzes — fuer die Bindung des Urteils."""
    return hashlib.sha256(
        json.dumps(bound, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# =====================================================================
# Das Bewertungsurteil — vollstaendig oder gar nicht
# =====================================================================

#: Jedes dieser Felder MUSS beantwortet sein. Keines ist optional.
#:
#: Gemessen am 5.9.2026: liess ein Urteil `fehlend`, `unsicher` oder
#: `weiterarbeit_noetig` weg, las die Auswertung die Abwesenheit als
#: Unbedenklichkeit (`.get(x) or []`) — und vier solcher Laeufe endeten
#: erfolgreich. **Eine weggelassene Aussage ist keine Aussage.** Wer nicht
#: sagt, ob etwas fehlt, hat nicht gesagt, dass nichts fehlt.
JUDGEMENT_LISTS = ("offen", "fehlend", "unsicher")

#: Ein Wachposten, der sich von `None` unterscheiden laesst — genau darum geht
#: es: `None` ist eine Antwort („weiss nicht"), Abwesenheit ist keine.
_MISSING = object()


class JudgementInvalid(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}:{detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _text_list(raw, feld: str) -> list[str]:
    """Eine Liste kurzer Texte — vollstaendig, richtig getypt, nicht gekuerzt.

    Ueberzaehlige Eintraege werden ABGELEHNT statt weggeschnitten. Ein Slice
    macht aus einer widerspruechlichen Antwort eine gefaellige, und genau die
    Eintraege, die nicht mehr hineinpassen, waeren die interessanten.
    """
    if not isinstance(raw, list):
        raise JudgementInvalid("not_a_list", feld)
    limit = MAX_REQUIREMENTS if feld == "offen" else MAX_ITEMS
    if len(raw) > limit:
        raise JudgementInvalid("too_many", feld)
    out = []
    for eintrag in raw:
        if not isinstance(eintrag, str):
            raise JudgementInvalid("entry_not_a_string", feld)
        text = eintrag.strip()[:MAX_TEXT]
        if text:
            out.append(text)
    return out


def validate_judgement(raw: object) -> dict:
    """Der Modellteil eines Urteils. Vollstaendig geprueft, bevor er zaehlt.

    Dieselbe Pruefung fuer ein frisch gelesenes und ein persistiertes Urteil:
    ein Satz ist nicht dadurch gueltig, dass er in der Datenbank steht.

    Die BINDUNG (Aufgabe, Lauf, Snapshot, Anforderungsdigest) steht bewusst
    NICHT hier — die stempelt der Core nach dieser Pruefung.
    """
    if not isinstance(raw, dict):
        raise JudgementInvalid("not_an_object")

    beantwortet = raw.get("beantwortet")
    if not isinstance(beantwortet, list):
        raise JudgementInvalid("not_a_list", "beantwortet")
    if len(beantwortet) > MAX_REQUIREMENTS:
        raise JudgementInvalid("too_many", "beantwortet")
    gedeckt = []
    for eintrag in beantwortet:
        if not isinstance(eintrag, dict):
            raise JudgementInvalid("entry_not_an_object", "beantwortet")
        kennung = eintrag.get("id")
        belege = eintrag.get("belege")
        if not isinstance(kennung, str) or not kennung.strip():
            raise JudgementInvalid("bad_id", "beantwortet")
        if not isinstance(belege, list):
            raise JudgementInvalid("not_a_list", "belege")
        if len(belege) > MAX_REFERENCES:
            raise JudgementInvalid("too_many", "belege")
        sauber = []
        for beleg in belege:
            if not isinstance(beleg, str):
                raise JudgementInvalid("entry_not_a_string", "belege")
            if beleg.strip():
                sauber.append(beleg.strip())
        gedeckt.append({"id": kennung.strip()[:MAX_ID], "belege": sauber})

    urteil = {"beantwortet": gedeckt}
    for feld in JUDGEMENT_LISTS:
        if feld not in raw:
            raise JudgementInvalid("missing_field", feld)
        urteil[feld] = _text_list(raw[feld], feld)

    weiter = raw.get("weiterarbeit_noetig", _MISSING)
    if weiter is _MISSING:
        raise JudgementInvalid("missing_field", "weiterarbeit_noetig")
    # `None` ist nicht `False`, `0` ist nicht `False`, "nein" erst recht nicht.
    if not isinstance(weiter, bool):
        raise JudgementInvalid("not_a_boolean", "weiterarbeit_noetig")
    urteil["weiterarbeit_noetig"] = weiter
    return urteil


# =====================================================================
# Der Ergebnis-Snapshot
# =====================================================================

def snapshot_body(findings, sources, *, user_context: str = "") -> str:
    """Genau das Material, ueber das geurteilt wird. Nicht gekuerzt."""
    return json.dumps(
        {"v": VERSION,
         "befunde": [str(f) for f in (findings or []) if str(f).strip()],
         "quellen": [str(s) for s in (sources or []) if str(s).strip()],
         **({"nutzerangaben": user_context} if user_context else {})},
        sort_keys=True, ensure_ascii=False)


def snapshot_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def write_snapshot(ledger, run_id: str, findings, sources, *, user_context: str = "") -> tuple[str, str]:
    """Den Snapshot ablegen und `(sha256, artifact_id)` liefern.

    Gleiche Eingabe → gleicher Hash → gleiche Datei. Ein zweiter Aufruf mit
    unveraendertem Material erzeugt kein zweites Artefakt; sobald sich ein
    Befund aendert, gibt es einen neuen Snapshot mit neuem Hash. Genau das
    konnte der Bericht nicht, weil er nur einmal geschrieben wird.
    """
    from solvio.agent_runtime import store as S

    body = snapshot_body(findings, sources, user_context=user_context)
    digest = snapshot_digest(body)
    folder = S.artifact_root(run_id)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = os.path.join(folder, f"snapshot-{digest[:16]}.json")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o600)
    vorhanden = [a for a in ledger.artifacts_for_run(run_id)
                 if a.kind == "evaluation_snapshot" and a.sha256 == digest]
    if vorhanden:
        return digest, vorhanden[0].artifact_id
    artifact = ledger.add_artifact(run_id=run_id, kind="evaluation_snapshot",
                                   path=path, sha256=digest,
                                   size=len(body.encode("utf-8")))
    return digest, artifact.artifact_id


def read_snapshot(ledger, run_id: str, digest: str) -> dict | None:
    """Den abgelegten Snapshot lesen — die Quelle jeder Referenzaufloesung.

    Referenzen werden gegen DIESE Datei aufgeloest und nie gegen den
    fluechtigen `RunContext`: der kann sich seit dem Urteil geaendert haben,
    und dann pruefte man ein altes Urteil gegen neues Material.
    """
    for artifact in ledger.artifacts_for_run(run_id):
        if artifact.kind != "evaluation_snapshot" or artifact.sha256 != digest:
            continue
        try:
            with open(artifact.path, encoding="utf-8") as handle:
                body = handle.read()
        except OSError:
            return None
        if snapshot_digest(body) != digest:
            log.warning("agent_runtime.snapshot_tampered", run_id=run_id)
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def snapshot_references(snapshot: dict) -> set[str]:
    """Jede Zeile, auf die ein Urteil zeigen darf."""
    out = set()
    for key in ("befunde", "quellen"):
        for eintrag in (snapshot.get(key) or []):
            text = str(eintrag).strip()
            if text:
                out.add(text)
    return out


__all__ = ["ACTION", "ASK", "JUDGEMENT_LISTS", "JudgementInvalid",
           "MAX_EVALUATION_CHARS", "MAX_ITEMS", "MAX_REQUIREMENTS", "MAX_REFERENCES",
           "RequirementsInvalid", "UNCLEAR", "VERSION", "digest_of", "load",
           "validate_judgement",
           "objective_digest", "read_snapshot", "requirement_ids",
           "snapshot_body", "snapshot_digest", "snapshot_references",
           "validate", "write_snapshot"]
