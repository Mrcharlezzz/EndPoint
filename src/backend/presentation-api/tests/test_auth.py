"""Tests for POST /api/v1/auth/moodle/."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client

from tests.conftest import make_session, make_user


@pytest.fixture
def client():
    return Client()


def _post_login(client, body):
    return client.post(
        "/api/v1/auth/moodle/",
        data=body,
        content_type="application/json",
    )


def _identity(
    *,
    email="sample1@example.edu",
    full_name="Sample User",
    token="moodle-token",
    user_id=42,
):
    return SimpleNamespace(
        email=email,
        full_name=full_name,
        token=token,
        user_id=user_id,
    )


class TestMoodleLoginSuccess:
    def test_returns_200(self, client):
        user = make_user(email="sample1@example.edu")
        session = make_session(user=user)

        with (
            patch("api.views.auth.moodle_authenticate", return_value=_identity()),
            patch("api.views.auth._establish_session", return_value=(user, session)),
            patch("api.views.auth.set_session_cookie"),
        ):
            response = _post_login(client, {"username": "sample", "password": "secret"})

        assert response.status_code == 200

    def test_response_contains_user_shape(self, client):
        user = make_user(email="sample1@example.edu", display_name="Sample User")
        session = make_session(user=user)

        with (
            patch("api.views.auth.moodle_authenticate", return_value=_identity()),
            patch("api.views.auth._establish_session", return_value=(user, session)),
            patch("api.views.auth.set_session_cookie"),
        ):
            response = _post_login(client, {"username": "sample", "password": "secret"})

        user_data = response.json()["user"]
        assert user_data["id"] == str(user.id)
        assert user_data["email"] == "sample1@example.edu"
        assert user_data["display_name"] == "Sample User"

    def test_response_contains_expires_at(self, client):
        user = make_user()
        session = make_session(user=user)

        with (
            patch("api.views.auth.moodle_authenticate", return_value=_identity()),
            patch("api.views.auth._establish_session", return_value=(user, session)),
            patch("api.views.auth.set_session_cookie"),
        ):
            response = _post_login(client, {"username": "sample", "password": "secret"})

        assert "expires_at" in response.json()

    def test_sets_session_cookie(self, client):
        user = make_user()
        session = make_session(user=user)

        with (
            patch("api.views.auth.moodle_authenticate", return_value=_identity()),
            patch("api.views.auth._establish_session", return_value=(user, session)),
            patch("api.views.auth.set_session_cookie") as mock_set_cookie,
        ):
            _post_login(client, {"username": "sample", "password": "secret"})

        mock_set_cookie.assert_called_once()

    def test_establishes_session_from_moodle_identity(self, client):
        user = make_user(email="sample1@example.edu")
        session = make_session(user=user)
        identity = _identity(
            email="sample1@example.edu",
            full_name="Example Student",
            token="moodle-token-xyz",
            user_id=123,
        )

        with (
            patch("api.views.auth.moodle_authenticate", return_value=identity),
            patch(
                "api.views.auth._establish_session", return_value=(user, session)
            ) as mock_establish,
            patch("api.views.auth.set_session_cookie"),
        ):
            _post_login(client, {"username": "sample", "password": "secret"})

        mock_establish.assert_called_once()
        assert mock_establish.call_args.kwargs == {
            "email": "sample1@example.edu",
            "display_name": "Example Student",
            "platform": "moodle",
            "payload": {"token": "moodle-token-xyz", "user_id": 123},
        }

    def test_calls_moodle_authenticate_with_username_and_password(self, client):
        user = make_user()
        session = make_session(user=user)

        with (
            patch(
                "api.views.auth.moodle_authenticate", return_value=_identity()
            ) as mock_authenticate,
            patch("api.views.auth._establish_session", return_value=(user, session)),
            patch("api.views.auth.set_session_cookie"),
        ):
            _post_login(client, {"username": "sample", "password": "secret"})

        mock_authenticate.assert_called_once_with("sample", "secret")


class TestMoodleLoginValidation:
    def test_missing_username_returns_400(self, client):
        response = _post_login(client, {"password": "secret"})
        assert response.status_code == 400

    def test_missing_username_error_envelope(self, client):
        response = _post_login(client, {"password": "secret"})
        data = response.json()
        assert data["error"]["code"] == "validation_error"
        assert "message" in data["error"]

    def test_empty_string_username_returns_400(self, client):
        response = _post_login(client, {"username": "", "password": "secret"})
        assert response.status_code == 400

    def test_missing_password_returns_400(self, client):
        response = _post_login(client, {"username": "sample"})
        assert response.status_code == 400

    def test_empty_body_returns_400(self, client):
        response = client.post(
            "/api/v1/auth/moodle/",
            data="",
            content_type="application/json",
        )
        assert response.status_code == 400


class TestMoodleLoginNoAuthRequired:
    def test_login_accessible_without_token(self, client):
        response = _post_login(client, {"username": "sample"})
        assert response.status_code != 401
