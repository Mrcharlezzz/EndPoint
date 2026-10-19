from datetime import datetime, timezone

from api.views.announcements import announcement_data
from api.views.assignments import assignment_data
from api.views.events import event_data
from tests.conftest import make_announcement, make_assignment, make_event


def test_assignment_serialization_uses_v5_source_url_and_removed_field_shims():
    assignment = make_assignment(source_url="https://moodle.example/assignment/1")
    assignment.ingested_item.payload = {
        "source_url": "https://legacy-payload.example/should-not-be-used"
    }

    data = assignment_data(assignment)

    assert data["source_url"] == "https://moodle.example/assignment/1"
    assert data["max_points"] is None
    assert data["points"] is None
    assert data["status"] is None


def test_announcement_serialization_uses_v5_source_url_and_published_at():
    published_at = datetime(2026, 4, 20, 9, 30, tzinfo=timezone.utc)
    announcement = make_announcement(
        source_url="https://teams.example/announcements/1",
        published_at=published_at,
    )
    announcement.ingested_item.payload = {
        "source_url": "https://legacy-payload.example/should-not-be-used"
    }

    data = announcement_data(announcement)

    assert data["source_url"] == "https://teams.example/announcements/1"
    assert data["published_at"] == published_at


def test_event_serialization_uses_v5_source_url_and_published_at():
    published_at = datetime(2026, 4, 19, 13, 15, tzinfo=timezone.utc)
    event = make_event(
        source_url="https://moodle.example/events/1",
        published_at=published_at,
    )
    event.ingested_item.payload = {
        "source_url": "https://legacy-payload.example/should-not-be-used"
    }

    data = event_data(event)

    assert data["source_url"] == "https://moodle.example/events/1"
    assert data["published_at"] == published_at
