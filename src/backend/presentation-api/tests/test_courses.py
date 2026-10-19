"""
Tests for:
  GET /api/v1/courses/
  GET /api/v1/users/me/courses/
  PUT /api/v1/users/me/courses/
"""

import uuid
from unittest.mock import patch, MagicMock

from tests.conftest import make_user, make_session, make_course
import pytest


def _authed(client, session, method, url, **kwargs):
    fn = getattr(client, method)
    with patch("api.middleware.Session.objects.select_related") as mock_qs:
        mock_qs.return_value.get.return_value = session
        return fn(url, HTTP_AUTHORIZATION=f"Bearer {session.token}", **kwargs)


def _authed_with_cookie(client, session, method, url, **kwargs):
    fn = getattr(client, method)
    with patch("api.middleware.Session.objects.select_related") as mock_qs:
        mock_qs.return_value.get.return_value = session
        client.cookies["endpoint_session"] = session.token
        return fn(url, **kwargs)


# ---------------------------------------------------------------------------
# GET /api/v1/courses/
# ---------------------------------------------------------------------------


class TestListCourses:
    def _mock_courses(self, courses):
        mock_qs = MagicMock()
        mock_qs.values.return_value = [
            {"id": c.id, "name": c.name, "code": c.code, "major": c.major, "semester": c.semester}
            for c in courses
        ]
        return mock_qs

    def test_returns_200(self, client):
        session = make_session()
        with patch("api.views.courses.Course.objects.all", return_value=self._mock_courses([])):
            response = _authed(client, session, "get", "/api/v1/courses/")
        assert response.status_code == 200

    def test_response_wrapped_in_results(self, client):
        session = make_session()
        with patch("api.views.courses.Course.objects.all", return_value=self._mock_courses([])):
            response = _authed(client, session, "get", "/api/v1/courses/")
        assert "results" in response.json()

    def test_returns_all_courses(self, client):
        session = make_session()
        courses = [make_course(name="Math"), make_course(name="Physics")]
        with patch(
            "api.views.courses.Course.objects.all", return_value=self._mock_courses(courses)
        ):
            response = _authed(client, session, "get", "/api/v1/courses/")
        assert len(response.json()["results"]) == 2

    def test_course_shape(self, client):
        session = make_session()
        cid = uuid.uuid4()
        course = make_course(id=cid, name="Math", code="MA-01", major="Science", semester="WS26")
        with patch(
            "api.views.courses.Course.objects.all", return_value=self._mock_courses([course])
        ):
            response = _authed(client, session, "get", "/api/v1/courses/")
        item = response.json()["results"][0]
        assert item["id"] == str(cid)
        assert item["name"] == "Math"
        assert item["code"] == "MA-01"
        assert item["major"] == "Science"
        assert item["semester"] == "WS26"

    def test_course_with_null_fields(self, client):
        session = make_session()
        course = make_course(code=None, major=None, semester=None)
        with patch(
            "api.views.courses.Course.objects.all", return_value=self._mock_courses([course])
        ):
            response = _authed(client, session, "get", "/api/v1/courses/")
        item = response.json()["results"][0]
        assert item["code"] is None
        assert item["major"] is None
        assert item["semester"] is None

    def test_empty_course_list(self, client):
        session = make_session()
        with patch("api.views.courses.Course.objects.all", return_value=self._mock_courses([])):
            response = _authed(client, session, "get", "/api/v1/courses/")
        assert response.json()["results"] == []

    def test_anon_list_returns_200(self, client):
        with patch("api.views.courses.Course.objects.all", return_value=self._mock_courses([])):
            response = client.get("/api/v1/courses/")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/users/me/courses/
# ---------------------------------------------------------------------------


class TestGetUserCourses:
    def _setup(self, user, course_ids, courses):
        mock_selections = MagicMock()
        mock_selections.values_list.return_value = course_ids

        mock_course_qs = MagicMock()
        mock_course_qs.values.return_value = [
            {"id": c.id, "name": c.name, "code": c.code, "major": c.major, "semester": c.semester}
            for c in courses
        ]
        return mock_selections, mock_course_qs

    def test_returns_200(self, client):
        user = make_user()
        session = make_session(user=user)
        mock_sel, mock_cqs = self._setup(user, [], [])

        with (
            patch("api.views.courses.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.courses.Course.objects.filter", return_value=mock_cqs),
        ):
            response = _authed(client, session, "get", "/api/v1/users/me/courses/")

        assert response.status_code == 200

    def test_response_wrapped_in_results(self, client):
        user = make_user()
        session = make_session(user=user)
        mock_sel, mock_cqs = self._setup(user, [], [])

        with (
            patch("api.views.courses.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.courses.Course.objects.filter", return_value=mock_cqs),
        ):
            response = _authed(client, session, "get", "/api/v1/users/me/courses/")

        assert "results" in response.json()

    def test_uses_course_id_key_not_id(self, client):
        """TDD §6.3: response field must be course_id, not id."""
        user = make_user()
        session = make_session(user=user)
        cid = uuid.uuid4()
        course = make_course(id=cid)
        mock_sel, mock_cqs = self._setup(user, [cid], [course])

        with (
            patch("api.views.courses.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.courses.Course.objects.filter", return_value=mock_cqs),
        ):
            response = _authed(client, session, "get", "/api/v1/users/me/courses/")

        item = response.json()["results"][0]
        assert "course_id" in item
        assert "id" not in item

    def test_empty_selection_returns_empty_list(self, client):
        user = make_user()
        session = make_session(user=user)
        mock_sel, mock_cqs = self._setup(user, [], [])

        with (
            patch("api.views.courses.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.courses.Course.objects.filter", return_value=mock_cqs),
        ):
            response = _authed(client, session, "get", "/api/v1/users/me/courses/")

        assert response.json()["results"] == []

    def test_requires_auth(self, client):
        response = client.get("/api/v1/users/me/courses/")
        assert response.status_code == 401

    def test_accepts_session_cookie_when_authorization_header_is_missing(self, client):
        user = make_user()
        session = make_session(user=user)
        mock_sel, mock_cqs = self._setup(user, [], [])

        with (
            patch("api.views.courses.UserCourseSelection.objects.filter", return_value=mock_sel),
            patch("api.views.courses.Course.objects.filter", return_value=mock_cqs),
        ):
            response = _authed_with_cookie(client, session, "get", "/api/v1/users/me/courses/")

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# PUT /api/v1/users/me/courses/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPutUserCourses:
    def _put(self, client, session, body):
        with patch("api.middleware.Session.objects.select_related") as mock_qs:
            mock_qs.return_value.get.return_value = session
            return client.put(
                "/api/v1/users/me/courses/",
                data=body,
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {session.token}",
            )

    def test_returns_200_on_valid_ids(self, client):
        user = make_user()
        session = make_session(user=user)
        cid = uuid.uuid4()

        mock_existing_qs = MagicMock()
        mock_existing_qs.values_list.return_value = [cid]

        with (
            patch("api.views.courses.Course.objects.filter", return_value=mock_existing_qs),
            patch("api.views.courses.UserCourseSelection.objects.filter") as mock_sel_filter,
            patch("api.views.courses.UserCourseSelection.objects.bulk_create"),
        ):
            mock_sel_filter.return_value.delete = MagicMock()
            response = self._put(client, session, {"course_ids": [str(cid)]})

        assert response.status_code == 200

    def test_deletes_existing_selections_before_insert(self, client):
        """Full replace semantics: old selections must be deleted first."""
        user = make_user()
        session = make_session(user=user)
        cid = uuid.uuid4()

        mock_existing_qs = MagicMock()
        mock_existing_qs.values_list.return_value = [cid]

        mock_delete = MagicMock()
        mock_sel_filter_qs = MagicMock()
        mock_sel_filter_qs.delete = mock_delete

        with (
            patch("api.views.courses.Course.objects.filter", return_value=mock_existing_qs),
            patch(
                "api.views.courses.UserCourseSelection.objects.filter",
                return_value=mock_sel_filter_qs,
            ),
            patch("api.views.courses.UserCourseSelection.objects.bulk_create"),
        ):
            self._put(client, session, {"course_ids": [str(cid)]})

        mock_delete.assert_called_once()

    def test_invalid_course_id_returns_400(self, client):
        user = make_user()
        session = make_session(user=user)
        fake_id = str(uuid.uuid4())

        mock_existing_qs = MagicMock()
        mock_existing_qs.values_list.return_value = []  # no matching courses

        with patch("api.views.courses.Course.objects.filter", return_value=mock_existing_qs):
            response = self._put(client, session, {"course_ids": [fake_id]})

        assert response.status_code == 400

    def test_empty_list_deletes_all_selections(self, client):
        """Sending [] is valid and removes all selections (full replace)."""
        user = make_user()
        session = make_session(user=user)

        mock_existing_qs = MagicMock()
        mock_existing_qs.values_list.return_value = []

        mock_course_qs = MagicMock()
        mock_course_qs.values.return_value = []

        with (
            patch("api.views.courses.Course.objects.filter", return_value=mock_existing_qs),
            patch("api.views.courses.UserCourseSelection.objects.filter") as mock_sel_filter,
            patch("api.views.courses.UserCourseSelection.objects.bulk_create"),
        ):
            mock_delete = MagicMock()
            mock_sel_filter.return_value.delete = mock_delete
            response = self._put(client, session, {"course_ids": []})

        assert response.status_code == 200
        mock_delete.assert_called_once()

    def test_response_wrapped_in_results(self, client):
        user = make_user()
        session = make_session(user=user)

        mock_existing_qs = MagicMock()
        mock_existing_qs.values_list.return_value = []

        mock_course_qs = MagicMock()
        mock_course_qs.values.return_value = []

        with (
            patch("api.views.courses.Course.objects.filter", return_value=mock_existing_qs),
            patch("api.views.courses.UserCourseSelection.objects.filter") as mock_sel_filter,
            patch("api.views.courses.UserCourseSelection.objects.bulk_create"),
        ):
            mock_sel_filter.return_value.delete = MagicMock()
            response = self._put(client, session, {"course_ids": []})

        assert "results" in response.json()

    def test_requires_auth(self, client):
        response = client.put(
            "/api/v1/users/me/courses/",
            data={"course_ids": []},
            content_type="application/json",
        )
        assert response.status_code == 401
