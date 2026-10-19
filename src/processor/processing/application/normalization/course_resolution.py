from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from Levenshtein import ratio as levenshtein_ratio

from processing.config import ProcessingConfig
from processing.exceptions import CourseResolutionError
from processing.models.course import Course, CourseMatch
from processing.models.envelope import ProcessingEnvelope
from processing.ports.repository import EntityRepositoryPort

SEMESTER_PREFIX_RE = re.compile(r"^(?P<semester>[FS]\d{2})(?:[-_ ]+)?", re.IGNORECASE)
COURSE_CODE_RE = re.compile(
    r"(?<![A-Z0-9])"
    r"(?P<prefix>[A-Z]{2,6})"
    r"(?P<numeric>(?:[-_]\d{1,6})+)"
    r"(?P<section>[-_][A-Z](?=$|[^A-Z0-9]))?"
    r"(?=$|[^A-Z0-9])",
    re.IGNORECASE,
)
TERM_PREFIX_RE = re.compile(r"^[A-Z]\d{2}\s+", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedCourseName:
    semester: str | None
    code: str | None
    class_section: str | None
    title: str


class CourseResolver:
    """Resolves canonical course identity from the ingested payload."""

    def __init__(self, repository: EntityRepositoryPort, config: ProcessingConfig):
        self.repository = repository
        self.config = config
        self._course_cache: list[Course] | None = None
        self._course_lock = threading.Lock()

    def load_courses(self) -> list[Course]:
        if self._course_cache is None:
            self._course_cache = self.repository.get_all_courses()
        return self._course_cache

    def invalidate_course_cache(self) -> None:
        self._course_cache = None

    def resolve_course(self, envelope: ProcessingEnvelope) -> CourseMatch:
        with self._course_lock:
            return self._resolve_course_locked(envelope)

    def _resolve_course_locked(self, envelope: ProcessingEnvelope) -> CourseMatch:
        platform = envelope.ingested_item.platform
        external_course_id = envelope.ingested_item.course_external_id
        if platform and external_course_id:
            mapped_course = self.repository.get_course_by_platform_mapping(
                platform, external_course_id
            )
            if mapped_course is not None:
                return CourseMatch(
                    course_id=mapped_course.id,
                    matched_by="platform_mapping",
                    course_code=mapped_course.code,
                    course_name=mapped_course.name,
                )

        raw_course_name = envelope.ingested_item.payload.get("course_name")
        if not raw_course_name or not raw_course_name.strip():
            raise CourseResolutionError(
                "Cannot resolve course without payload.course_name"
            )

        extracted_code = self.extract_course_code(raw_course_name)
        courses = self.load_courses()
        normalized_title = self._normalize_course_name(raw_course_name, extracted_code)

        if extracted_code:
            # Code present: exact code match first.
            exact_match = self._find_exact_code_match(courses, extracted_code)
            if exact_match is not None:
                match = CourseMatch(
                    course_id=exact_match.id,
                    matched_by="code_exact",
                    course_code=exact_match.code,
                    course_name=exact_match.name,
                )
                self._record_platform_mapping(envelope, exact_match.id, raw_course_name)
                return match

            # Code mismatch: title-match only against code-less courses, then backfill.
            title_match = self._find_title_match(
                courses,
                normalized_title,
                include_coded=False,
                min_score=self._strict_title_match_threshold(),
            )
            if title_match is not None:
                self.repository.update_course_code(title_match.id, extracted_code)
                self.invalidate_course_cache()
                match = CourseMatch(
                    course_id=title_match.id,
                    matched_by="title_levenshtein",
                    course_code=extracted_code,
                    course_name=title_match.name,
                )
                self._record_platform_mapping(envelope, title_match.id, raw_course_name)
                return match
        else:
            # No code: strict title-match against all courses.
            title_match = self._find_title_match(
                courses,
                normalized_title,
                include_coded=True,
                min_score=self._strict_title_match_threshold(),
            )
            if title_match is not None:
                match = CourseMatch(
                    course_id=title_match.id,
                    matched_by="title_levenshtein",
                    course_code=title_match.code,
                    course_name=title_match.name,
                )
                self._record_platform_mapping(envelope, title_match.id, raw_course_name)
                return match

        # No match: create a new course.
        created = self.repository.create_course(
            name=normalized_title, code=extracted_code
        )
        self.invalidate_course_cache()
        self._record_platform_mapping(envelope, created.id, raw_course_name)
        return CourseMatch(
            course_id=created.id,
            matched_by="created_new",
            course_code=created.code,
            course_name=created.name,
        )

    def extract_course_code(self, text: str) -> str | None:
        match = COURSE_CODE_RE.search(text.replace("–", "-"))
        if match is None:
            return None
        return self._normalize_base_code(match.group("prefix"), match.group("numeric"))

    def _find_exact_code_match(
        self, courses: list[Course], extracted_code: str
    ) -> Course | None:
        for course in courses:
            course_code = self.extract_course_code(course.code) if course.code else None
            if course_code == extracted_code:
                return course
        return None

    def _find_title_match(
        self,
        courses: list[Course],
        normalized_title: str,
        *,
        include_coded: bool,
        min_score: float,
    ) -> Course | None:
        candidates = (
            courses
            if include_coded
            else [course for course in courses if course.code is None]
        )
        if not candidates:
            return None

        best_course: Course | None = None
        best_score = 0.0
        normalized_target = normalized_title.casefold()
        for course in candidates:
            score = levenshtein_ratio(
                normalized_target,
                self._normalize_course_name(course.name, course.code).casefold(),
            )
            if score >= min_score and score > best_score:
                best_course = course
                best_score = score
        return best_course

    def _strict_title_match_threshold(self) -> float:
        return self.config.course_title_match_threshold

    def _normalize_course_name(
        self, raw_course_name: str, extracted_code: str | None = None
    ) -> str:
        parsed = self._parse_course_name(raw_course_name)
        normalized = parsed.title or raw_course_name.replace("–", "-").strip()
        normalized = normalized.replace("_", " ")
        normalized = TERM_PREFIX_RE.sub("", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip(" -_")

    def _parse_course_name(self, raw_course_name: str) -> ParsedCourseName:
        normalized = raw_course_name.replace("–", "-").strip()
        semester: str | None = None

        semester_match = SEMESTER_PREFIX_RE.match(normalized)
        if semester_match is not None:
            semester = semester_match.group("semester").upper()
            normalized = normalized[semester_match.end() :].lstrip(" -_")

        code_match = COURSE_CODE_RE.match(normalized)
        if code_match is None:
            return ParsedCourseName(
                semester=semester,
                code=None,
                class_section=None,
                title=self._clean_course_title(normalized),
            )

        title = normalized[code_match.end() :]
        class_section = code_match.group("section")
        return ParsedCourseName(
            semester=semester,
            code=self._normalize_base_code(
                code_match.group("prefix"),
                code_match.group("numeric"),
            ),
            class_section=class_section[1:].upper() if class_section else None,
            title=self._clean_course_title(title),
        )

    def _clean_course_title(self, title: str) -> str:
        normalized = title.replace("_", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip(" -_")

    def _normalize_base_code(self, prefix: str, numeric: str) -> str:
        normalized_numeric = numeric.replace("–", "-").replace("_", "-")
        return f"{prefix.upper()}{normalized_numeric.upper()}"

    def _record_platform_mapping(
        self,
        envelope: ProcessingEnvelope,
        course_id,
        raw_course_name: str | None,
    ) -> None:
        platform = envelope.ingested_item.platform
        external_course_id = envelope.ingested_item.course_external_id
        if not platform or not external_course_id:
            return
        self.repository.upsert_course_platform_mapping(
            course_id,
            platform,
            external_course_id,
            raw_course_name,
        )
