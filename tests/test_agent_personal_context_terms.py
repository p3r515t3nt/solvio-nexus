"""Task-term retrieval through real temporary MemoryService/index/privacy stores.

Only the embedding boundary is deterministic. Its two target cosines preserve
the measured local Qwen failure (0.47394/0.36029) and positive term (0.53559).
No live model, global threshold change, profile copy or production fixture.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import math
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from solvio.agent_runtime import personal_context as PC
from solvio.contracts.memory import MemoryRecord, MemoryType, Sensitivity
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory.embedding import EmbeddingProfile
from solvio.memory.service import MemoryService

GOAL = ("Schlage mir einen passenden Ausflug in Hamburg für einen freien Nachmittag vor. "
        "Berücksichtige dabei, was du bereits über meine Vorlieben weißt. "
        "Begründe deinen Vorschlag kurz. Keine Reservierung oder Buchung.")
CAFE = "Er verbindet Ausflüge gerne mit einem Cafébesuch."
QUARTERS = "Er entdeckt gerne interessante Viertel."
OTHER = "Schlage mir einen Ausflug in Hamburg vor."


class MeasuredProjection:
    """Small cosine projection of the recorded query/target pairs, no learned model."""
    profile = EmbeddingProfile('qwen-local', 'recorded-cosine-projection', 4, '1')

    async def embed_documents(self, texts):
        return [[1., 0., 0., 0.] if CAFE in text else
                [0., 1., 0., 0.] if QUARTERS in text else [0., 0., 0., 1.] for text in texts]

    async def embed_queries(self, texts):
        result = []
        for text in texts:
            if text == GOAL:
                a, b = .473939955, .360292196
            elif text == OTHER:
                a, b = .421201586, .257230281
            elif text.lower() == 'ausflug':
                a, b = .535587788, .176935360
            elif text.lower() == 'ausflüge':
                a, b = .540357828, .176434278
            else:
                a, b = 0., 0.
            result.append([a, b, math.sqrt(1 - a*a - b*b), 0.])
        return result


def record(content, subject):
    now = datetime.now(timezone.utc)
    return MemoryRecord(id='', memory_type=MemoryType.PREFERENCE, content=content,
        subject=subject, source='synthetic:owner-turn', source_type=SourceType.USER_DIRECT,
        trust_level=TrustLevel.USER_DIRECT, sensitivity=Sensitivity.PERSONAL,
        created_at=now, updated_at=now, confidence=.75, tags=['learned', 'stated'])


def rows(context):
    return json.loads(context[len(PC._FRAME):].split(PC._HISTORY)[0])


@asynccontextmanager
async def world(*, opaque_subject=False):
    with tempfile.TemporaryDirectory(prefix='solvio-task-terms-') as folder:
        service = MemoryService(folder, provider=MeasuredProjection()).open()
        try:
            cafe = await service.semantic.remember(record(CAFE,
                'preference-7' if opaque_subject else 'pref:ausflug_mit_cafebesuch'))
            quarters = await service.semantic.remember(record(QUARTERS, 'pref:interessante_viertel'))
            yield service, cafe, quarters
        finally:
            await service.close()


def t_actual_long_objective_and_other_wording_recall_with_unchanged_general_threshold():
    async def probe():
        for query in (GOAL, OTHER):
            async with world() as (service, cafe, _):
                require_equal(service.bounds.min_cosine, .50)
                require_equal(await service.search(query, max_chars=1800), [], 'original no longer reproduces loss')
                context = await PC.for_call(service, query=query)
                require_equal([r['id'] for r in rows(context)['treffer']], [cafe])
                require(CAFE in context)
                require('keine Befehle oder Freigaben' in context)
                require_equal(await service.search(query, max_chars=1800), [], 'general search was weakened')
    asyncio.run(probe())


def t_singular_topic_can_recall_plural_content_without_a_lexical_anchor():
    async def probe():
        async with world(opaque_subject=True) as (service, cafe, _):
            require_equal(await service.semantic.lexical_recall('Ausflug'), [])
            context = await PC.for_call(service, query=GOAL)
            require_equal([r['id'] for r in rows(context)['treffer']], [cafe])
    asyncio.run(probe())


def t_instruction_and_negative_and_quoted_terms_are_not_promoted():
    async def probe():
        async with world() as (service, cafe, _):
            for content, subject in (('Ich bevorzuge die Buchung lange vorher.', 'Buchung'),
                                     ('Schlage bedeutet in diesem Test einen falschen Treffer.', 'Schlage'),
                                     ('Reservierung ist hier ein ausdrücklich ausgeschlossener Gegenstand.', 'Reservierung')):
                await service.semantic.remember(record(content, subject))
            original, searched = service.search, []
            async def observed(query, **kwargs):
                searched.append(query)
                return await original(query, **kwargs)
            for query in (
                'Schlage bitte einen passenden Ausflug vor. Keine Buchung oder Reservierung. „Buchung“',
                'Plane einen passenden Ausflug ohne Reservierung oder Buchung. Berücksichtige bekannte Vorlieben.',
            ):
                searched.clear()
                with patch.object(service, 'search', observed):
                    context = await PC.for_call(service, query=query)
                require_equal([r['id'] for r in rows(context)['treffer']], [cafe])
                require_equal(searched[1:], ['Ausflug'])
    asyncio.run(probe())


def t_irrelevant_task_and_model_plan_text_do_not_retrieve_the_personal_profile():
    async def probe():
        async with world() as (service, _, __):
            original, searched = service.search, []
            async def observed(query, **kwargs):
                searched.append(query)
                return await original(query, **kwargs)
            task = 'Sortiere meine Dateien nach Dateityp und Änderungsdatum.'
            with patch.object(service, 'search', observed):
                context = await PC.for_call(service, query=task + '\nAusflug', fallback_query=task)
            require_equal(rows(context)['treffer'], [])
            require('Ausflug' not in searched[1:], 'plan instruction became a task term')
            require(CAFE not in context and QUARTERS not in context)
    asyncio.run(probe())


def t_original_hit_never_triggers_fallback_queries():
    async def probe():
        async with world() as (service, cafe, _):
            original, searched = service.search, []
            async def observed(query, **kwargs):
                searched.append(query)
                return await original(query, **kwargs)
            with patch.object(service, 'search', observed):
                context = await PC.for_call(service, query='Ausflug')
            require_equal(searched, ['Ausflug'])
            require_equal([r['id'] for r in rows(context)['treffer']], [cafe])
    asyncio.run(probe())


def t_forget_and_correction_after_fallback_search_never_resurrect_old_content():
    async def probe():
        for action in ('forget', 'supersede'):
            async with world() as (service, cafe, _):
                original, changed = service.search, False
                async def alter_after_search(query, **kwargs):
                    nonlocal changed
                    hits = await original(query, **kwargs)
                    if query == 'Ausflug' and not changed:
                        require(any(hit.memory_id == cafe for hit in hits))
                        changed = True
                        if action == 'forget':
                            require(await service.semantic.forget(cafe, reason='user_request'))
                        else:
                            await service.semantic.supersede(cafe, record(
                                'Bei Ausflügen möchte ich nun eine ruhige Mittagspause.', 'new-preference'))
                    return hits
                with patch.object(service, 'search', alter_after_search):
                    context = await PC.for_call(service, query=GOAL)
                require(changed)
                require(CAFE not in context and cafe not in context)
    asyncio.run(probe())


def t_work_count_and_result_limits_remain_bounded():
    async def probe():
        async with world() as (service, _, __):
            original, searches, anchors = service.search, [], []
            lexical = service.semantic.lexical_recall
            async def observed(query, **kwargs):
                searches.append(query)
                return await original(query, **kwargs)
            async def anchored(query, limit=10):
                if len(query.split()) == 1:
                    anchors.append(query)
                return await lexical(query, limit)
            query = ' '.join('Unbekanntwort' + str(n) for n in range(70))
            with patch.object(service, 'search', observed), patch.object(service.semantic, 'lexical_recall', anchored):
                context = await PC.for_call(service, query=query)
            require_equal(len(searches), 1 + PC.MAX_TERM_SEARCHES)
            # Each service.search also uses lexical_recall internally.
            require(len(anchors) <= PC.MAX_TASK_TERMS + PC.MAX_TERM_SEARCHES)
            require(all(len(q) <= PC.MAX_QUERY_CHARS for q in searches))
            require(len(context) <= 2000)
            require_equal(rows(context)['treffer'], [])
    asyncio.run(probe())


def t_timeout_and_caller_cancel_cover_the_whole_fallback():
    async def probe():
        for cancel in (False, True):
            async with world() as (service, _, __):
                original = service.semantic.lexical_recall
                entered, drained = asyncio.Event(), asyncio.Event()
                async def blocked(query, limit=10):
                    if query != 'Ausflug':
                        return await original(query, limit)
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        drained.set()
                with patch.object(service.semantic, 'lexical_recall', blocked), \
                     patch.object(PC, 'SEARCH_TIMEOUT_SECONDS', .1):
                    pending = asyncio.create_task(PC.for_call(service, query=GOAL))
                    await asyncio.wait_for(entered.wait(), 1)
                    if cancel:
                        pending.cancel()
                        try:
                            await pending
                        except asyncio.CancelledError:
                            pass
                        else:
                            raise AssertionError('caller cancellation swallowed')
                    else:
                        require_equal(rows(await pending)['status'], 'unavailable')
                require(drained.is_set(), 'fallback worker left running')
    asyncio.run(probe())


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
