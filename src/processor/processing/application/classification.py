from __future__ import annotations

import logging
import re
from time import perf_counter

from processing.application.extraction_resolution import ExtractionResolutionService
from processing.models.classification import (
    ClassificationLogEntry,
    ClassificationResult,
    ExtractionPatch,
    LLMUsage,
)
from processing.models.envelope import ProcessingEnvelope
from processing.observability.llm_usage import LLMUsageCollector
from processing.ports.classifier import EntityInferencePort
from processing.service_metadata import PROMPT_VERSION

logger = logging.getLogger(__name__)

_SHORT_RAW_MESSAGE_CHAR_LIMIT = 40
_URL_PATTERN = re.compile(r"\b(?:https?://|www\.)", re.IGNORECASE)


class ClassificationOrchestrator:
    """Coordinate classification and field extraction for raw messages."""

    def __init__(
        self,
        classifier: EntityInferencePort,
        resolution_service: ExtractionResolutionService | None = None,
        usage_collector: LLMUsageCollector | None = None,
    ):
        self.classifier = classifier
        self.resolution_service = resolution_service or ExtractionResolutionService()
        self.usage_collector = (
            usage_collector
            or getattr(classifier, "usage_collector", None)
            or LLMUsageCollector()
        )

    async def classify(
        self, envelope: ProcessingEnvelope
    ) -> tuple[ProcessingEnvelope, ClassificationResult]:
        classification = await self.classifier.classify(envelope)
        logger.info(
            "Classified ingested item %s as %s (confidence %.2f)",
            envelope.ingested_item.id,
            classification.entity_type,
            classification.confidence,
        )
        return (
            envelope.model_copy(
                update={
                    "classification_log": self._classification_log(
                        envelope,
                        classification,
                        processing_time_ms=None,
                    ),
                }
            ),
            classification,
        )

    async def extract(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> tuple[ProcessingEnvelope, ExtractionPatch]:
        extraction = await self.classifier.extract_fields(envelope, classified_type)
        resolved = self.resolution_service.resolve(
            envelope,
            classified_type,
            extraction,
        )
        return (
            envelope.model_copy(
                update={
                    "entity": resolved.entity,
                    "extracted_links": resolved.extracted_links,
                    "extracted_attachments": resolved.extracted_attachments,
                }
            ),
            extraction,
        )

    async def classify_and_extract(
        self, envelope: ProcessingEnvelope
    ) -> ProcessingEnvelope:
        started_at = perf_counter()
        if self._should_skip_llm(envelope):
            processing_time_ms = int((perf_counter() - started_at) * 1000)
            return self._with_classification_observability(
                envelope.model_copy(
                    update={
                        "classification_log": self._classification_log(
                            envelope,
                            ClassificationResult(
                                entity_type="raw_message",
                                confidence=1.0,
                                reasoning="Short raw message without URL evidence.",
                            ),
                            processing_time_ms=None,
                            classification_method="rule_based",
                        ),
                    }
                ),
                processing_time_ms,
                LLMUsage(),
            )

        with self.usage_collector.capture():
            classified_envelope, classification = await self.classify(envelope)

            if classification.entity_type == "raw_message":
                processing_time_ms = int((perf_counter() - started_at) * 1000)
                usage = self.usage_collector.current_usage()
                return self._with_classification_observability(
                    classified_envelope,
                    processing_time_ms,
                    usage,
                )

            extracted_envelope, _ = await self.extract(
                classified_envelope,
                classification.entity_type,
            )
            usage = self.usage_collector.current_usage()

        processing_time_ms = int((perf_counter() - started_at) * 1000)
        return self._with_classification_observability(
            extracted_envelope,
            processing_time_ms,
            usage,
        )

    def _classification_log(
        self,
        envelope: ProcessingEnvelope,
        classification: ClassificationResult,
        *,
        processing_time_ms: int | None,
        classification_method: str = "llm",
    ) -> ClassificationLogEntry:
        return ClassificationLogEntry(
            ingested_item_id=envelope.ingested_item.id,
            entity_type=classification.entity_type,
            classification_method=classification_method,
            llm_model=(
                getattr(self.classifier, "model", None)
                if classification_method == "llm"
                else None
            ),
            confidence=classification.confidence,
            processing_time_ms=processing_time_ms,
            prompt_version=(
                getattr(self.classifier, "prompt_version", PROMPT_VERSION)
                if classification_method == "llm"
                else None
            ),
        )

    def _should_skip_llm(self, envelope: ProcessingEnvelope) -> bool:
        body = envelope.entity.body or ""
        return (
            len(body.strip()) < _SHORT_RAW_MESSAGE_CHAR_LIMIT
            and _URL_PATTERN.search(body) is None
            and not any(link.url for link in envelope.extracted_links)
        )

    def _with_classification_observability(
        self,
        envelope: ProcessingEnvelope,
        processing_time_ms: int,
        usage: LLMUsage,
    ) -> ProcessingEnvelope:
        if envelope.classification_log is None:
            return envelope
        return envelope.model_copy(
            update={
                "classification_log": envelope.classification_log.model_copy(
                    update={
                        "processing_time_ms": processing_time_ms,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    }
                )
            }
        )
