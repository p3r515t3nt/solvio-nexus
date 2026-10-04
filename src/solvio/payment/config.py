"""Wo ein Anbieter erreichbar ist. Adressen, nie Geheimnisse.

Die Datei liegt bewusst NICHT im Repository: eine Anbieteradresse ist
Laufzeitkonfiguration, und was im Repository steht, wandert in jede Kopie.

    ~/.solvio/payment.json

    {
      "providers": {
        "sandbox": {"kind": "sandbox", "base_url": "http://127.0.0.1:8795"}
      }
    }

Was hier NIE steht: ein Anbieterschluessel, ein Token, eine Kartennummer. Der
Zugang liegt im Tresor und wird ueber den Makler geliehen; diese Datei sagt nur,
WOHIN der Executor spricht. Fehlt sie, gibt es keinen Anbieter und damit keine
Zahlung — fail-closed, wie ueberall.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from solvio.logging_setup import get_logger

log = get_logger("payment")

DEFAULT_PATH = "~/.solvio/payment.json"
PATH_ENV = "SOLVIO_PAYMENT_CONFIG"

#: Anbieterarten, fuer die es einen Client gibt. Abschliessend.
KNOWN_KINDS = ("sandbox",)


def config_path() -> str:
    return os.path.expanduser(os.environ.get(PATH_ENV) or DEFAULT_PATH)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    base_url: str

    def __post_init__(self) -> None:
        if self.kind not in KNOWN_KINDS:
            raise ValueError(f"unknown provider kind: {self.kind[:32]}")


def load() -> dict[str, ProviderConfig]:
    """Liest die Anbieteradressen. Eine unlesbare Datei ist kein Anbieter."""
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("payment.config_unreadable", kind=type(exc).__name__)
        return {}
    out: dict[str, ProviderConfig] = {}
    for name, entry in (raw.get("providers") or {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            out[str(name)] = ProviderConfig(name=str(name),
                                            kind=str(entry.get("kind", "")),
                                            base_url=str(entry.get("base_url", "")))
        except ValueError as exc:
            log.error("payment.config_provider_rejected", provider=str(name)[:32],
                      reason=str(exc)[:60])
    return out


def provider(name: str) -> ProviderConfig | None:
    return load().get(str(name or "").strip())
