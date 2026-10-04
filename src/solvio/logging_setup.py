"""Logging-Einrichtung fuer den SOLVIO Core (structlog, menschenlesbar).

Seit dem Geheimnistresor haengt hier zusaetzlich die zentrale Redaktion. Sie
sitzt an ZWEI Stellen, weil zwei verschiedene Dinge in dasselbe Protokoll
schreiben:

* in der structlog-Kette, direkt VOR dem Renderer — dort ist ein Ereignis noch
  ein Woerterbuch, und es laesst sich auch nach Schluesselnamen redigieren
  (`password=` verschwindet, egal wie der Wert aussieht);
* als Filter am Wurzel-Handler — dort kommen `websockets`, `aiohttp` und alles
  andere an, was SOLVIOs Hausregeln nicht kennt.

Das ist die ZWEITE Verteidigung. Die erste ist, dass ein Wert gar nicht erst in
die Naehe eines Loggers kommt; siehe `solvio.redaction` und
`solvio.secret_vault.broker`.
"""

from __future__ import annotations

import logging
import sys

import structlog

from solvio.redaction import RedactingFilter, redact_processor


def setup_logging(level: str = "INFO") -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric)
    _install_root_filter()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            # Vor dem Renderer: danach ist alles nur noch eine Zeile Text.
            redact_processor,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _install_root_filter() -> None:
    """Haengt die Redaktion an jeden Wurzel-Handler — genau einmal."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


def get_logger(name: str = "solvio"):
    return structlog.get_logger(name)
