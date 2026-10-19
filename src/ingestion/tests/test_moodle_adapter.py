"""Tests for MoodleAdapter — scope routing, credential validation, error boundary."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.moodle.adapter import MoodleAdapter
from ingestion.config import MoodleConfig
from ingestion.schemas import (
    ContentType,
    ErrorCode,
    FetchResult,
    FetchScope,
    FileRef,
)

MOODLE_BASE = "https://moodle.test"
MOODLE_ENDPOINT = f"{MOODLE_BASE}/webservice/rest/server.php"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def config() -> MoodleConfig:
    return MoodleConfig(
        base_url=MOODLE_BASE,
        max_retries=0,
        request_timeout=5,
        requests_per_second=0,
        forum_scope="announcements",
    )


@pytest.fixture
def adapter(config: MoodleConfig) -> MoodleAdapter:
    return MoodleAdapter(config, sleep_func=_noop_sleep)


@pytest.fixture
def credentials() -> dict:
    return {"token": "moodle-tok-123", "user_id": 12059}


def _mock_courses() -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            {"id": 9273, "fullname": "DSCA Tutorial", "visible": 1},
            {"id": 9296, "fullname": "Core Algorithms", "visible": 1},
        ],
    )


def _mock_assignments() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "courses": [
                {
                    "id": 9273,
                    "fullname": "DSCA Tutorial",
                    "assignments": [
                        {
                            "id": 5897,
                            "name": "Homework Test",
                            "intro": "<p>Test assignment</p>",
                            "introformat": 1,
                            "duedate": 1770937140,
                            "grade": 10,
                            "timemodified": 1770992339,
                        }
                    ],
                },
                {
                    "id": 9296,
                    "fullname": "Core Algorithms",
                    "assignments": [],
                },
            ],
            "warnings": [],
        },
    )


def _mock_forums() -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            {"id": 10473, "type": "news", "name": "Announcements", "course": 9296},
        ],
    )


def _mock_discussions() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "discussions": [
                {
                    "discussion": 101,
                    "name": "Welcome",
                    "message": "<p>Welcome to class</p>",
                    "userfullname": "Prof Smith",
                    "timemodified": 1710288000,
                    "created": 1710200000,
                    "subject": "Welcome",
                }
            ]
        },
    )


def _mock_course_contents() -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            {
                "id": 1,
                "name": "Week 1",
                "modules": [
                    {
                        "id": 40000,
                        "name": "Lecture Slides",
                        "modname": "resource",
                        "instance": 3000,
                        "description": "<p>Slides</p>",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "slides.pdf",
                                "filesize": 1024,
                                "fileurl": "https://moodle.test/file.php/slides.pdf",
                                "mimetype": "application/pdf",
                            }
                        ],
                    }
                ],
            }
        ],
    )


def _mock_calendar_events() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "events": [
                {
                    "id": 5180405,
                    "name": "HW5 is due",
                    "description": "",
                    "descriptionformat": 1,
                    "timesort": 1773961140,
                    "course": {"id": 9273, "fullname": "DSCA Tutorial"},
                    "action": {"name": "Add submission", "url": "https://moodle.test/assign"},
                    "eventtype": "course",
                    "modulename": "page",
                    "component": "mod_page",
                    "activitystr": "Page",
                }
            ]
        },
    )


def _mock_assignment_due_calendar_events() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "events": [
                {
                    "id": 5180990,
                    "name": "Homework 12# Sequential Logic and Synchronization is due",
                    "description": "<p>Homework details</p>",
                    "descriptionformat": 1,
                    "timesort": 1778277540,
                    "course": {"id": 9273, "fullname": "DSCA Tutorial"},
                    "action": {"name": "Add submission", "url": "https://moodle.test/assign"},
                    "eventtype": "due",
                    "modulename": "assign",
                    "component": "mod_assign",
                    "activitystr": "Assignment is due",
                }
            ]
        },
    )


def _mock_site_info() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "userid": 12059,
            "fullname": "Eris Frangu",
            "sitename": "Moodle at Constructor University",
        },
    )


def _setup_mock_for_function(wsfunction: str, response: httpx.Response) -> None:
    """Helper: respx route matching on wsfunction in POST body."""
    # respx doesn't natively match POST body content, so we match the endpoint
    # and rely on call ordering. For isolated tests, this works because each
    # test uses a single scope.
    pass


class TestProtocolCompliance:
    def test_platform_attribute(self, adapter: MoodleAdapter) -> None:
        assert adapter.platform == "moodle"

    @respx.mock
    async def test_test_connection_success(self, adapter: MoodleAdapter, credentials: dict) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(return_value=_mock_site_info())
        assert await adapter.test_connection(credentials) is True

    @respx.mock
    async def test_test_connection_failure_bad_token(
        self, adapter: MoodleAdapter, credentials: dict
    ) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "exception": "moodle_exception",
                    "errorcode": "invalidtoken",
                    "message": "Invalid token",
                },
            )
        )
        assert await adapter.test_connection(credentials) is False

    @respx.mock
    async def test_test_connection_failure_network(
        self, adapter: MoodleAdapter, credentials: dict
    ) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
        assert await adapter.test_connection(credentials) is False


class TestCredentialValidation:
    async def test_missing_token(self, adapter: MoodleAdapter) -> None:
        result = await adapter.fetch({"user_id": 12059})
        assert isinstance(result, FetchResult)
        assert len(result.errors) == 1
        assert "token" in result.errors[0].message.lower()

    async def test_missing_user_id(self, adapter: MoodleAdapter) -> None:
        result = await adapter.fetch({"token": "tok"})
        assert len(result.errors) == 1
        assert "user_id" in result.errors[0].message.lower()

    async def test_empty_token(self, adapter: MoodleAdapter) -> None:
        result = await adapter.fetch({"token": "", "user_id": 12059})
        assert len(result.errors) == 1


class TestScopeRouting:
    @respx.mock
    async def test_assignments_scope(self, adapter: MoodleAdapter, credentials: dict) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(side_effect=[_mock_courses(), _mock_assignments()])
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.ASSIGNMENTS}))
        assert len(result.items) == 1
        assert result.items[0].content_type == ContentType.ASSIGNMENT
        assert result.items[0].title == "Homework Test"

    @respx.mock
    async def test_forums_scope(self, adapter: MoodleAdapter, credentials: dict) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            side_effect=[_mock_courses(), _mock_forums(), _mock_discussions()]
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.FORUMS}))
        assert len(result.items) == 1
        assert result.items[0].content_type == ContentType.ANNOUNCEMENT
        assert result.items[0].author == "Prof Smith"

    @respx.mock
    async def test_materials_scope(self, adapter: MoodleAdapter, credentials: dict) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            side_effect=[_mock_courses(), _mock_course_contents(), _mock_course_contents()]
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.MATERIALS}))
        assert len(result.items) >= 1
        assert result.items[0].content_type == ContentType.MATERIAL

    @respx.mock
    async def test_events_scope(self, adapter: MoodleAdapter, credentials: dict) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            side_effect=[_mock_courses(), _mock_calendar_events()]
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.EVENTS}))
        assert len(result.items) == 1
        assert result.items[0].content_type == ContentType.EVENT

    @respx.mock
    async def test_events_scope_skips_assignment_due_calendar_duplicates(
        self, adapter: MoodleAdapter, credentials: dict
    ) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            side_effect=[_mock_courses(), _mock_assignment_due_calendar_events()]
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.EVENTS}))
        assert result.items == []
        assert result.errors == []
        assert result.stats.items_fetched == 0
        assert result.stats.per_scope_stats["events"].items_fetched == 0
        assert result.stats.per_scope_stats["events"].items_skipped == 1

    @respx.mock
    async def test_files_scope(self, adapter: MoodleAdapter, credentials: dict) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            side_effect=[_mock_courses(), _mock_course_contents(), _mock_course_contents()]
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.FILES}))
        assert len(result.files) >= 1
        assert isinstance(result.files[0], FileRef)
        assert result.files[0].filename == "slides.pdf"

    @respx.mock
    async def test_unsupported_scope_skipped(
        self, adapter: MoodleAdapter, credentials: dict
    ) -> None:
        """MESSAGES scope is unsupported for Moodle — skipped with no error."""
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.MESSAGES}))
        assert result.items == []
        assert result.errors == []


class TestErrorBoundary:
    @respx.mock
    async def test_token_expired_captured(self, adapter: MoodleAdapter, credentials: dict) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "exception": "moodle_exception",
                    "errorcode": "invalidtoken",
                    "message": "Invalid token",
                },
            )
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.ASSIGNMENTS}))
        assert len(result.errors) >= 1
        token_errors = [e for e in result.errors if e.code == ErrorCode.TOKEN_EXPIRED]
        assert len(token_errors) >= 1
        assert token_errors[0].recoverable is False

    @respx.mock
    async def test_platform_unavailable_captured(
        self, adapter: MoodleAdapter, credentials: dict
    ) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.ASSIGNMENTS}))
        assert len(result.errors) >= 1


class TestCursorManagement:
    @respx.mock
    async def test_cursor_contains_last_sync_at(
        self, adapter: MoodleAdapter, credentials: dict
    ) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(side_effect=[_mock_courses(), _mock_assignments()])
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.ASSIGNMENTS}))
        assert result.cursor is not None
        assert "last_sync_at" in result.cursor


class TestStats:
    @respx.mock
    async def test_stats_populated(self, adapter: MoodleAdapter, credentials: dict) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(side_effect=[_mock_courses(), _mock_assignments()])
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.ASSIGNMENTS}))
        assert result.stats.items_fetched == 1
        assert result.stats.duration_seconds > 0
