"""Der kognitive Router — WER denkt, nie WAS gilt.

Das Paket haelt bewusst keine Autoritaet: es importiert weder Gedaechtnis noch
Wissen noch Tresor, es kennt keinen Anbieterschluessel, und es hat genau eine
Stelle, an der es etwas bewirkt (`router.py`, `capabilities.execute`).

Ohne `attach_cognition` existiert `solvio_task` nicht, und die Oberflaeche von
gestern steht byte-gleich wieder da. Das ist die Rollback-Zusage.
"""
from __future__ import annotations

MODES: tuple[str, ...] = ("off", "shadow", "active")


def normalise_mode(raw: object) -> str:
    """`off` bei allem, was nicht ausdruecklich etwas anderes sagt.

    Die Polarität ist hier UMGEKEHRT zu `approval_policy_mode`, und das ist
    Absicht: dort bedeutet ein Tippfehler `enforce`, also die strengere Lage.
    Hier bedeutet ein Tippfehler `off`, also die heutige Lage — eine vertippte
    Konfiguration darf keine stille EINSCHALTUNG sein.
    """
    value = str(raw or "").strip().lower()
    return value if value in MODES else "off"
