"""
Tests for:
  GET /api/v1/announcements/
  GET /api/v1/announcements/{id}/
"""

import uuid
from unittest.mock import patch, MagicMock

from tests.conftest import make_session, make_announcement, make_course, make_link, make_attachment


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
# GET /api/v1/announcements/
# ---------------------------------------------------------------------------


class TestListAnnouncements:
    def test_anon_list_returns_200(self, client):
        with patch(
            "api.views.announcements.Announcement.objects.filter",
            return_value=_stub_queryset([]),
        ):
            response = client.get("/api/v1/announcements/")

        assert response.status_code == 200

    def test_returns_200(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        assert response.status_code == 200

    def test_response_wrapped_in_results(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        assert "results" in response.json()

    def test_empty_returns_empty_list(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        assert response.json()["results"] == []

    def test_grouped_by_course(self, client):
        """TDD §6.6: response must be grouped by course, not a flat list."""
        session = make_session()
        course = make_course(name="Math")
        a1 = make_announcement(course=course)
        a2 = make_announcement(course=course)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [course.id]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([a1, a2]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["course"]["name"] == "Math"
        assert len(results[0]["announcements"]) == 2

    def test_multiple_courses_produce_multiple_groups(self, client):
        session = make_session()
        course1 = make_course(name="Math")
        course2 = make_course(name="Physics")
        a1 = make_announcement(course=course1)
        a2 = make_announcement(course=course2)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [course1.id, course2.id]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([a1, a2]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        results = response.json()["results"]
        assert len(results) == 2

    def test_course_shape_in_group(self, client):
        session = make_session()
        cid = uuid.uuid4()
        course = make_course(id=cid, name="Math", code="MA-01")
        announcement = make_announcement(course=course)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [cid]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([announcement]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        course_data = response.json()["results"][0]["course"]
        assert course_data["id"] == str(cid)
        assert course_data["name"] == "Math"
        assert course_data["code"] == "MA-01"

    def test_announcement_shape(self, client):
        session = make_session()
        course = make_course()
        aid = uuid.uuid4()
        announcement = make_announcement(
            id=aid,
            title="Exam postponed",
            body="Details inside",
            author="Prof. Smith",
            course=course,
            platform="teams",
        )

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [course.id]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([announcement]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        item = response.json()["results"][0]["announcements"][0]
        assert item["id"] == str(aid)
        assert item["title"] == "Exam postponed"
        assert item["body"] == "Details inside"
        assert item["author"] == "Prof. Smith"
        assert item["platform"] == "teams"
        assert "created_at" in item

    def test_announcement_includes_links(self, client):
        session = make_session()
        link = make_link(url="https://example.com", label="Link")
        announcement = make_announcement(links=[link])

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [announcement.course_id]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([announcement]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        links = response.json()["results"][0]["announcements"][0]["links"]
        assert len(links) == 1
        assert links[0]["url"] == "https://example.com"

    def test_announcement_includes_attachments(self, client):
        session = make_session()
        att = make_attachment(filename="notice.pdf")
        announcement = make_announcement(attachments=[att])

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [announcement.course_id]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([announcement]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/announcements/")

        attachments = response.json()["results"][0]["announcements"][0]["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "notice.pdf"


# ---------------------------------------------------------------------------
# GET /api/v1/announcements/{id}/
# ---------------------------------------------------------------------------


class TestGetAnnouncement:
    def test_returns_200_for_owned_announcement(self, client):
        session = make_session()
        announcement = make_announcement()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [announcement.course_id]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([announcement]),
            ),
        ):
            response = _authed_get(client, session, f"/api/v1/announcements/{announcement.id}/")

        assert response.status_code == 200

    def test_returns_404_for_unknown_id(self, client):
        session = make_session()
        fake_id = uuid.uuid4()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([]),
            ),
        ):
            response = _authed_get(client, session, f"/api/v1/announcements/{fake_id}/")

        assert response.status_code == 404

    def test_404_uses_standard_error_envelope(self, client):
        """TDD §7: 404 must use {error: {code, message}} envelope."""
        session = make_session()
        fake_id = uuid.uuid4()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([]),
            ),
        ):
            response = _authed_get(client, session, f"/api/v1/announcements/{fake_id}/")

        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "not_found"
        assert "message" in data["error"]

    def test_detail_includes_course_at_top_level(self, client):
        """TDD §6.6: detail response includes course object at top level."""
        session = make_session()
        cid = uuid.uuid4()
        course = make_course(id=cid, name="Math")
        announcement = make_announcement(course=course)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [cid]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([announcement]),
            ),
        ):
            response = _authed_get(client, session, f"/api/v1/announcements/{announcement.id}/")

        data = response.json()
        assert "course" in data
        assert data["course"]["name"] == "Math"

    def test_detail_is_not_wrapped_in_results_list(self, client):
        """Detail endpoint returns a single object, not {results: [...]}."""
        session = make_session()
        announcement = make_announcement()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [announcement.course_id]

        with (
            patch(
                "api.views.announcements.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.announcements.Announcement.objects.filter",
                return_value=_stub_queryset([announcement]),
            ),
        ):
            response = _authed_get(client, session, f"/api/v1/announcements/{announcement.id}/")

        data = response.json()
        assert "results" not in data

    def test_anon_detail_without_item_returns_404(self, client):
        fake_id = uuid.uuid4()

        with patch(
            "api.views.announcements.Announcement.objects.filter",
            return_value=_stub_queryset([]),
        ):
            response = client.get(f"/api/v1/announcements/{fake_id}/")

        assert response.status_code == 404
