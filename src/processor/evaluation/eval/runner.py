from __future__ import annotations

import asyncio
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

from evaluation.eval.call_debug_logger import CallDebugLogger
from evaluation.eval.loader import EvalRow
from processing.application.classification import ClassificationOrchestrator
from processing.models.classification import ClassificationResult
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import RawMessage
from processing.ports.classifier import EntityInferencePort


@dataclass(slots=True)
class PipelineRowResult:
    row: EvalRow
    status: str
    envelope: ProcessingEnvelope | None = None
    classification_output: dict[str, Any] | None = None
    extraction_output: dict[str, Any] | None = None
    processing_time_ms: int | None = None
    classification_time_ms: int | None = None
    extract_time_ms: int | None = None
    classification_usage: dict[str, Any] | None = None
    extraction_usage: dict[str, Any] | None = None
    prompt_version: str | None = None
    prompt_fingerprint: str | None = None
    retry_count: int = 0
    error_type: str | None = None
    error_message: str | None = None
    traceback_short: str | None = None


class _CapturingClassifier:
    def __init__(
        self,
        classifier: EntityInferencePort,
        *,
        row: EvalRow,
        retry_count: int,
        eval_run_id: str | None,
        call_debug_logger: CallDebugLogger | None,
    ):
        self._classifier = classifier
        self._row = row
        self._retry_count = retry_count
        self._eval_run_id = eval_run_id
        self._call_debug_logger = call_debug_logger
        self.model = getattr(classifier, "model", None)
        self.prompt_version = getattr(classifier, "prompt_version", None)
        self.prompt_fingerprint = getattr(classifier, "prompt_fingerprint", None)
        self.classification_output: ClassificationResult | None = None
        self.extraction_output: BaseModel | None = None
        self.classification_time_ms: int | None = None
        self.extract_time_ms: int | None = None
        self.classification_usage: dict[str, Any] | None = None
        self.extraction_usage: dict[str, Any] | None = None

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        call_id = await self._log_call_start("classify")
        started_at = perf_counter()
        with _capture_parse_usage(self._classifier) as usage_sink:
            try:
                result = await self._classifier.classify(envelope)
                await self._log_call_finish(
                    call_id=call_id,
                    operation="classify",
                    elapsed_ms=int((perf_counter() - started_at) * 1000),
                    status="success",
                    usage=usage_sink.get("usage"),
                )
            except Exception as exc:
                await self._log_call_finish(
                    call_id=call_id,
                    operation="classify",
                    elapsed_ms=int((perf_counter() - started_at) * 1000),
                    status="error",
                    usage=usage_sink.get("usage"),
                    error_type=_error_type(exc),
                    error_message=str(exc),
                )
                raise
            finally:
                self.classification_time_ms = int((perf_counter() - started_at) * 1000)
                self.classification_usage = usage_sink.get("usage")
        self.classification_output = result
        return result

    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> Any:
        call_id = await self._log_call_start("extract")
        started_at = perf_counter()
        with _capture_parse_usage(self._classifier) as usage_sink:
            try:
                result = await self._classifier.extract_fields(
                    envelope, classified_type
                )
                await self._log_call_finish(
                    call_id=call_id,
                    operation="extract",
                    elapsed_ms=int((perf_counter() - started_at) * 1000),
                    status="success",
                    usage=usage_sink.get("usage"),
                )
            except Exception as exc:
                await self._log_call_finish(
                    call_id=call_id,
                    operation="extract",
                    elapsed_ms=int((perf_counter() - started_at) * 1000),
                    status="error",
                    usage=usage_sink.get("usage"),
                    error_type=_error_type(exc),
                    error_message=str(exc),
                )
                raise
            finally:
                self.extract_time_ms = int((perf_counter() - started_at) * 1000)
                self.extraction_usage = usage_sink.get("usage")
        if isinstance(result, BaseModel):
            self.extraction_output = result
        return result

    async def _log_call_start(self, operation: str) -> int | None:
        if self._call_debug_logger is None or self._eval_run_id is None:
            return None
        return await self._call_debug_logger.log_start(
            eval_run_id=self._eval_run_id,
            row=self._row,
            operation=operation,
            retry_count=self._retry_count,
            model=self.model,
            prompt_version=self.prompt_version,
            prompt_fingerprint=self.prompt_fingerprint,
        )

    async def _log_call_finish(
        self,
        *,
        call_id: int | None,
        operation: str,
        elapsed_ms: int,
        status: str,
        usage: dict[str, Any] | None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if (
            self._call_debug_logger is None
            or self._eval_run_id is None
            or call_id is None
        ):
            return
        await self._call_debug_logger.log_finish(
            eval_run_id=self._eval_run_id,
            call_id=call_id,
            row=self._row,
            operation=operation,
            retry_count=self._retry_count,
            elapsed_ms=elapsed_ms,
            model=self.model,
            prompt_version=self.prompt_version,
            prompt_fingerprint=self.prompt_fingerprint,
            status=status,
            error_type=error_type,
            error_message=error_message,
            usage=usage,
        )


class EvalRunner:
    def __init__(
        self,
        classifier: EntityInferencePort,
        *,
        concurrency: int,
        timeout_seconds: float,
        retry_count: int,
        max_errors: int | None,
        continue_on_error: bool,
        eval_run_id: str | None = None,
        call_debug_logger: CallDebugLogger | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if retry_count < 0:
            raise ValueError("retry_count must be >= 0")
        if max_errors is not None and max_errors < 0:
            raise ValueError("max_errors must be >= 0")
        self.classifier = classifier
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.retry_count = retry_count
        self.max_errors = max_errors
        self.continue_on_error = continue_on_error
        self.eval_run_id = eval_run_id
        self.call_debug_logger = call_debug_logger

    async def run_rows(self, rows: list[EvalRow]) -> list[PipelineRowResult]:
        semaphore = asyncio.Semaphore(self.concurrency)
        abort_event = asyncio.Event()
        error_lock = asyncio.Lock()
        error_count = 0

        async def run_one(row: EvalRow) -> PipelineRowResult:
            nonlocal error_count
            if abort_event.is_set():
                return _skipped_result(row, "error limit reached before row started")
            async with semaphore:
                if abort_event.is_set():
                    return _skipped_result(
                        row, "error limit reached before row started"
                    )
                result = await self._run_with_retries(row)
                if result.status == "system_error":
                    async with error_lock:
                        error_count += 1
                        if (
                            self.max_errors is not None
                            and error_count > self.max_errors
                            and not self.continue_on_error
                        ):
                            abort_event.set()
                return result

        return list(await asyncio.gather(*(run_one(row) for row in rows)))

    async def _run_with_retries(self, row: EvalRow) -> PipelineRowResult:
        last_error: BaseException | None = None
        for attempt in range(self.retry_count + 1):
            envelope = build_envelope_for_row(row)
            capturing_classifier = _CapturingClassifier(
                self.classifier,
                row=row,
                retry_count=attempt,
                eval_run_id=self.eval_run_id,
                call_debug_logger=self.call_debug_logger,
            )
            orchestrator = ClassificationOrchestrator(capturing_classifier)
            started_at = perf_counter()
            try:
                result = await asyncio.wait_for(
                    self._run_orchestrator_for_row(orchestrator, envelope, row),
                    timeout=self.timeout_seconds,
                )
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                return PipelineRowResult(
                    row=row,
                    status="completed",
                    envelope=result,
                    classification_output=_dump_model(
                        capturing_classifier.classification_output
                    ),
                    extraction_output=_dump_model(
                        capturing_classifier.extraction_output
                    ),
                    processing_time_ms=elapsed_ms,
                    classification_time_ms=capturing_classifier.classification_time_ms,
                    extract_time_ms=capturing_classifier.extract_time_ms,
                    classification_usage=capturing_classifier.classification_usage,
                    extraction_usage=capturing_classifier.extraction_usage,
                    prompt_version=capturing_classifier.prompt_version,
                    prompt_fingerprint=capturing_classifier.prompt_fingerprint,
                    retry_count=attempt,
                )
            except Exception as exc:  # noqa: BLE001 - row-level capture is required.
                last_error = exc
                if attempt < self.retry_count:
                    continue
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                return PipelineRowResult(
                    row=row,
                    status="system_error",
                    envelope=envelope,
                    classification_output=_dump_model(
                        capturing_classifier.classification_output
                    ),
                    extraction_output=_dump_model(
                        capturing_classifier.extraction_output
                    ),
                    processing_time_ms=elapsed_ms,
                    classification_time_ms=capturing_classifier.classification_time_ms,
                    extract_time_ms=capturing_classifier.extract_time_ms,
                    classification_usage=capturing_classifier.classification_usage,
                    extraction_usage=capturing_classifier.extraction_usage,
                    prompt_version=capturing_classifier.prompt_version,
                    prompt_fingerprint=capturing_classifier.prompt_fingerprint,
                    retry_count=attempt,
                    error_type=_error_type(exc),
                    error_message=str(exc),
                    traceback_short=_traceback_short(exc),
                )
        raise RuntimeError(f"unreachable retry state after {last_error!r}")

    async def _run_orchestrator_for_row(
        self,
        orchestrator: ClassificationOrchestrator,
        envelope: ProcessingEnvelope,
        row: EvalRow,
    ) -> ProcessingEnvelope:
        if row.dataset_name == "classification":
            classified_envelope, _ = await orchestrator.classify(envelope)
            return classified_envelope
        if not row.gold_fields:
            return envelope
        extracted_envelope, _ = await orchestrator.extract(
            envelope,
            row.gold_entity_type,
        )
        return extracted_envelope


def build_envelope_for_row(row: EvalRow) -> ProcessingEnvelope:
    ingested_id = uuid5(NAMESPACE_URL, f"endpoint-eval:{row.dataset_name}:{row.row_id}")
    payload: dict[str, Any] = {
        "content_type": "raw_message",
        "title": None,
        "body": row.body,
        "course_name": row.course_name,
        "published_at": row.published_at,
        "deadline": None,
        "submission_format": None,
        "start_time": None,
        "end_time": None,
        "location": None,
        "attachments": [],
        "links": [],
    }
    ingested = IngestedItemContext(
        id=ingested_id,
        external_id=row.external_id or row.row_id,
        platform=row.platform,
        course_external_id=None,
        schema_version="eval-v1",
        adapter_version="eval-v1",
        ingestion_mode="evaluation",
        processing_status="pending",
        source_url=None,
        payload=payload,
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=RawMessage(title=None, body=row.body, author=None),
    )


def _dump_model(model: BaseModel | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return model.model_dump(mode="json")


@contextmanager
def _capture_parse_usage(classifier: EntityInferencePort):
    target = _completion_target(classifier)
    parse = getattr(target, "parse", None) if target is not None else None
    if target is None or parse is None:
        yield {}
        return

    sink: dict[str, Any] = {}

    async def wrapped_parse(*args, **kwargs):
        completion = await parse(*args, **kwargs)
        sink["usage"] = _dump_usage(getattr(completion, "usage", None))
        return completion

    target.parse = wrapped_parse
    try:
        yield sink
    finally:
        target.parse = parse


def _completion_target(classifier: EntityInferencePort) -> Any | None:
    client = getattr(classifier, "client", None)
    beta = getattr(client, "beta", None)
    chat = getattr(beta, "chat", None)
    return getattr(chat, "completions", None)


def _dump_usage(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump(mode="json", exclude_none=True)

    keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
    )
    dumped = {
        key: getattr(usage, key)
        for key in keys
        if hasattr(usage, key) and getattr(usage, key) is not None
    }
    return dumped or None


def _error_type(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__


def _traceback_short(exc: BaseException) -> str:
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__, limit=6)
    ).strip()


def _skipped_result(row: EvalRow, reason: str) -> PipelineRowResult:
    return PipelineRowResult(
        row=row,
        status="skipped",
        retry_count=0,
        error_type="skipped",
        error_message=reason,
        traceback_short=None,
    )
