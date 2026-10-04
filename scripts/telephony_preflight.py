#!/usr/bin/env python3
"""READ_ONLY-Preflight der Telefonie. Liest, entscheidet nichts, ruft niemanden an.

Was hier NICHT passiert: kein Anruf, kein Agent wird angelegt, keine Nummer
importiert, keine Einstellung geaendert. Das Skript benutzt ausschliesslich den
Bereich `telephony_preflight`, und dessen Pfadliste enthaelt nur GETs. Selbst
wenn jemand hier einen POST hineinschriebe, kaeme er am Pfadtor nicht vorbei.

Der Schluessel wird nie angezeigt. Er wird auch nie zurueckgegeben — dieses
Skript sieht ihn selbst nicht, weil der Wert im `with`-Block von
`solvio.telephony.upstream` bleibt.

    python3 scripts/telephony_preflight.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from solvio.capabilities import policy as AP              # noqa: E402
from solvio.secret_vault import context as SC             # noqa: E402
from solvio.telephony import elevenlabs as EL             # noqa: E402
from solvio.telephony import upstream as U                # noqa: E402


#: Der einzige Schalter, der offenstehen DARF.
ERLAUBTER_OVERRIDE = "conversation_config_override.conversation.max_duration_seconds"


def offene_schalter(baum, pfad: str = "") -> list[str]:
    """Jeder Schalter, der auf True steht — rekursiv.

    Rekursiv, weil die Schalter verschachtelt sind: `agent.prompt` ist selbst
    ein Objekt mit `prompt`, `llm`, `tool_ids`, `native_mcp_server_ids` und
    `knowledge_base`. Eine flache Wahrheitspruefung haelt dieses nicht-leere
    Objekt fuer einen offenen Schalter und schlaegt falschen Alarm — genau das
    ist beim ersten Lauf passiert.
    """
    gefunden: list[str] = []
    for schluessel, wert in (baum or {}).items():
        voll = f"{pfad}.{schluessel}" if pfad else schluessel
        if isinstance(wert, dict):
            gefunden.extend(offene_schalter(wert, voll))
        elif wert is True:
            gefunden.append(voll)
    return gefunden


def unerlaubte_schalter(platform_settings) -> list[str]:
    """Was offensteht und nicht offenstehen darf — ueber den VOLLEN Baum.

    Die erste Fassung verglich nur innerhalb von
    `conversation_config_override` und schloss dort den ganzen
    `conversation`-Zweig aus. Damit waeren `conversation.text_only` oder ein
    offenes `custom_llm_extra_body` als „keine (richtig)" durchgegangen. Das
    Einrichtungsskript hat immer ueber den vollen Baum verglichen; hier steht
    derselbe Vergleich, denn dieser hier laeuft JEDES Mal.
    """
    ueber = (platform_settings or {}).get("overrides") or {}
    return [p for p in offene_schalter(ueber) if p != ERLAUBTER_OVERRIDE]


def _line(label: str, value: object) -> None:
    print(f"  {label:26s} {value}")


async def run() -> int:
    up = U.ElevenLabsUpstream()
    fehler = 0

    # Herkunft LOCAL_OWNER: der Eigentuemer startet das hier selbst am
    # Terminal. Ohne gesetzten Vorgang waere die Herkunft UNSPECIFIED, und der
    # Tresor verweigert — richtigerweise.
    with SC.bound(SC.UseContext(origin=AP.OriginClass.LOCAL_OWNER,
                                capability=EL.CAPABILITY_PREFLIGHT,
                                user_present=True)):

        print("== Zugang ==")
        try:
            agents = await up.call("GET", "/v1/convai/agents",
                                   scope=EL.CAPABILITY_PREFLIGHT)
        except U.TelephonyUpstreamError as exc:
            _line("credential", f"FEHLER {exc.reason} {exc.detail}")
            return 1

        _line("http", agents.status)
        if agents.status == 200:
            _line("schluessel", "gueltig, ElevenAgents lesbar")
        elif agents.status == 401:
            _line("schluessel", "UNGUELTIG (401) — falsch eingetippt?")
            return 1
        elif agents.status == 403:
            _line("schluessel", "KEINE BERECHTIGUNG (403) — Zeile "
                                "'ElevenAgents' im Schluesseldialog pruefen, "
                                "oder Tarif enthaelt Agents nicht")
            body = agents.body if isinstance(agents.body, dict) else {}
            _line("anbietergrund", str(body.get("detail", ""))[:200])
            return 1
        else:
            _line("schluessel", f"unerwartet {agents.status}")
            fehler += 1

        print()
        print("== Agenten ==")
        liste = []
        if isinstance(agents.body, dict):
            liste = agents.body.get("agents") or []
        _line("anzahl", len(liste))
        for a in liste[:10]:
            if isinstance(a, dict):
                _line("  agent", f"{a.get('name','?')}  id={a.get('agent_id','?')}")
        if not liste:
            _line("hinweis", "noch kein Agent — wird im Bau angelegt")

        print()
        print("== Agentenkonfiguration ==")
        # Der Preflight muss nachsehen, ob die freigegebene Hoechstdauer am
        # Agenten ueberhaupt uebernommen werden DARF. Eine stillschweigend
        # ignorierte Ueberschreibung waere genau das Loch, das der Technical
        # Lead gefunden hat: eine Zahl auf dem Display, an die sich niemand
        # halten muss.
        agent_id = ""
        for a in liste:
            if isinstance(a, dict) and a.get("agent_id"):
                agent_id = str(a["agent_id"])
                break
        if not agent_id:
            _line("hinweis", "kein Agent — Ueberschreibung nicht pruefbar")
        else:
            try:
                detail = await up.call("GET", f"/v1/convai/agents/{agent_id}",
                                       scope=EL.CAPABILITY_PREFLIGHT)
            except U.TelephonyUpstreamError as exc:
                _line("agent lesen", f"FEHLER {exc.reason}")
                detail = None
            if detail is not None and isinstance(detail.body, dict):
                plattform = detail.body.get("platform_settings") or {}
                ueber = (plattform.get("overrides") or {}).get(
                    "conversation_config_override") or {}
                gespraech = ueber.get("conversation") or {}
                erlaubt = bool(gespraech.get("max_duration_seconds"))
                _line("Hoechstdauer ueberschreibbar", erlaubt)
                if not erlaubt:
                    _line("ACHTUNG", "die freigegebene Hoechstdauer wirkt NICHT — "
                                     "Schalter 'Maximale Gespraechsdauer' am Agenten "
                                     "einschalten")
                    fehler += 1
                # Und die Gegenprobe: alles andere muss zu bleiben.
                offen = unerlaubte_schalter(plattform)
                _line("sonstige Ueberschreibungen offen", offen or "keine (richtig)")
                if offen:
                    fehler += 1

                # Und die Aufzeichnung — beim ANBIETER, nicht im eigenen Code.
                #
                # Hier stand `record_voice: true` mit `retention_days: -1`, also
                # Mitschnitt auf unbegrenzte Zeit, waehrend SOLVIO je Anruf
                # `call_recording_enabled: false` sendet. Welches von beiden
                # gewinnt, laesst sich ohne echten Anruf nicht belegen. Diese
                # Zeile prueft deshalb die aeussere Schranke bei jedem Lauf —
                # eine Einstellung, die jemand in der Oberflaeche zurueckdreht,
                # faellt sonst niemandem auf.
                privat = plattform.get("privacy") or {}
                aufzeichnung = privat.get("record_voice")
                _line("Sprachaufzeichnung", "aus" if aufzeichnung is False
                      else f"AN ({aufzeichnung}) — muss aus sein")
                if aufzeichnung is not False:
                    fehler += 1

                # Das Transkript dagegen MUSS bleiben: es ist der einzige Beleg
                # dafuer, ob die Nachricht ausgerichtet wurde. Ohne es faellt
                # die Zustellwahrheit dauerhaft auf UNKNOWN.
                weg = privat.get("delete_transcript_and_pii")
                _line("Transkript", "bleibt (richtig)" if weg is not True
                      else "WIRD GELOESCHT — Zustellwahrheit waere blind")
                if weg is True:
                    fehler += 1

        print()
        print("== Telefonnummern ==")
        try:
            nums = await up.call("GET", "/v1/convai/phone-numbers",
                                 scope=EL.CAPABILITY_PREFLIGHT)
        except U.TelephonyUpstreamError as exc:
            _line("fehler", f"{exc.reason} {exc.detail}")
            return 1

        _line("http", nums.status)
        rows = nums.body if isinstance(nums.body, list) else []
        _line("anzahl", len(rows))
        outbound_faehig = 0
        for n in rows:
            if not isinstance(n, dict):
                continue
            _line("  nummer", f"label={n.get('label','?')} "
                              f"provider={n.get('provider','?')} "
                              f"id={n.get('phone_number_id','?')}")
            # `supports_outbound` ist beim Anbieter als veraltet markiert; es
            # wird hier ANGEZEIGT, aber ausdruecklich nicht als Beweis
            # genommen. Der belastbare Nachweis ist der Knopf in der
            # Oberflaeche bzw. der erste echte Anruf.
            _line("    supports_outbound (veraltet)", n.get("supports_outbound"))
            _line("    assigned_agent", (n.get("assigned_agent") or {}) if
                  isinstance(n.get("assigned_agent"), dict) else n.get("assigned_agent"))
            if n.get("supports_outbound"):
                outbound_faehig += 1
        if not rows:
            _line("hinweis", "keine Nummer importiert — Twilio fehlt noch")

        print()
        print("== Ergebnis ==")
        _line("ELEVENLABS READY", "ja" if agents.status == 200 else "nein")
        _line("OUTBOUND PHONE READY", "ja" if outbound_faehig else "nein")
        _line("aufgerufene Pfade", "nur GET aus der Preflight-Liste")
        _line("schluessel angezeigt", "nie")

    return 0 if not fehler else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
