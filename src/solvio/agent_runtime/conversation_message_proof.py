"""Der App-Attest-Beweis fuer genau EINE Chat-Nachricht (N8/C3 §2.4).

Exakt das Unterklassenmuster von `task_followup_endpoint.py`: eigener
Domain-Separator, eigener Nonce-Namensraum, eigener Bindungstyp; Nonce-TTL,
einmaliger Verbrauch und die Bindung an Core-ID, Geraet, Enrollment und
App-Attest-Key kommen unveraendert aus `TaskStartProofService`. Eine Task-,
Followup- oder Antwort-Assertion kann nie als Nachricht durchgehen — und
umgekehrt.

Der kanonische Body und sein Digest wohnen im Kern
(`solvio.conversation.message_proof`); dieses Modul liegt in der Agentenlaufzeit,
weil ein Kernmodul die Laufzeit nie auf Modulebene importiert (Rollback-Zusage
von `SOLVIO_AGENT_RUNTIME=off`).
"""
from __future__ import annotations

import hashlib

from solvio.agent_runtime.task_start_proof import TaskStartProofService
from solvio.conversation.message_proof import request_digest

DOMAIN_CONVERSATION_MESSAGE = b"SOLVIO_APP_CONVERSATION_MESSAGE_V1"
TYPE_CONVERSATION_MESSAGE_BINDING = "app_conversation_message_binding"


def client_data_hash(raw):
    return hashlib.sha256(DOMAIN_CONVERSATION_MESSAGE + b"\x00" + raw).digest()


class ConversationMessageProofService(TaskStartProofService):
    body_digest = staticmethod(request_digest)
    assertion_hash = staticmethod(client_data_hash)
    challenge_prefix = "app-conversation-message:"
    binding_type = TYPE_CONVERSATION_MESSAGE_BINDING
    audit_prefix = "app_conversation_message"
