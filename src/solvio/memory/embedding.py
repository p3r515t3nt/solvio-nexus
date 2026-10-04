"""Framework-neutrale Embedding-Provider-Abstraktion (STEP 21, PHASE 3/4).

Die Memory-Schicht weiss NICHT, ob ein Vektor von OpenAI, einem lokalen Qwen3-
Modell oder etwas anderem stammt. Ein Provider liefert ausschliesslich Vektoren
zu Texten; er erzeugt keine Fakten, veraendert keine Provenance, hebt kein Trust.

Kein LangChain / LlamaIndex / OpenClaw.

Ein EmbeddingProfile identifiziert (provider, model, dimension, version). Vektoren
verschiedener Profile duerfen NIEMALS still verglichen werden (PHASE 4): der
SemanticIndex speichert pro Vektor den profile_key; bei Profilwechsel gelten alte
Vektoren als STALE und werden neu indexiert.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import math
import os
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"[0-9a-zà-ÿ]+", re.IGNORECASE)


@dataclass(frozen=True)
class EmbeddingProfile:
    """Identitaet eines Embedding-Raums. Nur gleicher key() ist vergleichbar."""
    provider_id: str
    model_id: str
    dimension: int
    profile_version: str = "1"

    @property
    def key(self) -> str:
        return f"{self.provider_id}:{self.model_id}:d{self.dimension}:v{self.profile_version}"


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Alle Operationen async. Rueckgabe: L2-normalisierte float-Vektoren."""

    @property
    def profile(self) -> EmbeddingProfile: ...
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...
    async def health(self) -> dict: ...


# --------------------------------------------------------------------------- utils
def l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0.0:
        return vec
    return [x / n for x in vec]


# ---------------------------------------------------- deterministic (no deps/network)
class HashingEmbeddingProvider:
    """Deterministischer, dependency-/netzfreier Provider (Feature-Hashing).

    Bildet Tokens per SHA-basiertem Hashing auf Dimensionen ab (signierte
    Bag-of-Words). Gleicher Text -> gleicher Vektor. Geteilte Tokens -> hoehere
    Kosinus-Naehe. NUTZEN: die gesamte Semantic-Pipeline (Index, Hybrid, Filter,
    Purge, Rebuild, Migration, Fallback) ohne ML-Stack testen. GRENZE: rein
    lexikalisch (keine echte Synonym-/Cross-Language-Semantik) — NICHT als
    Qualitaetsmodell gedacht.
    """

    def __init__(self, dimension: int = 256, profile_version: str = "1") -> None:
        self._profile = EmbeddingProfile(
            provider_id="deterministic", model_id="hashing", dimension=dimension,
            profile_version=profile_version,
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def _embed_one(self, text: str) -> list[float]:
        import hashlib
        d = self._profile.dimension
        vec = [0.0] * d
        for tok in _TOKEN_RE.findall((text or "").lower()):
            h = hashlib.sha1(tok.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "big") % d
            sign = 1.0 if (h[4] & 1) else -1.0
            vec[idx] += sign
        return l2_normalize(vec)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    async def health(self) -> dict:
        return {"provider": self._profile.provider_id, "model": self._profile.model_id,
                "dimension": self._profile.dimension, "ok": True, "network": False}


# ------------------------------------------------------------------ OpenAI (cloud)
class OpenAIEmbeddingProvider:
    """Cloud-Provider (OpenAI Embeddings). Lazy, stdlib-only HTTP (urllib).

    Der API-Key wird aus der Umgebung gelesen (OPENAI_API_KEY o. injiziert) und
    NIE geloggt/zurueckgegeben. In STEP 21 ausschliesslich fuer SYNTHETISCHE
    Benchmark-Daten (PHASE 8) — keine echten Nutzer-Memories in die Cloud.
    """
    _DIMS = {"text-embedding-3-large": 3072, "text-embedding-3-small": 1536}

    def __init__(self, model_id: str = "text-embedding-3-large",
                 dimension: int | None = None, api_key_env: str = "OPENAI_API_KEY",
                 base_url: str = "https://api.openai.com/v1",
                 profile_version: str = "1") -> None:
        dim = dimension or self._DIMS.get(model_id, 3072)
        self._model_id = model_id
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._explicit_dim = dimension  # nur senden, wenn ausdruecklich gekuerzt
        self._profile = EmbeddingProfile(
            provider_id="openai", model_id=model_id, dimension=dim,
            profile_version=profile_version,
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def _key(self) -> str:
        key = os.environ.get(self._api_key_env, "")
        if not key:
            raise RuntimeError(f"OpenAI API key not set (env {self._api_key_env})")
        return key

    def _post(self, texts: list[str]) -> list[list[float]]:
        import json
        import urllib.request
        payload: dict = {"model": self._model_id, "input": texts}
        if self._explicit_dim is not None:
            payload["dimensions"] = self._explicit_dim
        req = urllib.request.Request(
            self._base_url + "/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._key(),
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = sorted(data["data"], key=lambda r: r["index"])
        return [l2_normalize(r["embedding"]) for r in rows]

    async def _embed(self, texts: list[str], batch: int = 256) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            out.extend(await asyncio.to_thread(self._post, chunk))
        return out

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def health(self) -> dict:
        present = bool(os.environ.get(self._api_key_env, ""))
        return {"provider": "openai", "model": self._model_id,
                "dimension": self._profile.dimension, "key_present": present,
                "network": True}


# --------------------------------------------------------------- Qwen3 (local)
class QwenLocalEmbeddingProvider:
    """Lokaler Provider fuer Qwen3-Embedding via sentence-transformers (lazy).

    Modell + ML-Stack werden ERST beim ersten Embed geladen (lazy, PHASE 11), damit
    der Voice-IDLE-Pfad keinen RAM belegt. Query-Instruktion optional (Qwen3 nutzt
    fuer Retrieval einen Query-Prompt); Dokumente ohne Instruktion.
    """

    def __init__(self, model_id: str = "Qwen/Qwen3-Embedding-0.6B",
                 dimension: int = 1024, profile_version: str = "1",
                 query_prompt_name: str | None = "query", device: str | None = None,
                 normalize: bool = True) -> None:
        self._model_id = model_id
        self._device = device
        self._normalize = normalize
        self._query_prompt_name = query_prompt_name  # offiziell: "query" fuer Retrieval
        self._model = None
        #: EIN Thread fuer das Modell. Nicht aus Ordnungsliebe, sondern weil
        #: PyTorch/MPS bei gleichzeitigen Aufrufen den Prozess mitnimmt.
        #:
        #: Gemessen am 2026-08-26, zweimal: SIGSEGV in
        #: `at::native::mps::copy_cast_kernel_mps` waehrend `.to(device)`.
        #: Vorher lief `asyncio.to_thread` — also der Standard-Pool mit
        #: MEHREREN Arbeitern. Solange nur der Gespraechspfad einbettete, kam
        #: das nie zusammen. Mit Adaptive Memory rechnet der Lern-Arbeiter im
        #: Hintergrund, waehrend der Gespraechspfad abruft: zwei Threads, ein
        #: Metal-Kontext, Absturz.
        #:
        #: Derselbe Zuschnitt wie ueberall sonst im Speicher (`store.py`,
        #: `semantic.py`, `candidates.py`): genau ein Arbeiter, serialisiert.
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="solvio-embed")
        self._profile = EmbeddingProfile(
            provider_id="qwen-local", model_id=model_id.split("/")[-1],
            dimension=dimension, profile_version=profile_version,
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy
            self._model = SentenceTransformer(self._model_id, device=self._device)
        return self._model

    def _encode(self, texts: list[str], is_query: bool) -> list[list[float]]:
        model = self._load()
        kwargs = {"normalize_embeddings": self._normalize, "convert_to_numpy": True,
                  "batch_size": 16}
        if is_query and self._query_prompt_name:
            kwargs["prompt_name"] = self._query_prompt_name  # offizielle Query-Instruktion
        vecs = model.encode(texts, **kwargs)
        return [v.astype("float32").tolist() for v in vecs]

    async def _run(self, is_query: bool, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool, lambda: self._encode(texts, is_query))

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._run(False, texts)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._run(True, texts)

    async def health(self) -> dict:
        try:
            import sentence_transformers  # noqa: F401
            avail = True
        except Exception:  # noqa: BLE001
            avail = False
        return {"provider": "qwen-local", "model": self._model_id,
                "dimension": self._profile.dimension, "loaded": self._model is not None,
                "backend_available": avail, "network": False}
