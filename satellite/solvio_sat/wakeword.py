"""Wake-Word-Erkennung: duenne Huelle um openWakeWord.

WakeWordDetector.feed() nimmt 20-ms-Mono-Haeppchen (640 Bytes) entgegen und
liefert einen Score, sobald ein volles 80-ms-Frame (1280 Samples) zusammen ist.

Ueber `reset()` muss man Bescheid wissen, sonst rechnet man mit einem sauberen
Detektor, den es nicht gibt: `Model.reset()` in openWakeWord 0.4.0 ersetzt
AUSSCHLIESSLICH den `prediction_buffer` (model.py:152-154). Der eigentliche
Streaming-Zustand liegt in `AudioFeatures`, und das hat gar kein `reset()` —
`raw_data_buffer` (10 s rohes PCM), `melspectrogram_buffer` und `feature_buffer`
(bis zu 120 Embeddings, rund 9,6 s) ueberdauern es unveraendert (utils.py:67-72).

Der Klassifikator sieht immer die letzten 16 Embeddings, also 1280 ms
(utils.py:328-334). Wer `reset()` aufruft und annimmt, danach hoere der
Detektor unvoreingenommen zu, irrt sich um genau diese 1280 ms: darin steht
weiterhin, was vorher gesagt wurde. Genau daraus entstand der Phantom-Wake nach
jedem Sessionende. Die Loesung liegt nicht hier, sondern in der Aufrufseite:
den Detektor LUECKENLOS fuettern, dann gibt es keinen Sprung, den man
zuruecksetzen muesste.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

import numpy as np
from openwakeword.model import Model

RATE = 16000
FRAME = 1280                     # 80 ms, von openWakeWord erwartet
FRAME_BYTES = FRAME * 2
MODELS_DIR = os.environ.get("SOLVIO_WAKEWORD_MODELS", str(Path(__file__).resolve().parents[1] / "models"))

#: Wie viele 80-ms-Frames der Klassifikator sieht (openwakeword utils.py:328).
#: Solange nicht so viele frische Frames durchgelaufen sind, ist ein Score eine
#: Aussage ueber die Vergangenheit.
CONTEXT_FRAMES = 16


def find_model(name: str) -> str:
    """Findet ein Wake-Word-Modell, bevorzugt im eigenen models/-Verzeichnis."""
    for cand in (os.path.join(MODELS_DIR, name),
                 os.path.join(MODELS_DIR, name + ".onnx")):
        if os.path.exists(cand):
            return cand
    import openwakeword
    res = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
    for cand in (res / f"{name}.onnx", res / name):
        if cand.exists():
            return str(cand)
    hits = glob.glob(os.path.join(MODELS_DIR, f"*{name}*.onnx"))
    if hits:
        return hits[0]
    raise FileNotFoundError(f"Modell nicht gefunden: {name}")


class WakeWordDetector:
    def __init__(self, model_path: str, threshold: float = 0.5, **kwargs) -> None:
        self.model_path = model_path
        self.kwargs = kwargs
        self.model = Model(wakeword_model_paths=[model_path], **kwargs)
        self.name = Path(model_path).stem
        self.threshold = threshold
        self._buf = bytearray()

    def reset(self) -> None:
        """Setzt den Vorhersagepuffer zurueck — NICHT den Streaming-Zustand.

        Siehe Modulkopf: das ist eine Eigenschaft von openWakeWord, keine
        Nachlaessigkeit hier. Der Name bleibt, damit er nicht mehr verspricht,
        als er haelt.
        """
        self.model.reset()
        self._buf.clear()

    def reinitialize(self) -> None:
        """Baut den Detektor vollstaendig neu auf — ONNX-Sitzung und Merkmale.

        Das ist der einzige Weg zu einem wirklich unvoreingenommenen Detektor,
        und er ist teuer (mehrere Sekunden auf dem Pi). Deshalb ist er kein
        Bestandteil des normalen Ablaufs, sondern eine Reparatur, die nur auf
        BELEGTEN Befund hin ausgeloest wird und die sich im Journal zeigt.
        """
        self.model = Model(wakeword_model_paths=[self.model_path], **self.kwargs)
        self._buf.clear()

    def feed(self, pcm_mono: bytes) -> float | None:
        """Score, sobald ein 80-ms-Frame voll ist, sonst None."""
        self._buf += pcm_mono
        score = None
        while len(self._buf) >= FRAME_BYTES:
            frame = np.frombuffer(bytes(self._buf[:FRAME_BYTES]), dtype=np.int16)
            del self._buf[:FRAME_BYTES]
            out = self.model.predict(frame)
            if self.name in out:
                score = float(out[self.name])
            elif out:
                score = float(max(out.values()))
        return score
