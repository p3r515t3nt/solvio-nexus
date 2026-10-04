"""Der kanonische Nachrichtenvertrag eines Chats (N8/C3 §2.3).

Eine Textnachricht aus Dashboard oder iPhone-App ist auf beiden Kanaelen
derselbe Body: `conversation_id`, `client_message_id`, `text`, optional genau
EIN Anhang (`file_request` oder `document_request`, am `operation`-Schluessel
unterschieden) und optional ein `target` (ein Auftrag dieses Chats, an den die
Nachricht als Folgeanweisung geht). Der Digest ueber die kanonischen Bytes ist
die Idempotenz der Zustellung.

Fuer die App reicht die Transportkennung nicht: sie darf lesen und einen Chat
anlegen, aber eine Nachricht kostet einen Modellaufruf und kann einen Auftrag
starten. Deshalb traegt sie denselben Beweis wie ein Auftragsstart — der
Beweisdienst dazu (`ConversationMessageProofService`) wohnt in
`agent_runtime/conversation_message_proof.py`: ein Kernmodul importiert die
Laufzeit nie auf Modulebene. Hier steht nur der Vertrag, den beide Kanaele und
der Beweis teilen.

Was hier NICHT steht: Autoritaet. Der Beweis zeigt, dass genau diese Nachricht
von der attestierten App-Instanz auf dem eingeschriebenen Geraet kam. Ob
daraus ein Auftrag wird, entscheidet der Prozessor mit den bestehenden Toren.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

from solvio.security.mobile_approval import protocol as P

#: Ein Nachrichtentext: getrimmt, 1..4000 Zeichen, kein NUL.
MAX_TEXT = 4000
_FIELDS = frozenset({"conversation_id", "client_message_id", "text"})
_TASK_ID = re.compile(r"at-[0-9a-f]{16}\Z")
_RUN_ID = re.compile(r"ar-[0-9a-f]{16}\Z")
#: Der Anhang wird an seinem `operation`-Schluessel erkannt — derselbe, den
#: die beiden Vertraege selbst verlangen.
ATTACHMENT_FILE = "process_files"
ATTACHMENT_DOCUMENT = "extract_text"


def message_text(value) -> str:
    """Die eine Textregel, an beiden Kanaelen gleich."""
    if (type(value) is not str or value != value.strip() or not 1 <= len(value) <= MAX_TEXT
            or "\x00" in value):
        raise ValueError("invalid_message_text")
    return value


def attachment_kind(attachments) -> str:
    """`file`, `document` oder `` — nur aus der Form, nie aus einer Behauptung."""
    if attachments is None:
        return ""
    if type(attachments) is not dict:
        raise ValueError("invalid_message_attachments")
    operation = attachments.get("operation")
    if operation == ATTACHMENT_FILE:
        return "file"
    if operation == ATTACHMENT_DOCUMENT:
        return "document"
    raise ValueError("invalid_message_attachments")


def canonical_attachments(attachments) -> dict:
    """Genau EIN Anhang, kanonisch ueber den Vertrag, der ihn kennt."""
    kind = attachment_kind(attachments)
    if kind == "file":
        from solvio.agent_runtime.file_inputs import canonical_request
        return canonical_request(attachments)
    from solvio.agent_runtime.document_contract import canonical_request
    return canonical_request(attachments)


def canonical_target(target) -> dict:
    """`{task_id, run_id, revision}` — Formen wie das Agentenbuch sie praegt."""
    if type(target) is not dict or set(target) != {"task_id", "run_id", "revision"}:
        raise ValueError("invalid_message_target")
    task_id, run_id, revision = target["task_id"], target["run_id"], target["revision"]
    if type(task_id) is not str or not _TASK_ID.fullmatch(task_id):
        raise ValueError("invalid_message_target")
    if type(run_id) is not str or not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid_message_target")
    if type(revision) is not int or isinstance(revision, bool) or revision < 1:
        raise ValueError("invalid_message_target")
    return {"task_id": task_id, "run_id": run_id, "revision": revision}


def canonical_message_body(message) -> dict:
    """Den vollstaendigen, exakten Body pruefen — nie kuerzen, nie ergaenzen.

    Dasselbe Ergebnis benutzen Challenge, Beweis und Annahme: was hier
    herauskommt, wird gehasht, signiert und persistiert. Ein Feld, das erst
    nach dem Beweis normalisiert wuerde, waere ein anderer Body.
    """
    fields = set(message) if isinstance(message, dict) else set()
    if (not isinstance(message, dict)
            or fields - {"attachments", "target"} != _FIELDS):
        raise ValueError("invalid_message_fields")
    # Dieselben Formregeln wie beim Auftragsstart — aufgerufen, nicht kopiert;
    # spaet importiert, weil der Kern die Laufzeit nie auf Modulebene laedt.
    from solvio.agent_runtime.task_start_service import conversation_reference, request_identifier
    conversation_id = message["conversation_id"]
    if type(conversation_id) is not str or not conversation_id:
        raise ValueError("invalid_message_conversation")
    conversation_reference(conversation_id)
    request_identifier(message["client_message_id"])
    text = message_text(message["text"])
    result = {"conversation_id": conversation_id,
              "client_message_id": message["client_message_id"], "text": text}
    if "attachments" in message:
        result["attachments"] = canonical_attachments(message["attachments"])
    if "target" in message:
        result["target"] = canonical_target(message["target"])
    # Unpaarige Surrogate fallen hier, nicht erst in einer Autoritaetstransaktion.
    P.canonical_bytes(result).decode("utf-8")
    return result


def request_digest(message) -> str:
    return hashlib.sha256(P.canonical_bytes(canonical_message_body(message))).hexdigest()


def source_fingerprint(generation) -> str:
    """Der oeffentliche Abdruck einer Geraete-Generation (§2.4, §3.2).

    `voice_task_session.device_generation` liefert ein Tupel aus Kennungen,
    Fingerabdruecken und dem attestierten Schluessel (hex). Persistiert wird
    nur dieser Abdruck — er ist kein Geheimnis, und `current()` vergleicht
    spaeter denselben Abdruck gegen den frisch gelesenen Stand.
    """
    def _bytes(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return base64.b64encode(bytes(value)).decode("ascii")
        raise TypeError("unserialisable_generation_member")

    raw = json.dumps(list(generation), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, default=_bytes).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
