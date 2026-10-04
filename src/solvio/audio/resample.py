"""Abtastraten-Umrechnung ohne Fremdbibliotheken.

Zwischen dem Satelliten (16 kHz, fest durch den ReSpeaker vorgegeben) und der
Realtime-Schnittstelle (24 kHz, fest durch OpenAI vorgegeben) muss in beide
Richtungen umgerechnet werden. Das Verhaeltnis ist 2:3 und damit gutartig.

Fuer diesen Prototyp genuegt lineare Interpolation. Sie ist nicht die
hochwertigste Methode, aber sie ist schnell, ohne Abhaengigkeiten umsetzbar,
gut nachvollziehbar und fuer Sprache in diesem Ratenbereich ausreichend.
Der Umrechner ist zustandsbehaftet, damit es an den Chunk-Grenzen nicht
knackt.
"""

from __future__ import annotations

import array


class Resampler:
    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.step = src_rate / dst_rate
        self._prev = 0
        self._pos = 0.0

    def reset(self) -> None:
        self._prev = 0
        self._pos = 0.0

    def process(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        src = array.array("h")
        src.frombytes(pcm)
        n = len(src)
        if n == 0:
            return b""

        # Vorheriges letztes Sample voranstellen, damit ueber die Chunk-Grenze
        # hinweg interpoliert werden kann.
        work = array.array("h", [self._prev])
        work.extend(src)

        out = array.array("h")
        pos = self._pos
        step = self.step
        while pos < n:
            i = int(pos)
            frac = pos - i
            a = work[i]
            b = work[i + 1]
            out.append(int(a + (b - a) * frac))
            pos += step

        self._pos = pos - n
        self._prev = src[-1]
        return out.tobytes()


def left_channel(pcm_stereo: bytes) -> bytes:
    """Nimmt den linken Kanal aus verschraenktem Stereo.

    Der ReSpeaker XVF3800 liefert auf dem linken Kanal das aufbereitete
    Sprachsignal und auf dem rechten das Referenzsignal seiner
    Echounterdrueckung. Fuer die Spracherkennung ist ausschliesslich der
    linke Kanal brauchbar.
    """
    a = array.array("h")
    a.frombytes(pcm_stereo)
    return a[0::2].tobytes()
