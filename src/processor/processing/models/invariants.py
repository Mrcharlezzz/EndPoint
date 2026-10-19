from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .entities import Assignment, ClassifiedBase, Event
from .envelope import ProcessingEnvelope
from .extracted import ExtractedAttachment, ExtractedLink


class Severity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class InvariantCode(str, Enum):
    NO_TITLE_OR_BODY = "no_title_or_body"
    EMPTY_TITLE = "empty_title"
    EMPTY_BODY = "empty_body"
    EMPTY_AUTHOR = "empty_author"
    TITLE_TOO_LONG = "title_too_long"
    AUTHOR_TOO_LONG = "author_too_long"
    DEADLINE_NOT_TIMEZONE_AWARE = "deadline_not_timezone_aware"
    DEADLINE_BEFORE_PUBLISHED_AT = "deadline_before_published_at"
    NO_START_TIME = "no_start_time"
    END_BEFORE_START = "end_before_start"
    START_TIME_NOT_TIMEZONE_AWARE = "start_time_not_timezone_aware"
    END_TIME_NOT_TIMEZONE_AWARE = "end_time_not_timezone_aware"
    URL_NOT_HTTP = "url_not_http"
    DUPLICATE_URL = "duplicate_url"
    GHOST_ATTACHMENT = "ghost_attachment"
    INVALID_MIME_TYPE = "invalid_mime_type"
    EMPTY_FILENAME = "empty_filename"
    NO_COURSE_RESOLVED = "no_course_resolved"
    NO_CLASSIFICATION_LOG = "no_classification_log"


@dataclass(frozen=True)
class InvariantWarning:
    code: InvariantCode
    field: str
    message: str
    severity: Severity


def check_envelope(envelope: ProcessingEnvelope) -> list[InvariantWarning]:
    warnings: list[InvariantWarning] = []
    warnings.extend(_check_base_fields(envelope.entity))

    if isinstance(envelope.entity, Assignment):
        warnings.extend(
            _check_assignment_dates(envelope.entity, envelope.ingested_item.payload)
        )
    elif isinstance(envelope.entity, Event):
        warnings.extend(_check_event_fields(envelope.entity))

    warnings.extend(_check_links(envelope.extracted_links))
    warnings.extend(_check_attachments(envelope.extracted_attachments))
    warnings.extend(_check_envelope_state(envelope))
    return warnings


def _warning(
    *,
    code: InvariantCode,
    field: str,
    message: str,
    severity: Severity,
) -> InvariantWarning:
    return InvariantWarning(
        code=code,
        field=field,
        message=message,
        severity=severity,
    )


def _check_base_fields(entity: ClassifiedBase) -> list[InvariantWarning]:
    warnings: list[InvariantWarning] = []
    if entity.title is None and entity.body is None:
        warnings.append(
            _warning(
                code=InvariantCode.NO_TITLE_OR_BODY,
                field="title,body",
                message="missing both title and body",
                severity=Severity.ERROR,
            )
        )
    if entity.title is not None and entity.title.strip() == "":
        warnings.append(
            _warning(
                code=InvariantCode.EMPTY_TITLE,
                field="title",
                message="title is empty after trimming",
                severity=Severity.WARNING,
            )
        )
    if entity.body is not None and entity.body.strip() == "":
        warnings.append(
            _warning(
                code=InvariantCode.EMPTY_BODY,
                field="body",
                message="body is empty after trimming",
                severity=Severity.WARNING,
            )
        )
    if entity.author is not None and entity.author.strip() == "":
        warnings.append(
            _warning(
                code=InvariantCode.EMPTY_AUTHOR,
                field="author",
                message="author is empty after trimming",
                severity=Severity.WARNING,
            )
        )
    if entity.title is not None and len(entity.title) > 500:
        warnings.append(
            _warning(
                code=InvariantCode.TITLE_TOO_LONG,
                field="title",
                message="title exceeds DB limit of 500 characters",
                severity=Severity.ERROR,
            )
        )
    if entity.author is not None and len(entity.author) > 255:
        warnings.append(
            _warning(
                code=InvariantCode.AUTHOR_TOO_LONG,
                field="author",
                message="author exceeds DB limit of 255 characters",
                severity=Severity.ERROR,
            )
        )
    return warnings


def _check_assignment_dates(
    entity: Assignment, payload: dict[str, object]
) -> list[InvariantWarning]:
    warnings: list[InvariantWarning] = []
    if entity.deadline is not None and not _is_timezone_aware(entity.deadline):
        warnings.append(
            _warning(
                code=InvariantCode.DEADLINE_NOT_TIMEZONE_AWARE,
                field="deadline",
                message="deadline must be timezone-aware",
                severity=Severity.ERROR,
            )
        )
    published_at = entity.published_at
    if (
        entity.deadline is not None
        and _is_timezone_aware(entity.deadline)
        and published_at is not None
        and entity.deadline < published_at
    ):
        warnings.append(
            _warning(
                code=InvariantCode.DEADLINE_BEFORE_PUBLISHED_AT,
                field="deadline",
                message="deadline is earlier than published_at",
                severity=Severity.WARNING,
            )
        )
    return warnings


def _check_event_fields(entity: Event) -> list[InvariantWarning]:
    warnings: list[InvariantWarning] = []
    if entity.start_time is None:
        warnings.append(
            _warning(
                code=InvariantCode.NO_START_TIME,
                field="start_time",
                message="event has no start_time",
                severity=Severity.WARNING,
            )
        )
    if (
        entity.end_time is not None
        and entity.start_time is not None
        and _is_timezone_aware(entity.end_time) == _is_timezone_aware(entity.start_time)
        and entity.end_time < entity.start_time
    ):
        warnings.append(
            _warning(
                code=InvariantCode.END_BEFORE_START,
                field="end_time",
                message="end_time is earlier than start_time",
                severity=Severity.ERROR,
            )
        )
    if entity.start_time is not None and not _is_timezone_aware(entity.start_time):
        warnings.append(
            _warning(
                code=InvariantCode.START_TIME_NOT_TIMEZONE_AWARE,
                field="start_time",
                message="start_time must be timezone-aware",
                severity=Severity.ERROR,
            )
        )
    if entity.end_time is not None and not _is_timezone_aware(entity.end_time):
        warnings.append(
            _warning(
                code=InvariantCode.END_TIME_NOT_TIMEZONE_AWARE,
                field="end_time",
                message="end_time must be timezone-aware",
                severity=Severity.ERROR,
            )
        )
    return warnings


def _check_links(links: list[ExtractedLink]) -> list[InvariantWarning]:
    warnings: list[InvariantWarning] = []
    seen: set[str] = set()
    duplicates_reported: set[str] = set()

    for link in links:
        if not link.url.startswith(("http://", "https://")):
            warnings.append(
                _warning(
                    code=InvariantCode.URL_NOT_HTTP,
                    field="url",
                    message="url does not use http or https",
                    severity=Severity.WARNING,
                )
            )
        if link.url in seen and link.url not in duplicates_reported:
            warnings.append(
                _warning(
                    code=InvariantCode.DUPLICATE_URL,
                    field="url",
                    message="duplicate url in extracted_links",
                    severity=Severity.WARNING,
                )
            )
            duplicates_reported.add(link.url)
        seen.add(link.url)
    return warnings


def _check_attachments(
    attachments: list[ExtractedAttachment],
) -> list[InvariantWarning]:
    warnings: list[InvariantWarning] = []
    for attachment in attachments:
        if (
            attachment.url is None
            and attachment.mime_type is None
            and attachment.filename.strip() in {"", "attachment"}
        ):
            warnings.append(
                _warning(
                    code=InvariantCode.GHOST_ATTACHMENT,
                    field="filename",
                    message="attachment has no url or mime_type",
                    severity=Severity.WARNING,
                )
            )
        if attachment.mime_type is not None and "/" not in attachment.mime_type:
            warnings.append(
                _warning(
                    code=InvariantCode.INVALID_MIME_TYPE,
                    field="mime_type",
                    message="mime_type is not in type/subtype form",
                    severity=Severity.WARNING,
                )
            )
        if attachment.filename.strip() == "":
            warnings.append(
                _warning(
                    code=InvariantCode.EMPTY_FILENAME,
                    field="filename",
                    message="attachment filename is empty",
                    severity=Severity.WARNING,
                )
            )
    return warnings


def _check_envelope_state(envelope: ProcessingEnvelope) -> list[InvariantWarning]:
    warnings: list[InvariantWarning] = []
    if envelope.entity.entity_type == "raw_message":
        return warnings
    if envelope.resolved_course_id is None:
        warnings.append(
            _warning(
                code=InvariantCode.NO_COURSE_RESOLVED,
                field="resolved_course_id",
                message="structured item has no resolved course",
                severity=Severity.ERROR,
            )
        )
    if envelope.classification_log is None:
        warnings.append(
            _warning(
                code=InvariantCode.NO_CLASSIFICATION_LOG,
                field="classification_log",
                message="structured item has no classification log",
                severity=Severity.WARNING,
            )
        )
    return warnings


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
