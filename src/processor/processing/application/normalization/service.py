from __future__ import annotations

from processing.config import ProcessingConfig
from processing.application.normalization.course_resolution import CourseResolver
from processing.application.normalization.text_normalization import TextNormalizer
from processing.models.envelope import ProcessingEnvelope
from processing.ports.repository import EntityRepositoryPort


class NormalizationService:
    """Deterministic normalization logic for all envelopes."""

    def __init__(self, repository: EntityRepositoryPort, config: ProcessingConfig):
        self.course_resolver = CourseResolver(repository, config)
        self.text_normalizer = TextNormalizer()

    def normalize_body(self, body: str | None) -> str | None:
        return self.text_normalizer.normalize_body(body)

    def normalize_title(self, title: str | None) -> str | None:
        return self.text_normalizer.normalize_title(title)

    def normalize_author(self, author: str | None) -> str | None:
        return self.text_normalizer.normalize_author(author)

    def normalize_envelope(self, envelope: ProcessingEnvelope) -> ProcessingEnvelope:
        return self.text_normalizer.normalize_envelope(envelope)

    def load_courses(self):
        return self.course_resolver.load_courses()

    def invalidate_course_cache(self) -> None:
        self.course_resolver.invalidate_course_cache()

    def resolve_course(self, envelope: ProcessingEnvelope):
        return self.course_resolver.resolve_course(envelope)

    def extract_course_code(self, text: str) -> str | None:
        return self.course_resolver.extract_course_code(text)
