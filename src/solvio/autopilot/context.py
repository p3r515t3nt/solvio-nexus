"""Context Package und Handoff — klein, gedeckelt, und ehrlich beschnitten.

Ein neuer Builder darf nicht 500k Token Historie lesen muessen. Er bekommt,
was er fuer den NAECHSTEN Schritt braucht, in der Reihenfolge der Wichtigkeit —
und wenn der Deckel greift, steht das drin. Ein Beschnitt, der schweigt, ist
eine Luege ueber die Vollstaendigkeit.

Was hier **nie** hineinkommt: alte Konversation, ganze Testprotokolle, das
ganze Repository. Das ist keine Sparmassnahme, sondern die Aussage selbst —
wer einem Review das ganze Repository vorlegt, bekommt ein Review ueber das
ganze Repository.

Alles laeuft durch `redact()`. Der Contract kommt als **Projektion** herein,
mit seinem Hash: der Empfaenger kann keine andere Fassung meinen, ohne dass es
auffaellt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from solvio.autopilot import store as S

#: Der harte Deckel. Startwert mit Kalibrierungsauftrag.
MAX_CONTEXT_CHARS = 30_000
#: Wie viel davon der Diff hoechstens bekommt.
MAX_DIFF_CHARS = 12_000


def _redact(text: str) -> str:
    from solvio.specialists.launcher import redact
    return redact(text or "")


@dataclass
class Package:
    """Was ein Empfaenger sieht — und was er nicht sieht."""

    text: str
    omitted: tuple[str, ...] = ()
    chars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"chars": self.chars, "omitted": list(self.omitted)}


def compile_context(ledger: S.AutopilotLedger, milestone_id: str, *,
                    diff: str = "", gate_summary: str = "",
                    next_task: str = "", evidence: list[str] | None = None,
                    max_chars: int = MAX_CONTEXT_CHARS
                    ) -> Package:
    """Das Context Package. Reihenfolge = Prioritaet beim Beschneiden.

    Zuerst kommt, was ohne Ersatz ist (Auftrag, offene Findings, letzte
    Entscheidung), zuletzt, was nachschlagbar bleibt (Diff, Chronik). Wer
    umgekehrt beschneidet, nimmt dem Empfaenger den Auftrag und laesst ihm den
    Diff.
    """
    zustand = ledger.milestone(milestone_id)
    vertrag = json.loads(zustand.contract_json)
    kriterien = ledger.criteria(milestone_id)
    findings = ledger.findings(milestone_id, open_only=True)
    letzte = ledger.last_decision(milestone_id)
    bewiesen, gesamt = ledger.acceptance_counts(milestone_id)

    bloecke: list[tuple[str, str]] = []

    bloecke.append(("auftrag", (
        f"## Auftrag (Contract {vertrag['version']}, {zustand.contract_hash})\n"
        f"READ-ONLY. Der kanonische Contract liegt im Core. Eine Aenderung im\n"
        f"Arbeitsbaum ist ein Vorschlag und erzeugt CONTRACT_CHANGE_REQUIRED.\n\n"
        f"{vertrag['objective']}\n")))

    # Drei Regeln stehen in diesem Block, und alle drei aus demselben Grund:
    # `lead.validate()` verwirft ein Urteil, das gegen sie verstoesst — als
    # GANZES, nicht die eine Zeile. Jede dieser Regeln hat in der A7-Abnahme
    # einmal eine Runde gekostet, weil sie nur in der Pruefung stand und nie
    # im Context. Eine Regel, gegen die geprueft wird und die niemand nennt,
    # ist keine Regel, sondern eine Falle.
    schluessel = [k["key"] for k in kriterien]
    zeilen = [f"## Akzeptanz ({bewiesen}/{gesamt} bewiesen)",
              "Gueltige Schluessel, vollstaendig: "
              + ", ".join(f"`{s}`" for s in schluessel) + ".",
              "Ein anderer verwirft das ganze Urteil. Nimm den Schluessel "
              "GENAU so, ohne Typ und ohne Text."]
    mess = [k["key"] for k in kriterien if k["evidence_type"] == "DETERMINISTIC"]
    for k in kriterien:
        marke = {"proven": "[BEWIESEN]", "refuted": "[WIDERLEGT]"}.get(
            k["state"], "[offen]  ")
        # `key=` davor, damit der Schluessel nicht mit der Zeile verwechselt
        # wird. Live gemessen im Produktions-Smoke am 2026-09-02: der Lead
        # nannte `naht (REVIEW_SUPPORTED)` als Kriterium — er hatte die ganze
        # Zeile bis zum Doppelpunkt genommen, und das war eine faire Lesart
        # der alten Form.
        zeilen.append(f"{marke} key={k['key']}  ({k['evidence_type']})  "
                      f"{k['text']}"
                      + (f"  -> {k['evidence_ref']}" if k["evidence_ref"] else ""))
    if mess:
        # Die Regel MIT der Liste, gegen die geprueft wird.
        #
        # Live gelernt in der A7-Abnahme: `validate()` verwirft ein Urteil, das
        # ein DETERMINISTIC-Kriterium `proven` setzt — aber der Context nannte
        # die Regel nie. Der Lead setzte `gate`, das ganze Urteil fiel mit
        # `deterministic_criterion_judged`, und die Runde war verloren. Eine
        # Regel, die nur in der Pruefung steht, ist eine Falle.
        zeilen.append("")
        zeilen.append("Diese Kriterien setzt AUSSCHLIESSLICH die Messung, nie "
                      "ein Urteil: " + ", ".join(mess) + ".")
        zeilen.append("Nenne sie NICHT unter `proven` — ein Urteil, das es "
                      "tut, wird als Ganzes verworfen.")
    bloecke.append(("akzeptanz", "\n".join(zeilen) + "\n"))

    if next_task:
        bloecke.append(("aufgabe", f"## Naechste Aufgabe\n{next_task}\n"))

    # Erst die Messung, dann die Behauptungen ueber sie.
    #
    # Ebenfalls live gelernt: bei gruenem Gate (3056/3056, Drift 0) urteilte
    # der Lead „das Test-Gate ist noch rot" — weil ueber der Messung ein
    # offenes Finding aus der Vorrunde stand, das genau das behauptete. Ein
    # Finding ist eine Aussage von FRUEHER; die Messung ist von JETZT.
    if gate_summary:
        bloecke.append(("gate", f"## Test-Gate (die aktuelle Messung)\n"
                                f"{gate_summary}\n"))

    if findings:
        zeilen = ["## Offene Findings (aus frueheren Runden)",
                  "Sie beschreiben, was damals galt. Widerspricht eines der "
                  "Messung oben, ist es erledigt — schliesse es ueber "
                  "`close_findings` mit seiner Kennung."]
        for f in findings:
            zeilen.append(f"- [{f.severity}] {f.finding_id}: {f.title}"
                          + (f"\n  {f.detail}" if f.detail else ""))
        bloecke.append(("findings", "\n".join(zeilen) + "\n"))

    # Die verfuegbaren Belege — MIT ihren Kennungen.
    #
    # Live gelernt in der A7-Abnahme: ein `REVIEW_SUPPORTED`-Kriterium darf nur
    # mit einer Evidence-Referenz auf `proven` gesetzt werden, aber der Context
    # nannte keine einzige. Der Technical Lead musste raten, und `validate()`
    # verwarf sein Urteil mit `proven_without_known_evidence` — zu Recht.
    # Eine Regel, die eine Referenz verlangt und keine nennt, ist keine Regel,
    # sondern eine Falle.
    if evidence:
        zeilen = ["## Verfuegbare Belege",
                  "Nur diese Kennungen sind als `evidence_ref` gueltig."]
        for eid in evidence:
            beleg = ledger.evidence(eid)
            if beleg is None:
                continue
            marke = "ok" if beleg.ok else "ROT"
            zeilen.append(f"- {eid} ({beleg.kind}, {marke}): {beleg.summary}")
        bloecke.append(("belege", "\n".join(zeilen) + "\n"))

    grenzen = vertrag.get("architecture_boundaries") or []
    sicherheit = vertrag.get("security_boundaries") or []
    verboten = vertrag.get("forbidden_actions") or []
    if grenzen or sicherheit or verboten:
        zeilen = ["## Grenzen"]
        for name, werte in (("Architektur", grenzen), ("Sicherheit", sicherheit),
                            ("Verboten", verboten)):
            for w in werte:
                zeilen.append(f"- {name}: {w}")
        bloecke.append(("grenzen", "\n".join(zeilen) + "\n"))

    if letzte:
        bloecke.append(("entscheidung", (
            f"## Letzte Entscheidung des Technical Lead\n"
            f"{letzte['verdict']} -> {letzte['next_action']}\n"
            f"{letzte['rationale']}\n")))

    if diff:
        gekuerzt = diff[:MAX_DIFF_CHARS]
        if len(diff) > MAX_DIFF_CHARS:
            gekuerzt += (f"\n[... {len(diff) - MAX_DIFF_CHARS} Zeichen des Diffs "
                         f"ausgelassen ...]")
        bloecke.append(("diff", f"## Aenderungen\n```diff\n{gekuerzt}\n```\n"))

    # -- Zusammensetzen und ehrlich beschneiden ------------------------------
    text = ""
    ausgelassen: list[str] = []
    for name, block in bloecke:
        if len(text) + len(block) > max_chars:
            ausgelassen.append(name)
            continue
        text += block + "\n"
    if ausgelassen:
        text += ("## Hinweis\nAus Platzgruenden ausgelassen: "
                 + ", ".join(ausgelassen) + "\n")
    sauber = _redact(text)
    return Package(text=sauber, omitted=tuple(ausgelassen), chars=len(sauber))


def build_handoff(ledger: S.AutopilotLedger, milestone_id: str, *,
                  state_after: str, builder: str = "", model: str = "",
                  task_size: str = "", base_commit: str = "",
                  result_commit: str = "", changed_files: list[str] | None = None,
                  gate: dict | None = None, evidence: list[str] | None = None,
                  findings_opened: list[str] | None = None,
                  findings_closed: list[str] | None = None,
                  human_required: dict | None = None,
                  recommended_next_action: str = "") -> dict[str, Any]:
    """Der Handoff — die maschinenlesbare Zusammenfassung einer Phase.

    Er ersetzt den kilometerlangen Zwischenbericht, den heute ein Mensch
    kopiert. Alles darin ist entweder gemessen oder eine benannte Entscheidung.
    """
    zustand = ledger.milestone(milestone_id)
    bewiesen, gesamt = ledger.acceptance_counts(milestone_id)
    lage = ledger.capacity(milestone_id)
    nutzlast = {
        "milestone_id": milestone_id,
        "contract_version": zustand.contract_version,
        "contract_hash": zustand.contract_hash,
        "state": state_after,
        "builder": builder or zustand.builder,
        "model": model or zustand.model,
        "task_size": task_size,
        "base_commit": base_commit or zustand.base_commit,
        "result_commit": result_commit or zustand.last_commit,
        "changed_files": list(changed_files or [])[:200],
        "tests": dict(gate or {}),
        "acceptance": {"proven": bewiesen, "total": gesamt},
        "evidence": list(evidence or []),
        "findings_opened": list(findings_opened or []),
        "findings_closed": list(findings_closed or []),
        "human_required": human_required,
        "capacity": {rolle: eintrag["state"] for rolle, eintrag in lage.items()},
        "token_usage": ledger.usage_summary(milestone_id),
        "recommended_next_action": recommended_next_action,
    }
    ledger.record_handoff(milestone_id, nutzlast)
    return nutzlast
