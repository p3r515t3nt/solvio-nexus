"""Core-side handling of research.fetch_public_url results (STEP 21.2F).

The Mac Core — never the node — assigns trust. A research.* result is ALWAYS
UNTRUSTED_WEB: valid WireGuard + mTLS + a controlled node + a valid site TLS
certificate prove only WHERE the bytes came from, never that the CONTENT is
trustworthy. Web content can inform; it can never authorize.

This module also separates a REMOTE_CONTENT_ERROR (the node answered, but the
fetch failed — SSRF block, DNS failure, unsupported content, a slow target) from a
NODE_TRANSPORT_ERROR (the node itself was unreachable). Only the latter reflects on
node health (PHASE 28).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from solvio.contracts.trust import TrustLevel, bears_authority
from solvio.nodes.errors import NodeCapabilityError, NodeUnavailableError
from solvio.nodes.models import NodeResponse
from solvio.nodes.trust_map import node_result_trust

RESEARCH_FETCH = "research.fetch_public_url"


class WebFetchResult(BaseModel):
    """A fetched public page, tagged with the Core-assigned trust level."""
    model_config = ConfigDict(extra="ignore")

    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    title: str | None = None
    text: str
    sha256: str
    bytes_read: int
    truncated: bool
    redirect_chain: list[dict[str, Any]] = Field(default_factory=list)
    resolved_peer: str | None = None
    fetched_at: str
    # Core-assigned — NOT taken from the node's payload:
    trust_level: TrustLevel
    node_id: str


class FetchOutcome(BaseModel):
    """Result OR a classified failure (never raises into the caller)."""
    ok: bool
    result: WebFetchResult | None = None
    error_kind: str | None = None   # REMOTE_CONTENT_ERROR | NODE_TRANSPORT_ERROR
    error_detail: str | None = None


def classify_web_fetch(response: NodeResponse) -> WebFetchResult:
    """Build a trust-tagged result. The Core decides trust from the capability id,
    NOT from anything the node claims."""
    trust = node_result_trust(RESEARCH_FETCH)  # -> UNTRUSTED_WEB
    if bears_authority(trust):  # invariant: a web result can never authorize
        raise AssertionError("web fetch result must never bear authority")
    payload = dict(response.result or {})
    payload.pop("trust_level", None)  # ignore any node-claimed trust
    payload.pop("node_id", None)
    return WebFetchResult(**payload, trust_level=trust,
                          node_id=response.node_id or "")


async def fetch_public_url(client, url: str) -> FetchOutcome:
    """Invoke the capability through a NodeClient and classify the outcome.

    Transport failures (node down/timeout/tls/circuit-open — all NodeUnavailableError
    subclasses) are NODE_TRANSPORT_ERROR. A node that answered with a capability
    error is REMOTE_CONTENT_ERROR and the node stays healthy.
    """
    try:
        response = await client.invoke(RESEARCH_FETCH, {"url": url})
    except NodeUnavailableError as exc:
        return FetchOutcome(ok=False, error_kind="NODE_TRANSPORT_ERROR",
                            error_detail=type(exc).__name__)
    except NodeCapabilityError as exc:
        return FetchOutcome(ok=False, error_kind="REMOTE_CONTENT_ERROR",
                            error_detail=(exc.error_code or type(exc).__name__))
    return FetchOutcome(ok=True, result=classify_web_fetch(response))
