"""SOLVIO Memory Foundation (STEP 20).

Native, lokale, provenance- und purge-bewusste Langzeit-Memory-Infrastruktur auf
Basis der Python-Standardbibliothek (sqlite3). Implementiert das eingefrorene
MemoryStore-Protokoll aus solvio.contracts.memory.

Bewusst OHNE: LLM/Embeddings, Netzwerk, Gmail/Web/Kalender/HA, Dreaming/
Konsolidierungs-KI. Diese Schicht ist das stabile Fundament fuer STEP 21
(Semantic Recall), STEP 22 (Consolidation/Dreaming) und STEP 23 (External Trust).

Der Laufzeit-Voice-Pfad importiert dieses Paket (noch) NICHT.
"""
from __future__ import annotations

from solvio.memory.store import SolvioMemory

__all__ = ["SolvioMemory"]
__version__ = "0.1.0"
