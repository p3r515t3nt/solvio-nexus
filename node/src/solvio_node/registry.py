"""Capability registry — EXPLICIT registration only.

There is no plugin auto-discovery and no dynamic import of arbitrary modules. A
capability exists on a node only because code here explicitly constructed and
registered it. Unknown ids resolve to None (-> UNKNOWN_CAPABILITY).
"""
from __future__ import annotations

from solvio_node.capabilities.base import Capability


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        if cap.id in self._caps:
            raise ValueError(f"duplicate capability id: {cap.id!r}")
        self._caps[cap.id] = cap

    def get(self, cap_id: str) -> Capability | None:
        return self._caps.get(cap_id)

    def __contains__(self, cap_id: str) -> bool:
        return cap_id in self._caps

    def ids(self) -> list[str]:
        return sorted(self._caps)

    def descriptors(self) -> list[dict]:
        return [self._caps[i].descriptor() for i in self.ids()]


def build_registry(enabled: list[str]) -> CapabilityRegistry:
    """Construct the registry from an explicit allowlist of capability ids.

    Only ids present in KNOWN are constructible; anything else is rejected at
    startup (fail-closed) — a node never silently gains a capability.
    """
    from solvio_node.capabilities.compute.sha256 import Sha256Capability
    from solvio_node.capabilities.research.fetch_public_url import (
        FetchPublicUrlCapability,
    )
    from solvio_node.capabilities.research.search_public_web import (
        SearchPublicWebCapability,
    )
    from solvio_node.capabilities.system.health import HealthCapability

    known: dict[str, type[Capability]] = {
        HealthCapability.id: HealthCapability,
        Sha256Capability.id: Sha256Capability,
        FetchPublicUrlCapability.id: FetchPublicUrlCapability,
        SearchPublicWebCapability.id: SearchPublicWebCapability,
    }
    reg = CapabilityRegistry()
    for cap_id in enabled:
        cls = known.get(cap_id)
        if cls is None:
            raise ValueError(f"unknown capability in config: {cap_id!r} "
                             f"(known: {sorted(known)})")
        reg.register(cls())
    return reg
