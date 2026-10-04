"""Der Milestone Contract — Core-owned, nicht Builder-owned (Vertrag §4).

Der Unterschied ist die ganze Pointe dieses Moduls. Eine Vertragsdatei, die im
Arbeitsbaum des Builders liegt, ist eine Datei, die der Builder aendern kann —
und ein Builder, der seinen eigenen Auftrag umschreiben darf, hat keinen
Auftrag mehr, sondern eine Meinung. Deshalb:

* Inhalt, Fassung und `sha256` leben im Ledger,
* der Builder bekommt eine **read-only Projektion**,
* eine Abweichung im Arbeitsbaum ist ein VORSCHLAG und erzeugt die Grenze
  `contract_change` — die einzige Aenderungskante.

**JSON, nicht YAML.** Beim Offsite-Release hat eine undeklarierte
PyYAML-Abhaengigkeit acht Gate-Pruefungen in einer frischen Umgebung
umgeworfen (DEBT-0164); sie kommt nur zufaellig ueber den Torch-Stack herein.
Laufzeitcode des Autopiloten haengt nicht an einem Zufall.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

#: Evidence-Typen eines Akzeptanzkriteriums (Amendment 9). Geschlossen.
DETERMINISTIC = "DETERMINISTIC"
REVIEW_SUPPORTED = "REVIEW_SUPPORTED"
EVIDENCE_TYPES = frozenset({DETERMINISTIC, REVIEW_SUPPORTED})

#: Wer freigeben darf. In V0.5 gibt es genau eine zulaessige Antwort: der
#: Eigentuemer. Ein Contract, der sich selbst die Freigabe erteilt, waere die
#: erste Stelle, an der ein Lauf sich Autoritaet schreibt.
RELEASE_AUTHORITY_OWNER = "owner"
RELEASE_AUTHORITIES = frozenset({RELEASE_AUTHORITY_OWNER})

#: Handlungen, die ein Contract NIEMALS erlauben kann — egal was darin steht.
#: Sie beruehren Produktion, Fremdsysteme oder die Sicherheitsgrenze.
NEVER_PERMITTED = (
    "merge_to_main", "push_remote", "deploy", "restart_service",
    "production_checkout", "migrate_database", "rotate_credential",
    "modify_contract", "grant_authority", "disable_gate",
)

#: Laengendeckel. Ein Contract ist ein Auftrag, kein Handbuch.
MAX_TEXT = 4_000
MAX_ITEM = 400
MAX_ITEMS = 40
MAX_CRITERIA = 30

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class ContractError(ValueError):
    """Ein Contract, der nicht traegt. Der Grund ist ein kurzes Wort."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class AcceptanceCriterion:
    """Ein Kriterium — und wer es beweisen darf.

    `evidence_type` ist kein Etikett, sondern eine Berechtigung: bei
    `DETERMINISTIC` kann ausschliesslich Mess-Evidence den Status auf `proven`
    setzen, und kein Urteil der Welt hilft darueber hinweg.
    """

    key: str
    text: str
    evidence_type: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "text": self.text,
                "evidence_type": self.evidence_type}


@dataclass(frozen=True)
class Contract:
    """Der Auftrag. Unveraenderlich; eine neue Fassung ist ein neues Objekt."""

    milestone_id: str
    version: str
    objective: str
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    non_goals: tuple[str, ...] = ()
    architecture_boundaries: tuple[str, ...] = ()
    security_boundaries: tuple[str, ...] = ()
    permitted_actions: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    release_authority: str = RELEASE_AUTHORITY_OWNER
    human_boundaries: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    repository: str = ""

    # -- Kanonisierung --------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "version": self.version,
            "objective": self.objective,
            "acceptance_criteria": [c.as_dict() for c in self.acceptance_criteria],
            "non_goals": list(self.non_goals),
            "architecture_boundaries": list(self.architecture_boundaries),
            "security_boundaries": list(self.security_boundaries),
            "permitted_actions": list(self.permitted_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "release_authority": self.release_authority,
            "human_boundaries": list(self.human_boundaries),
            "evidence_requirements": list(self.evidence_requirements),
            "repository": self.repository,
        }

    def canonical_json(self) -> str:
        """Eine Form, ein Hash. Sortierte Schluessel, feste Trenner.

        Ohne Kanonisierung waere der Hash von der Formatierung abhaengig — und
        ein Vergleich, der bei umsortierten Schluesseln anschlaegt, meldet
        Aenderungen, die keine sind.
        """
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")).hexdigest()

    # -- Die Projektion, die ein Builder sehen darf ---------------------------
    def projection(self) -> dict[str, Any]:
        """Read-only Sicht fuer Builder und Technical Lead (Amendment 5).

        Sie traegt den Hash mit. Wer sie in einen Prompt legt, legt damit auch
        die Aussage hinein, WELCHE Fassung gemeint ist — und der Empfaenger
        kann keine andere meinen, ohne dass es auffaellt.
        """
        data = self.as_dict()
        data["contract_hash"] = self.digest()
        data["readonly"] = True
        data["hinweis"] = (
            "Der kanonische Contract liegt im Autopilot-Ledger des Cores. "
            "Eine Aenderung dieser Projektion oder einer Datei im Arbeitsbaum "
            "ist ein Vorschlag und wird nie still uebernommen; sie erzeugt "
            "CONTRACT_CHANGE_REQUIRED.")
        return data

    def criterion(self, key: str) -> AcceptanceCriterion | None:
        for crit in self.acceptance_criteria:
            if crit.key == key:
                return crit
        return None


# -- Lesen und pruefen --------------------------------------------------------
def _text(raw: Any, feld: str, *, deckel: int = MAX_ITEM,
          pflicht: bool = True) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ContractError("field_not_text", feld)
    wert = raw.strip()
    if pflicht and not wert:
        raise ContractError("field_empty", feld)
    if len(wert) > deckel:
        raise ContractError("field_too_long", f"{feld} ({len(wert)}>{deckel})")
    return wert


def _liste(raw: Any, feld: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ContractError("field_not_list", feld)
    if len(raw) > MAX_ITEMS:
        raise ContractError("list_too_long", f"{feld} ({len(raw)}>{MAX_ITEMS})")
    return tuple(_text(item, f"{feld}[]") for item in raw)


def parse(raw: dict[str, Any]) -> Contract:
    """Aus rohem JSON ein geprueftes Objekt — oder ein benannter Fehler."""
    if not isinstance(raw, dict):
        raise ContractError("not_an_object")

    milestone_id = _text(raw.get("milestone_id"), "milestone_id")
    if not _ID.match(milestone_id):
        raise ContractError("bad_milestone_id", milestone_id[:40])
    version = _text(raw.get("version"), "version")
    if not _VERSION.match(version):
        raise ContractError("bad_version", version[:20])

    roh_krit = raw.get("acceptance_criteria")
    if not isinstance(roh_krit, list) or not roh_krit:
        raise ContractError("no_acceptance_criteria")
    if len(roh_krit) > MAX_CRITERIA:
        raise ContractError("too_many_criteria", str(len(roh_krit)))
    kriterien: list[AcceptanceCriterion] = []
    gesehen: set[str] = set()
    for eintrag in roh_krit:
        if not isinstance(eintrag, dict):
            raise ContractError("criterion_not_an_object")
        key = _text(eintrag.get("key"), "criterion.key")
        if key in gesehen:
            raise ContractError("duplicate_criterion_key", key)
        gesehen.add(key)
        art = _text(eintrag.get("evidence_type"), "criterion.evidence_type")
        if art not in EVIDENCE_TYPES:
            raise ContractError("unknown_evidence_type", art)
        kriterien.append(AcceptanceCriterion(
            key=key, text=_text(eintrag.get("text"), "criterion.text"),
            evidence_type=art))

    autoritaet = _text(raw.get("release_authority") or RELEASE_AUTHORITY_OWNER,
                       "release_authority")
    if autoritaet not in RELEASE_AUTHORITIES:
        raise ContractError("bad_release_authority", autoritaet)

    erlaubt = _liste(raw.get("permitted_actions"), "permitted_actions")
    verboten = _liste(raw.get("forbidden_actions"), "forbidden_actions")

    # Ein Contract kann sich nicht selbst Produktion erlauben. Diese Pruefung
    # ist der Grund, warum `permitted_actions` ueberhaupt geprueft wird und
    # nicht bloss uebernommen: sonst waere die Liste eine Selbstermaechtigung.
    verletzt = sorted(set(erlaubt) & set(NEVER_PERMITTED))
    if verletzt:
        raise ContractError("permits_forbidden_action", ", ".join(verletzt))
    doppelt = sorted(set(erlaubt) & set(verboten))
    if doppelt:
        raise ContractError("action_both_permitted_and_forbidden",
                            ", ".join(doppelt))

    return Contract(
        milestone_id=milestone_id, version=version,
        objective=_text(raw.get("objective"), "objective", deckel=MAX_TEXT),
        acceptance_criteria=tuple(kriterien),
        non_goals=_liste(raw.get("non_goals"), "non_goals"),
        architecture_boundaries=_liste(raw.get("architecture_boundaries"),
                                       "architecture_boundaries"),
        security_boundaries=_liste(raw.get("security_boundaries"),
                                   "security_boundaries"),
        permitted_actions=erlaubt, forbidden_actions=verboten,
        release_authority=autoritaet,
        human_boundaries=_liste(raw.get("human_boundaries"), "human_boundaries"),
        evidence_requirements=_liste(raw.get("evidence_requirements"),
                                     "evidence_requirements"),
        repository=_text(raw.get("repository"), "repository", pflicht=False),
    )


def load(path: str) -> Contract:
    """Einen Contract aus einer Datei lesen. Nur beim ERSTEN Anlegen.

    Danach ist der Ledger die Quelle — genau deshalb gibt es hier keine
    Funktion, die eine Datei ueber einen laufenden Milestone legt.
    """
    voll = os.path.abspath(os.path.expanduser(path))
    try:
        with open(voll, encoding="utf-8") as fh:
            roh = json.load(fh)
    except FileNotFoundError:
        raise ContractError("file_missing", voll) from None
    except ValueError as exc:
        raise ContractError("unreadable_json", str(exc)[:120]) from None
    return parse(roh)


def differs(canonical: Contract, proposal: dict[str, Any]) -> str:
    """Weicht ein Vorschlag vom kanonischen Contract ab? Gibt den Grund.

    Leer heisst gleich. Es wird bewusst der Hash verglichen und nicht Feld fuer
    Feld: eine Aenderung ist eine Aenderung, und wer sie bewerten will, tut das
    in der `contract_change`-Grenze, nicht hier.
    """
    try:
        andere = parse(proposal)
    except ContractError as exc:
        return f"invalid_proposal:{exc.reason}"
    if andere.digest() == canonical.digest():
        return ""
    if andere.milestone_id != canonical.milestone_id:
        return "different_milestone"
    if andere.version == canonical.version:
        # Derselbe Fassungsname mit anderem Inhalt ist der gefaehrlichste Fall:
        # er sieht in jeder Uebersicht unveraendert aus.
        return "same_version_different_content"
    return "new_version_proposed"
