import uuid
from datetime import datetime, timedelta, timezone

import pytest
from django.db import connection
from django.test import Client

from api.models.identity import Session, User, UserCourseSelection
from api.models.mirrors import (
    Announcement,
    Assignment,
    Attachment,
    BaseClassifiedItem,
    CanonicalEntity,
    Course,
    Event,
    IngestedItem,
    Link,
)


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def mirror_tables():
    models = [
        Course,
        IngestedItem,
        BaseClassifiedItem,
        CanonicalEntity,
        Assignment,
        Announcement,
        Event,
        Link,
        Attachment,
    ]
    with connection.schema_editor() as schema_editor:
        for model in models:
            schema_editor.create_model(model)

    yield

    with connection.schema_editor() as schema_editor:
        for model in reversed(models):
            schema_editor.delete_model(model)


@pytest.fixture
def authenticated_client():
    user = User.objects.create(
        email="student@example.test",
        display_name="Student",
    )
    session = Session.objects.create(
        user=user,
        token="integration-token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {session.token}")
    return client, user


@pytest.fixture
def selected_course(authenticated_client, mirror_tables):
    _, user = authenticated_client
    course = Course.objects.create(
        id=uuid.uuid4(),
        name="Software Engineering",
        code="CS-301",
        major="CS",
        semester="Spring 2026",
        created_at=_dt(8),
        updated_at=_dt(8),
    )
    UserCourseSelection.objects.create(user=user, course_id=course.id)
    return course


def _dt(hour: int) -> datetime:
    return datetime(2026, 4, 20, hour, 30, tzinfo=timezone.utc)


def _ingested_item(content_type: str, source_url: str, status: str = "processed") -> IngestedItem:
    return IngestedItem.objects.create(
        id=uuid.uuid4(),
        external_id=f"{content_type}-{uuid.uuid4()}",
        platform="moodle",
        course_external_id="CS-301",
        source_url=source_url,
        schema_version="5.0",
        adapter_version="test-adapter",
        ingestion_mode="server_poll",
        processing_status=status,
        error_message=None,
        payload={"content_type": content_type},
        raw_payload={"source": "integration-test"},
        created_at=_dt(7),
        updated_at=_dt(7),
    )


def _add_supporting_rows(classified_item: BaseClassifiedItem) -> None:
    Link.objects.create(
        id=uuid.uuid4(),
        classified_item=classified_item,
        url="https://example.test/link",
        label="Reference",
        created_at=_dt(12),
    )
    Attachment.objects.create(
        id=uuid.uuid4(),
        classified_item=classified_item,
        filename="brief.pdf",
        mime_type="application/pdf",
        url="https://example.test/brief.pdf",
        created_at=_dt(12),
    )


def _assignment(course: Course) -> Assignment:
    item = _ingested_item("assignment", "https://moodle.example.test/assignments/1")
    assignment = Assignment.objects.create(
        id=uuid.uuid4(),
        entity_type="assignment",
        author="Prof. Ada",
        ingested_item=item,
        course=course,
        dedup_status="primary",
        title="Project brief",
        body="Build the integration slice.",
        published_at=_dt(9),
        created_at=_dt(10),
        updated_at=_dt(10),
        deadline=_dt(18),
        submission_format="PDF",
    )
    _add_supporting_rows(assignment)
    return assignment


def _announcement(course: Course) -> Announcement:
    item = _ingested_item("announcement", "https://teams.example.test/announcements/1")
    announcement = Announcement.objects.create(
        id=uuid.uuid4(),
        entity_type="announcement",
        author="Prof. Turing",
        ingested_item=item,
        course=course,
        dedup_status="primary",
        title="Room changed",
        body="Tomorrow's lecture moves to room 2.",
        published_at=_dt(11),
        created_at=_dt(12),
        updated_at=_dt(12),
    )
    _add_supporting_rows(announcement)
    return announcement


def _event(course: Course) -> Event:
    item = _ingested_item("event", "https://moodle.example.test/events/1")
    event = Event.objects.create(
        id=uuid.uuid4(),
        entity_type="event",
        author="Prof. Hopper",
        ingested_item=item,
        course=course,
        dedup_status="primary",
        title="Demo session",
        body="Bring your prototype.",
        published_at=_dt(13),
        created_at=_dt(14),
        updated_at=_dt(14),
        start_time=_dt(16),
        end_time=_dt(17),
        location="Lab 4",
    )
    _add_supporting_rows(event)
    return event


def test_assignments_endpoint_fetches_v5_rows_from_database(authenticated_client, selected_course):
    client, _ = authenticated_client
    assignment = _assignment(selected_course)

    response = client.get("/api/v1/assignments/")

    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["id"] == str(assignment.id)
    assert data[0]["source_url"] == "https://moodle.example.test/assignments/1"
    assert data[0]["published_at"] == "2026-04-20T09:30:00Z"
    assert data[0]["created_at"] == "2026-04-20T09:30:00Z"
    assert data[0]["max_points"] is None
    assert data[0]["points"] is None
    assert data[0]["status"] is None
    assert data[0]["links"] == [{"url": "https://example.test/link", "label": "Reference"}]
    assert data[0]["attachments"] == [
        {
            "filename": "brief.pdf",
            "mime_type": "application/pdf",
            "url": "https://example.test/brief.pdf",
        }
    ]


def test_assignment_detail_endpoint_fetches_v5_row_from_database(
    authenticated_client, selected_course
):
    client, _ = authenticated_client
    assignment = _assignment(selected_course)

    response = client.get(f"/api/v1/assignments/{assignment.id}/")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(assignment.id)
    assert data["course"]["id"] == str(selected_course.id)
    assert data["source_url"] == "https://moodle.example.test/assignments/1"
    assert data["published_at"] == "2026-04-20T09:30:00Z"
    assert data["created_at"] == "2026-04-20T09:30:00Z"


def test_announcements_endpoint_fetches_v5_rows_from_database(
    authenticated_client, selected_course
):
    client, _ = authenticated_client
    announcement = _announcement(selected_course)

    response = client.get("/api/v1/announcements/")

    assert response.status_code == 200
    groups = response.json()["results"]
    assert len(groups) == 1
    assert groups[0]["course"]["id"] == str(selected_course.id)
    data = groups[0]["announcements"]
    assert len(data) == 1
    assert data[0]["id"] == str(announcement.id)
    assert data[0]["source_url"] == "https://teams.example.test/announcements/1"
    assert data[0]["published_at"] == "2026-04-20T11:30:00Z"
    assert data[0]["created_at"] == "2026-04-20T11:30:00Z"


def test_announcement_detail_endpoint_fetches_v5_row_from_database(
    authenticated_client, selected_course
):
    client, _ = authenticated_client
    announcement = _announcement(selected_course)

    response = client.get(f"/api/v1/announcements/{announcement.id}/")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(announcement.id)
    assert data["course"]["id"] == str(selected_course.id)
    assert data["source_url"] == "https://teams.example.test/announcements/1"
    assert data["published_at"] == "2026-04-20T11:30:00Z"
    assert data["created_at"] == "2026-04-20T11:30:00Z"


def test_events_endpoint_fetches_v5_rows_from_database(authenticated_client, selected_course):
    client, _ = authenticated_client
    event = _event(selected_course)

    response = client.get("/api/v1/events/")

    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["id"] == str(event.id)
    assert data[0]["course"]["id"] == str(selected_course.id)
    assert data[0]["source_url"] == "https://moodle.example.test/events/1"
    assert data[0]["published_at"] == "2026-04-20T13:30:00Z"
    assert data[0]["created_at"] == "2026-04-20T13:30:00Z"


def test_event_detail_endpoint_fetches_v5_row_from_database(authenticated_client, selected_course):
    client, _ = authenticated_client
    event = _event(selected_course)

    response = client.get(f"/api/v1/events/{event.id}/")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(event.id)
    assert data["course"]["id"] == str(selected_course.id)
    assert data["source_url"] == "https://moodle.example.test/events/1"
    assert data["published_at"] == "2026-04-20T13:30:00Z"
    assert data["created_at"] == "2026-04-20T13:30:00Z"
