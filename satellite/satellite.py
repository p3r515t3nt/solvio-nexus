#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SOLVIO Satellit - Zustandsautomat (Schritt 15) + Resilienz (Schritt 16).

IDLE           : nur lokale Wake-Word-Erkennung. KEINE Mac-Verbindung, kein Streaming.
WAKING         : ab 'Hey Solvio' Audio in den RAM puffern (max ~3 s), Core verbinden,
                 session_start; kein Audioverlust waehrend des Verbindungsaufbaus.
ACTIVE         : Vollduplex-Gespraech, Barge-in, mehrere Turns ohne neues Wake Word.
CLOSING        : auf session_end (Timeout vom Core) / Fehler Ressourcen schliessen -> IDLE.
ERROR_RECOVERY : bei Verbindungs-/Laufzeitfehler schliessen, kurze Pause -> IDLE.

Schritt 16:
- Audio-Device-Startup-Retry (USB evtl. spaeter da als der Dienst).
- Core-Discovery per Hostname/mDNS mit IP-Fallback (DHCP-robust).
- Robuste Recovery: Core nicht erreichbar / Verbindung waehrend ACTIVE verloren -> IDLE.
- Einfacher Health-Status als /tmp/solvio_health.json (keine Gespraechsinhalte).

Wake-Zuverlaessigkeit:
- Der Detektor wird LUECKENLOS gefuettert, in jedem Zustand. Vorher lief er nur
  im IDLE mit; waehrend einer Session sah er nichts, und danach stand das
  Weckwort, das die Session gestartet hatte, immer noch in seinem 1280-ms-
  Fenster. Es weckte den Satelliten ein zweites Mal, ohne dass jemand sprach.
- Der Satellit misst sein eigenes Hoeren (solvio_sat/telemetry.py) und kann
  belegte Taubheit von einem stillen Zimmer unterscheiden.
- Eine Reparatur wird nur auf BELEGTEN Befund ausgeloest, nie auf Uhrzeit.

Audio-DSP-Ebene (Firmware, Fixed Beam, AGC, MIC_GAIN, Wake-Modell) bleibt FROZEN.
Privacy: Im IDLE verlaesst kein Audio den Pi. Der Puffer liegt nur im RAM.
"""
import asyncio
from collections import deque
import faulthandler
import json
import os
import signal
import subprocess
import sys
import time

import websockets

from solvio_sat.audio import Recorder
from solvio_sat.telemetry import (ACTIONABLE, VERDICT_DETECTOR_FLAT, VERDICT_NO_FRAMES,
                                  WakeTelemetry, classify)
from solvio_sat.trial import (TRIAL_FLAG, TRIAL_POLL_S, TrialRecorder, host_stats,
                              read_flag)
from solvio_sat.wakeword import CONTEXT_FRAMES, WakeWordDetector, find_model

# Core-Adresse: Hostname/mDNS zuerst, IP nur als Fallback (DHCP-Wechsel-robust).
MAC_HOSTS = [host.strip() for host in os.environ.get("SOLVIO_CORE_HOSTS", "localhost").split(",") if host.strip()]
MAC_PORT = 8766
AUTH_TIMEOUT = 10.0

import satellite_auth as sat_auth  # noqa: E402
try:
    SATELLITE_ID, SATELLITE_SECRET = sat_auth.load_identity()
except sat_auth.SatelliteAuthError as _exc:
    print(f"[FATAL] Satellite-Credential nicht nutzbar: {_exc}", flush=True)
    raise SystemExit(2)
CHUNK_BYTES = 640
MAX_BUFFER_CHUNKS = 150          # ~3 s bei 20 ms/Chunk

#: Wie viel Audio VOR dem Weckwort mitgenommen wird — 25 Haeppchen a 20 ms.
#:
#: Bis hierher wurde im IDLE jedes Haeppchen dem Detektor vorgelegt und danach
#: weggeworfen. Wer "Hey Solvio, was steht heute an?" in einem Zug sagt, spricht
#: das "was" bereits, waehrend die Erkennung noch laeuft — und genau dieses
#: Stueck verliess den Pi nie. Der Core konnte es nicht retten: es kam dort nie
#: an.
#:
#: Die Groesse ist gemessen, nicht geraten. Aus dem ONNX-Modell:
#: Eingang [1, 16, 96] — der Klassifikator sieht 16 Embeddings a 80 ms, also
#: 1280 ms Kontext. Bis eine Aenderung am Mikrofon die Ausgabe erreichen kann,
#: vergehen im schlechtesten Fall
#:     <= 80 ms   Framequantisierung (feed sammelt 20-ms-Haeppchen zu 80 ms)
#:     ~80-120 ms Melspektrogramm + Embedding
#:     ~0-100 ms  bis der Score die Schwelle wirklich ueberschreitet
#: zusammen also rund 300 ms. 500 ms geben dem knapp das Doppelte und reichen
#: dabei hoechstens in den Ausklang des Weckworts hinein, nicht davor.
#:
#: Bewusst NICHT grosszuegiger: dieser Puffer ist fuer die Fortsetzung eines
#: Satzes da, nicht fuer eine Aufzeichnung des Raumes. Er liegt ausschliesslich
#: im Arbeitsspeicher, wird bei jedem Wecken geleert und ueberdauert den Prozess
#: nicht.
PREWAKE_CHUNKS = 25              # 500 ms bei 20 ms/Chunk
READY_TIMEOUT = 12.0
CONNECT_TIMEOUT = 5.0            # kurzer Timeout pro Kandidat
CONNECT_ATTEMPTS = 2            # wenige kontrollierte Runden pro Wake

#: Wie lange die Schliessung der Verbindung hoechstens dauern darf.
#:
#: `websockets` gibt `close()` per Voreinstellung 10 s fuer den Schliess-
#: Handshake (close_timeout). So lange liest in dieser Sitzung NIEMAND mehr vom
#: Mikrofon. Das Budget bis zum Verlust von Aufnahmematerial ist gemessen:
#: 64 KiB StreamReader-Grenze + 64 KiB Pipe + 100 ms ALSA-Ring, bei 64 000 B/s
#: also rund 2,05 s. Danach blockiert `arecord` im write() und der Aufnahmering
#: laeuft ueber — ausgerechnet in dem Moment, in dem der Nutzer nachfragt.
#: Eine Sekunde liegt sicher darunter und reicht fuer einen Handshake im LAN.
CLOSE_TIMEOUT = 1.0

#: Wie viele Haeppchen hoechstens auf den Detektor warten duerfen (5 s).
#: Nur eine Notbremse gegen unbegrenztes Wachsen.
#:
#: Die Reserve ist kleiner als hier urspruenglich behauptet. Gemessen auf
#: diesem Pi kostet ein `det.feed()` 23-25 ms je 80-ms-Frame, nicht 5 ms — der
#: Detektor ist rund 3,2-mal schneller als der Zufluss, nicht sechzehnmal. Das
#: deckt sich mit der beobachteten Grundlast von rund 31 % auf einem Kern und
#: reicht weiterhin klar; die Zahl steht hier korrigiert, weil eine um Faktor 5
#: zu optimistische Annahme jede spaetere Beurteilung dieser Schlange
#: verfaelscht.
FEED_QUEUE_MAX = 250

#: Pfad des Gesundheitswegs am Core. Der Sitzungsweg bleibt "".
HEALTH_PATH = "/health"
#: Wie oft berichtet wird. Der Core wertet einen Bericht nach dem 3,5-fachen
#: dieser Zeit als veraltet — drei ausgefallene Meldungen sind kein Zufall mehr.
HEARTBEAT_S = 60.0

#: Bruecke zwischen `main()` (wo gemessen wird) und `report_health()` (wo
#: berichtet wird). Bewusst schmal: eine Zahl, kein Zustand.
_LAST_CLASSIFY: list = [None]


def _last_classify_at() -> float:
    return _LAST_CLASSIFY[0] if _LAST_CLASSIFY[0] is not None else time.monotonic()

#: Wie lange auf ein 20-ms-Haeppchen gewartet wird, bevor die Schleife das
#: Stocken selbst bemerkt. Ohne diese Frist waere "die Schleife haengt im
#: Lesen" ein Zustand, den der Satellit nicht von "es ist still" unterscheiden
#: koennte — er wuerde einfach nie wieder etwas sagen.
READ_TIMEOUT = 2.0

#: Wie lange kein Tonhaeppchen ankommen muss, bis "der Core spricht" wieder
#: auf Falsch faellt. Bewusst grosszuegig: die Anzeige darf lieber eine halbe
#: Sekunde nachhaengen, als bei Netzjitter zu flackern.
SPEECH_GAP_S = 0.5
SPEECH_POLL_S = 0.1

#: Wie oft gemessen und beurteilt wird.
TELEMETRY_WINDOW_S = 10.0
#: Wie oft eine Zeile ins Journal geht. 60 s ergibt 1440 Zeilen am Tag; journald
#: rotiert selbst.
TELEMETRY_LOG_EVERY = 6

#: Wie viele Fenster hintereinander denselben belegten Befund zeigen muessen,
#: bevor repariert wird. Drei Fenster sind 30 s — lang genug, dass kein
#: einzelner Aussetzer genuegt, kurz genug, dass niemand einen Abend verliert.
REPAIR_AFTER_WINDOWS = 3
#: Hoechstens so viele Reparaturen pro Vorfall. Danach wird gemeldet, nicht
#: weiter probiert — dieselbe Bremse wie beim Arzt auf dem Mac.
REPAIR_MAX_ATTEMPTS = 2
#: Wie lange zwischen zwei Versuchen gewartet wird.
REPAIR_COOLDOWN_S = 300.0
#: Wie lange es gut gehen muss, bevor ein spaeterer Ausfall als NEUER Vorfall
#: zaehlt und das Versuchskonto wieder oeffnet.
REPAIR_HOLD_S = 900.0

HEALTH_FILE = "/tmp/solvio_health.json"
_health = {
    "service": "RUNNING", "state": "STARTING", "xvf3800": "UNKNOWN",
    "wake_model": "UNKNOWN", "core": "UNKNOWN", "core_host": None,
    "last_wake": None, "last_wake_score": None,
    "last_session_end": None, "last_error": None, "updated": None,
    "speaking": False,
    # Was der Satellit ueber sein eigenes Hoeren weiss. Erst hierdurch ist
    # "taub" ueberhaupt eine ueberpruefbare Aussage und nicht ein Eindruck.
    "hearing": None, "hearing_verdict": "unknown", "hearing_since": None,
    "repairs": 0, "last_repair": None,
}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def set_health(**kw):
    _health.update(kw)
    _health["updated"] = _now()
    try:
        tmp = HEALTH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_health, f)
        os.replace(tmp, HEALTH_FILE)
    except Exception:
        pass


def audio_device_ready() -> bool:
    """True, wenn die ReSpeaker-Aufnahmekarte am ALSA vorhanden ist."""
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True,
                             timeout=5).stdout
        return ("Array" in out) or ("XVF3800" in out)
    except Exception:
        return False


async def wait_for_audio_device():
    """Beim Boot kann USB spaeter kommen als der Dienst. Kontrollierter Backoff,
    kein sofortiger Dauer-Crash. Kein Firmware-Flash, kein DSP-Schreiben."""
    delays = [2, 5, 10]
    i = 0
    while not audio_device_ready():
        set_health(state="STARTING", xvf3800="NOT_READY")
        d = delays[i] if i < len(delays) else 15
        print(f"[STARTUP] XVF3800 noch nicht da, warte {d}s ...", flush=True)
        await asyncio.sleep(d)
        i += 1
    set_health(xvf3800="READY")


async def connect_core(path: str = "", *, attempts: int = CONNECT_ATTEMPTS,
                       announce: bool = True):
    """Verbindet zum Core: Kandidaten (Hostname/mDNS -> IP) je kurz probieren.
    Rueckgabe: (ws, host) oder (None, None). Kein minutenlanges Haengen.

    `announce=False` fuer den Gesundheitsbericht: der soll den Sitzungszustand
    in der Health-Datei nicht ueberschreiben. Ob der Core gerade erreichbar ist,
    ist eine andere Frage als ob gerade ein Gespraech laeuft."""
    last = None
    for _ in range(attempts):
        for host in MAC_HOSTS:
            url = f"ws://{host}:{MAC_PORT}{path}"
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(url, max_size=None, ping_interval=None),
                    timeout=CONNECT_TIMEOUT)
                if announce:
                    set_health(core="REACHABLE", core_host=host)
                return ws, host
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__} @ {host}"
        await asyncio.sleep(0.5)
    if announce:
        set_health(core="UNREACHABLE", last_error=f"connect: {last}")
    return None, None


async def report_health() -> bool:
    """Einen Gesundheitsbericht an den Core schicken. True, wenn er ankam.

    Der Core hat den Satelliten bisher nur beim Wecken gesehen. Ein Satellit,
    der nicht mehr hoert, meldet sich aber genau NIE — und weil "kein Wecken"
    nicht von "niemand hat gesprochen" zu unterscheiden war, stand im
    Kontrollzentrum alles auf gruen. Also berichtet er von sich aus.

    Ueber denselben authentifizierten Weg, nur auf eigenem Pfad: kein zweiter
    Port, kein zweites Geheimnis. Und ausdruecklich nur MESSWERTE — was daraus
    im Kontrollzentrum wird, entscheidet der Core.
    """
    ws, _host = await connect_core(HEALTH_PATH, attempts=1, announce=False)
    if ws is None:
        return False
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        challenge = json.loads(raw) if isinstance(raw, str) else {}
        if challenge.get("type") != "auth_challenge":
            return False
        await ws.send(json.dumps(sat_auth.build_hello(
            challenge, satellite_id=SATELLITE_ID, secret=SATELLITE_SECRET)))
        hearing = dict(_health.get("hearing") or {})
        if hearing:
            hearing["verdict_age_s"] = int(time.monotonic() - _last_classify_at())
        await ws.send(json.dumps({
            "type": "satellite_health",
            "state": _health.get("state"),
            "verdict": _health.get("hearing_verdict"),
            "hearing": hearing or None,
            "repairs": _health.get("repairs", 0),
            "last_repair": _health.get("last_repair"),
        }))
        ack = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        return json.loads(ack).get("type") == "health_ack"
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            await asyncio.wait_for(ws.close(), timeout=CLOSE_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass


async def run_session(rec: Recorder, det: WakeWordDetector, telem: WakeTelemetry,
                      prewake: list[bytes] | None = None) -> str:
    """WAKING -> ACTIVE -> CLOSING. Rueckgabe: 'closed' oder 'error'.

    `prewake` ist das kurze Stueck VOR der Erkennung. Es steht chronologisch
    vor allem Weiteren und wird genau einmal uebergeben; die Liste gehoert ab
    hier dieser Sitzung und stirbt mit ihr.

    Der Detektor laeuft hier MIT, obwohl waehrend einer Sitzung niemand auf ein
    Weckwort wartet. Das ist der Punkt: sein Zustand darf keine Luecke haben.
    Wer ihn waehrend der Sitzung schlafen legt, findet danach das alte Weckwort
    noch in seinem Fenster vor — und weckt sich selbst.
    """
    t_detect = time.monotonic()
    buffered: list[bytes] = list(prewake or [])
    n_prewake = len(buffered)
    stop_buffer = asyncio.Event()

    # Der Detektor laeuft waehrend der Sitzung mit, aber NEBEN dem Audiopfad,
    # nicht darin. Ein `det.feed()` kostet gemessen 23-25 ms ONNX je 80-ms-Frame;
    # stuende diese Zeit im `mic_to_mac`-Task direkt vor dem Senden, waere sie
    # genau die Verzoegerung, die Voice UX V2 aus dem Weg geraeumt hat. Die
    # Schlange nimmt sie nicht aus dem Ereignisschleife-Budget — das kann sie
    # nicht, es gibt nur einen Thread — aber sie loest sie vom Senden ab, sodass
    # ein Frame nie hinter einer Inferenz wartet.
    feed_q: asyncio.Queue = asyncio.Queue()

    def observe(chunk: bytes) -> None:
        if feed_q.qsize() < FEED_QUEUE_MAX:
            feed_q.put_nowait(chunk)
        else:
            # Bei 23-25 ms Arbeit je 80 ms Audio praktisch nicht erreichbar.
            # Gezaehlt wird es trotzdem: eine Luecke im Detektorzustand ist
            # genau das, was hier repariert werden soll, und sie darf nicht
            # unbemerkt zurueckkehren.
            telem.on_feed_drop()

    async def feeder():
        while True:
            chunk = await feed_q.get()
            sc = det.feed(chunk)
            if sc is not None:
                telem.on_score(sc)

    feed_task = asyncio.create_task(feeder())

    async def buffer_loop():
        # Ab Wake dauerhaft puffern (erste ~3 s behalten, Rest verwerfen,
        # damit arecord nicht ueberlaeuft) - so geht der erste Satz nicht verloren.
        while not stop_buffer.is_set():
            c = await rec.read_chunk()
            observe(c)
            if len(buffered) < MAX_BUFFER_CHUNKS:
                buffered.append(c)

    buf_task = asyncio.create_task(buffer_loop())

    # --- WAKING: verbinden (Puffern laeuft parallel weiter) ---
    set_health(state="WAKING")
    ws, host = await connect_core()
    if ws is None:
        stop_buffer.set(); buf_task.cancel()
        print("  [WAKING] Core nicht erreichbar -> ERROR_RECOVERY", flush=True)
        return "error"

    try:
        # --- M0/2: dem Core beweisen, wer wir sind, BEVOR irgendetwas passiert ---
        # Der Core schickt sofort nach dem Verbindungsaufbau eine frische Challenge und
        # wartet auf genau eine Antwort. Erst danach existiert ueberhaupt ein Session-
        # Protokoll; ein nicht authentifizierter Client kommt nie so weit.
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        challenge = json.loads(raw) if isinstance(raw, str) else {}
        if challenge.get("type") != "auth_challenge":
            raise RuntimeError(f"unerwartete erste Nachricht: {challenge.get('type')!r}")
        hello = sat_auth.build_hello(challenge, satellite_id=SATELLITE_ID,
                                     secret=SATELLITE_SECRET)
        await ws.send(json.dumps(hello))
        await ws.send(json.dumps({"type": "session_start"}))
        print(f"  [CONNECTING] via {host}, authentifiziert als {SATELLITE_ID}, "
              f"session_start gesendet, warte auf session_ready ...", flush=True)

        # auf session_ready warten (Puffern laeuft weiter)
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=READY_TIMEOUT)
            if isinstance(msg, (bytes, bytearray)):
                continue
            m = json.loads(msg)
            if m.get("type") == "session_ready":
                break
            if m.get("type") == "session_end":
                stop_buffer.set(); buf_task.cancel()
                return "closed"

        stop_buffer.set()
        # Mit Frist. `buffer_loop` haengt an demselben `read_chunk()` wie die
        # Hauptschleife, und dort ist das Warten seit diesem Milestone begrenzt
        # — hier war es die letzte Stelle im Audiopfad, an der ein stockendes
        # Mikrofon den Satelliten unbegrenzt festhalten konnte. Er stuende dann
        # in WAKING, ohne Sitzung, ohne Erkennung, ohne Log, und nur ein Mensch
        # koennte ihn herausholen. Bei Fristablauf faellt die Sitzung in den
        # bestehenden ERROR_RECOVERY-Weg — der fuehrt zurueck in die
        # Hauptschleife und damit an die Reparatur.
        try:
            await asyncio.wait_for(buf_task, timeout=READ_TIMEOUT)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            buf_task.cancel()
            print("  [WAKING] Mikrofon liefert nichts mehr -> ERROR_RECOVERY", flush=True)
            set_health(last_error="waking: mic_stall")
            return "error"

        t_ready = time.monotonic()
        print(f"  [ACTIVE] session_ready nach {int((t_ready - t_detect)*1000)} ms, "
              f"{len(buffered)} gepufferte Frames -> senden "
              f"(davon {n_prewake} vor dem Weckwort = {n_prewake*20} ms)",
              flush=True)
        set_health(state="ACTIVE")

        # gepuffertes Audio (erster Satz) zuerst, dann live
        for c in buffered:
            await ws.send(c)

        # --- ACTIVE: Vollduplex + Barge-in ---
        play_q: asyncio.Queue = asyncio.Queue()
        player = {"proc": None}

        # Ob gerade der Core spricht, laesst sich von aussen NICHT ablesen:
        # `aplay` laeuft die ganze Sitzung durch, unabhaengig davon, ob Ton
        # fliesst. Also wird der Fluss hier vermerkt - aber nur bei Wechseln.
        # `set_health` schreibt jedes Mal eine Datei, und die Haeppchen kommen
        # im 20-ms-Takt: ein Vermerk je Haeppchen waere Dauerschreiben auf die
        # SD-Karte fuer nichts.
        speak = {"on": False, "last": 0.0}

        def set_speaking(on):
            if speak["on"] != on:
                speak["on"] = on
                set_health(speaking=on)

        async def speech_watch():
            """Setzt 'spricht' zurueck, wenn der Tonstrom versiegt.

            Der Core meldet das Ende einer Aeusserung nicht - es zeigt sich
            allein daran, dass keine Haeppchen mehr ankommen.

            `play_q.empty()` gehoert zwingend dazu: zwischen "am Netz
            angekommen" und "aus dem Lautsprecher zu hoeren" liegt die
            unbegrenzte Warteschlange. Ohne diese Bedingung meldete der
            Satellit Stille, waehrend noch Sekunden Ton auf Wiedergabe
            warten - die Anzeige liefe dem Ton voraus. Der Puffer in `aplay`
            selbst (120 ms) bleibt unbeobachtbar; diese Restspanne ist
            hingenommen."""
            while True:
                await asyncio.sleep(SPEECH_POLL_S)
                if (speak["on"] and play_q.empty()
                        and time.monotonic() - speak["last"] > SPEECH_GAP_S):
                    set_speaking(False)

        async def start_player():
            player["proc"] = await asyncio.create_subprocess_exec(
                "aplay", "-D", "solvio", "-f", "S16_LE", "-r", "16000", "-c", "1",
                "-t", "raw", "-q", "--buffer-time=120000",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

        async def player_task():
            while True:
                chunk = await play_q.get()
                p = player["proc"]
                if p and p.stdin and not p.stdin.is_closing():
                    try:
                        p.stdin.write(chunk)
                        await p.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError, AssertionError):
                        pass

        async def flush_playback():
            try:
                while True:
                    play_q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            p = player["proc"]
            if p and p.returncode is None:
                try:
                    p.kill(); await p.wait()
                except ProcessLookupError:
                    pass
            await start_player()

        await start_player()
        ptask = asyncio.create_task(player_task())
        reason = {"v": "end"}

        async def mic_to_mac():
            while True:
                c = await rec.read_chunk()
                observe(c)
                await ws.send(c)

        async def mac_to_speaker():
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    play_q.put_nowait(message)
                    speak["last"] = time.monotonic()
                    set_speaking(True)
                else:
                    m = json.loads(message)
                    t = m.get("type")
                    if t == "flush":
                        # Barge-in: der gepufferte Ton wird verworfen, das
                        # Sprechen endet also JETZT - nicht erst, wenn die
                        # Luecke im Strom auffaellt.
                        set_speaking(False)
                        await flush_playback()
                    elif t == "ping":
                        await ws.send(json.dumps({"type": "pong", "t0": m.get("t0")}))
                    elif t == "session_end":
                        reason["v"] = m.get("reason", "end")
                        return

        tasks = {asyncio.create_task(mic_to_mac()), asyncio.create_task(mac_to_speaker())}
        swatch = asyncio.create_task(speech_watch())
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            # Exceptions der fertigen Tasks abholen (Verbindungsabriss waehrend ACTIVE),
            # damit keine "never retrieved"-Tracebacks entstehen und wir sauber schliessen.
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    reason["v"] = "connection_lost"
                    set_health(last_error=f"active: {type(exc).__name__}")
        finally:
            swatch.cancel()
            # Auch bei Abbruch oder Fehler darf kein 'spricht' stehenbleiben:
            # die Health-Datei ueberlebt die Sitzung.
            set_speaking(False)
            ptask.cancel()
            p = player["proc"]
            if p and p.returncode is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass

        print(f"  [CLOSING] Session beendet (reason={reason['v']}) -> IDLE", flush=True)
        set_health(state="CLOSING", last_session_end=_now())
        return "closed"
    except Exception as exc:  # noqa: BLE001 - jeder Sessionfehler -> sauber zu IDLE
        set_health(last_error=f"session: {type(exc).__name__}")
        print(f"  [ERROR] Sessionfehler {type(exc).__name__} -> ERROR_RECOVERY", flush=True)
        return "error"
    finally:
        stop_buffer.set()
        if not buf_task.done():
            buf_task.cancel()
        feed_task.cancel()
        # Mit Frist: siehe CLOSE_TIMEOUT. Solange hier niemand liest, laeuft der
        # Aufnahmering des Mikrofons auf sein Ende zu.
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=CLOSE_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass


class Repair:
    """Wann ueberhaupt repariert werden darf — und wann nicht mehr.

    Dieselbe Haltung wie beim Arzt auf dem Mac: erst ein BELEGTER Befund, dann
    hoechstens zwei Versuche, dazwischen eine Pause, und ein Erfolg muss halten,
    bevor ein spaeterer Ausfall wieder als neu zaehlt. Es gibt hier bewusst
    keinen Zweig, der nach Uhrzeit handelt: ein Neustart alle N Stunden wuerde
    das Symptom verdecken und die Ursache konservieren.
    """

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self.streak_verdict = ""
        self.streak = 0
        self.attempts = 0
        self.t_last_attempt = -1e9
        self.total = 0

    def observe(self, verdict: str) -> str | None:
        """Gibt den Befund zurueck, der jetzt repariert werden soll — oder None."""
        now = self._clock()
        if verdict not in ACTIONABLE:
            self.streak_verdict, self.streak = "", 0
            if self.attempts and now - self.t_last_attempt >= REPAIR_HOLD_S:
                # Die Reparatur hat gehalten. Ein spaeterer Ausfall ist damit
                # ein NEUER Vorfall und bekommt wieder ein Versuchskonto — ein
                # Rueckfall innerhalb der Haltefrist dagegen nicht.
                self.attempts = 0
            return None
        if verdict != self.streak_verdict:
            self.streak_verdict, self.streak = verdict, 1
        else:
            self.streak += 1
        if self.streak < REPAIR_AFTER_WINDOWS:
            return None
        if self.attempts >= REPAIR_MAX_ATTEMPTS:
            return None
        if now - self.t_last_attempt < REPAIR_COOLDOWN_S:
            return None
        return verdict

    def started(self) -> None:
        self.attempts += 1
        self.total += 1
        self.t_last_attempt = self._clock()
        self.streak = 0


async def main():
    thr = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
    set_health(service="RUNNING", state="STARTING")

    # Ein Stapelabzug auf Zuruf: `kill -USR1 <pid>` schreibt die Stacks aller
    # Threads ins Journal. Kostet nichts, solange niemand das Signal schickt,
    # und beantwortet im Ernstfall in einer Sekunde die Frage, die vorher nur
    # zu erraten war: haengt die Schleife, oder laeuft sie leer?
    faulthandler.register(signal.SIGUSR1, all_threads=True)

    # Schritt 16: warten bis das Audio-Device da ist (USB kann spaeter kommen).
    await wait_for_audio_device()

    try:
        det = WakeWordDetector(find_model("hey_solvio_final_v2"), thr)
        set_health(wake_model="READY")
    except Exception as exc:  # noqa: BLE001
        set_health(wake_model="ERROR", last_error=f"wake_model: {type(exc).__name__}")
        raise

    telem = WakeTelemetry()
    # Der Testmodus haengt am ENDE der ohnehin laufenden Messung. Solange
    # niemand ihn scharf schaltet, gibt er nichts aus; die produktive Erkennung
    # sieht ihn nicht.
    trial = TrialRecorder(threshold=thr)
    telem.observer = trial
    rec = Recorder("solvio", telemetry=telem)
    await rec.start()
    # Warmup: Detektor stabilisieren, damit er beim Start nicht faelschlich ausloest
    for _ in range(80):
        det.feed(await rec.read_chunk())
    det.reset()
    telem.reset_window()
    print(f"[IDLE] lausche lokal auf 'Hey Solvio' (Schwelle {thr}). "
          f"Kein Audio verlaesst den Pi.", flush=True)
    set_health(state="IDLE")
    trial.on_state("IDLE")

    repair = Repair()
    pending_repair: list[str] = []

    _LAST_CLASSIFY[0] = time.monotonic()

    async def telemetry_loop():
        """Misst, urteilt, meldet — und fasst NIE `read_chunk()` an.

        Das ist der Grund, warum dieser Task ueberhaupt getrennt ist: laege die
        Messung in derselben await-Kette wie das Lesen, wuerde ein haengendes
        Lesen beide gleichzeitig stumm schalten. Dann waere das Schweigen der
        Telemetrie wieder nicht von einem stillen Zimmer zu unterscheiden — und
        genau diese Verwechslung hat die Ursachensuche bisher blockiert.
        """
        n = 0
        while True:
            await asyncio.sleep(TELEMETRY_WINDOW_S)
            snap = telem.snapshot()
            verdict = classify(snap)
            # Wann dieses Urteil entstanden ist. Es reist mit zum Core, weil ein
            # frischer Bericht ueber eine stehengebliebene Messung dort sonst
            # als frische Gesundheit gelesen wuerde.
            _LAST_CLASSIFY[0] = time.monotonic()
            n += 1
            if verdict in ACTIONABLE or n % TELEMETRY_LOG_EVERY == 0:
                print(f"{telem.line(snap)} urteil={verdict}", flush=True)
            set_health(hearing=snap, hearing_verdict=verdict, hearing_since=_now())
            todo = repair.observe(verdict)
            if todo and not pending_repair:
                # Die Reparatur selbst passiert in der Hauptschleife. Wer hier
                # `rec.proc` oder `det.model` austauschte, wuerde es unter einem
                # laufenden `readexactly()` tun.
                pending_repair.append(todo)
                print(f"[REPAIR] Befund {todo} haelt seit "
                      f"{REPAIR_AFTER_WINDOWS * int(TELEMETRY_WINDOW_S)} s an "
                      f"-> Reparatur angefordert", flush=True)

    async def do_repair(what: str) -> None:
        """Der engste Eingriff, der den Befund erklaeren wuerde. Kein DSP."""
        repair.started()
        t0 = time.monotonic()
        if what == VERDICT_DETECTOR_FLAT:
            print("[REPAIR] Detektor wird neu aufgebaut ...", flush=True)
            det.reinitialize()
            action = "detector_reinit"
        else:
            # no_frames und left_dead betreffen den Zufluss, nicht den Detektor.
            # Ein neuer `arecord` oeffnet den ALSA-Strom neu. Das ist eine
            # Aufnahme-Sitzung, KEINE DSP-Aenderung: Firmware, Beam, Verstaerkung
            # und Wake-Modell bleiben unberuehrt.
            print("[REPAIR] Aufnahme wird neu geoeffnet ...", flush=True)
            await rec.stop()
            await asyncio.sleep(0.5)
            await rec.start()
            action = "recorder_restart"
        # Nach der Reparatur zaehlt nur ein frischer Befund. Das Fenster wird
        # geleert, damit kein Wert von vorher als Bestaetigung durchgeht.
        telem.reset_window()
        print(f"[REPAIR] {action} fertig nach {int((time.monotonic()-t0)*1000)} ms — "
              f"Wirkung wird im naechsten Fenster geprueft", flush=True)
        set_health(repairs=repair.total, last_repair=f"{_now()} {action} ({what})")

    async def heartbeat_loop():
        """Meldet dem Core regelmaessig, wie es dem Hoeren geht.

        Scheitert der Bericht, wird das genau EINMAL protokolliert und nicht
        bei jedem Versuch wieder: ein Core, der ueber Nacht aus ist, darf kein
        Journal fuellen. Der Satellit arbeitet ohne den Core ohnehin weiter —
        er hoert lokal, das ist der Sinn der Sache."""
        reachable = None
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            ok = await report_health()
            if ok != reachable:
                reachable = ok
                print(f"[HEARTBEAT] Core {'nimmt Berichte an' if ok else 'nicht erreichbar'}",
                      flush=True)

    def _task_died(task: asyncio.Task) -> None:
        """Ein still gestorbener Messtask ist schlimmer als ein Neustart.

        Stirbt `telemetry_loop`, laeuft der Satellit weiter und meldet dem Core
        unveraendert den letzten Bericht — der Core zeigt dann dauerhaft
        `healthy` ueber eine Messung, die es nicht mehr gibt. Genau diese
        Verwechslung von Schweigen mit Gesundheit ist der Defekt, gegen den
        dieser Milestone gebaut wurde; sie darf nicht durch die Hintertuer
        zurueckkommen. Also: laut sterben, systemd startet neu.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        print(f"[FATAL] Messtask beendet: {type(exc).__name__}: {str(exc)[:120]}",
              flush=True)
        set_health(service="DEGRADED", last_error=f"task_died: {type(exc).__name__}")
        asyncio.get_running_loop().call_soon(sys.exit, 3)

    async def throttled_state() -> str:
        """`vcgencmd get_throttled` — 2,1 ms, deshalb nur in den Rahmenzeilen."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "vcgencmd", "get_throttled",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            text = out.decode("ascii", "replace").strip()
            return text.split("=", 1)[-1] if text else "?"
        except Exception:  # noqa: BLE001
            return "?"

    async def trial_loop():
        """Der Testmodus. Ausserhalb des Audiopfads, ein Takt je Sekunde.

        Absicht kommt vom Menschen, nicht aus dem Pegel: JEDE Aenderung der
        Flagdatei oeffnet genau ein Versuchsfenster und erzeugt genau einen
        Datensatz. Der akustische Erkenner laeuft daneben weiter, aber seine
        Datensaetze heissen `burst` und sind Beifang — niemals Fehlerstatistik.
        """
        last_mtime = None
        while True:
            await asyncio.sleep(TRIAL_POLL_S)
            flag = read_flag()
            if flag is not None:
                mtime, spec = flag
                if mtime != last_mtime:
                    fresh_arm = not trial.armed
                    last_mtime = mtime
                    if fresh_arm:
                        trial.arm(marks=int(spec.get("trials", 20) or 20),
                                  deadline_s=float(spec.get("expires_s", 900) or 900),
                                  window_s=float(spec.get("window_s", 8) or 8))
                        print(f"[TRIAL] armed marks={trial.marks_left} "
                              f"fenster={trial.window_s:.0f}s "
                              f"frist={int(trial.armed_until - trial._clock())}s "
                              f"note={str(spec.get('note', ''))[:60]!r} "
                              f"throttled={await throttled_state()}", flush=True)
                    # Scharfschalten und Markieren sind getrennte Ereignisse.
                    # Das Anlegen der Datei ruestet nur; erst eine spaetere
                    # Beruehrung ist ein Versuch. Sonst verbraucht schon das
                    # Einrichten den ersten von N — und ausgerechnet der waere
                    # dann ein Fehlschlag, bei dem niemand gesprochen hat.
                    if not fresh_arm:
                        trial.mark()
            elif trial.armed:
                # Die Flagdatei ist weg — das ist die Abruestung. Vorher blieb
                # der Testmodus im Prozess scharf, obwohl aussen nichts mehr
                # darauf hindeutete: einmal lief er so acht Stunden weiter und
                # schrieb 200 Beifang-Datensaetze, waehrend ich behauptet hatte,
                # es sei alles abgeruestet. Wer die Datei entfernt, meint das.
                summary = trial.disarm()
                print(f"[TRIAL] disarmed (Flagdatei entfernt) nach "
                      f"{summary['dauer_s']} s — markiert {summary['markiert']}, "
                      f"aufgezeichnet {summary['aufgezeichnet']}; "
                      f"beifang {summary['beifang']}", flush=True)
                last_mtime = None
            elif last_mtime is not None:
                last_mtime = None

            trial.tick()
            for record in trial.drain():
                record.update(host_stats())
                print(f"[TRIAL] {json.dumps(record, ensure_ascii=False)}", flush=True)

            if trial.armed_until is not None and not trial.armed:
                summary = trial.disarm()
                for record in trial.drain():
                    record.update(host_stats())
                    print(f"[TRIAL] {json.dumps(record, ensure_ascii=False)}", flush=True)
                print(f"[TRIAL] disarmed nach {summary['dauer_s']} s — "
                      f"markiert {summary['markiert']}, aufgezeichnet "
                      f"{summary['aufgezeichnet']}, davon hit {summary['hit']} / "
                      f"miss {summary['miss']} / refractory {summary['refractory']} / "
                      f"not_idle {summary['not_idle']} / no_audio {summary['no_audio']}; "
                      f"beifang {summary['beifang']}; "
                      f"throttled={await throttled_state()}", flush=True)
                try:
                    os.unlink(TRIAL_FLAG)
                except OSError:
                    pass
                last_mtime = None

    tele_task = asyncio.create_task(telemetry_loop())
    beat_task = asyncio.create_task(heartbeat_loop())
    trial_task = asyncio.create_task(trial_loop())
    tele_task.add_done_callback(_task_died)
    beat_task.add_done_callback(_task_died)
    trial_task.add_done_callback(_task_died)

    # Das rollende Fenster. `maxlen` wirft die aeltesten Haeppchen von selbst
    # heraus — deterministisch, ganze Frames, ohne eigene Buchfuehrung. Es
    # liegt nur hier im Arbeitsspeicher: keine Datei, kein Log, und niemand
    # ausser dieser Schleife hat eine Referenz darauf.
    prewake: deque[bytes] = deque(maxlen=PREWAKE_CHUNKS)

    #: Wie viele frische 80-ms-Frames der Klassifikator noch braucht, bevor sein
    #: Fenster nichts Altes mehr enthaelt. Solange dieser Zaehler laeuft, ist ein
    #: Score eine Aussage ueber die Vergangenheit und weckt nichts.
    refractory = 0

    #: Ein einzelner ausgebliebener Haeppchen-Takt kann eine Planungsdelle sein.
    #: Zwei hintereinander sind es nicht — dann fehlen vier Sekunden Mikrofon.
    stalls = 0

    try:
        while True:
            try:
                c = await asyncio.wait_for(rec.read_chunk(), timeout=READ_TIMEOUT)
            except asyncio.TimeoutError:
                # Kein Haeppchen in READ_TIMEOUT. Das Abbrechen ist gefahrlos:
                # `readexactly` nimmt erst Bytes aus dem Puffer, wenn genug da
                # sind — ein Abbruch davor verbraucht nichts und verschiebt den
                # Strom nicht.
                stalls += 1
                print(f"[STALL] seit {int(telem.snapshot(reset=False)['since_last_chunk_ms'])} ms "
                      f"kein Mikrofonhaeppchen ({stalls}.)", flush=True)
                # Durch DIESELBE Bremse wie jeder andere Befund. Vorher haengte
                # dieser Pfad die Reparatur direkt an und umging damit
                # Versuchskonto, Abkuehlzeit und Haltefrist vollstaendig: ein
                # anhaltendes Stocken haette `arecord` im 2,5-s-Takt endlos neu
                # gestartet, und weil jede Reparatur das Messfenster leert,
                # haette genau die Schleife die Evidenz geloescht, wegen der sie
                # laeuft. Ein Dauerlauf ist die einzige Lage, in der das
                # passiert — und die einzige, in der niemand zusieht.
                if stalls >= 2 and not pending_repair:
                    todo = repair.observe(VERDICT_NO_FRAMES)
                    if todo:
                        pending_repair.append(todo)
                c = None
            if pending_repair:
                await do_repair(pending_repair.pop())
                refractory = CONTEXT_FRAMES
                trial.on_state("IDLE", refractory_left=refractory)
                stalls = 0          # der Neustart quittiert das Stocken
                prewake.clear()
                continue
            if c is None:
                continue

            stalls = 0
            prewake.append(c)
            s = det.feed(c)
            if s is None:
                continue
            telem.on_score(s)
            if refractory > 0:
                refractory -= 1
                trial.refractory_left = refractory
                if s >= thr:
                    # Genau der Phantom-Wake, den es vorher gab: der Score
                    # stammt aus Audio, das vor dieser Runde lag.
                    telem.on_phantom()
                    print(f"[PHANTOM] score {s:.2f} aus Alt-Kontext unterdrueckt "
                          f"({refractory} Frames bis frei)", flush=True)
                continue
            if s < thr:
                continue

            # Momentaufnahme und sofort leeren: das Fenster gibt seinen Inhalt
            # genau einmal ab. Was danach kommt, gehoert der Sitzung; was davor
            # war, ist ab hier vergessen — auch wenn die Sitzung gleich
            # scheitert.
            tail = list(prewake)
            prewake.clear()
            print(f"[WAKE_DETECTED] score {s:.2f} "
                  f"(+{len(tail)*20} ms Vorlauf)", flush=True)
            set_health(last_wake=_now(), last_wake_score=round(float(s), 2))
            telem.on_session()
            trial.on_wake(s)
            trial.on_state("WAKING")
            try:
                res = await run_session(rec, det, telem, tail)
            except Exception as exc:  # noqa: BLE001 - nie abstuerzen, immer zu IDLE
                print(f"  [ERROR_RECOVERY] {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                set_health(last_error=f"{type(exc).__name__}")
                res = "error"
            if res == "error":
                set_health(state="ERROR_RECOVERY")
                await asyncio.sleep(1.5)          # ERROR_RECOVERY kurze Pause
            # Kein `det.reset()` mehr. Der Detektor lief waehrend der Sitzung
            # mit, sein Zustand hat keine Luecke — und ein reset() wuerde in
            # openWakeWord ohnehin nur den Vorhersagepuffer treffen und die
            # 1280 ms Kontext stehen lassen, aus denen der Phantom-Wake kam.
            # Was bleibt, ist die ehrliche Sperre: bis der Klassifikator 16
            # frische Frames gesehen hat, redet er ueber die Vergangenheit.
            refractory = CONTEXT_FRAMES
            trial.on_session_end()
            trial.on_state("IDLE", refractory_left=refractory)
            # Waehrend der Sitzung hat `buffer_loop` gelesen, nicht diese
            # Schleife — das Fenster ist ohnehin leer. Ausdruecklich trotzdem,
            # damit ein spaeterer Umbau die Zusage nicht still verliert.
            prewake.clear()
            print("[IDLE] wieder lokal wartend.", flush=True)
            set_health(state="IDLE", core="UNKNOWN")
    finally:
        tele_task.cancel()
        beat_task.cancel()
        trial_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbeendet.")
