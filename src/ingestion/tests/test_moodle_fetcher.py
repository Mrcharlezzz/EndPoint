"""Tests for Moodle fetch helpers — pure functions over MoodleRestClient."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.moodle.client import MoodleRestClient
from ingestion.adapters.moodle.fetcher import (
    fetch_assignments,
    fetch_calendar_events,
    fetch_course_contents,
    fetch_forum_discussions,
    fetch_forums,
    fetch_site_info,
    fetch_user_courses,
)

MOODLE_BASE = "https://moodle.test"
MOODLE_ENDPOINT = f"{MOODLE_BASE}/webservice/rest/server.php"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def client() -> MoodleRestClient:
    return MoodleRestClient(
        base_url=MOODLE_BASE,
        token="test-token",
        max_retries=0,
        timeout=5,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )


class TestFetchUserCourses:
    @respx.mock
    async def test_returns_course_list(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 9296, "fullname": "Core Algorithms", "visible": 1},
                    {"id": 9295, "fullname": "JVM Languages", "visible": 1},
                ],
            )
        )
        courses = await fetch_user_courses(client, user_id=12059)
        assert len(courses) == 2
        assert courses[0]["fullname"] == "Core Algorithms"

    @respx.mock
    async def test_returns_empty_on_non_list(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"warning": "something"})
        )
        courses = await fetch_user_courses(client, user_id=1)
        assert courses == []


class TestFetchAssignments:
    @respx.mock
    async def test_flattens_assignments_with_course_context(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "courses": [
                        {
                            "id": 9273,
                            "fullname": "DSCA Tutorial",
                            "assignments": [
                                {"id": 5897, "name": "HW Test", "duedate": 1770937140},
                                {"id": 5956, "name": "HW1 NAND Gates", "duedate": 1771541940},
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
        )
        assignments = await fetch_assignments(client, course_ids=[9273, 9296])
        assert len(assignments) == 2
        assert assignments[0]["name"] == "HW Test"
        assert assignments[0]["_course_name"] == "DSCA Tutorial"
        assert assignments[0]["_course_id"] == 9273
        assert assignments[1]["_course_name"] == "DSCA Tutorial"

    async def test_empty_course_ids_returns_empty(self, client: MoodleRestClient) -> None:
        result = await fetch_assignments(client, course_ids=[])
        assert result == []

    @respx.mock
    async def test_non_dict_response_returns_empty(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(return_value=httpx.Response(200, json=[]))
        result = await fetch_assignments(client, course_ids=[1])
        assert result == []


class TestFetchForums:
    @respx.mock
    async def test_announcements_scope_filters_news_type(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 10473, "type": "news", "name": "Announcements", "course": 9296},
                    {"id": 10500, "type": "general", "name": "Discussion", "course": 9296},
                ],
            )
        )
        forums = await fetch_forums(client, course_ids=[9296], forum_scope="announcements")
        assert len(forums) == 1
        assert forums[0]["type"] == "news"

    @respx.mock
    async def test_all_scope_returns_everything(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 10473, "type": "news", "name": "Announcements", "course": 9296},
                    {"id": 10500, "type": "general", "name": "Discussion", "course": 9296},
                ],
            )
        )
        forums = await fetch_forums(client, course_ids=[9296], forum_scope="all")
        assert len(forums) == 2

    async def test_empty_course_ids_returns_empty(self, client: MoodleRestClient) -> None:
        result = await fetch_forums(client, course_ids=[])
        assert result == []


class TestFetchForumDiscussions:
    @respx.mock
    async def test_returns_discussions(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "discussions": [
                        {
                            "discussion": 101,
                            "name": "Welcome",
                            "userfullname": "Prof Smith",
                            "message": "<p>Welcome to class</p>",
                            "timemodified": 1710288000,
                        }
                    ]
                },
            )
        )
        discussions = await fetch_forum_discussions(client, forum_id=10473)
        assert len(discussions) == 1
        assert discussions[0]["name"] == "Welcome"

    @respx.mock
    async def test_empty_forum_returns_empty(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"discussions": []})
        )
        discussions = await fetch_forum_discussions(client, forum_id=999)
        assert discussions == []


class TestFetchCourseContents:
    @respx.mock
    async def test_returns_sections(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "name": "General",
                        "modules": [
                            {
                                "id": 38817,
                                "name": "Announcements",
                                "modname": "forum",
                                "instance": 10447,
                                "contents": [],
                            },
                            {
                                "id": 41249,
                                "name": "Homework Test",
                                "modname": "assign",
                                "instance": 5897,
                                "contents": [],
                            },
                        ],
                    },
                    {
                        "id": 2,
                        "name": "Week 1",
                        "modules": [
                            {
                                "id": 40000,
                                "name": "Lecture Slides",
                                "modname": "resource",
                                "instance": 3000,
                                "contents": [
                                    {
                                        "type": "file",
                                        "filename": "slides.pdf",
                                        "filesize": 1024000,
                                        "fileurl": "https://moodle.test/file/slides.pdf?token=abc",
                                        "mimetype": "application/pdf",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            )
        )
        sections = await fetch_course_contents(client, course_id=9273)
        assert len(sections) == 2
        assert sections[0]["name"] == "General"
        assert len(sections[0]["modules"]) == 2
        assert sections[1]["modules"][0]["contents"][0]["filename"] == "slides.pdf"


class TestFetchCalendarEvents:
    @respx.mock
    async def test_returns_events(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "id": 5180405,
                            "name": "HW5 is due",
                            "timesort": 1773961140,
                            "course": {"fullname": "DSCA Tutorial"},
                        }
                    ],
                    "firstid": 5180405,
                    "lastid": 5180405,
                },
            )
        )
        events = await fetch_calendar_events(client, time_from=1700000000, per_page=10)
        assert len(events) == 1
        assert events[0]["name"] == "HW5 is due"

    @respx.mock
    async def test_empty_events(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"events": []})
        )
        events = await fetch_calendar_events(client, time_from=1700000000)
        assert events == []


class TestFetchSiteInfo:
    @respx.mock
    async def test_returns_site_info(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "userid": 12059,
                    "fullname": "Eris Frangu",
                    "sitename": "Moodle at Constructor University",
                    "functions": [{"name": "core_enrol_get_users_courses"}],
                },
            )
        )
        info = await fetch_site_info(client)
        assert info["userid"] == 12059
        assert info["fullname"] == "Eris Frangu"
