"""Der starke Antwortweg im Sprachgespraech — gezielt, nicht flaechendeckend.

Geprueft wird, was DIESER Kandidat neu einfuehrt: der eigene Auftraggeber und
seine vier Grenzen, die Nicht-Belichtung vor dem Sprachmodell, die Ehrlichkeit
ueber „nicht gesucht", und die Zustandspruefung. Nicht geprueft wird der Broker
selbst, die Freigabematrix und der Kontrollsocket — die haben ihre eigenen
Suiten, und sie nachzutesten erzeugt Zeilen, keine Evidenz.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402

from solvio.capabilities.contract import (  # noqa: E402
    CapabilityDeclined, ExecutorUnavailable)
from solvio.capabilities.policy import (  # noqa: E402
    ActionClass, Decision, OriginClass, decide)
from solvio.capabilities.voice_supervisor import (  # noqa: E402
    ANTWORT_TOKENS, DENK_RESERVE, MAX_ATTEMPTS, MAX_INPUT_CHARS,
    MAX_OUTPUT_TOKENS, MAX_QUESTION_CHARS, MODEL, REASONING_EFFORT,
    REQUEST_TIMEOUT, SPECS, TEXT_VERBOSITY, VoiceSupervisorCapabilities,
    _payload)
from solvio.provider_broker.service import (  # noqa: E402
    RESEARCH_QUICK_PRINCIPAL, VOICE_SUPERVISOR_PRINCIPAL)
from solvio.provider_broker.session import (  # noqa: E402
    RESEARCH_QUICK_CAPS, VOICE_SUPERVISOR_CAPS)
from solvio.security.mobile_approval.execution import READ_ONLY  # noqa: E402
from solvio.tools.base import RiskLevel  # noqa: E402

enforce_assertions()


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- Hilfsattrappen

class FakeBroker:
    """Zaehlt, was der Weg beim Broker WIRKLICH tut."""

    def __init__(self, *, lease_fehler: Exception | None = None) -> None:
        self.principals: list[str] = []
        self.leases: list[tuple[str, str]] = []
        self.geschlossen: list[str] = []
        self.lease_fehler = lease_fehler

    def register_principal(self, name: str) -> str:
        self.principals.append(name)
        return f"token-fuer-{name}"

    def open_lease(self, name: str, ref: str, *, deadline: float = 0.0) -> str:
        if self.lease_fehler is not None:
            raise self.lease_fehler
        self.leases.append((name, ref))
        return f"lease-{len(self.leases)}"

    def close_lease(self, lease_id: str) -> None:
        self.geschlossen.append(lease_id)


class Beutel:
    def __init__(self, broker=None) -> None:
        self.provider_broker = broker


def _umschlag(text: str, *, status: str = "completed") -> dict:
    return {"ok": True, "data": {
        "status": status,
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 1200, "output_tokens": 180}}}


def _caps(antwort: dict, *, broker=None, mitschrift: list | None = None):
    broker = broker if broker is not None else FakeBroker()

    async def transport(payload, *, token="", port=0):
        if mitschrift is not None:
            mitschrift.append({"payload": payload, "token": token})
        return dict(antwort)

    return VoiceSupervisorCapabilities(Beutel(broker), transport=transport), broker


# ------------------------------------------------- A: Der eigene Auftraggeber

def t_a_der_auftraggeber_ist_eigen_und_eng():
    """Ein eigener Auftraggeber, nicht eine geborgte Eskalationsbahn."""
    require_equal(VOICE_SUPERVISOR_PRINCIPAL, "voice-supervisor")
    require(VOICE_SUPERVISOR_PRINCIPAL != RESEARCH_QUICK_PRINCIPAL,
            "er teilt sich den Auftraggeber mit der Recherche")
    require_equal(sorted(VOICE_SUPERVISOR_CAPS.allowed_models), ["gpt-5.4"],
                  "das Modelltor ist nicht auf genau ein Modell gestellt")
    require_equal(VOICE_SUPERVISOR_CAPS.max_leases, 1)
    require_equal(VOICE_SUPERVISOR_CAPS.max_inflight, 1)
    require(VOICE_SUPERVISOR_CAPS.requests_per_day <= 60,
            f"die Tageszahl ist nicht eng: {VOICE_SUPERVISOR_CAPS.requests_per_day}")
    require(VOICE_SUPERVISOR_CAPS.tokens_per_day <= 400_000,
            f"die Tagestoken sind nicht eng: {VOICE_SUPERVISOR_CAPS.tokens_per_day}")


def t_a_kein_einziges_anbieterwerkzeug():
    """Er sucht nicht, liest keine Datei, fuehrt keinen Code aus.

    Die Suchbelegpflicht wurde in `research_quick` gehaertet. Ein zweiter Weg
    mit `web_search` waere eine zweite, ungehaertete Suche.
    """
    require_equal(set(VOICE_SUPERVISOR_CAPS.allowed_provider_tools), set(),
                  "dieser Weg darf ein anbieterseitiges Werkzeug nennen")
    require(set(RESEARCH_QUICK_CAPS.allowed_provider_tools) == {"web_search"},
            "die Kurzrecherche wurde dabei mitveraendert")


def t_a_er_praegt_nur_seinen_eigenen_token():
    """Kein fremder Principal-Token, auch nicht der der Recherche."""
    mit: list = []
    caps, broker = _caps(_umschlag("Antwort."), mitschrift=mit)
    run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    require_equal(broker.principals, [VOICE_SUPERVISOR_PRINCIPAL],
                  f"gepraegt wurde: {broker.principals}")
    require_equal([n for n, _ in broker.leases], [VOICE_SUPERVISOR_PRINCIPAL])
    require_equal(mit[0]["token"], f"token-fuer-{VOICE_SUPERVISOR_PRINCIPAL}",
                  "ein fremder Token ging hinaus")


def t_a_das_lease_wird_immer_geschlossen():
    caps, broker = _caps({"ok": False, "reason": "irgendwas"})
    try:
        run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    except ExecutorUnavailable:
        pass
    require_equal(len(broker.geschlossen), 1,
                  "ein Fehlschlag liess das Lease offen")


# ------------------------------------------------------------ B: Die Grenzen

def t_b_das_modell_ist_festgelegt_nicht_waehlbar():
    mit: list = []
    caps, _ = _caps(_umschlag("Antwort."), mitschrift=mit)
    run(caps.answer({"question": "Erklaer mir kurz die Sache.",
                     "model": "gpt-5.4-turbo-erfunden"}))
    require_equal(mit[0]["payload"]["model"], MODEL,
                  "der Modellname war aus dem Aufruf waehlbar")
    require_equal(MODEL, "gpt-5.4")
    require("model" not in SPECS["voice_supervisor_answer"].input_schema["properties"],
            "das Schema laedt dazu ein, ein Modell mitzugeben")


def t_b_zu_grosser_zusammenhang_wird_abgelehnt_nicht_gekuerzt():
    """Ein stillschweigend halbierter Zusammenhang beantwortet eine andere Frage."""
    caps, broker = _caps(_umschlag("Antwort."))
    try:
        run(caps.answer({"question": "Erklaer mir kurz die Sache.",
                         "context": "x" * (MAX_INPUT_CHARS + 1)}))
    except CapabilityDeclined as exc:
        require_equal(exc.reason, "context_too_large")
        require_equal(broker.principals, [],
                      "es wurde trotzdem ein Anbieteraufruf begonnen")
        return
    require(False, "ein zu grosser Zusammenhang kam durch")


def t_b_antwortlaenge_und_denkbudget_sind_getrennt():
    """**Der Fehler, um den es geht.**

    `max_output_tokens` deckelt bei einem Reasoning-Modell Denken UND Text
    zusammen — woertlich: „including visible output tokens and reasoning
    tokens". Eine Kappe von 700 mit der Begruendung „das ist eine gesprochene
    Antwort" verwechselt beides; bei erschoepftem Budget kaeme ein LEERER Text
    zurueck, bezahlt.
    """
    require(MAX_OUTPUT_TOKENS == ANTWORT_TOKENS + DENK_RESERVE,
            "die Kappe ist von Hand gesetzt statt abgeleitet — dann laufen "
            "die beiden Groessen wieder auseinander")
    # Die hoechste gemessene Denkarbeit war 1080 Token (12 echte Laeufe,
    # effort=low). Eine Reserve darunter ist keine Reserve.
    require(DENK_RESERVE >= 2 * 1080,
            f"die Denk-Reserve unterschreitet das Gemessene: {DENK_RESERVE}")
    # Die laengste gemessene sichtbare Antwort hatte 320 Token.
    require(ANTWORT_TOKENS >= 320,
            f"die Antwortlaenge unterschreitet das Gemessene: {ANTWORT_TOKENS}")
    require(ANTWORT_TOKENS <= 600,
            f"fuer eine gesprochene Antwort zu gross: {ANTWORT_TOKENS}")


def t_b_beide_schrauben_stehen_im_rumpf():
    """Denkaufwand UND Ausfuehrlichkeit — zwei getrennte Objekte, wie in der Doku.

    Wer nur `reasoning.effort` setzt, bekommt fuer den sichtbaren Text die
    Voreinstellung `medium` — also die ausfuehrliche.
    """
    mit: list = []
    caps, _ = _caps(_umschlag("Antwort."), mitschrift=mit)
    run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    rumpf = mit[0]["payload"]
    require_equal(rumpf["max_output_tokens"], MAX_OUTPUT_TOKENS)
    require_equal(rumpf["reasoning"]["effort"], REASONING_EFFORT)
    require_equal(rumpf["text"]["verbosity"], TEXT_VERBOSITY,
                  "die Laengenschraube fehlt im Rumpf")
    require_equal(TEXT_VERBOSITY, "low",
                  "fuer eine gesprochene Antwort ist das zu ausfuehrlich")
    # `gpt-5.4` hat als Vorgabe `none` — wer nichts setzt, denkt gar nicht.
    require(REASONING_EFFORT in ("low", "medium"),
            f"unerwarteter Denkaufwand: {REASONING_EFFORT!r}")


def t_b_die_tageskappe_bindet_spaeter_als_die_aufrufzahl():
    """Eine Grenze, die frueher greift als die dokumentierte, ist eine Falle.

    Der Broker bucht `ceil(Rumpfbytes / 4) + max_output_tokens` VORAB gegen die
    Tageskappe und sieht `usage` auf dieser Strecke nie (gemessen: 20 von 20
    Zeilen `estimated`). Eine grosszuegige Kappe verbraucht also das
    Tagesbudget, ob genutzt oder nicht.
    """
    from solvio.provider_broker.proxy import BYTES_PER_TOKEN
    # Groesstmoeglicher Rumpf: Zusammenhang + Frage + Anweisung + Geruest.
    from solvio.capabilities.voice_supervisor import INSTRUCTION
    rumpf_bytes = MAX_INPUT_CHARS + len(INSTRUCTION) + 400
    je_aufruf = -(-rumpf_bytes // BYTES_PER_TOKEN) + MAX_OUTPUT_TOKENS
    gesamt = je_aufruf * VOICE_SUPERVISOR_CAPS.requests_per_day
    require(gesamt <= VOICE_SUPERVISOR_CAPS.tokens_per_day,
            f"{VOICE_SUPERVISOR_CAPS.requests_per_day} Aufrufe brauchen "
            f"{gesamt} Token, die Kappe ist {VOICE_SUPERVISOR_CAPS.tokens_per_day} "
            f"— die Tokenkappe bindet frueher als die Aufrufzahl")


def t_b_genau_ein_versuch_und_eine_frist():
    """Ein Gespraechsturn hat keine zweite Frist."""
    require_equal(MAX_ATTEMPTS, 1)
    require(REQUEST_TIMEOUT <= 45.0, f"die Frist ist zu lang: {REQUEST_TIMEOUT}")

    versuche = {"n": 0}

    async def transport(payload, *, token="", port=0):
        versuche["n"] += 1
        return {"ok": False, "reason": "broker_503"}

    caps = VoiceSupervisorCapabilities(Beutel(FakeBroker()), transport=transport)
    try:
        run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    except ExecutorUnavailable:
        pass
    require_equal(versuche["n"], 1,
                  f"es wurde {versuche['n']}-mal versucht statt einmal")


def t_b_keine_werkzeuge_im_rumpf():
    mit: list = []
    caps, _ = _caps(_umschlag("Antwort."), mitschrift=mit)
    run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    require_equal(mit[0]["payload"]["tools"], [],
                  "der Rumpf nennt ein anbieterseitiges Werkzeug")


# --------------------------------------------------- C: Ehrlichkeit der Antwort

def t_c_ein_abgebrochener_umschlag_ist_keine_antwort():
    """Ein halber Satz mit `status=incomplete` kam bei der Kurzrecherche durch.

    Positivliste, nicht Negativliste: NUR `completed` zaehlt.
    """
    for zustand in ("incomplete", "failed", "cancelled", "in_progress",
                    "erfundener_status", ""):
        caps, _ = _caps(_umschlag("Der Stand lautet: Partei A liegt bei",
                                  status=zustand))
        try:
            run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
        except CapabilityDeclined as exc:
            require_equal(exc.reason, "answer_incomplete", f"{zustand}: {exc.reason}")
            continue
        require(False, f"Zustand {zustand!r} kam als vollstaendige Antwort durch")


def t_c_der_weg_gibt_zu_dass_er_nicht_gesucht_hat():
    """Wer das verschweigt, laedt ein, seine Saetze fuer recherchiert zu halten."""
    caps, _ = _caps(_umschlag("Die Sache verhaelt sich so und so."))
    d = run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    require_equal(d["searched"], False, "er behauptet, gesucht zu haben")
    require_equal(d["content_trust"], "model_output")
    require("NICHT gesucht" in d["hinweis"], d["hinweis"])
    require_equal(d["model"], MODEL)


def t_c_die_antwort_ist_information_keine_autoritaet():
    """Das Vorbild macht den Supervisor faktisch zur Autoritaet. SOLVIO nicht."""
    caps, _ = _caps(_umschlag("Die Sache verhaelt sich so und so."))
    d = run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    require("eigenen Ton" in d["hinweis"], d["hinweis"])
    require("erfinde" in d["hinweis"], d["hinweis"])
    spec = SPECS["voice_supervisor_answer"]
    require_equal(spec.semantics, READ_ONLY, "der Weg ist nicht READ_ONLY")
    require_equal(spec.base_risk, RiskLevel.HARMLESS)


def t_c_der_erschoepfte_denkbudget_fall_hat_einen_eigenen_grund():
    """Damit die Kalibrierung ihn im Buch findet.

    `incomplete_details.reason == "max_output_tokens"` ist der Fall, der
    `DENK_RESERVE` korrigiert. Ihn mit jedem anderen Abbruch zu vermengen
    hiesse, die eine Zahl zu verlieren, um die es geht.
    """
    umschlag = _umschlag("", status="incomplete")
    umschlag["data"]["incomplete_details"] = {"reason": "max_output_tokens"}
    umschlag["data"]["usage"] = {"input_tokens": 1200, "output_tokens": 3000,
                                 "output_tokens_details": {"reasoning_tokens": 3000}}
    caps, _ = _caps(umschlag)
    try:
        run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    except CapabilityDeclined as exc:
        require_equal(exc.reason, "budget_exhausted", exc.reason)
        return
    require(False, "ein erschoepftes Budget kam als Antwort durch")


def t_c_denkarbeit_und_abbruchgrund_werden_protokolliert():
    """Ohne diese Zahlen kann der erste echte Lauf nichts kalibrieren.

    Am 07.09.2026 lief eine Kurzrecherche 16,9 s ins Leere, und es gab KEINE
    Stelle im Haus, die Verbrauch oder Abbruchgrund kannte.
    """
    import solvio.capabilities.voice_supervisor as V
    protokoll: list[dict] = []
    echt = V.log.info

    def merken(name, **felder):
        protokoll.append({"name": name, **felder})
        return echt(name, **felder)

    V.log.info = merken
    try:
        umschlag = _umschlag("Die Antwort.")
        umschlag["data"]["usage"] = {
            "input_tokens": 1200, "output_tokens": 1500,
            "output_tokens_details": {"reasoning_tokens": 1180}}
        caps, _ = _caps(umschlag)
        run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    finally:
        V.log.info = echt

    zeilen = [z for z in protokoll if z["name"] == "voice_supervisor.response"]
    require(zeilen, "es wurde gar nichts protokolliert")
    z = zeilen[0]
    for feld in ("reasoning_tokens", "incomplete", "out_tokens",
                 "sichtbare_tokens", "budget", "reserve", "effort",
                 "verbosity", "elapsed_ms"):
        require(feld in z, f"das Feld {feld!r} fehlt im Protokoll: {sorted(z)}")
    require_equal(z["reasoning_tokens"], 1180)
    # reasoning_tokens sind IN out_tokens enthalten, nicht zusaetzlich —
    # belegt an der Beispielrechnung der Anbieterdoku (75 + 1186 = 1261).
    require_equal(z["sichtbare_tokens"], 1500 - 1180,
                  "der sichtbare Anteil wird falsch gerechnet")


def t_c_leere_antwort_wird_nicht_als_erfolg_gemeldet():
    caps, _ = _caps(_umschlag(""))
    try:
        run(caps.answer({"question": "Erklaer mir kurz die Sache."}))
    except CapabilityDeclined as exc:
        require_equal(exc.reason, "answer_empty")
        return
    require(False, "eine leere Antwort kam als Erfolg durch")


# --------------------------------------------------- D: Zusammenhang und Quellen

def t_d_der_zusammenhang_erreicht_das_modell_vollstaendig():
    """Einschliesslich frueherer Ergebnisse UND ihrer Quellen."""
    verlauf = ("Mensch: Wie hoch war der DAX am Freitag?\n"
               "SOLVIO: 26.046,40 Punkte, Quelle Deutsche Boerse, "
               "Schlussstand 4. September 2026, 17:50.\n"
               "Mensch: Und was heisst das fuer mein Depot?")
    mit: list = []
    caps, _ = _caps(_umschlag("Antwort."), mitschrift=mit)
    run(caps.answer({"question": "Was heisst der DAX-Stand fuer das Depot?",
                     "context": verlauf}))
    gesendet = mit[0]["payload"]["input"][1]["content"]
    require("26.046,40" in gesendet, "das fruehere Ergebnis fehlt")
    require("Deutsche Boerse" in gesendet, "die Quelle fehlt")
    require("4. September 2026" in gesendet, "der Zeitbezug fehlt")
    require("Was heisst der DAX-Stand" in gesendet, "die Frage fehlt")


def t_d_ohne_zusammenhang_geht_nur_die_frage_hinaus():
    mit: list = []
    caps, _ = _caps(_umschlag("Antwort."), mitschrift=mit)
    run(caps.answer({"question": "Warum ist der Himmel blau?"}))
    gesendet = mit[0]["payload"]["input"][1]["content"]
    require_equal(gesendet, "Warum ist der Himmel blau?",
                  "ohne Zusammenhang wurde etwas hinzugefuegt")


def t_d_die_anweisung_verbietet_erfundene_quellen():
    from solvio.capabilities.voice_supervisor import INSTRUCTION
    fliess = " ".join(INSTRUCTION.split())
    require("erfinde keine Zahl, kein Datum und keine Quelle" in fliess, fliess[:200])
    require("keine Werkzeuge und keinen Netzzugang" in fliess,
            "die Anweisung sagt dem Modell nicht, dass es nichts nachsehen kann")
    require("Markdown" in fliess, "die Anweisung verlangt keinen sprechbaren Text")


# ------------------------------------------- E: Autoritaetsflaeche unveraendert

def t_e_das_sprachmodell_bekommt_diesen_weg_NICHT():
    """Die Autoritaetsflaeche des laufenden SOLVIO waechst um genau nichts."""
    from solvio.config import Settings
    from solvio.tools.registry import attach_cognition, build_dispatcher
    d = build_dispatcher(Settings(openai_api_key="probe-nicht-echt-0000"))
    attach_cognition(d, mode="active")
    angeboten = {w["name"] for w in d.openai_tools()}
    require("voice_supervisor_answer" not in angeboten,
            "der starke Weg liegt dem Sprachmodell offen")
    require("voice_supervisor_answer" in set(d.capabilities.names()),
            "die Faehigkeit ist gar nicht angemeldet")
    require("research_quick" in angeboten,
            "die Kurzrecherche wurde dabei mitveraendert")


def t_e_schreibfreigaben_unveraendert():
    for herkunft in OriginClass:
        lesend = decide(herkunft, ActionClass.READ_ONLY)
        schreibend = decide(herkunft, ActionClass.VERY_CRITICAL)
        require_equal(lesend.decision, Decision.EXECUTE_DIRECTLY, str(herkunft))
        require(schreibend.decision is not Decision.EXECUTE_DIRECTLY,
                f"{herkunft}: eine sehr kritische Handlung laeuft direkt")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
