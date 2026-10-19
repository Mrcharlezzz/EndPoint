import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest
from django.test import Client


# ---------------------------------------------------------------------------
# Factories — create lightweight fake model instances (no DB required)
# ---------------------------------------------------------------------------


def make_user(
    id=None,
    email="sample3@example.edu",
    display_name="Test User",
    ms_oid=None,
):
    user = MagicMock()
    user.id = id or uuid.uuid4()
    user.email = email
    user.display_name = display_name
    user.ms_oid = ms_oid or str(uuid.uuid4())
    user.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    user.updated_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    user.delete = MagicMock()
    return user


def make_session(
    user=None,
    token=None,
    expires_at=None,
):
    if user is None:
        user = make_user()
    session = MagicMock()
    session.id = uuid.uuid4()
    session.user = user
    session.token = token or "test-token-" + str(uuid.uuid4())
    session.expires_at = expires_at or datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return session


def make_course(
    id=None,
    name="Test Course",
    code: str | None = "TC-01",
    major: str | None = "Computer Science",
    semester: str | None = "WS26",
):
    course = MagicMock()
    course.id = id or uuid.uuid4()
    course.name = name
    course.code = code
    course.major = major
    course.semester = semester
    return course


def make_ingested_item(
    platform="moodle",
    processing_status="processed",
    source_url=None,
):
    item = MagicMock()
    item.platform = platform
    item.processing_status = processing_status
    item.source_url = source_url
    item.payload = {}
    return item


def make_link(url="https://example.com", label="Example"):
    link = MagicMock()
    link.url = url
    link.label = label
    return link


def make_attachment(
    filename="file.pdf", mime_type="application/pdf", url="https://example.com/file.pdf"
):
    att = MagicMock()
    att.filename = filename
    att.mime_type = mime_type
    att.url = url
    return att


def make_assignment(
    id=None,
    title="Test Assignment",
    body="Do the thing",
    author="Prof. Smith",
    course=None,
    deadline=None,
    submission_format="PDF",
    max_points=100.0,
    points=None,
    status="open",
    platform="moodle",
    source_url=None,
    published_at=None,
    links=None,
    attachments=None,
):
    if course is None:
        course = make_course()
    ingested_item = make_ingested_item(platform=platform, source_url=source_url)

    a = MagicMock()
    a.id = id or uuid.uuid4()
    a.title = title
    a.body = body
    a.author = author
    a.course = course
    a.course_id = course.id
    a.deadline = deadline
    a.submission_format = submission_format
    a.max_points = max_points
    a.points = points
    a.status = status
    a.ingested_item = ingested_item
    a.published_at = published_at or datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    a.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    a.links = MagicMock()
    a.links.all.return_value = links or []
    a.attachments = MagicMock()
    a.attachments.all.return_value = attachments or []
    return a


def make_announcement(
    id=None,
    title="Test Announcement",
    body="Announcement body",
    author="Prof. Smith",
    course=None,
    platform="moodle",
    source_url=None,
    published_at=None,
    links=None,
    attachments=None,
):
    if course is None:
        course = make_course()
    ingested_item = make_ingested_item(platform=platform, source_url=source_url)

    a = MagicMock()
    a.id = id or uuid.uuid4()
    a.title = title
    a.body = body
    a.author = author
    a.course = course
    a.course_id = course.id
    a.ingested_item = ingested_item
    a.published_at = published_at or datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    a.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    a.links = MagicMock()
    a.links.all.return_value = links or []
    a.attachments = MagicMock()
    a.attachments.all.return_value = attachments or []
    return a


def make_event(
    id=None,
    title="Test Event",
    body="Event body",
    author="Prof. Smith",
    course=None,
    start_time=None,
    end_time=None,
    location="Room 101",
    platform="moodle",
    source_url=None,
    published_at=None,
    links=None,
    attachments=None,
):
    if course is None:
        course = make_course()
    ingested_item = make_ingested_item(platform=platform, source_url=source_url)

    e = MagicMock()
    e.id = id or uuid.uuid4()
    e.title = title
    e.body = body
    e.author = author
    e.course = course
    e.course_id = course.id
    e.start_time = start_time
    e.end_time = end_time
    e.location = location
    e.ingested_item = ingested_item
    e.published_at = published_at or datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    e.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    e.links = MagicMock()
    e.links.all.return_value = links or []
    e.attachments = MagicMock()
    e.attachments.all.return_value = attachments or []
    return e


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return Client()
