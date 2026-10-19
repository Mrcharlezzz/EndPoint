"""Reliability primitives for the ingestion daemon.

Houses standalone, testable helpers that wrap fallible operations:

* :mod:`circuit_breaker` — per-key 3-state breaker. Trips after a
  configurable run of failures, half-opens for one probe after a
  cooldown, fully closes on probe success.
* :mod:`backoff` — exponential backoff + jitter retry helper plus the
  :class:`RetryPolicy` config dataclass.

These are intentionally lightweight: process-local state, stdlib +
structlog only, no I/O. Persistence (SQLite-backed breaker state) is
deferred — see ``circuit_breaker.CircuitBreaker`` docstring.
"""

from __future__ import annotations
