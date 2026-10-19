from __future__ import annotations

from typing import Protocol

from processing.models.classification import ClassificationResult, ExtractionPatch
from processing.models.envelope import ProcessingEnvelope


class EntityInferencePort(Protocol):
    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult: ...

    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> ExtractionPatch: ...


ClassifierPort = EntityInferencePort
