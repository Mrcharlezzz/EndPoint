"""
Tests for:
  GET    /api/v1/users/me/
  DELETE /api/v1/users/me/

NOTE: delete_me currently returns 200 with a body instead of 204 No Content
(BUG-2). The DELETE test reflects current behaviour and is marked with a
comment. Update the assertion to `assert response.status_code == 204` once
BUG-2 is fixed.

NOTE: DELETE is currently at /api/v1/users/me/delete/ (BUG-3). Once BUG-3
is fixed and both methods live on /api/v1/users/me/, the delete URL below
should be updated.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from tests.conftest import make_user, make_session


def _authed_get(client, session, url):
    with patch("api.middleware.Session.objects.select_related") as mock_qs:
        mock_qs.return_value.get.return_value = session
        return client.get(url, HTTP_AUTHORIZATION=f"Bearer {session.token}")


def _authed_delete(client, session, url):
    with patch("api.middleware.Session.objects.select_related") as mock_qs:
        mock_qs.return_value.get.return_value = session
        return client.delete(url, HTTP_AUTHORIZATION=f"Bearer {session.token}")


def _authed_post_json(client, session, url, payload):
    with patch("api.middleware.Session.objects.select_related") as mock_qs:
        mock_qs.return_value.get.return_value = session
        return client.post(
            url,
            data=payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {session.token}",
        )


# ---------------------------------------------------------------------------
# GET /api/v1/users/me/
# ---------------------------------------------------------------------------


class TestGetMe:
    def test_returns_200(self, client):
        user = make_user()
        session = make_session(user=user)
        response = _authed_get(client, session, "/api/v1/users/me/")
        assert response.status_code == 200

    def test_response_contains_id(self, client):
        user = make_user()
        session = make_session(user=user)
        response = _authed_get(client, session, "/api/v1/users/me/")
        assert response.json()["id"] == str(user.id)

    def test_response_contains_email(self, client):
        user = make_user(email="sample1@example.edu")
        session = make_session(user=user)
        response = _authed_get(client, session, "/api/v1/users/me/")
        assert response.json()["email"] == "sample1@example.edu"

    def test_response_contains_display_name(self, client):
        user = make_user(display_name="Example Student")
        session = make_session(user=user)
        response = _authed_get(client, session, "/api/v1/users/me/")
        assert response.json()["display_name"] == "Example Student"

    def test_response_contains_created_at(self, client):
        user = make_user()
        session = make_session(user=user)
        response = _authed_get(client, session, "/api/v1/users/me/")
        assert "created_at" in response.json()

    def test_response_does_not_contain_updated_at(self, client):
        """updated_at exists in the DB model but must not be exposed per TDD §6.2."""
        user = make_user()
        session = make_session(user=user)
        response = _authed_get(client, session, "/api/v1/users/me/")
        assert "updated_at" not in response.json()

    def test_response_does_not_contain_ms_oid(self, client):
        """ms_oid is an internal identity key and must never be exposed."""
        user = make_user()
        session = make_session(user=user)
        response = _authed_get(client, session, "/api/v1/users/me/")
        assert "ms_oid" not in response.json()

    def test_requires_auth_token(self, client):
        response = client.get("/api/v1/users/me/")
        assert response.status_code == 401

    def test_invalid_token_returns_401(self, client):
        with patch("api.middleware.Session.objects.select_related") as mock_qs:
            mock_qs.return_value.get.side_effect = Exception("DoesNotExist")
            with patch("api.models.identity.Session.DoesNotExist", Exception):
                response = client.get(
                    "/api/v1/users/me/",
                    HTTP_AUTHORIZATION="Bearer invalid-token",
                )
        assert response.status_code == 401

    def test_missing_bearer_prefix_returns_401(self, client):
        response = client.get(
            "/api/v1/users/me/",
            HTTP_AUTHORIZATION="invalid-token",
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/v1/users/me/
# NOTE: URL is /users/me/delete/ until BUG-3 is fixed.
# Update to /api/v1/users/me/ after BUG-3 fix.
# ---------------------------------------------------------------------------


class TestDeleteMe:
    def test_delete_calls_user_delete(self, client):
        user = make_user()
        session = make_session(user=user)
        _authed_delete(client, session, "/api/v1/users/me/")
        user.delete.assert_called_once()

    def test_delete_returns_success(self, client):
        user = make_user()
        session = make_session(user=user)
        response = _authed_delete(client, session, "/api/v1/users/me/")
        assert response.status_code == 204

    def test_delete_requires_auth(self, client):
        response = client.delete("/api/v1/users/me/")
        assert response.status_code == 401

    def test_delete_invalid_token_returns_401(self, client):
        with patch("api.middleware.Session.objects.select_related") as mock_qs:
            from api.models.identity import Session as SessionModel

            mock_qs.return_value.get.side_effect = SessionModel.DoesNotExist
            response = client.delete(
                "/api/v1/users/me/",
                HTTP_AUTHORIZATION="Bearer bogus",
            )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET|POST|DELETE /api/v1/users/me/read-state/
# ---------------------------------------------------------------------------


class TestReadState:
    def test_get_returns_read_state_map(self, client):
        user = make_user()
        session = make_session(user=user)
        item_id = uuid.uuid4()
        read_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with patch("api.middleware.Session.objects.select_related") as mock_auth:
            mock_auth.return_value.get.return_value = session
            with patch("api.views.users.ItemReadState.objects") as mock_read_state:
                mock_read_state.filter.return_value.values_list.return_value = [(item_id, read_at)]
                response = client.get(
                    "/api/v1/users/me/read-state/",
                    HTTP_AUTHORIZATION=f"Bearer {session.token}",
                )

        assert response.status_code == 200
        assert response.json() == {str(item_id): read_at.isoformat()}
        mock_read_state.filter.assert_called_once_with(user=user)

    def test_post_marks_items_as_read(self, client):
        user = make_user()
        session = make_session(user=user)
        item_id = uuid.uuid4()

        with patch("api.views.users.timezone.now") as mock_now:
            mock_now.return_value = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            with patch("api.views.users.ItemReadState.objects") as mock_read_state:
                response = _authed_post_json(
                    client,
                    session,
                    "/api/v1/users/me/read-state/",
                    {"item_ids": [str(item_id)]},
                )

        assert response.status_code == 204
        mock_read_state.bulk_create.assert_called_once()
        created_rows = mock_read_state.bulk_create.call_args.args[0]
        assert len(created_rows) == 1
        assert created_rows[0].user_id == user.id
        assert created_rows[0].item_id == item_id
        assert mock_read_state.bulk_create.call_args.kwargs == {
            "update_conflicts": True,
            "update_fields": ["read_at"],
            "unique_fields": ["user", "item_id"],
        }

    def test_post_rejects_invalid_item_ids(self, client):
        user = make_user()
        session = make_session(user=user)

        response = _authed_post_json(
            client,
            session,
            "/api/v1/users/me/read-state/",
            {"item_ids": ["not-a-uuid"]},
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"

    def test_delete_clears_read_state(self, client):
        user = make_user()
        session = make_session(user=user)

        with patch("api.middleware.Session.objects.select_related") as mock_auth:
            mock_auth.return_value.get.return_value = session
            with patch("api.views.users.ItemReadState.objects") as mock_read_state:
                response = client.delete(
                    "/api/v1/users/me/read-state/",
                    HTTP_AUTHORIZATION=f"Bearer {session.token}",
                )

        assert response.status_code == 204
        mock_read_state.filter.assert_called_once_with(user=user)
        mock_read_state.filter.return_value.delete.assert_called_once_with()
