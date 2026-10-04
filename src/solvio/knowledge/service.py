"""Der eine Weg, auf dem Gedaechtnis in den Vault kommt.

Klein mit Absicht. Er tut genau zwei Dinge: die kanonische Sicht holen und sie
kompilieren lassen. Die Sichtbarkeitsregel steht nicht hier — sie steht in
`SolvioMemory.active_records()`, und das ist der Punkt.

Es gibt keinen zweiten Weg. Insbesondere gibt es keine Funktion, die aus einer
Markdown-Datei Gedaechtnis machen koennte.
"""
from __future__ import annotations

import os

from solvio.knowledge import compiler
from solvio.memory.service import memory_base_dir
from solvio.memory.store import SolvioMemory

#: Wo der Vault liegt, wenn niemand etwas anderes sagt.
#:
#: Im Heimverzeichnis, nicht unter `~/.solvio`: dort liegt Betriebszustand, und
#: ein Mensch soll hier hineinsehen koennen, ohne ein verstecktes Verzeichnis
#: zu oeffnen. Ausdruecklich NICHT in `~/Documents` oder `~/Desktop` — beide
#: koennen von der iCloud-Ordnersynchronisierung erfasst werden, und
#: persoenliches Gedaechtnis gehoert nicht ungefragt in eine Wolke.
DEFAULT_VAULT = os.path.expanduser("~/SOLVIO Knowledge")


def vault_dir() -> str:
    return os.environ.get("SOLVIO_VAULT", DEFAULT_VAULT)


async def compile_knowledge(vault: str | None = None, *,
                            memory: SolvioMemory | None = None,
                            base_dir: str | None = None) -> dict[str, object]:
    """Baut das Wissensbuendel aus dem aktuellen Gedaechtnisstand.

    Zahlen, nie Inhalt: was kompiliert wurde, steht in den Dateien, nicht im
    Log. Ein uebergebener kanonischer Speicher bleibt Eigentum des Aufrufers;
    nur eine hier erzeugte Instanz wird wieder geschlossen. Ohne Injektion
    bleibt der bisherige Standardpfad erhalten.
    """
    if memory is not None and base_dir is not None:
        raise ValueError("memory_and_base_dir_are_mutually_exclusive")
    owned = memory is None
    if owned:
        memory = SolvioMemory(base_dir if base_dir is not None else memory_base_dir())
    try:
        records = await memory.active_records()
        target = vault or vault_dir()
        # Der Compiler sieht nur, DASS eine Kennung fehlt — nicht, warum. Fuer
        # ein Vergessen ist das der Unterschied zwischen einem inhaltsfreien
        # Grabstein und einer Archivdatei, in der der Satz weiterlebt
        # (DEBT-0092). Den Grund kennt nur der Core, also holt ihn der Core.
        known = compiler.known_ids(target) - {r.id for r in records}
        reasons = await memory.removal_reasons(known) if known else {}
        return compiler.compile_bundle(records, target,
                                       removal_reasons=reasons).as_dict()
    finally:
        close = getattr(memory, "close", None) if owned else None
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


def lint_knowledge(vault: str | None = None) -> list[str]:
    """Was in diesem Buendel ein Mensch entscheiden muss.

    Braucht kein Gedaechtnis und kein Netz: geprueft wird, was auf der Platte
    liegt. Ein Validator, der eine Datenbank oeffnen muss, ist keiner, den ein
    Fremder laufen lassen kann.
    """
    return compiler.lint(vault or vault_dir())
