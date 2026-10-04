"""Audioaufnahme und Wiedergabe des Satelliten ueber ALSA.

Beides laeuft ueber den stabilen Alias "solvio" (asound.conf), also unabhaengig
von der ALSA-Kartennummer. Aufgenommen wird der linke, aufbereitete Sprachkanal
des ReSpeaker XVF3800 (der rechte Kanal traegt das AEC-Referenzsignal).
"""
from __future__ import annotations

import array
import asyncio

import numpy as np

RATE = 16000
STREAM_MS = 20
STREAM_FRAMES = RATE * STREAM_MS // 1000        # 320
STREAM_STEREO_BYTES = STREAM_FRAMES * 2 * 2     # 1280 Bytes gelesen
WAKE_FRAMES = 1280                              # 80 ms, von openWakeWord erwartet
DEVICE = "solvio"


def left_channel(pcm_stereo: bytes) -> bytes:
    """Linker Kanal des XVF3800: aufbereitete Sprache."""
    a = array.array("h")
    a.frombytes(pcm_stereo)
    return a[0::2].tobytes()


def channel_power(pcm_stereo: bytes) -> tuple[float, float]:
    """Mittlere Leistung beider Kanaele — zwei Zahlen, kein Audio.

    Bewusst NEBEN `left_channel()` und nicht darin: der Sprachpfad ist
    eingefroren und bleibt Byte fuer Byte derselbe. Gemessen wird auf denselben
    Rohbytes, aber in einer eigenen Rechnung, die nichts zurueckgibt, woraus
    sich ein Wort rekonstruieren liesse.

    Zwei Kanaele statt einem ist der eigentliche Punkt: nur der Vergleich
    trennt "das Board liefert nichts mehr" (beide am Boden) von "der DSP klemmt
    den aufbereiteten Kanal" (nur links am Boden).
    """
    a = np.frombuffer(pcm_stereo, dtype="<i2")
    left = a[0::2].astype(np.float64)
    right = a[1::2].astype(np.float64)
    n = float(left.size) or 1.0
    return float(left @ left) / n, float(right @ right) / n


class Recorder:
    """Dauerhafte Aufnahme in 20-ms-Haeppchen (linker Kanal, mono 16k)."""

    def __init__(self, device: str = DEVICE, *, telemetry=None) -> None:
        self.device = device
        self.proc: asyncio.subprocess.Process | None = None
        #: Optional. Wer misst, haengt sich hier ein; der Sprachpfad merkt
        #: davon nichts und laeuft auch ohne weiter.
        self.telemetry = telemetry

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            "arecord", "-D", self.device, "-f", "S16_LE", "-r", str(RATE),
            "-c", "2", "-t", "raw", "-q",
            "--buffer-time=100000", "--period-time=20000",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

    async def read_chunk(self) -> bytes:
        """Liefert 20 ms Mono-Audio des linken Kanals (640 Bytes)."""
        assert self.proc is not None and self.proc.stdout is not None
        raw = await self.proc.stdout.readexactly(STREAM_STEREO_BYTES)
        if self.telemetry is not None:
            ms_l, ms_r = channel_power(raw)
            self.telemetry.on_chunk(ms_l, ms_r)
        return left_channel(raw)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
                await self.proc.wait()
            except ProcessLookupError:
                pass
        self.proc = None


class Player:
    """Einfache Wiedergabe von WAV-Dateien ueber ALSA 'solvio'."""

    def __init__(self, device: str = DEVICE) -> None:
        self.device = device

    async def play_file(self, path: str) -> None:
        p = await asyncio.create_subprocess_exec(
            "aplay", "-D", self.device, "-q", path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await p.wait()
