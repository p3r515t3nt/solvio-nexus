"""Explicit search-provider registry.

No dynamic/arbitrary plugin loading — only providers listed here are constructible.
The active provider is chosen by SOLVIO_SEARCH_PROVIDER (default "brave"). The API
token is read at runtime from a FILE (systemd LoadCredential dir, or an explicit token
FILE path), never from a plain env var or config JSON, so it can never be committed
or leak via an environment dump.
"""
from __future__ import annotations

import os

from solvio_node.capabilities.research.providers.base import SearchProvider
from solvio_node.capabilities.research.providers.brave import BraveSearchProvider

KNOWN_PROVIDERS = {"brave": BraveSearchProvider}


def _read_token_file() -> str | None:
    # systemd LoadCredential -> $CREDENTIALS_DIRECTORY/brave_token
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    candidates = []
    if cred_dir:
        candidates.append(os.path.join(cred_dir, "brave_token"))
    explicit = os.environ.get("SOLVIO_BRAVE_TOKEN_FILE")
    if explicit:
        candidates.append(explicit)
    for path in candidates:
        try:
            if os.path.isfile(path):
                token = open(path, encoding="utf-8").read().strip()
                if token:
                    return token
        except OSError:
            continue
    return None


def build_default_provider() -> SearchProvider | None:
    provider_id = os.environ.get("SOLVIO_SEARCH_PROVIDER", "brave")
    cls = KNOWN_PROVIDERS.get(provider_id)
    if cls is None:
        return None
    if provider_id == "brave":
        return BraveSearchProvider(_read_token_file())
    return None
