from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from processing.config import ProcessingConfig
from processing.application.classification import ClassificationOrchestrator
from processing.application.extraction_resolution import ExtractionResolutionService
from processing.application.normalization import NormalizationService
from processing.exceptions import RowScopedPersistenceError, TransientPersistenceError
from processing.models.envelope import ProcessingEnvelope
from processing.models.invariants import check_envelope
from processing.ports.classifier import EntityInferencePort
from processing.ports.repository import EntityRepositoryPort

logger = logging.getLogger(__name__)


class ProcessingPipeline:
    """Main pipeline orchestrator."""

    def __init__(
        self,
        repository: EntityRepositoryPort,
        classifier: EntityInferencePort,
        config: ProcessingConfig,
        dedup_service=None,
    ):
        self.repository = repository
        self.classifier = classifier
        self.config = config
        self.dedup_service = dedup_service
        self.normalization = NormalizationService(repository, config)
        self.classification = ClassificationOrchestrator(
            classifier,
            ExtractionResolutionService(config.university_timezone),
        )

    def run_loop(self) -> None:
        asyncio.run(self._run_loop_without_signal_handlers())

    async def _run_loop_without_signal_handlers(self) -> None:
        await self.run_until_shutdown(asyncio.Event())

    async def run_until_shutdown(self, shutdown: asyncio.Event) -> None:
        await self._async_loop(shutdown)

    def process_item(self, envelope: ProcessingEnvelope) -> None:
        """Sync compatibility wrapper used by tests and manual one-off runs."""
        try:
            logger.info("Processing ingested item %s", envelope.ingested_item.id)
            processed_envelope, error = asyncio.run(self._process_item(envelope))
            if error is not None:
                self._log_item_error(
                    processed_envelope,
                    error,
                    "Processing failed for ingested item %s",
                )
                asyncio.run(
                    self._mark_failed_safely(
                        processed_envelope.ingested_item.id, str(error)
                    )
                )
                return

            if processed_envelope.entity.entity_type == "raw_message":
                asyncio.run(self._persist_unclassified_batch([processed_envelope]))
                logger.info("Processed unclassified item %s", envelope.ingested_item.id)
                return

            deduplicated = asyncio.run(self._deduplicate_batch([processed_envelope]))
            persisted_ids = asyncio.run(self._persist_successes(deduplicated))
            if persisted_ids:
                logger.info("Processed ingested item %s", envelope.ingested_item.id)
            else:
                logger.warning(
                    "Leaving ingested item %s pending after persistence failure",
                    envelope.ingested_item.id,
                )
        except Exception as exc:
            logger.exception(
                "Unexpected pipeline failure for ingested item %s: %s",
                envelope.ingested_item.id,
                exc,
            )

    async def _async_loop(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            try:
                batch = await asyncio.to_thread(
                    self.repository.get_pending_batch, self.config.batch_size
                )
            except Exception as exc:
                logger.exception("Failed to fetch pending batch: %s", exc)
                if await self._wait_or_shutdown(shutdown):
                    break
                continue

            if not batch:
                logger.info(
                    "No pending items found; sleeping for %.1f seconds",
                    self.config.poll_interval_seconds,
                )
                if await self._wait_or_shutdown(shutdown):
                    break
                continue

            logger.info("Fetched %d pending items", len(batch))

            try:
                results = await self._process_batch(batch)
                successes = [envelope for envelope, error in results if error is None]
                failures = [
                    (envelope, error)
                    for envelope, error in results
                    if error is not None
                ]

                unclassified = [
                    e for e in successes if e.entity.entity_type == "raw_message"
                ]
                classified = [
                    e for e in successes if e.entity.entity_type != "raw_message"
                ]

                deduplicated = await self._deduplicate_batch(classified)
                persisted_ids = await self._persist_successes(deduplicated, shutdown)
                await self._persist_unclassified_batch(unclassified)
                for envelope, error in failures:
                    assert error is not None
                    self._log_item_error(
                        envelope,
                        error,
                        "Processing failed for ingested item %s",
                    )
                    await self._mark_failed_safely(
                        envelope.ingested_item.id, str(error), shutdown
                    )

                logger.info(
                    "Batch complete: %d persisted, %d unclassified, %d failed, %d still pending",
                    len(persisted_ids),
                    len(unclassified),
                    len(failures),
                    len(classified) - len(persisted_ids),
                )
            except Exception as exc:
                logger.exception("Batch crashed unexpectedly: %s", exc)
                if await self._wait_or_shutdown(shutdown):
                    break

        logger.info("Processor shutdown complete")

    async def _wait_or_shutdown(self, shutdown: asyncio.Event) -> bool:
        """Wait for the poll interval, waking early when shutdown is requested."""
        if shutdown.is_set():
            return True
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=self.config.poll_interval_seconds
            )
            return True
        except TimeoutError:
            return False

    async def _wait_retry_or_shutdown(
        self, shutdown: asyncio.Event | None, timeout: float
    ) -> bool:
        if shutdown is None:
            await asyncio.sleep(timeout)
            return False
        if shutdown.is_set():
            return True
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _process_batch(
        self, batch: list[ProcessingEnvelope]
    ) -> list[tuple[ProcessingEnvelope, Exception | None]]:
        semaphore = asyncio.Semaphore(self.config.max_concurrency)

        async def _run_with_semaphore(
            envelope: ProcessingEnvelope,
        ) -> tuple[ProcessingEnvelope, Exception | None]:
            async with semaphore:
                return await self._process_item(envelope)

        return await asyncio.gather(
            *[_run_with_semaphore(envelope) for envelope in batch]
        )

    async def _process_item(
        self, envelope: ProcessingEnvelope
    ) -> tuple[ProcessingEnvelope, Exception | None]:
        try:
            envelope = self.normalization.normalize_envelope(envelope)
            deterministically_classified = envelope.entity.entity_type != "raw_message"

            if envelope.entity.entity_type == "raw_message":
                envelope = await self.classification.classify_and_extract(envelope)

            if envelope.entity.entity_type == "raw_message":
                # LLM classified as raw_message — skip course resolution, dedup,
                # and invariant checks.
                return (envelope, None)

            match = await asyncio.to_thread(self.normalization.resolve_course, envelope)
            envelope = envelope.model_copy(
                update={"resolved_course_id": match.course_id}
            )

            if deterministically_classified:
                envelope = envelope.with_deterministic_classification_log()
            for warning in check_envelope(envelope):
                logger.warning(
                    "Item %s [%s] %s %s: %s",
                    envelope.ingested_item.id,
                    warning.severity.value,
                    warning.code.value,
                    warning.field,
                    warning.message,
                )
            return (envelope, None)
        except Exception as exc:
            return (envelope, exc)

    async def _deduplicate_batch(
        self, envelopes: list[ProcessingEnvelope]
    ) -> list[ProcessingEnvelope]:
        if not envelopes or self.dedup_service is None:
            return envelopes
        try:
            return await asyncio.to_thread(
                self.dedup_service.deduplicate_batch, envelopes
            )
        except Exception as exc:
            logger.exception(
                "Dedup failed for batch: %s; continuing without dedup decisions",
                exc,
            )
            return envelopes

    async def _persist_successes(
        self,
        envelopes: list[ProcessingEnvelope],
        shutdown: asyncio.Event | None = None,
    ) -> list[UUID]:
        if not envelopes:
            return []

        attempts = self.config.batch_persist_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(self.repository.persist_batch, envelopes)
                return [envelope.ingested_item.id for envelope in envelopes]
            except TransientPersistenceError as exc:
                if attempt >= attempts:
                    logger.exception(
                        "Batch persistence failed after %d attempts: %s",
                        attempts,
                        exc,
                    )
                    return []
                logger.warning(
                    "Batch persistence failed on attempt %d/%d: %s; retrying",
                    attempt,
                    attempts,
                    exc,
                    exc_info=True,
                )
                if await self._wait_retry_or_shutdown(
                    shutdown, self.config.batch_persist_retry_delay_seconds
                ):
                    return []
            except RowScopedPersistenceError as exc:
                logger.warning(
                    "Batch persistence hit a row-scoped failure: %s; falling back to per-item persistence",
                    exc,
                    exc_info=True,
                )
                return await self._persist_individually(envelopes)

        return []

    async def _persist_individually(
        self, envelopes: list[ProcessingEnvelope]
    ) -> list[UUID]:
        persisted_ids: list[UUID] = []
        for envelope in envelopes:
            try:
                await asyncio.to_thread(self.repository.persist_batch, [envelope])
            except Exception as exc:
                self._log_item_error(
                    envelope,
                    exc,
                    "Failed to persist ingested item %s during per-item fallback; leaving pending",
                )
                continue
            persisted_ids.append(envelope.ingested_item.id)
        return persisted_ids

    async def _persist_unclassified_batch(
        self, envelopes: list[ProcessingEnvelope]
    ) -> list[UUID]:
        if not envelopes:
            return []
        try:
            await asyncio.to_thread(
                self.repository.persist_unclassified_batch, envelopes
            )
            return [envelope.ingested_item.id for envelope in envelopes]
        except Exception as exc:
            logger.exception(
                "Failed to persist unclassified batch: %s; leaving items pending",
                exc,
            )
            return []

    async def _mark_failed_safely(
        self,
        ingested_item_id: UUID,
        error: str,
        shutdown: asyncio.Event | None = None,
    ) -> None:
        attempts = self.config.failure_mark_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                await asyncio.to_thread(
                    self.repository.mark_failed, ingested_item_id, error
                )
                return
            except Exception as exc:
                if attempt >= attempts:
                    logger.exception(
                        "Failed to mark ingested item %s as failed after %d attempts: %s",
                        ingested_item_id,
                        attempts,
                        exc,
                    )
                    return
                logger.warning(
                    "Failed to mark ingested item %s as failed on attempt %d/%d: %s; retrying",
                    ingested_item_id,
                    attempt,
                    attempts,
                    exc,
                    exc_info=True,
                )
                if await self._wait_retry_or_shutdown(
                    shutdown, self.config.failure_mark_retry_delay_seconds
                ):
                    return

    def _log_item_error(
        self,
        envelope: ProcessingEnvelope,
        error: Exception,
        message: str,
    ) -> None:
        logger.error(
            f"{message}: %s",
            envelope.ingested_item.id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
