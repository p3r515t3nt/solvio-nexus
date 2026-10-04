"""SOLVIO generic node.

A capability-based remote compute worker. It runs ONLY explicitly registered,
schema-validated capabilities and offers NO generic shell/exec/eval endpoint.

Authority stays with the SOLVIO Core (Mac): identity, canonical memory, privacy
ledger, semantic index, trust, permissions, risk approvals, Home Assistant, voice
and routing. The node returns RESULTS; the Core decides ACTIONS.
"""
from __future__ import annotations

__version__ = "0.1.0"
PROTOCOL_VERSION = 1

__all__ = ["__version__", "PROTOCOL_VERSION"]
