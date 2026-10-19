"""Pydantic models and enums shared across the ingestion pipeline.

Defines the universal data contract: NormalizedItem, FetchResult,
SyncStats, and supporting types used by all adapters and the CLI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENVELOPE_SCHEMA_VERSION = "2"
PAYLOAD_SCHEMA_VERSION = "4.0"


class ContentType(StrEnum):
    """Type of content item after normalization."""

    ASSIGNMENT = "assignment"
    ANNOUNCEMENT = "announcement"
    EVENT = "event"
    MATERIAL = "material"
    REMINDER = "reminder"
    RAW_MESSAGE = "raw_message"
    UNKNOWN = "unknown"


class FetchScope(StrEnum):
    """Shared scope enum across all adapters.

    Each adapter supports a subset. Unsupported scopes log a warning and are skipped.
    """

    MESSAGES = "messages"
    ASSIGNMENTS = "assignments"
    FILES = "files"
    EVENTS = "events"
    MATERIALS = "materials"
    FORUMS = "forums"


class ErrorCode(StrEnum):
    """Structured error codes for FetchError."""

    TOKEN_EXPIRED = "token_expired"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    RATE_LIMITED = "rate_limited"
    PLATFORM_UNAVAILABLE = "platform_unavailable"
    NORMALIZATION_ERROR = "normalization_error"
    CONNECTION_ERROR = "connection_error"
    UNKNOWN = "unknown"


class FileRef(BaseModel):
    """Reference to a downloadable file. Ingestion does NOT download — Django does."""

    model_config = ConfigDict(frozen=True)

    external_id: str
    filename: str
    download_url: str
    content_type: str | None = None
    size_bytes: int | None = None
    is_media: bool = False

    @field_validator("size_bytes")
    @classmethod
    def _size_must_be_non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            msg = "size_bytes must be non-negative"
            raise ValueError(msg)
        return v


class LinkRef(BaseModel):
    """A URL extracted from the content item."""

    model_config = ConfigDict(frozen=True)

    url: str
    label: str | None = None


class NormalizedItem(BaseModel):
    """Platform-agnostic content item. The universal currency of the pipeline.

    Mirrors the normalized ingestion contract.

    `source_url` remains part of the in-memory item because adapters populate it
    and callers may need it, but it is persisted as a top-level ingested_items
    field rather than inside the structured payload JSON.
    """

    # -- Ingestion metadata --
    external_id: str
    platform: str
    content_type: ContentType
    ingestion_mode: str = "server_poll"
    raw_payload: dict[str, object] = {}
    fetched_by_user_email: str | None = None

    # -- Base fields (all entity types) --
    title: str | None = None
    author: str | None = None
    body: str
    source_url: str | None = None
    course_name: str | None = None
    course_external_id: str | None = None
    published_at: datetime | None = None

    # -- Assignment fields --
    deadline: datetime | None = None
    submission_format: str | None = None

    # -- Event fields --
    start_time: datetime | None = None
    end_time: datetime | None = None
    location: str | None = None

    # -- Supporting data --
    attachments: list[FileRef] = []
    links: list[LinkRef] = []

    def __repr__(self) -> str:
        title_preview = (self.title or self.body)[:40]
        return (
            f"NormalizedItem(id={self.external_id!r}, "
            f"platform={self.platform!r}, "
            f"type={self.content_type.value!r}, "
            f"title={title_preview!r})"
        )

    def to_payload(self) -> dict[str, object]:
        """Return the structured payload persisted to ingested_items.payload.

        Per db-schema-v5 §3.3.1, ingestion metadata (external_id, platform,
        course_external_id, source_url, schema_version, adapter_version,
        ingestion_mode, raw_payload) lives outside this JSON blob as dedicated
        columns. Attachments project to the v5 shape {filename, mime_type, url}.
        """
        base = self.model_dump(
            mode="json",
            include={
                "content_type",
                "title",
                "author",
                "body",
                "course_name",
                "published_at",
                "deadline",
                "submission_format",
                "start_time",
                "end_time",
                "location",
            },
        )
        base["attachments"] = [
            {
                "filename": a.filename,
                "mime_type": a.content_type,
                "url": a.download_url or None,
            }
            for a in self.attachments
        ]
        base["links"] = [{"url": link.url, "label": link.label} for link in self.links]
        return base


class FetchError(BaseModel):
    """Structured error from a fetch operation.

    Gives Django enough context to decide: retry? notify user? skip?
    """

    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    message: str
    scope: str = ""
    recoverable: bool = True
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class ScopeStats(BaseModel):
    """Per-scope statistics within a sync run."""

    items_fetched: int = 0
    items_new: int = 0
    items_updated: int = 0
    items_skipped: int = 0
    errors: int = 0


class SyncStats(BaseModel):
    """Statistics from a sync run."""

    items_fetched: int = 0
    items_new: int = 0
    items_updated: int = 0
    items_skipped: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    api_call_duration_seconds: float = 0.0
    normalization_duration_seconds: float = 0.0
    per_scope_stats: dict[str, ScopeStats] = Field(default_factory=dict)


class FetchResult(BaseModel):
    """What an adapter returns from a fetch() call."""

    items: list[NormalizedItem] = []
    files: list[FileRef] = []
    cursor: dict[str, object] | None = None
    errors: list[FetchError] = []
    stats: SyncStats = Field(default_factory=SyncStats)


class RawPayloadEnvelope(BaseModel):
    """Standardized wrapper for raw_payload JSONB field."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = ENVELOPE_SCHEMA_VERSION
    source: str
    ingestion_mode: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    adapter_version: str = ""
    original: dict[str, object] = {}
