"""Circuit Breaker + beschraenkter Backoff fuer den Node-Layer (PHASE 14/15).

Bewusst klein und deterministisch testbar: Zeit- und Zufallsquelle sind
injizierbar, damit Tests ohne echte Uhr/echten Zufall auskommen.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class CircuitState(str, Enum):
    CLOSED = "closed"      # normal: Anfragen erlaubt
    OPEN = "open"          # gesperrt: schnelle Ablehnung ohne Netz
    HALF_OPEN = "half_open"  # Probe: begrenzte Testanfragen erlaubt


@dataclass
class CircuitBreaker:
    """Einfacher Dreizustands-Breaker (PHASE 14).

    Nach `failure_threshold` Fehlern in Folge -> OPEN. Nach `recovery_timeout_s`
    -> HALF_OPEN (eine Probeanfrage). Erfolg -> CLOSED, Fehler -> wieder OPEN.
    """

    failure_threshold: int = 3
    recovery_timeout_s: float = 30.0
    half_open_max_calls: int = 1
    clock: Callable[[], float] = time.monotonic

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _half_open_calls: int = field(default=0, init=False)

    @property
    def state(self) -> CircuitState:
        """Aktueller Zustand; wechselt OPEN->HALF_OPEN nach Ablauf des Timeouts."""
        if self._state is CircuitState.OPEN and (
            self.clock() - self._opened_at
        ) >= self.recovery_timeout_s:
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0
        return self._state

    def allow(self) -> bool:
        """Darf jetzt eine Anfrage an den Knoten gehen?"""
        st = self.state
        if st is CircuitState.CLOSED:
            return True
        if st is CircuitState.OPEN:
            return False
        if self._half_open_calls < self.half_open_max_calls:
            self._half_open_calls += 1
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._half_open_calls = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        # Fehler im HALF_OPEN -> sofort wieder OPEN.
        if self.state is CircuitState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self.clock()
        self._half_open_calls = 0


@dataclass
class Backoff:
    """Beschraenkter exponentieller Backoff mit Jitter (PHASE 15).

    Kein tight-retry-loop: die Verzoegerung waechst exponentiell bis `max_s`
    und wird um `jitter` (Anteil 0..1) nach unten gestreut. Zufallsquelle
    injizierbar fuer deterministische Tests.
    """

    base_s: float = 0.5
    factor: float = 2.0
    max_s: float = 30.0
    jitter: float = 0.5
    rng: random.Random = field(default_factory=random.Random)

    def delay(self, attempt: int) -> float:
        """Wartezeit fuer den `attempt`-ten Versuch (1-basiert), in Sekunden."""
        raw = min(self.max_s, self.base_s * (self.factor ** max(0, attempt - 1)))
        lo = raw * (1.0 - self.jitter)
        return lo + (raw - lo) * self.rng.random()
