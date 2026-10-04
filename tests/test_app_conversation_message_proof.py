"""Der Nachrichtenvertrag des Chats (N8/C3 §2.3/§2.4): kanonischer Body, Digest, Bindung.

Golden-Vektoren: die iOS-Seite uebernimmt sie aus dieser Datei (Dateiaustausch,
keine Abstimmung). Wer einen Wert hier aendert, aendert den Vertrag.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from solvio.agent_runtime import conversation_message_proof as CP
from solvio.agent_runtime import task_followup_endpoint as E, task_start_proof as T
from solvio.conversation import message_proof as MP
from solvio.security.mobile_approval import protocol as P

MESSAGE = {"conversation_id": "c-0123456789abcdef", "client_message_id": "message-golden-0001",
           "text": "Wie steht der DAX heute?"}
GOLDEN_DIGEST = "f3b8b224bdc7a05d3e190da3e7733c067e443325a51427c935c1ce085294ec7f"
GOLDEN_BYTES = (b'{"client_message_id":"message-golden-0001","conversation_id":"c-0123456789abcdef",'
                b'"text":"Wie steht der DAX heute?"}')
GOLDEN_CLIENT_DATA_HASH = "ce67476af7fdc12f773e413b954efdb65569c55f6ef46ab618b96063843fdcee"
GOLDEN_FILE_DIGEST = "17d16e96d78ef3a2a258fe94b06ad393ebe8e012d734fc34623edccd24d6e23e"
GOLDEN_FILE_BYTES = (b'{"attachments":{"files":[{"content_b64":"SGFsbG8=","name":"a.txt"}],'
                     b'"operation":"process_files"},"client_message_id":"message-golden-0001",'
                     b'"conversation_id":"c-0123456789abcdef","text":"Wie steht der DAX heute?"}')


def _binding_raw(digest):
    return P.canonical_bytes({"protocol_version": 1, "type": CP.TYPE_CONVERSATION_MESSAGE_BINDING,
        "core_instance_id": "core-fixture", "principal_id": "owner-fixture",
        "device_id": "device-fixture", "nonce": "b" * 64, "request_digest": digest,
        "enrollment_id": "enroll-fixture", "app_attest_key_id": "key-fixture",
        "approval_key_sha256": "c" * 64})


def t_app_message_golden_binding_uses_its_own_domain_and_exact_canonical_body():
    require_equal(P.canonical_bytes(MP.canonical_message_body(MESSAGE)), GOLDEN_BYTES)
    require_equal(MP.request_digest(MESSAGE), GOLDEN_DIGEST)
    raw = _binding_raw(GOLDEN_DIGEST)
    require_equal(CP.client_data_hash(raw).hex(), GOLDEN_CLIENT_DATA_HASH)
    # Dieselben Bytes unter der Task- oder Followup-Domain sind ein ANDERER Hash:
    # eine Assertion fuer einen Auftragsstart kann nie als Nachricht durchgehen.
    require(CP.client_data_hash(raw) != T.client_data_hash(raw))
    require(CP.client_data_hash(raw) != E.client_data_hash(raw))
    require_equal(CP.DOMAIN_CONVERSATION_MESSAGE, b"SOLVIO_APP_CONVERSATION_MESSAGE_V1")
    require_equal(CP.TYPE_CONVERSATION_MESSAGE_BINDING, "app_conversation_message_binding")
    # Die Dienstklasse traegt genau diese Bindung — und einen eigenen Nonce-Namensraum.
    service = CP.ConversationMessageProofService
    require(issubclass(service, T.TaskStartProofService))
    require_equal(service.binding_type, CP.TYPE_CONVERSATION_MESSAGE_BINDING)
    require_equal(service.challenge_prefix, "app-conversation-message:")
    require(service.challenge_prefix not in (T.TaskStartProofService.challenge_prefix,
                                             E.TaskFollowupProofService.challenge_prefix))
    require_equal(service.body_digest(MESSAGE), GOLDEN_DIGEST)
    require_equal(service.assertion_hash(raw), CP.client_data_hash(raw))
    # Der Kern kennt den Vertrag, nicht die Laufzeit: kein Modulimport der Agentenlaufzeit.
    import ast
    tree = ast.parse(open(MP.__file__, encoding="utf-8").read())
    modules = [n.module for n in tree.body if isinstance(n, ast.ImportFrom)] + \
              [a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names]
    require(not any(str(m).startswith("solvio.agent_runtime") for m in modules), modules)
    # Anhang: kanonisch ueber den Dateivertrag, im Digest enthalten.
    with_file = dict(MESSAGE, attachments={"operation": "process_files",
                                           "files": [{"name": "a.txt", "content_b64": "SGFsbG8="}]})
    require_equal(P.canonical_bytes(MP.canonical_message_body(with_file)), GOLDEN_FILE_BYTES)
    require_equal(MP.request_digest(with_file), GOLDEN_FILE_DIGEST)
    require(MP.request_digest(with_file) != GOLDEN_DIGEST)
    require_equal(MP.attachment_kind(with_file["attachments"]), "file")
    require_equal(MP.attachment_kind({"operation": "extract_text", "format": "txt",
                                      "content_b64": "SGFsbG8="}), "document")


def _refused(message):
    try:
        MP.canonical_message_body(message)
    except (ValueError, TypeError):
        return True
    return False


def t_the_canonical_message_body_is_exact_and_never_normalises_after_the_fact():
    base = dict(MESSAGE)
    # Feldmengen: genau die drei, plus optional attachments, plus optional target.
    for extra in ({"principal": "x"}, {"origin": "trusted_dashboard"}, {"proof": {}}, {"scope": "research"}):
        require(_refused(dict(base, **extra)), f"ein fremdes Feld wurde angenommen: {extra}")
    for missing in ("conversation_id", "client_message_id", "text"):
        broken = dict(base)
        del broken[missing]
        require(_refused(broken), f"ohne {missing} angenommen")
    require(_refused([]) and _refused(None) and _refused("text"))
    # Chatkennung: nur die vom Core gepraegte Form.
    for bad in ("", "c-0123", "at-0123456789abcdef", "C-0123456789ABCDEF", 7, None):
        require(_refused(dict(base, conversation_id=bad)), f"conversation_id {bad!r} angenommen")
    # Client-Kennung: dieselbe Regel wie beim Auftragsstart.
    for bad in ("", "kurz", "x" * 200, 5, "böse-umlaut-0001"):
        require(_refused(dict(base, client_message_id=bad)), f"client_message_id {bad!r} angenommen")
    # Text: getrimmt, 1..4000, kein NUL. Nichts wird still gekuerzt oder getrimmt.
    for bad in ("", " fuehrend", "nachlaufend ", "a" * 4001, "mit\x00nul", 12, None):
        require(_refused(dict(base, text=bad)), f"text {bad!r} angenommen")
    require_equal(MP.canonical_message_body(dict(base, text="a" * 4000))["text"], "a" * 4000)
    require_equal(MP.canonical_message_body(dict(base, text="Danke"))["text"], "Danke",
                  "ein kurzer Dank ist eine gueltige Nachricht — anders als ein Auftrag")
    # Anhang: genau EINES, an der Form erkannt; alles andere ist kein Anhang.
    for bad in ({}, {"operation": "process_files"}, {"operation": "unknown"}, [], "x",
                {"operation": "process_files", "files": []},
                {"operation": "extract_text", "format": "txt"},
                {"operation": "extract_text", "format": "txt", "content_b64": "SGFsbG8", "extra": 1}):
        require(_refused(dict(base, attachments=bad)), f"attachments {bad!r} angenommen")
    require(_refused(dict(base, attachments=None)), "attachments=None ist kein optionaler Anhang")
    # Ziel: exakte Feldmenge und Formen.
    good = {"task_id": "at-" + "1" * 16, "run_id": "ar-" + "2" * 16, "revision": 1}
    require_equal(MP.canonical_message_body(dict(base, target=good))["target"], good)
    for bad in ({}, dict(good, extra=1), dict(good, revision=0), dict(good, revision=True),
                dict(good, revision="1"), dict(good, task_id="ar-" + "1" * 16),
                dict(good, run_id="at-" + "2" * 16), {"task_id": good["task_id"]}, [], "x"):
        require(_refused(dict(base, target=bad)), f"target {bad!r} angenommen")
    # Unpaarige Surrogate fallen hier — nicht erst in einer Autoritaetstransaktion.
    require(_refused(dict(base, text="kaputt \ud800 ende")))
    # Das Ergebnis ist eine KOPIE mit genau den gepruefen Feldern.
    result = MP.canonical_message_body(dict(base, target=good))
    require_equal(set(result), {"conversation_id", "client_message_id", "text", "target"})


def t_the_source_fingerprint_is_stable_and_hides_nothing_but_bytes():
    generation = ("owner", "device-1", "enroll-1", "f" * 64, "key-1", "04ab", "production")
    first = MP.source_fingerprint(generation)
    require_equal(len(first), 64)
    require_equal(first, MP.source_fingerprint(tuple(generation)))
    require(first != MP.source_fingerprint(generation[:-1] + ("development",)))
    require(first != MP.source_fingerprint(("other",) + generation[1:]))
    # Bytes werden als Base64 abgebildet — deterministisch, und nie roh.
    with_bytes = MP.source_fingerprint(generation + (b"\x00\x01",))
    require_equal(with_bytes, MP.source_fingerprint(generation + (b"\x00\x01",)))
    require(with_bytes != first)
    for member in generation:
        require(member not in with_bytes)
    require(base64.b64encode(b"\x00\x01").decode() not in with_bytes)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
