"""
Tests for:
  GET /api/v1/events/
  GET /api/v1/events/{id}/
"""

import uuid
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from tests.conftest import make_session, make_event, make_course, make_link, make_attachment


def _authed_get(client, session, url):
    with patch("api.middleware.Session.objects.select_related") as mock_qs:
        mock_qs.return_value.get.return_value = session
        return client.get(url, HTTP_AUTHORIZATION=f"Bearer {session.token}")


def _authed_get_params(client, session, url, params):
    with patch("api.middleware.Session.objects.select_related") as mock_qs:
        mock_qs.return_value.get.return_value = session
        return client.get(url, params, HTTP_AUTHORIZATION=f"Bearer {session.token}")


class _QuerySetStub:
    """Minimal queryset stub that cannot synthesize recursive MagicMock trees."""

    def __init__(self, items):
        self._items = list(items)

    def filter(self, *args, **kwargs):
        return self

    def exclude(self, *args, **kwargs):
        return self

    def select_related(self, *args, **kwargs):
        return self

    def prefetch_related(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def __iter__(self):
        return iter(self._items)


def _stub_queryset(items):
    return _QuerySetStub(items)


# ---------------------------------------------------------------------------
# GET /api/v1/events/
# ---------------------------------------------------------------------------


class TestListEvents:
    def test_anon_list_returns_200(self, client):
        with patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([])):
            response = client.get("/api/v1/events/")

        assert response.status_code == 200

    def test_anon_not_filtered_by_selection(self, client):
        event = make_event()

        with (
            patch("api.views.events.UserCourseSelection.objects.filter") as mock_sel,
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = client.get("/api/v1/events/")

        assert response.status_code == 200
        assert len(response.json()["results"]) == 1
        mock_sel.assert_not_called()

    def test_returns_200(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([])),
        ):
            response = _authed_get(client, session, "/api/v1/events/")

        assert response.status_code == 200

    def test_response_wrapped_in_results(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([])),
        ):
            response = _authed_get(client, session, "/api/v1/events/")

        assert "results" in response.json()

    def test_empty_selection_returns_empty_list(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([])),
        ):
            response = _authed_get(client, session, "/api/v1/events/")

        assert response.json()["results"] == []

    def test_event_shape(self, client):
        session = make_session()
        course = make_course(id=uuid.uuid4(), name="Math", code="MA-01")
        eid = uuid.uuid4()
        start = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 1, 10, 30, 0, tzinfo=timezone.utc)
        event = make_event(
            id=eid,
            title="Lecture",
            body="Introduction",
            author="Prof. Smith",
            course=course,
            start_time=start,
            end_time=end,
            location="Room 101",
            platform="moodle",
        )

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [course.id]

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = _authed_get(client, session, "/api/v1/events/")

        item = response.json()["results"][0]
        assert item["id"] == str(eid)
        assert item["title"] == "Lecture"
        assert item["body"] == "Introduction"
        assert item["author"] == "Prof. Smith"
        assert item["location"] == "Room 101"
        assert item["platform"] == "moodle"
        assert item["course"]["name"] == "Math"
        assert item["course"]["code"] == "MA-01"
        assert "start_time" in item
        assert "end_time" in item
        assert "created_at" in item

    def test_event_with_null_times(self, client):
        session = make_session()
        event = make_event(start_time=None, end_time=None)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [event.course_id]

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = _authed_get(client, session, "/api/v1/events/")

        item = response.json()["results"][0]
        assert item["start_time"] is None
        assert item["end_time"] is None

    def test_event_includes_links(self, client):
        session = make_session()
        link = make_link(url="https://zoom.us/meeting", label="Zoom")
        event = make_event(links=[link])

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [event.course_id]

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = _authed_get(client, session, "/api/v1/events/")

        links = response.json()["results"][0]["links"]
        assert len(links) == 1
        assert links[0]["url"] == "https://zoom.us/meeting"
        assert links[0]["label"] == "Zoom"

    def test_event_includes_attachments(self, client):
        session = make_session()
        att = make_attachment(filename="slides.pdf")
        event = make_event(attachments=[att])

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [event.course_id]

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = _authed_get(client, session, "/api/v1/events/")

        attachments = response.json()["results"][0]["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "slides.pdf"

    def test_course_id_filter_valid(self, client):
        session = make_session()
        course = make_course()
        event = make_event(course=course)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [course.id]

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = _authed_get_params(
                client, session, "/api/v1/events/", {"course_id": str(course.id)}
            )

        assert response.status_code == 200

    def test_course_id_filter_not_in_selection_returns_400(self, client):
        """course_id filter must be in user's selection — TDD §6.7 SEC-3."""
        session = make_session()
        other_course_id = str(uuid.uuid4())

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel):
            response = _authed_get_params(
                client, session, "/api/v1/events/", {"course_id": other_course_id}
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# GET /api/v1/events/{id}/
# ---------------------------------------------------------------------------


class TestGetEvent:
    def test_returns_200_for_owned_event(self, client):
        session = make_session()
        event = make_event()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [event.course_id]

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = _authed_get(client, session, f"/api/v1/events/{event.id}/")

        assert response.status_code == 200

    def test_returns_404_for_unknown_id(self, client):
        session = make_session()
        fake_id = uuid.uuid4()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([])),
        ):
            response = _authed_get(client, session, f"/api/v1/events/{fake_id}/")

        assert response.status_code == 404

    def test_404_uses_standard_error_envelope(self, client):
        """TDD §7: 404 must use {error: {code, message}} envelope."""
        session = make_session()
        fake_id = uuid.uuid4()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([])),
        ):
            response = _authed_get(client, session, f"/api/v1/events/{fake_id}/")

        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "not_found"
        assert "message" in data["error"]

    def test_detail_is_flat_object_not_list(self, client):
        """TDD §6.7: detail returns a single object, not {results: [...]}."""
        session = make_session()
        event = make_event()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [event.course_id]

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = _authed_get(client, session, f"/api/v1/events/{event.id}/")

        data = response.json()
        assert "results" not in data
        assert "id" in data

    def test_detail_contains_correct_id(self, client):
        session = make_session()
        eid = uuid.uuid4()
        event = make_event(id=eid)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [event.course_id]

        with (
            patch("api.views.events.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([event])),
        ):
            response = _authed_get(client, session, f"/api/v1/events/{eid}/")

        assert response.json()["id"] == str(eid)

    def test_anon_detail_without_item_returns_404(self, client):
        fake_id = uuid.uuid4()
        with patch("api.views.events.Event.objects.filter", return_value=_stub_queryset([])):
            response = client.get(f"/api/v1/events/{fake_id}/")

        assert response.status_code == 404
