"""SemanticMemory — abgeleitete semantische Retrieval-Schicht (STEP 21).

Komponiert:
    SolvioMemory       kanonische Wahrheit + Privacy-Autoritaet (STEP 20)
    SemanticIndex      derived/rebuildable Vektoren (semantic_index.sqlite3)
    EmbeddingProvider  liefert Vektoren (deterministic / OpenAI / Qwen-local)

Unverrueckbare Regeln (STEP 21):
  * Der Index ist NIEMALS Authority. Vor jedem Return wird gegen die kanonische
    Schicht auf aktuelle Wahrheit geprueft (nicht purged/forgotten/superseded,
    zeitlich gueltig) — fail-closed (PHASE 16).
  * Retrieval erzeugt keine Fakten; es liefert nur existierende MemoryRecords.
  * Faellt der Provider oder der Index aus, funktioniert Memory weiter; Retrieval
    faellt auf FTS/lexical zurueck (PHASE 20).
  * Der Index ist derived: jederzeit loeschbar und via rebuild() aus dem
    kanonischen Store reproduzierbar (PHASE 21).

KEINE Runtime-Integration in STEP 21 (Voice/Realtime binden das nicht ein).
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from solvio.contracts.memory import MemoryRecord, MemoryType
from solvio.memory.embedding import EmbeddingProvider
from solvio.memory.embedding_text import record_embedding_text_and_hash
from solvio.memory.fusion import HybridConfig, fuse, is_no_match
from solvio.memory.semantic_index import SemanticIndex
from solvio.memory.store import SolvioMemory

_T = TypeVar("_T")
SEMANTIC_DB = "semantic_index.sqlite3"
_RRF_K = 60


class SemanticMemory:
    def __init__(self, base_dir: str, provider: EmbeddingProvider, *,
                 memory: SolvioMemory | None = None) -> None:
        self.memory = memory or SolvioMemory(base_dir)
        self.base_dir = self.memory.base_dir
        self.provider = provider
        self.profile = provider.profile
        self.index_path = os.path.join(self.base_dir, SEMANTIC_DB)
        self._ipool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solvio-sem")
        self.index: SemanticIndex | None = None
        self.semantic_available = False
        self._open_index()
        # STEP 21.1: eingefrorene, auf DEV kalibrierte Hybrid-Strategie.
        self.hybrid_config = HybridConfig()
        self._closed = False

    def _open_index(self) -> None:
        try:
            self.index = SemanticIndex(self.index_path)
            self.index.register_profile(self.profile)
            if self.index.active_profile() is None:
                self.index.set_active_profile(self.profile.key)
            self.semantic_available = True
        except Exception:  # noqa: BLE001 - korrupter/nicht lesbarer Index -> FTS-Fallback
            self.index = None
            self.semantic_available = False

    async def _irun(self, fn: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._ipool, lambda: fn(*args))

    # ================================================================= writes
    async def _index_one(self, record: MemoryRecord) -> bool:
        """Best-effort Embedding eines Records. Provider-/Index-Fehler brechen den
        kanonischen Schreibvorgang NICHT ab (Index ist derived)."""
        if not self.semantic_available or self.index is None:
            return False
        txt, h = record_embedding_text_and_hash(record)
        try:
            cur = await self._irun(self.index.content_hash_of, record.id, self.profile.key)
            if cur == h:
                return True  # bereits aktuell -> kein Re-Embedding (PHASE 18)
            vec = (await self.provider.embed_documents([txt]))[0]
            await self._irun(self.index.upsert, record.id, self.profile.key, h, vec)
            return True
        except Exception:  # noqa: BLE001
            return False  # kanonisch bereits gespeichert; Index via rebuild nachziehbar

    async def remember(self, record: MemoryRecord) -> str:
        rid = await self.memory.remember(record)
        stored = await self.memory.get(rid)
        if stored is not None:
            await self._index_one(stored)
        return rid

    async def update(self, id: str, changes: dict[str, Any]) -> MemoryRecord:
        rec = await self.memory.update(id, changes)
        await self._index_one(rec)  # content_hash-Vergleich entscheidet ueber Re-Embed
        return rec

    async def supersede(self, old_id: str, new_record: MemoryRecord) -> MemoryRecord:
        rec = await self.memory.supersede(old_id, new_record)
        if self.index is not None:
            await self._irun(self.index.delete, old_id, self.profile.key)  # alt -> nicht aktiv
        await self._index_one(rec)
        return rec

    async def forget(self, id: str, *, reason: str) -> bool:
        ok = await self.memory.forget(id, reason=reason)
        if self.index is not None:
            await self._irun(self.index.delete, id, None)  # aus allen Profilen
        return ok

    async def purge(self, id: str, *, reason: str) -> bool:
        # kanonisch tombstone-first + hard delete; danach Vektor entfernen (PHASE 17).
        ok = await self.memory.purge(id, reason=reason)
        if self.index is not None:
            await self._irun(self.index.delete, id, None)
        return ok

    # ================================================================ retrieval
    async def lexical_recall(self, query: str, limit: int = 10) -> list[MemoryRecord]:
        """FTS/lexical ueber die kanonische Schicht (bleibt immer verfuegbar)."""
        return await self.memory.search(query, include_superseded=False, limit=limit)

    async def fts_candidate_ids(self, query: str, k: int = 50) -> list[str]:
        return [r.id for r in await self.lexical_recall(query, k)]

    async def _embed_query(self, query: str) -> list[float] | None:
        """Genau EIN Query-Embedding pro Retrieval-Vorgang (PHASE 9 Dedup)."""
        if not self.semantic_available or self.index is None:
            return None
        try:
            return (await self.provider.embed_queries([query]))[0]
        except Exception:  # noqa: BLE001 - Provider aus
            return None

    async def semantic_hits(self, query: str, k: int = 50,
                            qvec: list[float] | None = None) -> list[tuple[str, float]]:
        """Semantische Kandidaten [(id, cosine)] im aktiven Profil (qvec wiederverwendbar)."""
        if qvec is None:
            qvec = await self._embed_query(query)
        if qvec is None or self.index is None:
            return []
        try:
            return await self._irun(self.index.search, qvec, self.profile.key, k)
        except Exception:  # noqa: BLE001
            return []

    async def _visible_map(self, ids: list[str]) -> dict[str, MemoryRecord]:
        out: dict[str, MemoryRecord] = {}
        for rid in ids:
            if rid in out:
                continue
            rec = await self.memory.get_visible(rid)  # fail-closed Active-Truth
            if rec is not None:
                out[rid] = rec
        return out

    async def semantic_recall(self, query: str, limit: int = 10, *,
                              candidate_k: int = 50,
                              config: HybridConfig | None = None) -> list[MemoryRecord]:
        cfg = config or self.hybrid_config
        hits = await self.semantic_hits(query, candidate_k)
        if is_no_match(hits, cfg):
            return []
        out: list[MemoryRecord] = []
        for cid, _ in hits:
            rec = await self.memory.get_visible(cid)
            if rec is not None:
                out.append(rec)
            if len(out) >= limit:
                break
        return out

    async def hybrid_recall(self, query: str, limit: int = 10, *,
                            fts_k: int = 50, sem_k: int = 50,
                            config: HybridConfig | None = None,
                            memory_types: list[MemoryType] | None = None) -> list[MemoryRecord]:
        """FTS- + semantische Kandidaten -> deterministische Fusion (Strategie aus
        config) -> fail-closed Active-Truth-Filter. Query-Embedding genau einmal.
        Kein LLM-Reranker; Semantic ersetzt FTS nicht."""
        cfg = config or self.hybrid_config
        qvec = await self._embed_query(query)
        sem = await self.semantic_hits(query, sem_k, qvec=qvec)
        fts = await self.fts_candidate_ids(query, fts_k)
        recmap = await self._visible_map(fts + [i for i, _ in sem])
        ctext = {rid: (r.subject + " " + r.content) for rid, r in recmap.items()}
        # Semantic-Arm leer/aus (Provider/Index down) -> deterministischer FTS-Fallback.
        fused = fuse(fts, sem, cfg, candidate_text=ctext, query=query) if sem else list(fts)
        types = {t.value for t in memory_types} if memory_types else None
        out: list[MemoryRecord] = []
        for rid in fused:
            rec = recmap.get(rid)
            if rec is None:
                continue
            if types is not None and rec.memory_type.value not in types:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    # ============================================================ maintenance
    async def reconcile(self) -> dict:
        """Stale Vektoren entfernen: alles im Index, dessen id nicht (mehr) aktive
        kanonische Wahrheit ist (purged/forgotten/superseded/absent)."""
        if self.index is None:
            return {"removed_stale": 0, "available": False}
        active = await self.memory.active_ids()
        idx_ids = await self._irun(self.index.ids_for_profile, self.profile.key)
        stale = idx_ids - active
        removed = await self._irun(self.index.delete_ids, stale, self.profile.key)
        return {"removed_stale": removed, "active": len(active), "indexed": len(idx_ids)}

    async def rebuild(self, *, batch: int = 64, reconcile: bool = True) -> dict:
        """Index aus kanonischem Memory (neu) aufbauen. Nur aktive, zulaessige
        Records. Resumable: unveraenderte content_hashes werden uebersprungen."""
        if self.index is None:
            self._open_index()
        if self.index is None:
            return {"indexed": 0, "available": False}
        await self._irun(self.index.register_profile, self.profile)
        if reconcile:
            await self.reconcile()
        recs = await self.memory.active_records()
        pending: list[tuple[str, str, str]] = []
        for rec in recs:
            txt, h = record_embedding_text_and_hash(rec)
            cur = await self._irun(self.index.content_hash_of, rec.id, self.profile.key)
            if cur != h:
                pending.append((rec.id, txt, h))
        for i in range(0, len(pending), batch):
            chunk = pending[i:i + batch]
            vecs = await self.provider.embed_documents([t for _, t, _ in chunk])
            for (rid, _txt, h), v in zip(chunk, vecs):
                await self._irun(self.index.upsert, rid, self.profile.key, h, v)
        await self._irun(self.index.set_active_profile, self.profile.key)
        return {"indexed": len(pending), "active": len(recs),
                "total_in_index": await self._irun(self.index.count, self.profile.key)}

    async def migrate_to(self, new_provider: EmbeddingProvider, *, batch: int = 64) -> dict:
        """Auf ein neues Provider-/Modell-Profil migrieren (PHASE 19).

        Baut die Vektoren des NEUEN Profils auf, waehrend das ALTE aktiv bleibt
        (Queries/FTS laufen weiter). Erst nach vollstaendigem Rebuild wird das neue
        Profil atomar aktiviert; das alte Profil kann danach geloescht werden.
        """
        if self.index is None:
            self._open_index()
        new_profile = new_provider.profile
        old_key = self.profile.key
        await self._irun(self.index.register_profile, new_profile)
        recs = await self.memory.active_records()
        pending = []
        for rec in recs:
            txt, h = record_embedding_text_and_hash(rec)
            cur = await self._irun(self.index.content_hash_of, rec.id, new_profile.key)
            if cur != h:
                pending.append((rec.id, txt, h))
        for i in range(0, len(pending), batch):
            chunk = pending[i:i + batch]
            vecs = await new_provider.embed_documents([t for _, t, _ in chunk])
            for (rid, _txt, h), v in zip(chunk, vecs):
                await self._irun(self.index.upsert, rid, new_profile.key, h, v)
        # atomar aktivieren
        await self._irun(self.index.set_active_profile, new_profile.key)
        self.provider, self.profile = new_provider, new_profile
        return {"migrated_to": new_profile.key, "from": old_key, "indexed": len(pending)}

    async def drop_old_profile(self, profile_key: str) -> int:
        if self.index is None or profile_key == self.profile.key:
            return 0
        ids = await self._irun(self.index.ids_for_profile, profile_key)
        return await self._irun(self.index.delete_ids, ids, profile_key)

    # ============================================================ backup/restore
    async def backup(self, dest_path: str | None = None) -> dict:
        # Kanonisches Backup wie STEP 20 (Semantic Index ist derived, nicht Teil davon).
        return await self.memory.backup(dest_path)

    async def restore(self, backup_path: str, *, rebuild: bool = False) -> dict:
        """Kanonisch restaurieren (STEP 20), dann Index reconciliieren. Ein
        Pre-Purge-Vektor kann keinen gepurgten Fakt reaktivieren: Active-Truth-Filter
        + reconcile verwerfen ihn (PHASE 22)."""
        stats = await self.memory.restore(backup_path)
        rec = await self.reconcile()
        stats["semantic"] = rec
        if rebuild:
            stats["rebuild"] = await self.rebuild()
        return stats

    # ================================================================ passthrough
    async def get(self, id: str) -> MemoryRecord | None:
        return await self.memory.get(id)

    async def recall(self, query: str, **kw) -> list[MemoryRecord]:
        return await self.memory.recall(query, **kw)

    async def search(self, query: str, **kw) -> list[MemoryRecord]:
        return await self.memory.search(query, **kw)

    async def history(self, subject: str) -> list[MemoryRecord]:
        return await self.memory.history(subject)

    async def list_tombstones(self):
        return await self.memory.list_tombstones()

    async def semantic_count(self) -> int:
        if self.index is None:
            return 0
        return await self._irun(self.index.count, self.profile.key)

    async def health(self) -> dict:
        prov = await self.provider.health()
        return {"provider": prov, "semantic_available": self.semantic_available,
                "profile": self.profile.key, "indexed": await self.semantic_count()}

    async def close(self) -> None:
        if self._closed:
            return
        if self.index is not None:
            await self._irun(self.index.close)
        self._ipool.shutdown(wait=True)
        await self.memory.close()
        self._closed = True
