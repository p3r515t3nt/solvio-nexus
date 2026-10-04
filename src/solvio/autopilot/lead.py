"""Der Technical Lead — Core-owned, builderunabhaengig (Amendment 1).

Er ist **nicht** Claude. Er laeuft ueber den vorhandenen Provider Broker, mit
eigenen Auftraggebern und eigenen Kappen — genau den Weg, den der Planer der
Agentenlaufzeit seit V1 geht und live bewiesen hat. Der Grund ist nicht
Geschmack, sondern Ausfallverhalten: haengen Builder und Lead am selben
Anbieter, endet ein Kontingent beide gleichzeitig, und der Autopilot steht.
So haengt der Builder an einem Abo-CLI und der Lead am Broker.

**Das Urteil ist ein Vorschlag, kein Befehl.** Dasselbe Muster wie beim Planer
und bei Adaptive Memory: zwischen dem Modell und jeder Wirkung liegt
`validate()`, und was dort nicht durchkommt, existiert nicht. Ein Urteil kann
`READY` sagen; ob READY passiert, entscheidet `machine.py` an der Evidence.

**Fable 5 ist hier nicht der Lead.** Er darf als Challenger gerufen werden —
aber nur aus einem geschlossenen Ereigniskatalog heraus, und die Entscheidung
trifft der Lead, nicht der Builder.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from solvio.logging_setup import get_logger

log = get_logger("autopilot")

#: Urteile. Geschlossen — ein unbekanntes Wort ist kein neues Konzept,
#: sondern ein Modell, das sich etwas ausgedacht hat.
READY = "READY"
FIX = "FIX"
BUILD = "BUILD"
ESCALATE = "ESCALATE"
NEEDS_HUMAN = "NEEDS_HUMAN"
VERDICTS = frozenset({READY, FIX, BUILD, ESCALATE, NEEDS_HUMAN})

#: Der geschlossene Ereigniskatalog fuer eine Eskalation (Vertrag §5).
#: Ein Urteil `ESCALATE` ohne eines dieser Ereignisse wird verworfen — sonst
#: koennte sich jede Runde zum teuren Modell hochreden.
ARCHITECTURE_DECISION = "ARCHITECTURE_DECISION"
SECURITY_FINDING = "SECURITY_FINDING"
SECOND_FIX_FAILED = "SECOND_FIX_FAILED"
ROOT_CAUSE_REQUESTED = "ROOT_CAUSE_REQUESTED"
ESCALATION_EVENTS = frozenset({ARCHITECTURE_DECISION, SECURITY_FINDING,
                               SECOND_FIX_FAILED, ROOT_CAUSE_REQUESTED})

#: Naechste Handlungen, die ein Lead vorschlagen darf. Auch geschlossen.
NEXT_ACTIONS = frozenset({"build", "fix", "root_cause", "builder_switch",
                          "model_escalation", "challenger", "ready",
                          "human_required"})

#: Modelle. Der Name ist keine Berechtigung — die liegt beim Auftraggeber.
TIER_SMALL = "small"
TIER_LARGE = "large"

MAX_RATIONALE = 1_500
MAX_TASK = 800
MAX_FINDINGS = 12
LEASE_SECONDS = 180.0


class LeadRefused(RuntimeError):
    """Ein Urteil, das nicht gilt. Mit Grund."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Verdict:
    """Was der Lead entschieden hat — nach der Pruefung."""

    verdict: str
    next_action: str
    rationale: str = ""
    task: str = ""
    findings: tuple[dict[str, str], ...] = ()
    close_findings: tuple[str, ...] = ()
    proven: tuple[dict[str, str], ...] = ()
    escalation_event: str = ""
    builder: str = ""
    task_size: str = "MEDIUM"
    model: str = ""
    tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "next_action": self.next_action,
                "rationale": self.rationale, "task": self.task,
                "findings": [dict(f) for f in self.findings],
                "close_findings": list(self.close_findings),
                "proven": [dict(p) for p in self.proven],
                "escalation_event": self.escalation_event,
                "builder": self.builder, "task_size": self.task_size,
                "model": self.model, "tokens": self.tokens}


# -- Die Pruefung -------------------------------------------------------------
def _json_block(text: str) -> dict[str, Any]:
    """Den JSON-Block aus der Antwort holen — tolerant im Rahmen, streng im Inhalt.

    Tolerant, weil ein Modell gern einen Zaun aus Backticks baut. Streng, weil
    danach jedes Feld einzeln geprueft wird.
    """
    roh = (text or "").strip()
    if roh.startswith("```"):
        roh = re.sub(r"^```[a-zA-Z]*\n?", "", roh)
        roh = re.sub(r"\n?```\s*$", "", roh)
    try:
        return json.loads(roh)
    except ValueError:
        pass
    anfang, ende = roh.find("{"), roh.rfind("}")
    if anfang >= 0 and ende > anfang:
        try:
            return json.loads(roh[anfang:ende + 1])
        except ValueError:
            pass
    raise LeadRefused("unreadable_verdict", roh[:120])


def validate(raw_text: str, *, allowed_builders: set[str],
             open_finding_ids: set[str],
             criterion_keys: set[str],
             evidence_ids: set[str],
             deterministic_keys: set[str]) -> Verdict:
    """Aus Modelltext ein gueltiges Urteil — oder ein benannter Fehlschlag.

    Die vier schaerfsten Regeln stehen hier, und jede hat einen Grund:

    * **`ESCALATE` braucht ein Ereignis aus dem Katalog.** Sonst koennte sich
      jede Runde zum teuren Modell hochreden.
    * **Ein `DETERMINISTIC`-Kriterium kann der Lead nicht beweisen.** Es ist
      Mess-Evidence vorbehalten (Amendment 9). Nennt er eines, faellt **der
      Eintrag** weg, nicht das Urteil: das Kriterium bleibt offen, nur die
      Messung schliesst es, und `ready_check` laesst ohne sie kein READY zu.
      Das ganze Urteil zu verwerfen kostete in der A7-Abnahme zweimal eine
      volle Runde und aenderte an der Sicherheit nichts.
    * **`REVIEW_SUPPORTED` braucht eine echte Evidence-Referenz.** Ein
      Kriterium ohne Beleg bleibt offen.
    * **Ein unbekannter Builder ist kein Builder.** Der Lead waehlt aus der
      Liste, die der Core ihm gibt.
    """
    daten = _json_block(raw_text)
    if not isinstance(daten, dict):
        raise LeadRefused("verdict_not_an_object")

    urteil = str(daten.get("verdict", "")).strip().upper()
    if urteil not in VERDICTS:
        raise LeadRefused("unknown_verdict", urteil[:40] or "leer")

    aktion = str(daten.get("next_action", "")).strip().lower()
    if aktion not in NEXT_ACTIONS:
        raise LeadRefused("unknown_next_action", aktion[:40] or "leer")

    ereignis = str(daten.get("escalation_event", "")).strip().upper()
    if urteil == ESCALATE:
        # Hier ist die Strenge der ganze Zweck: ohne Ereignis aus dem Katalog
        # koennte sich jede Runde zum teuren Modell hochreden.
        if ereignis not in ESCALATION_EVENTS:
            raise LeadRefused("escalation_without_event", ereignis[:40] or "leer")
    elif ereignis not in ESCALATION_EVENTS:
        # Ohne ESCALATE bedeutet das Feld nichts — also wird es GELEERT, nicht
        # bestraft. Das Schema zeigt es an; ein Modell, das dort "NONE" oder
        # "KEINES" hinschreibt, sagt damit „ich eskaliere nicht", und genau
        # das steht auch im Urteil.
        #
        # Live gemessen im Produktions-Smoke am 2026-09-02: der Lead schickte
        # `escalation_event: "NONE"` zu einem FIX-Urteil, und die Pruefung
        # verwarf das ganze Urteil mit `unknown_escalation_event`. Es war
        # nichts daran falsch ausser meiner Strenge an der falschen Stelle.
        ereignis = ''

    builder = str(daten.get("builder", "")).strip()
    if builder and builder not in allowed_builders:
        raise LeadRefused("unknown_builder", builder[:40])

    groesse = str(daten.get("task_size", "MEDIUM")).strip().upper()
    if groesse not in ("SMALL", "MEDIUM", "LARGE"):
        raise LeadRefused("unknown_task_size", groesse[:20])

    # -- Findings, die geoeffnet werden sollen -------------------------------
    befunde: list[dict[str, str]] = []
    for eintrag in (daten.get("findings") or [])[:MAX_FINDINGS]:
        if not isinstance(eintrag, dict):
            raise LeadRefused("finding_not_an_object")
        schwere = str(eintrag.get("severity", "")).strip().lower()
        if schwere not in ("blocker", "major", "minor", "info"):
            raise LeadRefused("unknown_severity", schwere[:20] or "leer")
        titel = str(eintrag.get("title", "")).strip()
        if not titel:
            raise LeadRefused("finding_without_title")
        befunde.append({"severity": schwere, "title": titel[:300],
                        "detail": str(eintrag.get("detail", ""))[:2_000]})

    # -- Findings, die geschlossen werden sollen -----------------------------
    schliessen: list[str] = []
    for fid in (daten.get("close_findings") or [])[:MAX_FINDINGS]:
        kennung = str(fid).strip()
        if kennung not in open_finding_ids:
            raise LeadRefused("unknown_finding", kennung[:40])
        schliessen.append(kennung)

    # -- Kriterien, die als bewiesen gelten sollen ---------------------------
    bewiesen: list[dict[str, str]] = []
    verworfen: list[str] = []
    for eintrag in (daten.get("proven") or [])[:MAX_FINDINGS]:
        if not isinstance(eintrag, dict):
            raise LeadRefused("proven_not_an_object")
        key = str(eintrag.get("key", "")).strip()
        if key not in criterion_keys:
            raise LeadRefused("unknown_criterion", key[:40] or "leer")
        if key in deterministic_keys:
            # Amendment 9: was gemessen werden muss, wird nicht beurteilt.
            #
            # Der Eintrag faellt weg — das ganze Urteil faellt NICHT. Das ist
            # kein weicheres Tor, sondern ein genaueres: das Kriterium bleibt
            # offen, nur die Messung kann es je schliessen, und `ready_check`
            # laesst ohne sie kein READY zu. Was der Lead sonst noch richtig
            # gesehen hat — Findings, naechster Schritt, andere Belege —
            # ueberlebt.
            #
            # Live gelernt in zwei A7-Laeufen: das Modell nannte `gate` trotz
            # ausdruecklicher Anweisung UND ausdruecklicher Nennung im
            # Context. Ein ganzes Urteil dafuer zu verwerfen kostete jedes Mal
            # eine volle Runde (30 Minuten Bauen, 14 Minuten Messen) und
            # aenderte an der Sicherheit nichts.
            verworfen.append(key)
            continue
        beleg = str(eintrag.get("evidence_ref", "")).strip()
        if beleg not in evidence_ids:
            raise LeadRefused("proven_without_known_evidence", f"{key}:{beleg[:30]}")
        bewiesen.append({"key": key, "evidence_ref": beleg})

    if verworfen:
        log.info("autopilot.lead_proven_dropped", keys=",".join(verworfen))
    return Verdict(verdict=urteil, next_action=aktion,
                   rationale=str(daten.get("rationale", ""))[:MAX_RATIONALE],
                   task=str(daten.get("task", ""))[:MAX_TASK],
                   findings=tuple(befunde), close_findings=tuple(schliessen),
                   proven=tuple(bewiesen), escalation_event=ereignis,
                   builder=builder, task_size=groesse)


# -- Der Aufruf ---------------------------------------------------------------
INSTRUCTION = (
    "Du bist der Technical Lead eines Entwicklungs-Autopiloten. Du bewertest "
    "eine Runde Arbeit und entscheidest den naechsten Schritt.\n"
    "Antworte AUSSCHLIESSLICH mit JSON nach dem Schema, ohne Fliesstext.\n"
    "Du entscheidest NICHT ueber Freigabe, Autoritaet oder Sicherheitsgrenzen "
    "— diese Felder gibt es nicht.\n"
    "READY darfst du nur vorschlagen, wenn das Test-Gate gruen ist und jedes "
    "Akzeptanzkriterium einen Beleg hat. Ein rotes Gate ist niemals READY.\n"
    "Kriterien vom Typ DETERMINISTIC kannst du NICHT beweisen — sie brauchen "
    "eine Messung. Nenne sie nicht unter `proven`.\n"
    "ESCALATE nur mit einem `escalation_event` aus dem Katalog."
)

SCHEMA = {
    "verdict": "READY|FIX|BUILD|ESCALATE|NEEDS_HUMAN",
    "next_action": "build|fix|root_cause|builder_switch|model_escalation|"
                   "challenger|ready|human_required",
    "rationale": "kurz, warum",
    "task": "die konkrete naechste Aufgabe fuer den Builder",
    "findings": [{"severity": "blocker|major|minor|info", "title": "",
                  "detail": ""}],
    "close_findings": ["finding-id"],
    "proven": [{"key": "kriterium", "evidence_ref": "evidence-id"}],
    "escalation_event": "ARCHITECTURE_DECISION|SECURITY_FINDING|"
                        "SECOND_FIX_FAILED|ROOT_CAUSE_REQUESTED",
    "builder": "name aus der Liste",
    "task_size": "SMALL|MEDIUM|LARGE",
}


def transport_for(tier: str) -> tuple[str, str]:
    """Stufe → (Auftraggeber, Modell).

    Der Auftraggeber IST der Zugang: `gpt-5.4` erreicht nur, wer den Token des
    Eskalations-Auftraggebers haelt, und den haelt ausschliesslich Core-Code.
    Ein Lead, der das grosse Modell bloss NENNT, bekommt vom Modelltor eine
    Absage.
    """
    from solvio.provider_broker.proxy import LARGE_MODEL, MINI_MODEL
    from solvio.provider_broker.service import (AUTOPILOT_LEAD_ESCALATION_PRINCIPAL,
                                                AUTOPILOT_LEAD_PRINCIPAL)
    if tier == TIER_LARGE:
        return AUTOPILOT_LEAD_ESCALATION_PRINCIPAL, LARGE_MODEL
    return AUTOPILOT_LEAD_PRINCIPAL, MINI_MODEL


def build_request(*, context: str, allowed_builders: list[str],
                  model: str) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": INSTRUCTION,
        "input": (f"SCHEMA:\n{json.dumps(SCHEMA, ensure_ascii=False, indent=1)}\n\n"
                  f"ERLAUBTE BUILDER: {', '.join(allowed_builders) or 'keine'}\n\n"
                  f"LAGE:\n{context}"),
    }


def _ask_core(payload: dict, *, socket_path: str = "",
              timeout: float = 20.0) -> dict:
    """Eine Frage an den laufenden Core ueber seinen besitzergebundenen Socket.

    Der Socket ist auf die Kennung des Besitzers geprueft; ein Modell, ein
    Kaefig oder ein Satellit erreicht ihn nicht. Der Aufruf ist absichtlich
    ohne Ausnahme: laeuft der Core nicht, ist das eine Lage, kein Absturz —
    zurueck kommt dann ein leeres Wortbuch.
    """
    import socket as _socket
    import struct

    pfad = socket_path or os.path.expanduser("~/.solvio/control.sock")
    try:
        verbindung = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        verbindung.settimeout(timeout)
        verbindung.connect(pfad)
    except OSError:
        return {}
    try:
        nutzlast = json.dumps(payload, separators=(",", ":")).encode()
        verbindung.sendall(struct.pack("!I", len(nutzlast)) + nutzlast)

        def genau(anzahl: int) -> bytes:
            puffer = b""
            while len(puffer) < anzahl:
                teil = verbindung.recv(anzahl - len(puffer))
                if not teil:
                    raise OSError("Verbindung endete")
                puffer += teil
            return puffer

        laenge = struct.unpack("!I", genau(4))[0]
        antwort = json.loads(genau(laenge))
    except (OSError, ValueError):
        return {}
    finally:
        verbindung.close()
    return antwort if isinstance(antwort, dict) else {}


def token_from_core(principal: str, *, socket_path: str = "",
                    timeout: float = 20.0) -> str:
    """Ein Broker-Token aus dem laufenden Core. Leer heisst „nein"."""
    antwort = _ask_core({"op": "autopilot_token", "principal": principal},
                        socket_path=socket_path, timeout=timeout)
    if not antwort.get("ok"):
        log.warning("autopilot.token_refused",
                    reason=str(antwort.get("reason", ""))[:40])
        return ""
    return str(antwort.get("token", ""))


def lease_from_core(principal: str, *, socket_path: str = "",
                    timeout: float = 20.0) -> str:
    """Ein offenes Lease am KANONISCHEN Broker. Leer heisst „keins".

    Ohne diesen Weg muesste ein Treiber ausserhalb des Core-Prozesses einen
    zweiten Broker starten — und zwei Kappen sind keine Kappe.
    """
    antwort = _ask_core({"op": "autopilot_lease", "principal": principal,
                         "action": "open"},
                        socket_path=socket_path, timeout=timeout)
    if not antwort.get("ok"):
        log.info("autopilot.lease_refused_by_core",
                 reason=str(antwort.get("reason", ""))[:40])
        return ""
    return str(antwort.get("lease_id", ""))


def close_lease_in_core(principal: str, lease_id: str, *, socket_path: str = "",
                        timeout: float = 20.0) -> None:
    """Steht in einem `finally` und darf deshalb nie werfen."""
    if not lease_id:
        return
    _ask_core({"op": "autopilot_lease", "principal": principal,
               "action": "close", "lease_id": lease_id},
              socket_path=socket_path, timeout=timeout)


class TechnicalLead:
    """Der Lead als Objekt — mit genau einem Weg nach draussen: dem Broker."""

    def __init__(self, broker=None, *, port: int = 0) -> None:
        self.broker = broker
        self.port = port

    def _token(self, principal: str) -> str:
        """Den Broker-Token besorgen — im Prozess oder ueber den Kontrollsocket.

        Der Treiber laeuft als eigener Prozess; die Broker-Registratur lebt im
        Core. Statt einer zweiten Broker-Instanz mit eigenen Kappen (zwei
        Kappen sind keine Kappe) fragt er den Core ueber dessen
        besitzergebundenen Socket. Was zurueckkommt, ist ein Broker-Token —
        kein Anbieterschluessel, und ohne offenes Lease oeffnet er nichts.

        **Je Aufruf frisch. Kein Zwischenspeicher.** Der Broker rotiert den
        Token, sobald das letzte Lease eines Auftraggebers schliesst — das ist
        seine dritte Schicht, „kein Zugang ueber den Auftrag hinaus". Ein
        gemerkter Token ist danach `401`.

        Live gemessen in der A7-Abnahme: der erste Review-Aufruf gelang, der
        zweite endete 45 Minuten spaeter an `broker.denied principal=unknown
        reason=bad_token status=401` — und der Milestone stand BLOCKED. Der
        Fehler war nicht die Rotation, sondern die Annahme, ein Token gelte
        laenger als sein Auftrag.
        """
        if self.broker is not None:
            return self.broker.register_principal(principal)
        token = token_from_core(principal)
        if not token:
            raise LeadRefused("no_broker", principal)
        return token

    async def judge(self, *, context: str, allowed_builders: list[str],
                    open_finding_ids: set[str], criterion_keys: set[str],
                    evidence_ids: set[str], deterministic_keys: set[str],
                    tier: str = TIER_SMALL) -> Verdict:
        """Eine Runde beurteilen. Wirft `LeadRefused`, wenn nichts Gueltiges kam."""
        principal, modell = transport_for(tier)
        anfrage = build_request(context=context,
                                allowed_builders=allowed_builders, model=modell)
        begonnen = time.time()

        # Ohne offenes Lease weist der Broker mit 403 `lease_absent` ab — und
        # das ist Absicht: ein Token allein oeffnet nichts. Live gelernt in der
        # A7-Abnahme, wo genau diese Zeile fehlte und der erste Review-Aufruf
        # nach 42 Minuten Arbeit an einem 403 endete.
        # Reihenfolge: erst der Token, DANN das Lease. Ein Lease auf einen
        # unregistrierten Auftraggeber wird mit `CapExceeded` abgewiesen —
        # live gemessen im zweiten A7-Lauf, wo genau diese Vertauschung den
        # Aufruf danach in ein 403 laufen liess.
        token = self._token(principal)
        lease = self._open_lease(principal)
        if not lease:
            # Kein Lease heisst: nicht senden. Ob der Broker im Prozess sitzt
            # oder hinter dem Kontrollsocket, aendert daran nichts — ohne
            # Lease endet der Aufruf mit 403, und ihn trotzdem abzuschicken
            # hiesse, den Fehler spaeter und unklarer zu bekommen. Frueher
            # stand hier eine Ausnahme fuer den Fall „kein Broker im Prozess";
            # sie war der Grund, warum ein Treiber ausserhalb des Cores
            # ueberhaupt ohne Lease losschickte.
            if tier == TIER_LARGE:
                log.info("autopilot.lead_escalation_capped", reason="lease")
                return await self.judge(
                    context=context, allowed_builders=allowed_builders,
                    open_finding_ids=open_finding_ids,
                    criterion_keys=criterion_keys, evidence_ids=evidence_ids,
                    deterministic_keys=deterministic_keys, tier=TIER_SMALL)
            raise LeadRefused("lead_unreachable", "lease_refused")
        try:
            antwort = await self._call(anfrage, token=token)
        finally:
            self._close_lease(lease, principal)

        if not antwort.get("ok"):
            grund = str(antwort.get("reason"))[:80]
            if tier == TIER_LARGE:
                # Eine Stufe, die nicht durchkommt, kostet keinen zweiten
                # Versuch: derselbe Aufruf laeuft klein weiter. Dieselbe Regel
                # wie beim Planer — ein gekappter Lauf darf nie oefter
                # scheitern als einer ohne Eskalation.
                log.info("autopilot.lead_escalation_capped", reason=grund)
                return await self.judge(
                    context=context, allowed_builders=allowed_builders,
                    open_finding_ids=open_finding_ids,
                    criterion_keys=criterion_keys, evidence_ids=evidence_ids,
                    deterministic_keys=deterministic_keys, tier=TIER_SMALL)
            raise LeadRefused("lead_unreachable", grund)
        urteil = validate(antwort.get("text", ""),
                          allowed_builders=set(allowed_builders),
                          open_finding_ids=open_finding_ids,
                          criterion_keys=criterion_keys,
                          evidence_ids=evidence_ids,
                          deterministic_keys=deterministic_keys)
        urteil.model = modell
        urteil.tokens = antwort.get("tokens")
        log.info("autopilot.lead_verdict", verdict=urteil.verdict,
                 action=urteil.next_action, model=modell,
                 seconds=round(time.time() - begonnen, 1))
        return urteil

    def _open_lease(self, principal: str) -> str:
        """Ein Lease je Aufruf. Ohne es oeffnet der Token nichts.

        Zwei Wege, und beide fuehren zu DERSELBEN Registratur:

        * im Core-Prozess direkt am Broker-Objekt;
        * ausserhalb ueber den besitzergebundenen Kontrollsocket des
          laufenden Cores.

        Der zweite Weg ist der Grund, warum es keinen zweiten Broker braucht.
        Bis zum 2026-09-02 gab es ihn nicht: der Treiber lief dann ohne Lease
        und bekam ein ehrliches 403 — ehrlich, aber unbrauchbar. Ein zweiter
        Broker waere die falsche Antwort gewesen, denn zwei Kappen sind keine
        Kappe: die Tageskappe des Anbieters waere doppelt vergeben.
        """
        if self.broker is None:
            return lease_from_core(principal)
        try:
            return self.broker.open_lease(
                principal, ref=f"autopilot:{principal}",
                deadline=time.time() + LEASE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
            log.info("autopilot.lease_refused", principal=principal,
                     kind=type(exc).__name__)
            return ""

    def _close_lease(self, lease_id: str, principal: str = "") -> None:
        """Steht in einem `finally` und darf deshalb nie werfen."""
        if not lease_id:
            return
        if self.broker is None:
            try:
                close_lease_in_core(principal, lease_id)
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            self.broker.close_lease(lease_id)
        except Exception:  # noqa: BLE001
            pass

    async def _call(self, payload: dict, *, token: str) -> dict[str, Any]:
        """Ueber den Broker, nie direkt. Der Lead traegt einen Broker-Token,
        keinen Anbieterschluessel — und ohne offenes Lease oeffnet der nichts."""
        import aiohttp

        from solvio.provider_broker.service import configured_port
        port = int(self.port) or configured_port()
        url = f"http://127.0.0.1:{port}/v1/responses"
        kopf = {"Authorization": f"Bearer {token}",
                "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=LEASE_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sitzung:
                async with sitzung.post(url, json=payload, headers=kopf) as antwort:
                    rumpf = await antwort.text()
                    if antwort.status != 200:
                        return {"ok": False, "reason": f"broker_{antwort.status}"}
                    try:
                        daten = json.loads(rumpf)
                    except ValueError:
                        return {"ok": False, "reason": "broker_unreadable"}
        except aiohttp.ClientError as exc:
            return {"ok": False, "reason": f"broker_unreachable:{type(exc).__name__}"}
        except TimeoutError:
            return {"ok": False, "reason": "broker_timeout"}
        from solvio.agent_runtime.planner import response_text, response_tokens
        return {"ok": True, "text": response_text(daten),
                "tokens": response_tokens(daten)}
