from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Production-safe three-state circuit breaker.

    - CLOSED: calls pass through; count failures.
    - OPEN: fail fast until reset timeout elapses.
    - HALF_OPEN: allow a probe; close on success or re-open on failure.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted.

        - CLOSED → always allow
        - HALF_OPEN → allow (probe request)
        - OPEN → check if reset_timeout_seconds has elapsed since opened_at
          - If elapsed: transition to HALF_OPEN (use _transition()) and allow
          - If not elapsed: deny (return False)

        Use time.monotonic() for elapsed time comparison.
        """
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.HALF_OPEN:
            return True

        if self.state == CircuitState.OPEN:
            if self.opened_at is None:
                return False

            elapsed = time.monotonic() - self.opened_at

            if elapsed >= self.reset_timeout_seconds:
                self._transition(
                    CircuitState.HALF_OPEN,
                    "reset_timeout_elapsed",
                )
                return True

            return False

        return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function and update breaker state from its outcome."""
        if not self.allow_request():
            raise CircuitOpenError(f"circuit '{self.name}' is open")

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise

        self.record_success()
        return result

    def record_success(self) -> None:
        """Record a successful call and close a recovered circuit."""
        self.failure_count = 0
        self.success_count += 1
        if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0
            self.opened_at = None

    def record_failure(self) -> None:
        """Record a failed call and open or re-open when required."""
        self.failure_count += 1
        self.success_count = 0

        if self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN, "probe_failure")
            self.opened_at = time.monotonic()
        elif self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN, "failure_threshold_reached")
            self.opened_at = time.monotonic()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state
