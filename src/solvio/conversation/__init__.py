"""SOLVIO-eigene Gespraechsverwaltung (M1, seit N8/C3 auch dauerhafte Text-Chats).

Eine Provider-Sitzung ist Transport. Ein SOLVIO-Gespraech ist ein Produktobjekt, das
den Wechsel des Providers ueberlebt.
"""
from solvio.conversation.store import (  # noqa: F401
    ConversationStore,
    ConversationStoreError,
    DEFAULT_LINGER_SECONDS,
    DEFAULT_CONTEXT_CHARS,
    DELIVERY_ACCEPTED,
    DELIVERY_BLOCKED,
    DELIVERY_COMPLETED,
    DELIVERY_OPEN,
    DELIVERY_RUNNING,
    KIND_TEXT,
    KIND_VOICE,
    default_db_path,
    derive_title,
    state_dir,
)
