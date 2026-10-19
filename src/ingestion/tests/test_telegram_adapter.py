"""Tests for TelegramAdapter — polling, cursor, credential validation, error boundary."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.telegram.adapter import TelegramAdapter
from ingestion.config import TelegramConfig
from ingestion.schemas import (
    ContentType,
    ErrorCode,
    FetchResult,
    FetchScope,
)

BOT_TOKEN = "TEST-TOKEN"
API_BASE = "https://api.telegram.org"
GET_UPDATES_URL = f"{API_BASE}/bot{BOT_TOKEN}/getUpdates"
GET_ME_URL = f"{API_BASE}/bot{BOT_TOKEN}/getMe"
GET_CHAT_URL = f"{API_BASE}/bot{BOT_TOKEN}/getChat"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def config() -> TelegramConfig:
    return TelegramConfig(
        api_base_url=API_BASE,
        max_retries=0,
        request_timeout=5,
        requests_per_second=0,
        poll_timeout=0,
        max_updates=100,
    )


@pytest.fixture
def adapter(config: TelegramConfig) -> TelegramAdapter:
    return TelegramAdapter(config, sleep_func=_noop_sleep)


@pytest.fixture
def credentials() -> dict:
    return {"bot_token": BOT_TOKEN}


def _make_update(
    update_id: int,
    message_id: int = 1,
    text: str = "Hello",
    chat_id: int = -1001234,
    chat_title: str = "CS101 Group",
    chat_type: str = "supergroup",
    from_first: str = "John",
    from_last: str = "Doe",
    date: int = 1710288000,
    key: str = "message",
) -> dict:
    msg = {
        "message_id": message_id,
        "text": text,
        "date": date,
        "chat": {"id": chat_id, "title": chat_title, "type": chat_type},
        "from": {"id": 999, "first_name": from_first, "last_name": from_last},
    }
    return {"update_id": update_id, key: msg}


def _updates_response(*updates: dict) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": list(updates)})


def _mock_get_chat(
    *,
    username: str | None = None,
    chat_type: str = "supergroup",
) -> respx.Route:
    result: dict[str, object] = {
        "id": -1001234,
        "type": chat_type,
        "title": "CS101 Group",
    }
    if username is not None:
        result["username"] = username
    return respx.get(url=GET_CHAT_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": result})
    )


class TestProtocolCompliance:
    def test_platform_attribute(self, adapter: TelegramAdapter) -> None:
        assert adapter.platform == "telegram"

    @respx.mock
    async def test_test_connection_success(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_ME_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"id": 123, "is_bot": True, "username": "test_bot"},
                },
            )
        )
        assert await adapter.test_connection(credentials) is True

    @respx.mock
    async def test_test_connection_failure_bad_token(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_ME_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "ok": False,
                    "error_code": 401,
                    "description": "Unauthorized",
                },
            )
        )
        assert await adapter.test_connection(credentials) is False

    @respx.mock
    async def test_test_connection_failure_network(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_ME_URL).mock(side_effect=httpx.ConnectError("refused"))
        assert await adapter.test_connection(credentials) is False


class TestCredentialValidation:
    async def test_missing_bot_token(self, adapter: TelegramAdapter) -> None:
        result = await adapter.fetch({})
        assert isinstance(result, FetchResult)
        assert len(result.errors) == 1
        assert "bot_token" in result.errors[0].message.lower()

    async def test_empty_bot_token(self, adapter: TelegramAdapter) -> None:
        result = await adapter.fetch({"bot_token": ""})
        assert len(result.errors) == 1


class TestScopeRouting:
    @respx.mock
    async def test_messages_scope(self, adapter: TelegramAdapter, credentials: dict) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(
                _make_update(100, message_id=1, text="Hello"),
                _make_update(101, message_id=2, text="World"),
            )
        )
        chat_route = _mock_get_chat()
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.MESSAGES}))
        assert len(result.items) == 2
        assert result.items[0].content_type == ContentType.RAW_MESSAGE
        assert result.items[0].body == "Hello"
        assert result.items[1].body == "World"
        assert chat_route.call_count == 1

    @respx.mock
    async def test_public_group_source_url_from_chat_username(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(
                _make_update(100, message_id=78, chat_id=-1003579184587),
            )
        )
        _mock_get_chat(username="ctms22_course")

        result = await adapter.fetch(credentials)

        assert len(result.items) == 1
        assert result.items[0].source_url == "https://t.me/ctms22_course/78"

    @respx.mock
    async def test_private_group_source_url_from_internal_chat_id(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(
                _make_update(100, message_id=78, chat_id=-1003579184587),
            )
        )
        _mock_get_chat(username=None)

        result = await adapter.fetch(credentials)

        assert len(result.items) == 1
        assert result.items[0].source_url == "https://t.me/c/3579184587/78"

    @respx.mock
    async def test_unsupported_scope_skipped(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        """ASSIGNMENTS scope is unsupported for Telegram — skipped with no error."""
        result = await adapter.fetch(credentials, scope=frozenset({FetchScope.ASSIGNMENTS}))
        assert result.items == []
        assert result.errors == []

    @respx.mock
    async def test_default_scope_is_messages(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_UPDATES_URL).mock(return_value=_updates_response(_make_update(100)))
        _mock_get_chat()
        result = await adapter.fetch(credentials)
        assert len(result.items) == 1


class TestChatFiltering:
    @respx.mock
    async def test_filters_private_messages(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        """Only group/supergroup messages should be included."""
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(
                _make_update(100, chat_type="private", text="Private"),
                _make_update(101, chat_type="supergroup", text="Group"),
                _make_update(102, chat_type="group", text="Group2"),
            )
        )
        chat_route = _mock_get_chat()
        result = await adapter.fetch(credentials)
        assert len(result.items) == 2
        assert all(item.body in ("Group", "Group2") for item in result.items)
        assert chat_route.call_count == 1

    @respx.mock
    async def test_filters_channel_posts(self, adapter: TelegramAdapter, credentials: dict) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(
                _make_update(100, chat_type="channel", text="Channel post"),
                _make_update(101, chat_type="supergroup", text="Supergroup"),
            )
        )
        _mock_get_chat()
        result = await adapter.fetch(credentials)
        assert len(result.items) == 1
        assert result.items[0].body == "Supergroup"


class TestEditedMessages:
    @respx.mock
    async def test_edited_message_processed(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        """Decision #17: edited_message uses same external_id."""
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(
                _make_update(100, message_id=42, text="Edited text", key="edited_message"),
            )
        )
        _mock_get_chat()
        result = await adapter.fetch(credentials)
        assert len(result.items) == 1
        assert result.items[0].external_id == "tg_-1001234_42"
        assert result.items[0].body == "Edited text"


class TestCursorManagement:
    @respx.mock
    async def test_cursor_contains_last_update_id(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(
                _make_update(100),
                _make_update(101),
                _make_update(102),
            )
        )
        _mock_get_chat()
        result = await adapter.fetch(credentials)
        assert result.cursor is not None
        assert result.cursor["last_update_id"] == 102

    @respx.mock
    async def test_cursor_used_as_offset(self, adapter: TelegramAdapter, credentials: dict) -> None:
        """Cursor last_update_id should be used as offset+1 for getUpdates."""
        route = respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(_make_update(200))
        )
        _mock_get_chat()
        await adapter.fetch(credentials, cursor={"last_update_id": 150})
        request = route.calls[0].request
        assert "offset=151" in str(request.url)

    @respx.mock
    async def test_no_cursor_no_offset(self, adapter: TelegramAdapter, credentials: dict) -> None:
        route = respx.get(url=GET_UPDATES_URL).mock(return_value=_updates_response())
        await adapter.fetch(credentials)
        request = route.calls[0].request
        # No offset param should be present (or offset=0)
        url_str = str(request.url)
        assert "offset=0" in url_str or "offset" not in url_str

    @respx.mock
    async def test_empty_result_preserves_cursor(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_UPDATES_URL).mock(return_value=_updates_response())
        result = await adapter.fetch(credentials, cursor={"last_update_id": 50})
        assert result.cursor is not None
        assert result.cursor["last_update_id"] == 50


class TestErrorBoundary:
    @respx.mock
    async def test_token_expired_captured(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "ok": False,
                    "error_code": 401,
                    "description": "Unauthorized",
                },
            )
        )
        result = await adapter.fetch(credentials)
        assert len(result.errors) >= 1
        token_errors = [e for e in result.errors if e.code == ErrorCode.TOKEN_EXPIRED]
        assert len(token_errors) >= 1
        assert token_errors[0].recoverable is False

    @respx.mock
    async def test_platform_unavailable_captured(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        result = await adapter.fetch(credentials)
        assert len(result.errors) >= 1

    @respx.mock
    async def test_connection_error_captured(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        respx.get(url=GET_UPDATES_URL).mock(side_effect=httpx.ConnectError("refused"))
        result = await adapter.fetch(credentials)
        assert len(result.errors) >= 1


class TestStats:
    @respx.mock
    async def test_stats_populated(self, adapter: TelegramAdapter, credentials: dict) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=_updates_response(
                _make_update(100, text="one"),
                _make_update(101, text="two"),
            )
        )
        _mock_get_chat()
        result = await adapter.fetch(credentials)
        assert result.stats.items_fetched == 2
        assert result.stats.duration_seconds > 0


class TestAttachmentsInFetch:
    @respx.mock
    async def test_files_extracted_from_messages(
        self, adapter: TelegramAdapter, credentials: dict
    ) -> None:
        update = _make_update(100)
        update["message"]["document"] = {
            "file_id": "BQACAgIAAxk",
            "file_name": "notes.pdf",
            "mime_type": "application/pdf",
            "file_size": 1024,
        }
        respx.get(url=GET_UPDATES_URL).mock(return_value=_updates_response(update))
        _mock_get_chat()
        result = await adapter.fetch(credentials)
        assert len(result.items) == 1
        assert len(result.items[0].attachments) == 1
        # Files in the FetchResult.files list
        assert len(result.files) >= 1
