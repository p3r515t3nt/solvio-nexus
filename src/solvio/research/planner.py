"""Query planning + external-egress gate (STEP 21.2H, PHASE 3/4/5).

The `question` (which may reflect private context) NEVER leaves the Mac. Only a
`public_query` may go to an external search provider, and only when BOTH the
placement privacy is REMOTE_CONTROLLED_ALLOWED AND the external-egress policy allows
it. Default is NO_EXTERNAL_EGRESS (fail-closed).

V1 the caller supplies the public_query. The future path
(PRIVATE CONTEXT -> MAC LOCAL QUERY PLANNING -> SANITIZED PUBLIC QUERY) is documented
in SOLVIO_RESEARCH_ENGINE.md and NOT implemented here.
"""
from __future__ import annotations

from solvio.research.errors import ExternalEgressDenied
from solvio.research.models import ExternalEgressPolicy

ALLOWED_PRIVACY_FOR_EXTERNAL = "REMOTE_CONTROLLED_ALLOWED"


def egress_allowed(privacy_class: str, policy: ExternalEgressPolicy) -> bool:
    if policy == ExternalEgressPolicy.NO_EXTERNAL_EGRESS:
        return False
    if privacy_class != ALLOWED_PRIVACY_FOR_EXTERNAL:
        return False
    return policy in (ExternalEgressPolicy.PUBLIC_QUERY_ALLOWED,
                      ExternalEgressPolicy.EXPLICIT_THIRD_PARTY_ALLOWED)


def plan_public_query(public_query: str, privacy_class: str,
                      policy: ExternalEgressPolicy) -> str:
    """Return the public query if egress is allowed, else raise (fail-closed)."""
    if not egress_allowed(privacy_class, policy):
        raise ExternalEgressDenied(
            f"external egress denied (policy={policy.value}, privacy={privacy_class})")
    if not public_query.strip():
        raise ExternalEgressDenied("empty public query")
    return public_query.strip()
