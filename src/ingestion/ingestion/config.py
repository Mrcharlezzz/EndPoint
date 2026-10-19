from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ingestion import __version__

SKIP_MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v"}
)

_DEFAULT_ROLLING_DAYS = 90


def _get_earliest_fetch_date(semester_start: date | None) -> datetime:
    """Return the earliest date to fetch content from.

    Uses semester_start if set, otherwise falls back to a rolling window.
    """
    if semester_start is not None:
        return datetime.combine(semester_start, datetime.min.time(), tzinfo=UTC)
    return datetime.now(tz=UTC) - timedelta(days=_DEFAULT_ROLLING_DAYS)


class TeamsConfig(BaseSettings):
    """Teams adapter configuration.

    Decision #2: CSA primary, Graph for structural data. Configurable strategy.
    Decision #5: semester_start with 90-day rolling fallback.
    """

    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    csa_base_url: str = "https://teams.cloud.microsoft/api/csa/emea/api/v2"
    assignments_base_url: str = "https://assignments.edu.cloud.microsoft"
    csa_page_size: int = 50
    max_retries: int = 3
    request_timeout: int = 30
    requests_per_second: float = 10.0
    max_concurrent_requests: int = 5
    skip_media_extensions: frozenset[str] = SKIP_MEDIA_EXTENSIONS
    semester_start: date | None = None

    def get_earliest_fetch_date(self) -> datetime:
        return _get_earliest_fetch_date(self.semester_start)


class MoodleConfig(BaseSettings):
    """Moodle adapter configuration.

    Decision #6: semester_start with 90-day fallback.
    Decision #14: forum_scope configurable (announcements | all).
    """

    base_url: str = "https://elearning.constructor.university"
    request_timeout: int = 30
    max_retries: int = 3
    requests_per_second: float = 10.0
    max_concurrent_requests: int = 5
    include_grades: bool = False
    forum_scope: str = "announcements"
    skip_media_extensions: frozenset[str] = SKIP_MEDIA_EXTENSIONS
    semester_start: date | None = None

    def get_earliest_fetch_date(self) -> datetime:
        return _get_earliest_fetch_date(self.semester_start)


class TelegramConfig(BaseSettings):
    """Telegram adapter configuration."""

    api_base_url: str = "https://api.telegram.org"
    poll_timeout: int = 0
    max_updates: int = 100
    request_timeout: int = 30
    max_retries: int = 3
    requests_per_second: float = 1.0
    max_concurrent_requests: int = 5

    model_config = SettingsConfigDict(extra="ignore")


class IngestionConfig(BaseSettings):
    """Top-level configuration composing all adapter configs.

    Decision #27: adapter_version pulled from pyproject.toml.
    """

    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    moodle: MoodleConfig = Field(default_factory=MoodleConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    adapter_version: str = __version__

    model_config = SettingsConfigDict(
        env_prefix="ENDPOINT_",
        env_nested_delimiter="__",
    )
