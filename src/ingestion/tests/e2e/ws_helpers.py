"""WebSocket test helpers — drain-with-deadline + event-id filtering.

Populated in PR-6 when WS tests land. Placeholder keeps the import graph
intact so PR-1 through PR-5 can import freely.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def collect_until(
    websocket: object,
    predicate: Callable[[dict[str, Any]], bool],
    deadline_seconds: float = 5.0,
) -> dict[str, Any]:
    raise NotImplementedError("implemented in PR-6")
