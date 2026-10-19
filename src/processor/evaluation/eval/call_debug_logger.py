from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any

from evaluation.eval.loader import EvalRow


class CallDebugLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._counter = count(1)

    async def log_start(
        self,
        *,
        eval_run_id: str,
        row: EvalRow,
        operation: str,
        retry_count: int,
        model: str | None,
        prompt_version: str | None,
        prompt_fingerprint: str | None,
    ) -> int:
        async with self._lock:
            call_id = next(self._counter)
            self._write_event(
                {
                    "timestamp": _utc_now(),
                    "event": "start",
                    "eval_run_id": eval_run_id,
                    "call_id": call_id,
                    "row_id": row.row_id,
                    "external_id": row.external_id,
                    "dataset_name": row.dataset_name,
                    "operation": operation,
                    "retry_count": retry_count,
                    "order_index": row.order_index,
                    "model": model,
                    "prompt_version": prompt_version,
                    "prompt_fingerprint": prompt_fingerprint,
                }
            )
            return call_id

    async def log_finish(
        self,
        *,
        eval_run_id: str,
        call_id: int,
        row: EvalRow,
        operation: str,
        retry_count: int,
        elapsed_ms: int,
        model: str | None,
        prompt_version: str | None,
        prompt_fingerprint: str | None,
        status: str,
        error_type: str | None = None,
        error_message: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            self._write_event(
                {
                    "timestamp": _utc_now(),
                    "event": "finish",
                    "eval_run_id": eval_run_id,
                    "call_id": call_id,
                    "row_id": row.row_id,
                    "external_id": row.external_id,
                    "dataset_name": row.dataset_name,
                    "operation": operation,
                    "retry_count": retry_count,
                    "order_index": row.order_index,
                    "model": model,
                    "prompt_version": prompt_version,
                    "prompt_fingerprint": prompt_fingerprint,
                    "elapsed_ms": elapsed_ms,
                    "status": status,
                    "error_type": error_type,
                    "error_message": error_message,
                    "usage": usage,
                }
            )

    def _write_event(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
