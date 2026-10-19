from __future__ import annotations

from uuid import uuid4

import pytest

from processing.config import ProcessingConfig
from processing.application.normalization import NormalizationService
from processing.application.normalization.course_resolution import CourseResolver
from processing.application.normalization.text_normalization import TextNormalizer
from processing.exceptions import CourseResolutionError
from processing.models.classification import ClassificationLogEntry
from processing.models.course import Course
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import Assignment, RawMessage
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)


class FakeRepository:
    def __init__(self, courses: list[Course] | None = None):
        self.courses = list(courses or [])
        self.get_all_courses_calls = 0
        self.created_courses: list[Course] = []
        self.updated_codes: list[tuple] = []
        self.course_mappings: dict[tuple[str, str], Course] = {}
        self.recorded_mappings: list[tuple] = []

    def get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]:
        raise NotImplementedError

    def persist_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        raise NotImplementedError

    def persist_unclassified_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        raise NotImplementedError

    def get_existing_canonicals(self, course_id, entity_type: str):
        return []

    def save_entity(self, envelope: ProcessingEnvelope):
        raise NotImplementedError

    def mark_processed(self, ingested_item_id):
        raise NotImplementedError

    def mark_failed(self, ingested_item_id, error: str):
        raise NotImplementedError

    def save_links(self, classified_item_id, links: list[ExtractedLink]) -> None:
        raise NotImplementedError

    def save_attachments(
        self,
        classified_item_id,
        attachments: list[ExtractedAttachment],
    ) -> None:
        raise NotImplementedError

    def save_classification_log(self, log: ClassificationLogEntry) -> None:
        raise NotImplementedError

    def get_all_courses(self) -> list[Course]:
        self.get_all_courses_calls += 1
        return list(self.courses)

    def create_course(self, name: str, code: str | None = None) -> Course:
        course = Course(id=uuid4(), name=name, code=code)
        self.courses.append(course)
        self.created_courses.append(course)
        return course

    def update_course_code(self, course_id, code: str) -> None:
        self.updated_codes.append((course_id, code))
        self.courses = [
            course.model_copy(update={"code": code})
            if course.id == course_id
            else course
            for course in self.courses
        ]

    def get_course_by_platform_mapping(
        self, platform: str, external_course_id: str
    ) -> Course | None:
        return self.course_mappings.get((platform, external_course_id))

    def upsert_course_platform_mapping(
        self,
        course_id,
        platform: str,
        external_course_id: str,
        external_course_name: str | None,
    ) -> None:
        self.recorded_mappings.append(
            (course_id, platform, external_course_id, external_course_name)
        )


def _make_envelope(
    course_name: str | None, body: str | None = None
) -> ProcessingEnvelope:
    ingested = IngestedItemContext(
        id=uuid4(),
        external_id="teams_msg_1",
        platform="teams",
        course_external_id=None,
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url=None,
        payload={
            "content_type": "raw_message",
            "title": None,
            "author": None,
            "body": body,
            "course_name": course_name,
            "published_at": None,
            "deadline": None,
            "submission_format": None,
            "start_time": None,
            "end_time": None,
            "location": None,
            "attachments": [],
            "links": [],
        },
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=RawMessage(body=body),
    )


def _make_assignment_envelope(
    *,
    title: str | None = "  Homework   1  ",
    body: str | None = "Hello\r\n\r\n\r\n<style>.c0{color:red;}</style>World\u00a0",
    author: str | None = "  Ada   Lovelace  ",
) -> ProcessingEnvelope:
    base = _make_envelope(
        "SYNTHETIC EXAMPLE COURSE",
        body=body,
    )
    payload = {**base.ingested_item.payload, "content_type": "assignment"}
    return base.model_copy(
        update={
            "ingested_item": base.ingested_item.model_copy(update={"payload": payload}),
            "entity": Assignment(title=title, body=body, author=author),
        }
    )


def test_extract_course_code_returns_first_normalized_match():
    service = CourseResolver(FakeRepository(), ProcessingConfig())

    code = service.extract_course_code("See CH–230-A and then MA-101 in the portal.")

    assert code == "EX-101"


def test_extract_course_code_keeps_numeric_third_segment_and_skips_class_suffix():
    service = CourseResolver(FakeRepository(), ProcessingConfig())

    assert (
        service.extract_course_code("F25_CA-999-0_CSC - Info Session CSC Services")
        == "CA-999-0"
    )
    assert (
        service.extract_course_code("F25_CH_230_A_Programming in C and C++") == "EX-101"
    )


def test_normalize_title_trims_and_collapses_whitespace():
    service = TextNormalizer()

    assert service.normalize_title("  Homework   1 \n ") == "Homework 1"
    assert service.normalize_title(" \n\t ") is None


def test_normalize_author_only_trims_and_collapses_whitespace():
    service = TextNormalizer()

    assert service.normalize_author("Schönwälder, Jürgen") == "Schönwälder, Jürgen"
    assert service.normalize_author("  Ada   Lovelace  ") == "Ada Lovelace"
    assert service.normalize_author(" \n\t ") is None


def test_normalize_body_only_applies_generic_text_cleanup():
    service = TextNormalizer()

    body = (
        " Hello\r\n\r\n\r\n"
        "<style>.c0{color:red;}</style>\r\n"
        ".c1{font-size:12px;}\n"
        "World\u00a0\n\n\nBye "
    )

    assert service.normalize_body(body) == (
        "Hello\n\n<style>.c0{color:red;}</style>\n.c1{font-size:12px;}\nWorld\n\nBye"
    )


def test_normalize_course_name_preserves_short_title_prefix_after_code():
    service = CourseResolver(FakeRepository(), ProcessingConfig())

    normalized = service._normalize_course_name("F25_CH_230_A_C Programming", "EX-101")

    assert normalized == "C Programming"


def test_normalize_envelope_applies_only_generic_text_cleanup():
    service = TextNormalizer()
    envelope = _make_assignment_envelope()
    envelope = envelope.model_copy(
        update={
            "resolved_course_id": uuid4(),
            "extracted_links": [
                ExtractedLink(url="https://flightproxy.teams.microsoft.com/foo"),
                ExtractedLink(url="https://example.edu/course"),
            ],
            "extracted_attachments": [
                ExtractedAttachment(filename="attachment", mime_type=None, url=None),
                ExtractedAttachment(
                    filename="homework1.pdf",
                    mime_type="#microsoft.education.assignments.api.educationFileResource",
                    url="https://example.com/homework1.pdf",
                ),
            ],
        }
    )

    result = service.normalize_envelope(envelope)

    assert result.entity.title == "Homework 1"
    assert result.entity.body == "Hello\n\n<style>.c0{color:red;}</style>World"
    assert result.entity.author == "Ada Lovelace"
    assert result.extracted_links == [
        ExtractedLink(url="https://flightproxy.teams.microsoft.com/foo"),
        ExtractedLink(url="https://example.edu/course"),
    ]
    assert result.extracted_attachments == [
        ExtractedAttachment(filename="attachment", mime_type=None, url=None),
        ExtractedAttachment(
            filename="homework1.pdf",
            mime_type="#microsoft.education.assignments.api.educationFileResource",
            url="https://example.com/homework1.pdf",
        ),
    ]
    assert not hasattr(result, "dedup_key")


def test_load_courses_caches_until_invalidated():
    repository = FakeRepository(
        courses=[Course(id=uuid4(), name="Programming in C and C++", code="EX-101-A")]
    )
    service = NormalizationService(repository, ProcessingConfig())

    service.load_courses()
    service.load_courses()
    service.invalidate_course_cache()
    service.load_courses()

    assert repository.get_all_courses_calls == 2


def test_resolve_course_prefers_exact_code_match():
    coded_course = Course(
        id=uuid4(),
        name="Programming in C and C++",
        code="EX-101-A",
    )
    repository = FakeRepository(courses=[coded_course])
    service = NormalizationService(repository, ProcessingConfig())

    match = service.resolve_course(
        _make_envelope("SYNTHETIC EXAMPLE COURSE")
    )

    assert match.course_id == coded_course.id
    assert match.matched_by == "code_exact"
    assert match.course_code == "EX-101-A"
    assert match.course_name == "Programming in C and C++"
    assert repository.created_courses == []


def test_resolve_course_prefers_platform_mapping_before_name():
    mapped_course = Course(
        id=uuid4(),
        name="Programming in C and C++",
        code="EX-101-A",
    )
    repository = FakeRepository(courses=[mapped_course])
    repository.course_mappings[("moodle", "1000")] = mapped_course
    service = NormalizationService(repository, ProcessingConfig())
    envelope = _make_envelope(None)
    envelope = envelope.model_copy(
        update={
            "ingested_item": envelope.ingested_item.model_copy(
                update={"platform": "moodle", "course_external_id": "1000"}
            )
        }
    )

    match = service.resolve_course(envelope)

    assert match.course_id == mapped_course.id
    assert match.matched_by == "platform_mapping"
    assert match.course_code == "EX-101-A"
    assert match.course_name == "Programming in C and C++"
    assert repository.created_courses == []
    assert repository.recorded_mappings == []


def test_resolve_course_title_match_backfills_code_for_code_less_course():
    title_only_course = Course(
        id=uuid4(),
        name="Programming in C and C++",
        code=None,
    )
    repository = FakeRepository(courses=[title_only_course])
    service = NormalizationService(repository, ProcessingConfig())

    match = service.resolve_course(
        _make_envelope("SYNTHETIC EXAMPLE COURSE")
    )

    assert match.course_id == title_only_course.id
    assert match.matched_by == "title_levenshtein"
    assert match.course_code == "EX-101"
    assert match.course_name == "Programming in C and C++"
    assert repository.updated_codes == [(title_only_course.id, "EX-101")]


def test_resolve_course_matches_bare_title_to_existing_coded_course():
    coded_course = Course(
        id=uuid4(),
        name="Linear Algebra",
        code="CH-151-A",
    )
    repository = FakeRepository(courses=[coded_course])
    service = NormalizationService(repository, ProcessingConfig())

    match = service.resolve_course(_make_envelope("Linear Algebra"))

    assert match.course_id == coded_course.id
    assert match.matched_by == "title_levenshtein"
    assert match.course_code == "CH-151-A"
    assert match.course_name == "Linear Algebra"
    assert repository.created_courses == []


def test_resolve_course_backfills_code_when_title_matches_code_less_course():
    title_only_course = Course(
        id=uuid4(),
        name="Linear Algebra",
        code=None,
    )
    repository = FakeRepository(courses=[title_only_course])
    service = NormalizationService(repository, ProcessingConfig())

    match = service.resolve_course(_make_envelope("F25_CH-151-A_Linear Algebra"))

    assert match.course_id == title_only_course.id
    assert match.matched_by == "title_levenshtein"
    assert match.course_code == "CH-151"
    assert match.course_name == "Linear Algebra"
    assert repository.updated_codes == [(title_only_course.id, "CH-151")]
    assert repository.created_courses == []


def test_resolve_course_does_not_title_match_coded_course_when_base_code_mismatches():
    coded_course = Course(
        id=uuid4(),
        name="Linear Algebra",
        code="CH-152-B",
    )
    repository = FakeRepository(courses=[coded_course])
    service = NormalizationService(repository, ProcessingConfig())

    match = service.resolve_course(_make_envelope("F25_CH-151-A_Linear Algebra"))

    created_course = repository.created_courses[0]
    assert match.course_id == created_course.id
    assert match.matched_by == "created_new"
    assert match.course_code == "CH-151"
    assert match.course_name == "Linear Algebra"
    assert repository.updated_codes == []


def test_resolve_course_matches_existing_coded_course_when_only_class_suffix_differs():
    coded_course = Course(
        id=uuid4(),
        name="Programming in C and C++ Tutorial",
        code="EX-101-B",
    )
    repository = FakeRepository(courses=[coded_course])
    service = NormalizationService(repository, ProcessingConfig())

    match = service.resolve_course(
        _make_envelope("SYNTHETIC EXAMPLE COURSE")
    )

    assert match.course_id == coded_course.id
    assert match.matched_by == "code_exact"
    assert match.course_code == "EX-101-B"
    assert match.course_name == "Programming in C and C++ Tutorial"
    assert repository.created_courses == []


def test_resolve_course_creates_course_when_no_match_exists():
    repository = FakeRepository(
        courses=[Course(id=uuid4(), name="Algorithms", code="CS-101")]
    )
    service = NormalizationService(repository, ProcessingConfig())

    match = service.resolve_course(
        _make_envelope("SYNTHETIC EXAMPLE COURSE")
    )

    created_course = repository.created_courses[0]
    assert match.course_id == created_course.id
    assert match.matched_by == "created_new"
    assert match.course_code == "EX-101"
    assert match.course_name == "Programming in C and C++"
    assert created_course.code == "EX-101"


def test_resolve_course_creates_clean_title_for_underscore_separated_code():
    repository = FakeRepository()
    service = NormalizationService(repository, ProcessingConfig())

    match = service.resolve_course(_make_envelope("SYNTHETIC EXAMPLE COURSE"))

    created_course = repository.created_courses[0]
    assert match.matched_by == "created_new"
    assert match.course_code == "SAT-02"
    assert match.course_name == "SAT Training - English"
    assert created_course.code == "SAT-02"
    assert created_course.name == "SAT Training - English"


def test_resolve_course_records_platform_mapping_after_name_based_resolution():
    coded_course = Course(
        id=uuid4(),
        name="Programming in C and C++",
        code="EX-101-A",
    )
    repository = FakeRepository(courses=[coded_course])
    service = NormalizationService(repository, ProcessingConfig())
    envelope = _make_envelope("SYNTHETIC EXAMPLE COURSE")
    envelope = envelope.model_copy(
        update={
            "ingested_item": envelope.ingested_item.model_copy(
                update={"platform": "moodle", "course_external_id": "1000"}
            )
        }
    )

    match = service.resolve_course(envelope)

    assert match.course_id == coded_course.id
    assert repository.recorded_mappings == [
        (coded_course.id, "moodle", "1000", "SYNTHETIC EXAMPLE COURSE")
    ]


def test_resolve_course_raises_for_missing_course_name():
    service = NormalizationService(FakeRepository(), ProcessingConfig())

    with pytest.raises(CourseResolutionError):
        service.resolve_course(_make_envelope(None))


def test_resolve_course_raises_for_missing_course_name_without_mapping():
    service = NormalizationService(FakeRepository(), ProcessingConfig())
    envelope = _make_envelope(None)
    envelope = envelope.model_copy(
        update={
            "ingested_item": envelope.ingested_item.model_copy(
                update={"platform": "moodle", "course_external_id": "1000"}
            )
        }
    )

    with pytest.raises(CourseResolutionError):
        service.resolve_course(envelope)
