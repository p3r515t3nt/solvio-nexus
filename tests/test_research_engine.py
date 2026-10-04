"""STEP 21.2H — Core research engine (no network; the BackgroundManager is stubbed).

Covers Phase 41: store + state, egress policy, query boundary, discover, candidate
persist, dedupe, selection, fetch orchestration, partial failures, ranking (lexical +
embedder fallback), provenance, citations, evidence report, restart/reopen, no memory
write, no authority escalation.
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

import os
import shutil
import tempfile
import unittest
import uuid
from dataclasses import dataclass
from unittest import IsolatedAsyncioTestCase

from solvio.background.models import LocalJobStatus
from solvio.research.errors import ExternalEgressDenied
from solvio.research.manager import FETCH_CAP, SEARCH_CAP, ResearchManager
from solvio.research.models import (
    EvidenceReport,
    ExternalEgressPolicy,
    ResearchRunSpec,
    ResearchState,
    SourceRecord,
)
from solvio.research.planner import egress_allowed, plan_public_query
from solvio.research.provenance import canonical_domain, make_source_id, select_candidates
from solvio.research.ranking import lexical_relevance, rank_sources
from solvio.research.store import ResearchStore

REMOTE = "REMOTE_CONTROLLED_ALLOWED"
QUESTION = "which SECRET private insurance fits Gregor"     # must never leave the Mac
PUBLIC_Q = "official python asyncio task groups documentation"


@dataclass
class _Spec:
    local_job_id: str
    status: LocalJobStatus
    error_detail: str | None = None


class StubBg:
    """Simulates the durable BackgroundManager: submit -> immediate terminal + result."""

    def __init__(self, *, search_candidates=None, fetch_fail_urls=None,
                 search_fail=False):
        self.search_candidates = search_candidates or []
        self.fetch_fail_urls = set(fetch_fail_urls or [])
        self.search_fail = search_fail
        self.submits = []
        self._jobs = {}

    async def submit(self, cap, payload, *, data_class, max_attempts=3):
        lid = uuid.uuid4().hex
        self.submits.append({"cap": cap, "payload": payload, "data_class": data_class})
        self._jobs[lid] = (cap, payload)
        return lid

    async def refresh(self, lid):
        cap, payload = self._jobs[lid]
        if cap == SEARCH_CAP:
            status = LocalJobStatus.FAILED if self.search_fail else LocalJobStatus.SUCCEEDED
        else:
            fail = payload.get("url") in self.fetch_fail_urls
            status = LocalJobStatus.FAILED if fail else LocalJobStatus.SUCCEEDED
        return _Spec(lid, status, "boom" if status == LocalJobStatus.FAILED else None)

    async def result(self, lid):
        cap, payload = self._jobs[lid]
        if cap == SEARCH_CAP:
            return {"result": {"query_digest": "d", "provider": "fake",
                               "candidates": self.search_candidates}}
        url = payload["url"]
        return {"result": {"final_url": url, "text": f"Body about python asyncio {url}",
                           "sha256": "hash-" + url[-4:], "title": f"Title {url}",
                           "content_type": "text/html"}}

    async def reconcile(self):
        return {"refreshed": 0}


def _candidates():
    return [
        {"url": "https://docs.python.org/3/library/asyncio-task.html", "title": "asyncio tasks",
         "snippet": "task groups", "provider": "fake", "provider_rank": 0},
        {"url": "https://docs.python.org/3/whatsnew/3.11.html", "title": "3.11", "provider": "fake",
         "provider_rank": 1},
        {"url": "https://realpython.com/async-io-python/", "title": "async io", "provider": "fake",
         "provider_rank": 2},
    ]


def make_manager(store, bg, **kw):
    return ResearchManager(store, bg, poll_interval=0.001, **kw)


class TestEgressPolicy(unittest.TestCase):
    def test_matrix(self):
        P = ExternalEgressPolicy
        self.assertFalse(egress_allowed(REMOTE, P.NO_EXTERNAL_EGRESS))
        self.assertFalse(egress_allowed("HOME_ONLY", P.PUBLIC_QUERY_ALLOWED))
        self.assertFalse(egress_allowed("LAN_ALLOWED", P.PUBLIC_QUERY_ALLOWED))
        self.assertTrue(egress_allowed(REMOTE, P.PUBLIC_QUERY_ALLOWED))
        self.assertTrue(egress_allowed(REMOTE, P.EXPLICIT_THIRD_PARTY_ALLOWED))

    def test_plan_denies(self):
        with self.assertRaises(ExternalEgressDenied):
            plan_public_query(PUBLIC_Q, REMOTE, ExternalEgressPolicy.NO_EXTERNAL_EGRESS)
        with self.assertRaises(ExternalEgressDenied):
            plan_public_query(PUBLIC_Q, "HOME_ONLY", ExternalEgressPolicy.PUBLIC_QUERY_ALLOWED)
        self.assertEqual(plan_public_query(PUBLIC_Q, REMOTE,
                         ExternalEgressPolicy.PUBLIC_QUERY_ALLOWED), PUBLIC_Q)


class TestProvenanceRanking(unittest.TestCase):
    def test_domain_and_id(self):
        self.assertEqual(canonical_domain("https://www.Example.com/x"), "example.com")
        a = make_source_id("run1", "https://x/1")
        self.assertEqual(a, make_source_id("run1", "https://x/1"))
        self.assertNotEqual(a, make_source_id("run2", "https://x/1"))

    def test_selection_dedup_diversity_cap(self):
        sel = select_candidates(_candidates(), max_sources=2)
        self.assertEqual(len(sel), 2)
        # diversity: two different domains preferred
        self.assertEqual({canonical_domain(c["url"]) for c in sel},
                         {"docs.python.org", "realpython.com"})

    def test_lexical_and_rank(self):
        self.assertGreater(lexical_relevance("python asyncio", "python asyncio tasks"), 0)
        self.assertEqual(lexical_relevance("", "x"), 0.0)
        srcs = [SourceRecord(source_id="a", research_run_id="r", original_search_url="u1",
                             provider="p", provider_rank=5, title="python asyncio",
                             text_excerpt="python asyncio task groups", fetch_status="OK"),
                SourceRecord(source_id="b", research_run_id="r", original_search_url="u2",
                             provider="p", provider_rank=0, title="unrelated",
                             text_excerpt="cooking recipes", fetch_status="ERROR")]
        ranked = rank_sources(srcs, "python asyncio", embedder=None)
        self.assertEqual(ranked[0].source_id, "a")   # relevant+fetched outranks

    def test_embedder_fallback(self):
        srcs = [SourceRecord(source_id="a", research_run_id="r", original_search_url="u",
                             provider="p", provider_rank=0, fetch_status="OK")]
        def bad_embedder(q, texts):
            raise RuntimeError("qwen down")
        ranked = rank_sources(srcs, "q", embedder=bad_embedder)   # must not raise
        self.assertIsNotNone(ranked[0].rank_score)


class TestStore(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "research_runs.sqlite3")
        self.store = ResearchStore(self.path)
        await self.store.open()

    async def asyncTearDown(self):
        await self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_permissions(self):
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    async def test_run_and_reopen(self):
        spec = ResearchRunSpec(research_run_id="R1", question=QUESTION, public_query=PUBLIC_Q,
                               privacy_class=REMOTE, external_egress_policy=ExternalEgressPolicy.PUBLIC_QUERY_ALLOWED,
                               status=ResearchState.CREATED, created_at=1.0, updated_at=1.0)
        await self.store.create_run(spec)
        await self.store.set_status("R1", ResearchState.READY)
        await self.store.close()
        s2 = ResearchStore(self.path)
        await s2.open()
        got = await s2.get_run("R1")
        self.assertEqual(got.status, ResearchState.READY)
        self.assertEqual(got.question, QUESTION)
        await s2.close()
        self.store = ResearchStore(self.path)
        await self.store.open()


class TestPipeline(IsolatedAsyncioTestCase):
    async def _mgr(self, bg):
        self.dir = tempfile.mkdtemp()
        store = ResearchStore(os.path.join(self.dir, "r.sqlite3"))
        await store.open()
        self.addAsyncCleanup(self._cleanup, store)
        return make_manager(store, bg), store

    async def _cleanup(self, store):
        await store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _create(self, mgr, *, policy=ExternalEgressPolicy.PUBLIC_QUERY_ALLOWED,
                      privacy=REMOTE):
        return await mgr.create_run(QUESTION, PUBLIC_Q, privacy_class=privacy,
                                    egress_policy=policy, max_sources=3, max_fetches=3)

    async def test_full_pipeline_ready(self):
        bg = StubBg(search_candidates=_candidates())
        mgr, store = await self._mgr(bg)
        run_id = await self._create(mgr)
        report = await mgr.run(run_id)
        self.assertEqual(report.status, ResearchState.READY)
        self.assertGreaterEqual(report.sources_fetched, 2)
        self.assertTrue(report.citations)
        self.assertIn("UNTRUSTED_WEB", report.trust_note)
        # query boundary: only public_query went to the provider, never the question
        search_submit = next(s for s in bg.submits if s["cap"] == SEARCH_CAP)
        self.assertEqual(search_submit["payload"]["query"], PUBLIC_Q)
        self.assertNotIn("SECRET", str(bg.submits))
        # persisted report retrievable
        self.assertIsNotNone(await store.get_report(run_id))

    async def test_partial_on_fetch_failure(self):
        cands = _candidates()
        bg = StubBg(search_candidates=cands, fetch_fail_urls=[cands[2]["url"]])
        mgr, _ = await self._mgr(bg)
        run_id = await self._create(mgr)
        report = await mgr.run(run_id)
        self.assertEqual(report.status, ResearchState.PARTIAL)
        self.assertTrue(report.errors)
        self.assertGreaterEqual(report.sources_fetched, 1)

    async def test_egress_denied_no_provider_call(self):
        bg = StubBg(search_candidates=_candidates())
        mgr, _ = await self._mgr(bg)
        # NO_EXTERNAL_EGRESS -> FAILED, no submit at all
        run_id = await self._create(mgr, policy=ExternalEgressPolicy.NO_EXTERNAL_EGRESS)
        with self.assertRaises(ExternalEgressDenied):
            await mgr.run(run_id)
        self.assertEqual(bg.submits, [])
        self.assertEqual((await mgr.get_run(run_id)).status, ResearchState.FAILED)

    async def test_home_only_never_searched(self):
        bg = StubBg(search_candidates=_candidates())
        mgr, _ = await self._mgr(bg)
        run_id = await self._create(mgr, privacy="HOME_ONLY")
        with self.assertRaises(ExternalEgressDenied):
            await mgr.run(run_id)
        self.assertEqual(bg.submits, [])

    async def test_ready_when_discovered_exceeds_fetches(self):
        # A discovered candidate never SELECTED for fetch stays PENDING; it must NOT
        # count as an error nor force PARTIAL when all selected sources fetched OK (21.2H).
        bg = StubBg(search_candidates=_candidates())      # 3 candidates
        mgr, _ = await self._mgr(bg)
        run_id = await mgr.create_run(QUESTION, PUBLIC_Q, privacy_class=REMOTE,
                                      egress_policy=ExternalEgressPolicy.PUBLIC_QUERY_ALLOWED,
                                      max_sources=2, max_fetches=2)
        report = await mgr.run(run_id)
        self.assertEqual(report.status, ResearchState.READY)
        self.assertEqual(report.errors, [])
        self.assertGreater(report.sources_discovered, report.sources_fetched)

    async def test_restart_retains_run_and_report(self):
        bg = StubBg(search_candidates=_candidates())
        mgr, store = await self._mgr(bg)
        run_id = await self._create(mgr)
        await mgr.run(run_id)
        await store.close()
        store2 = ResearchStore(store.path)      # reopen (Core restart)
        await store2.open()
        mgr2 = make_manager(store2, StubBg())
        self.assertEqual((await mgr2.get_run(run_id)).status, ResearchState.READY)
        report = await mgr2.get_report(run_id)
        self.assertIsInstance(report, EvidenceReport)
        self.assertTrue(report.citations)
        await store2.close()


class TestNoMemoryOrAuthority(unittest.TestCase):
    def test_research_does_not_import_memory_or_dispatcher(self):
        import solvio.research.manager as m
        import inspect
        src = inspect.getsource(m)
        self.assertNotIn("solvio.memory", src)
        self.assertNotIn("semantic_index", src)
        self.assertNotIn("tools.dispatcher", src)
        self.assertNotIn("realtime", src)


if __name__ == "__main__":
    from _harness import run_unittest
    raise SystemExit(run_unittest(globals(), __name__))
