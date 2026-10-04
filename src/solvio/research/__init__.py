"""Core research engine (STEP 21.2H) — DISCOVER -> SELECT -> FETCH -> RANK -> EVIDENCE.

MAC OWNS RESEARCH. NODE EXECUTES. SEARCH PROVIDER IS AN EXTERNAL THIRD PARTY. No LLM
synthesis in V1: the output is a deterministic evidence report with provenance and
citations. The `question` never leaves the Mac; only a `public_query` may go out, and
only past the fail-closed external-egress gate. Search + fetched content are
UNTRUSTED_WEB; nothing is written to canonical/semantic memory; there is no scheduler,
no auto-poller, and no Voice/Realtime integration.
"""
from solvio.research.errors import (
    ExternalEgressDenied,
    ResearchError,
    ResearchRunNotFound,
    ResearchStoreCorrupt,
    SearchProviderError,
)
from solvio.research.manager import ResearchManager
from solvio.research.models import (
    RESEARCH_TERMINAL,
    Citation,
    EvidenceReport,
    ExternalEgressPolicy,
    ResearchRunSpec,
    ResearchState,
    SourceRecord,
)
from solvio.research.planner import egress_allowed, plan_public_query
from solvio.research.provenance import (
    canonical_domain,
    make_source_id,
    select_candidates,
)
from solvio.research.ranking import lexical_relevance, rank_sources
from solvio.research.report import build_report
from solvio.research.store import ResearchStore

__all__ = [
    "ResearchError", "ResearchRunNotFound", "ExternalEgressDenied",
    "SearchProviderError", "ResearchStoreCorrupt",
    "ResearchManager", "ResearchStore",
    "ResearchRunSpec", "ResearchState", "RESEARCH_TERMINAL", "ExternalEgressPolicy",
    "SourceRecord", "Citation", "EvidenceReport",
    "egress_allowed", "plan_public_query",
    "canonical_domain", "make_source_id", "select_candidates",
    "lexical_relevance", "rank_sources", "build_report",
]
