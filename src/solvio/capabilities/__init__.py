"""Capability Contract + TrustContext V1.

Ein Vertrag, unter dem jede kuenftige Faehigkeit laeuft — HA, Kalender, Gmail,
Hermes, Browser. Die Regel, die er durchsetzt, steht in
`docs/architecture/TRUST_BOUNDARY.md` und lautet:

    Unvertrauter Inhalt kann Information liefern, aber NIE Autoritaet.

Sie ist hier Code (`contract.authority_refusal`), nicht Prompt-Wortlaut.
"""
from solvio.capabilities.contract import (
    AmbiguousExecution, ArgumentSource, CapabilityError, CapabilitySpec,
    ExecutionClass, ExecutorUnavailable, authority_refusal, effective_risk,
    requires_approval, worst_source,
)
from solvio.capabilities.envelope import (
    NO_EFFECT_OUTCOMES, CapabilityOutcome, CapabilityResult,
)
from solvio.capabilities.router import (
    CapabilityEvent, CapabilityRouter, binding_digest, canonical_binding,
)

__all__ = [
    "AmbiguousExecution", "ArgumentSource", "CapabilityError", "CapabilityEvent",
    "CapabilityOutcome", "CapabilityResult", "CapabilityRouter", "CapabilitySpec",
    "ExecutionClass", "ExecutorUnavailable", "NO_EFFECT_OUTCOMES",
    "authority_refusal", "binding_digest", "canonical_binding", "effective_risk",
    "requires_approval", "worst_source",
]
