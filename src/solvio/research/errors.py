"""Errors for the Core research engine (STEP 21.2H)."""
from __future__ import annotations


class ResearchError(Exception):
    """Base for research-engine errors."""


class ResearchRunNotFound(ResearchError):
    pass


class ExternalEgressDenied(ResearchError):
    """The egress policy / privacy class forbids sending any query to an external
    provider. Fail-closed: no provider is ever called."""


class SearchProviderError(ResearchError):
    """The external search provider failed (e.g. rate limited). Distinct from a node
    transport error and from a fetch-content error."""


class ResearchStoreCorrupt(ResearchError):
    """The local research ledger failed its integrity check — never fabricate runs."""
