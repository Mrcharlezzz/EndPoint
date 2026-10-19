from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from processing.models.classification import LLMUsage


class LLMUsageCollector:
    def __init__(self) -> None:
        self._usage_var: ContextVar[LLMUsage | None] = ContextVar(
            "llm_usage",
            default=None,
        )

    @contextmanager
    def capture(self) -> Iterator[None]:
        token = self._usage_var.set(LLMUsage())
        try:
            yield
        finally:
            self._usage_var.reset(token)

    def record_call(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        usage = self._usage_var.get()
        if usage is None:
            return

        self._usage_var.set(
            LLMUsage(
                input_tokens=(
                    None
                    if input_tokens is None and usage.input_tokens is None
                    else (usage.input_tokens or 0) + (input_tokens or 0)
                ),
                output_tokens=(
                    None
                    if output_tokens is None and usage.output_tokens is None
                    else (usage.output_tokens or 0) + (output_tokens or 0)
                ),
            )
        )

    def current_usage(self) -> LLMUsage:
        usage = self._usage_var.get()
        return usage or LLMUsage()
