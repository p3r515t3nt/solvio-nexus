"""Die zweite Verteidigung — was trotzdem in ein Protokoll rutscht.

**Das hier ist ausdruecklich nicht die erste Verteidigung.** Die erste ist, dass
ein Wert den Tresor nur ueber `SecretBroker.use()` verlaesst, direkt in den
Executor geht und nie in ein Argument, einen Freigabetext oder eine Logzeile
kommt. Wer Redaktion fuer die Sicherheitsarchitektur haelt, hat sie schon
verloren: ein regulaerer Ausdruck kennt nur die Formen, die jemand
aufgeschrieben hat, und ein Passwort wie `Hund1234` hat keine Form.

Wozu es sie trotzdem gibt: der Core schreibt in EIN Protokoll, das nicht
rotiert und mit Rechten 0644 dasteht (DEBT-0056, und die Schuld sagt selbst:
„solange dieses Log unbegrenzt waechst, darf nichts Sensibles hinein"). In
dasselbe Protokoll schreiben auch `websockets` und `aiohttp` — Bibliotheken, die
SOLVIOs Hausregel „nur `kind=`, nie den Text" nicht kennen. `SOLVIO_LOG_LEVEL=DEBUG`
macht aus `websockets` einen Mitschreiber jedes Rahmens.

Warum eine Datei statt fuenf: es gab bisher fuenf voneinander unabhaengige
Musterlisten (`realtime/core_server.py`, `specialists/launcher.py`,
`bots/redaction.py`, `knowledge/obsidian.py`, `portal/redact.py`), und keine
kannte alle Formen der anderen — `AIza` stand nur in einer, `ghp_` in dreien,
der PEM-Kopf in einer. Diese Datei ist die Vereinigung, und sie sitzt an der
Protokollgrenze, wo sie alles erwischt, was durchgeht.

Zwei Wege hinein, weil `structlog` und `logging` verschiedene Dinge sind:

* `redact_processor` haengt in der structlog-Kette vor dem Renderer und sieht
  SOLVIOs eigene Ereignisse als Woerterbuch — dort wird auch nach SCHLUESSELNAMEN
  redigiert, nicht nur nach Form.
* `RedactingFilter` haengt am Wurzel-Handler und sieht alles andere als Text.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

MASK = "<redigiert>"

#: Bekannte Formen. Die Vereinigung der fuenf bisherigen Listen im Baum.
_SHAPES: tuple[re.Pattern[str], ...] = (
    # Anbieterschluessel mit erkennbarem Praefix
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
    # JSON Web Token
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    # Privater Schluessel als PEM-Block
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
               re.DOTALL),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    # Kopfzeilen
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[^\s,;\"']{8,}"),
)

#: Zuweisungen mit sprechendem Namen: `passwort: ...`, `api_key=...`.
#: Der Name selbst bleibt stehen — er ist die Information, dass hier etwas war.
#:
#: `(?!//)` ist kein Schoenheitsfehler, sondern ein gefundener Fehlalarm: ohne
#: das schlug die Regel auf `secret://amazon/gregor` an und machte ausgerechnet
#: den Verweis unlesbar, der als einziger sicher ins Protokoll darf. Eine
#: Redaktion, die die harmlose Haelfte frisst, wird abgeschaltet.
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|secret|token|password|passwort|passphrase|kennwort|"
    r"credential|credentials)\b(\s*[:=]\s*)(?!//)([\"']?)([^\s\"',;&]{6,})")

#: Ein langer Blob mit Gross, Klein UND Ziffer. Absichtlich nicht „32 Zeichen
#: irgendwas": eine SHA-256-Hexsumme hat keine Grossbuchstaben und bleibt damit
#: lesbar — Pruefsummen sind in diesem Baum die haeufigste lange Zeichenkette in
#: einem Protokoll, und sie unlesbar zu machen kostet Diagnose ohne Gewinn.
_BLOB = re.compile(r"\b(?=[A-Za-z0-9+/_-]{32,}\b)(?=[^\s]*[a-z])(?=[^\s]*[A-Z])"
                   r"(?=[^\s]*[0-9])[A-Za-z0-9+/_-]{32,}\b")

#: Schluesselnamen in einem Ereignis-Woerterbuch, deren WERT nie ins Protokoll
#: gehoert — unabhaengig davon, wie er aussieht. `Hund1234` hat keine Form.
_FORBIDDEN_KEYS = frozenset({
    "password", "passwort", "passphrase", "kennwort", "secret", "token",
    "api_key", "apikey", "access_token", "refresh_token", "id_token",
    "client_secret", "authorization", "auth", "credential", "credentials",
    "plaintext", "secret_value", "value", "body", "payload", "assertion",
})


def redact_text(text: str) -> str:
    """Bekannte Geheimnisformen aus einem Text nehmen. Nie perfekt, immer besser."""
    if not text:
        return text
    out = text
    for pattern in _SHAPES:
        out = pattern.sub(MASK, out)
    out = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", out)
    out = _BLOB.sub(MASK, out)
    return out


def looks_redactable(text: str) -> bool:
    """Enthaelt dieser Text etwas, das wie ein Geheimnis aussieht?"""
    return redact_text(text) != text


def redact_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Ein structlog-Ereignis redigieren — nach Schluesselname UND nach Form."""
    out: dict[str, Any] = {}
    for key, value in event.items():
        lowered = str(key).lower()
        if lowered in _FORBIDDEN_KEYS:
            out[key] = MASK
            continue
        if isinstance(value, str):
            out[key] = redact_text(value)
        elif isinstance(value, (bytes, bytearray)):
            # Bytes gehoeren nie in ein Protokoll. Die Laenge ist Diagnose genug.
            out[key] = f"<{len(value)} bytes>"
        elif isinstance(value, Mapping):
            out[key] = redact_event(value)
        else:
            out[key] = value
    return out


def redact_processor(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    """structlog-Prozessor. Gehoert VOR den Renderer, sonst sieht er nur Text."""
    return redact_event(event)


class RedactingFilter(logging.Filter):
    """Fuer alles, was nicht durch structlog kommt — `websockets`, `aiohttp`, Dritte.

    Ein Filter und kein Formatter: der Formatter haengt am Handler und sieht nur
    das fertige Ergebnis, der Filter sieht den Datensatz, bevor irgendwer ihn
    formatiert. Beides wuerde gehen; der Filter greift frueher, und `args` sind
    hier noch getrennt vom Format.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and looks_redactable(record.msg):
                record.msg = redact_text(record.msg)
                # `args` sind bereits in die Nachricht eingesetzt worden, wenn
                # sie hier noch stuenden — also entfernen, sonst formatiert
                # `logging` die redigierte Nachricht ein zweites Mal.
                record.args = ()
            elif record.args:
                rendered = record.getMessage()
                if looks_redactable(rendered):
                    record.msg = redact_text(rendered)
                    record.args = ()
        except Exception:  # noqa: BLE001 - Protokollieren darf nie die Ursache sein
            record.msg = MASK
            record.args = ()
        return True
