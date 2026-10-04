"""SOLVIO Architektur-Contracts (STEP 19B.3) - DESIGN-ONLY.

Dieses Paket enthaelt framework-neutrale Typdefinitionen (Protocols, Dataclasses,
Enums), gegen die Backends (Memory, Deep-Runtime) getestet und spaeter
implementiert werden.

WICHTIG: Es wird vom produktiven Core BEWUSST NICHT importiert und aendert kein
Laufzeitverhalten. Es ist die Messlatte fuer den OpenClaw-Benchmark (STEP 19C)
und die Vorlage fuer spaetere native Implementierungen. Siehe docs/architecture/.
"""
