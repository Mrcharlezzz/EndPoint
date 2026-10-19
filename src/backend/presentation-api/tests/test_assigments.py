"""
Tests for:
  GET /api/v1/assignments/
  GET /api/v1/assignments/{id}/
"""

import uuid
from unittest.mock import patch, MagicMock

from tests.conftest import make_session, make_assignment, make_course, make_link, make_attachment


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
# GET /api/v1/assignments/
# ---------------------------------------------------------------------------


class TestListAssignments:
    def test_anon_returns_200(self, client):
        with patch(
            "api.views.assignments.Assignment.objects.filter",
            return_value=_stub_queryset([]),
        ):
            response = client.get("/api/v1/assignments/")

        assert response.status_code == 200

    def test_anon_not_filtered_by_selection(self, client):
        assignment = make_assignment()

        with (
            patch("api.views.assignments.UserCourseSelection.objects.filter") as mock_sel,
            patch(
                "api.views.assignments.Assignment.objects.filter",
                return_value=_stub_queryset([assignment]),
            ),
        ):
            response = client.get("/api/v1/assignments/")

        assert response.status_code == 200
        assert len(response.json()["results"]) == 1
        mock_sel.assert_not_called()

    def test_returns_200(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter", return_value=_stub_queryset([])
            ),
        ):
            response = _authed_get(client, session, "/api/v1/assignments/")

        assert response.status_code == 200

    def test_response_wrapped_in_results(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter", return_value=_stub_queryset([])
            ),
        ):
            response = _authed_get(client, session, "/api/v1/assignments/")

        assert "results" in response.json()

    def test_empty_selection_returns_empty_list(self, client):
        session = make_session()
        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter", return_value=_stub_queryset([])
            ),
        ):
            response = _authed_get(client, session, "/api/v1/assignments/")

        assert response.json()["results"] == []

    def test_returns_assignments_for_user_selection(self, client):
        session = make_session()
        course = make_course()
        assignment = make_assignment(course=course)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [course.id]

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter",
                return_value=_stub_queryset([assignment]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/assignments/")

        assert len(response.json()["results"]) == 1

    def test_assignment_shape(self, client):
        session = make_session()
        course = make_course(id=uuid.uuid4(), name="Math", code="MA-01")
        aid = uuid.uuid4()
        assignment = make_assignment(
            id=aid,
            title="Essay",
            body="Write something",
            author="Prof. Smith",
            course=course,
            status="open",
            platform="moodle",
            submission_format="PDF",
            max_points=100.0,
            points=None,
        )

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [course.id]

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter",
                return_value=_stub_queryset([assignment]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/assignments/")

        item = response.json()["results"][0]
        assert item["id"] == str(aid)
        assert item["title"] == "Essay"
        assert item["body"] == "Write something"
        assert item["author"] == "Prof. Smith"
        assert item["status"] is None
        assert item["platform"] == "moodle"
        assert item["submission_format"] == "PDF"
        assert item["max_points"] is None
        assert item["points"] is None
        assert item["course"]["name"] == "Math"
        assert item["course"]["code"] == "MA-01"
        assert "published_at" in item
        assert "created_at" in item

    def test_assignment_includes_links(self, client):
        session = make_session()
        link = make_link(url="https://example.com", label="Docs")
        assignment = make_assignment(links=[link])

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [assignment.course_id]

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter",
                return_value=_stub_queryset([assignment]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/assignments/")

        links = response.json()["results"][0]["links"]
        assert len(links) == 1
        assert links[0]["url"] == "https://example.com"
        assert links[0]["label"] == "Docs"

    def test_assignment_includes_attachments(self, client):
        session = make_session()
        att = make_attachment(
            filename="brief.pdf", mime_type="application/pdf", url="https://example.com/brief.pdf"
        )
        assignment = make_assignment(attachments=[att])

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [assignment.course_id]

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter",
                return_value=_stub_queryset([assignment]),
            ),
        ):
            response = _authed_get(client, session, "/api/v1/assignments/")

        attachments = response.json()["results"][0]["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "brief.pdf"
        assert attachments[0]["mime_type"] == "application/pdf"

    def test_course_id_filter_valid(self, client):
        session = make_session()
        course = make_course()
        assignment = make_assignment(course=course)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [course.id]

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter",
                return_value=_stub_queryset([assignment]),
            ),
        ):
            response = _authed_get_params(
                client, session, "/api/v1/assignments/", {"course_id": str(course.id)}
            )

        assert response.status_code == 200

    def test_course_id_filter_not_in_selection_returns_400(self, client):
        """course_id filter must be in user's selection — TDD §6.5 SEC-3."""
        session = make_session()
        other_course_id = str(uuid.uuid4())

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []  # user has no selections

        with patch(
            "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
        ):
            response = _authed_get_params(
                client, session, "/api/v1/assignments/", {"course_id": other_course_id}
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# GET /api/v1/assignments/{id}/
# ---------------------------------------------------------------------------


class TestGetAssignment:
    def test_returns_200_for_owned_assignment(self, client):
        session = make_session()
        real_id = uuid.uuid4()
        assignment = make_assignment(id=real_id)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [assignment.course_id]

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter",
                return_value=_stub_queryset([assignment]),
            ),
        ):
            response = _authed_get(client, session, f"/api/v1/assignments/{real_id}/")

        assert response.status_code == 200

    def test_returns_404_for_unknown_id(self, client):
        session = make_session()
        fake_id = uuid.uuid4()

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = []

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter", return_value=_stub_queryset([])
            ),
        ):
            response = _authed_get(client, session, f"/api/v1/assignments/{fake_id}/")

        assert response.status_code == 404
        assert response.json() == {
            "error": {"code": "not_found", "message": "Assignment not found"}
        }

    def test_detail_response_contains_correct_id(self, client):
        session = make_session()
        real_id = uuid.uuid4()
        assignment = make_assignment(id=real_id)

        mock_sel = MagicMock()
        mock_sel.values_list.return_value = [assignment.course_id]

        with (
            patch(
                "api.views.assignments.UserCourseSelection.objects.filter", return_value=mock_sel
            ),
            patch(
                "api.views.assignments.Assignment.objects.filter",
                return_value=_stub_queryset([assignment]),
            ),
        ):
            response = _authed_get(client, session, f"/api/v1/assignments/{real_id}/")

        assert response.json()["id"] == str(real_id)

    def test_anon_detail_without_item_returns_404(self, client):
        fake_id = uuid.uuid4()
        with patch(
            "api.views.assignments.Assignment.objects.filter", return_value=_stub_queryset([])
        ):
            response = client.get(f"/api/v1/assignments/{fake_id}/")

        assert response.status_code == 404
