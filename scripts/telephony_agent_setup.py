#!/usr/bin/env python3
"""Oeffnet am Telefonagenten GENAU EINEN Ueberschreibungsschalter.

Der Schalter heisst `conversation.max_duration_seconds`, und ohne ihn ist die
freigegebene Hoechstdauer wirkungslos: SOLVIO sendet sie mit, der Anbieter
verwirft sie stillschweigend, und auf dem iPhone stuende eine Zahl, an die sich
niemand halten muss. Genau das hat der Technical Lead gefunden.

Was dieses Skript ausdruecklich NICHT oeffnet: `agent.prompt` (der
Systemprompt), `agent.prompt.tool_ids`, `native_mcp_server_ids`,
`knowledge_base`, `first_message`, `language`, die Stimme. Sie bleiben zu, und
der Preflight prueft das bei jedem Lauf nach. Ein offener Prompt-Schalter waere
der Unterschied zwischen „der Agent hat feste Regeln" und „wer den Schluessel
hat, schreibt die Regeln neu".

Es ist ein EINRICHTUNGSSKRIPT: der Eigentuemer startet es selbst, es benutzt
den Bereich `telephony_setup`, und die Laufzeitflaeche erreicht diesen Pfad
nie.

    python3 scripts/telephony_agent_setup.py --agent-id agent_...
    python3 scripts/telephony_agent_setup.py --agent-id agent_... --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from solvio.capabilities import policy as AP              # noqa: E402
from solvio.secret_vault import context as SC             # noqa: E402
from solvio.telephony import elevenlabs as EL             # noqa: E402
from solvio.telephony import upstream as U                # noqa: E402


def _offene(baum, pfad=""):
    """Jeder Schalter, der auf True steht — rekursiv, weil sie verschachtelt sind."""
    gefunden = []
    for schluessel, wert in (baum or {}).items():
        voll = f"{pfad}.{schluessel}" if pfad else schluessel
        if isinstance(wert, dict):
            gefunden.extend(_offene(wert, voll))
        elif wert is True:
            gefunden.append(voll)
    return gefunden


async def run(agent_id: str, dry_run: bool) -> int:
    up = U.ElevenLabsUpstream()
    with SC.bound(SC.UseContext(origin=AP.OriginClass.LOCAL_OWNER,
                                capability=EL.CAPABILITY_SETUP,
                                user_present=True)):
        vorher = await up.call("GET", f"/v1/convai/agents/{agent_id}",
                               scope=EL.CAPABILITY_SETUP)
        if vorher.status != 200 or not isinstance(vorher.body, dict):
            print(f"FEHLER: Agent nicht lesbar (http {vorher.status})")
            return 1

        plattform = vorher.body.get("platform_settings") or {}
        ueber = plattform.get("overrides") or {}
        print("vorher offen:", _offene(ueber) or "keine")

        if dry_run:
            print("dry-run: nichts geaendert.")
            return 0

        # Zwei Dinge werden gesetzt, und nur diese zwei.
        #
        # (1) Die Hoechstdauer darf ueberschrieben werden — sonst ist die
        #     freigegebene Grenze wirkungslos.
        # (2) Die SPRACHAUFZEICHNUNG wird abgeschaltet.
        #
        # Zu (2): am Agenten stand `record_voice: true` mit `retention_days: -1`,
        # also Mitschnitt auf unbegrenzte Zeit. SOLVIO sendet je Anruf zwar
        # `call_recording_enabled: false`, aber ob dieses Feld die Einstellung
        # des Agenten wirklich schlaegt, laesst sich ohne echten Anruf nicht
        # belegen — und bei einer Aufzeichnung ist „vermutlich aus" zu wenig.
        # Zwei Schranken, und die aeussere ist die des Anbieters.
        #
        # Das TRANSKRIPT bleibt ausdruecklich erhalten. Es ist der einzige
        # Beleg dafuer, ob die Nachricht ueberhaupt ausgerichtet wurde; ohne es
        # faellt die Zustellwahrheit dauerhaft auf UNKNOWN.
        rumpf = {"platform_settings": {
            "overrides": {"conversation_config_override": {
                "conversation": {"max_duration_seconds": True}}},
            "privacy": {"record_voice": False}}}
        antwort = await up.call("PATCH", f"/v1/convai/agents/{agent_id}",
                                scope=EL.CAPABILITY_SETUP, body=rumpf)
        if antwort.status >= 400:
            print(f"FEHLER: Aenderung abgelehnt (http {antwort.status})")
            print(json.dumps(antwort.body, ensure_ascii=False)[:400])
            return 1

        nachher = await up.call("GET", f"/v1/convai/agents/{agent_id}",
                                scope=EL.CAPABILITY_SETUP)
        ueber2 = ((nachher.body or {}).get("platform_settings")
                  or {}).get("overrides") or {}
        offen = _offene(ueber2)
        print("nachher offen:", offen or "keine")

        # Die Gegenprobe ist der Zweck: genau EIN Schalter, und zwar dieser.
        if offen != ["conversation_config_override.conversation.max_duration_seconds"]:
            print("FEHLER: die Menge der offenen Schalter ist nicht die erwartete.")
            return 1
        print("ok: genau die Hoechstdauer ist ueberschreibbar, sonst nichts.")

        # Und die zweite Gegenprobe: die Aufzeichnung ist wirklich aus, das
        # Transkript ist wirklich noch da.
        privat = ((nachher.body or {}).get("platform_settings") or {}).get("privacy") or {}
        print("record_voice:", privat.get("record_voice"))
        if privat.get("record_voice") is not False:
            print("FEHLER: die Sprachaufzeichnung ist NICHT aus.")
            return 1
        if privat.get("delete_transcript_and_pii") is True:
            print("FEHLER: das Transkript wird geloescht — die Zustellwahrheit "
                  "faellt damit dauerhaft auf UNKNOWN.")
            return 1
        print("ok: Aufzeichnung aus, Transkript erhalten.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="telephony_agent_setup")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.agent_id, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
