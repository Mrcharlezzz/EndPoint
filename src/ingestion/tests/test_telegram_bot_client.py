"""Tests for TelegramBotClient — GET-based Bot API, error detection, retry."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.telegram.bot_client import TelegramBotClient
from ingestion.exceptions import (
    PlatformUnavailableError,
    RateLimitError,
    TokenExpiredError,
)

BOT_TOKEN = "TEST-TOKEN"
API_BASE = "https://api.telegram.org"
GET_UPDATES_URL = f"{API_BASE}/bot{BOT_TOKEN}/getUpdates"
GET_ME_URL = f"{API_BASE}/bot{BOT_TOKEN}/getMe"
GET_CHAT_URL = f"{API_BASE}/bot{BOT_TOKEN}/getChat"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def client() -> TelegramBotClient:
    return TelegramBotClient(
        bot_token=BOT_TOKEN,
        api_base_url=API_BASE,
        max_retries=0,
        timeout=5,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )


@pytest.fixture
def retry_client() -> TelegramBotClient:
    return TelegramBotClient(
        bot_token=BOT_TOKEN,
        api_base_url=API_BASE,
        max_retries=2,
        timeout=5,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )


class TestGetUpdates:
    @respx.mock
    async def test_get_updates_success(self, client: TelegramBotClient) -> None:
        """getUpdates returns list of Update objects on success."""
        route = respx.get(url=GET_UPDATES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 100, "message": {"message_id": 1, "text": "hi"}},
                        {"update_id": 101, "message": {"message_id": 2, "text": "hello"}},
                    ],
                },
            )
        )
        updates = await client.get_updates(offset=99, timeout=0, limit=100)
        assert len(updates) == 2
        assert updates[0]["update_id"] == 100
        assert route.called

    @respx.mock
    async def test_get_updates_sends_params(self, client: TelegramBotClient) -> None:
        """Verify offset, timeout, limit are sent as query params."""
        route = respx.get(url=GET_UPDATES_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": []})
        )
        await client.get_updates(offset=42, timeout=30, limit=50)
        assert route.called
        request = route.calls[0].request
        assert "offset=42" in str(request.url)
        assert "timeout=30" in str(request.url)
        assert "limit=50" in str(request.url)

    @respx.mock
    async def test_get_updates_empty(self, client: TelegramBotClient) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": []})
        )
        updates = await client.get_updates()
        assert updates == []


class TestGetMe:
    @respx.mock
    async def test_get_me_success(self, client: TelegramBotClient) -> None:
        respx.get(url=GET_ME_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": 123456,
                        "is_bot": True,
                        "first_name": "EndpointBot",
                        "username": "endpoint_bot",
                    },
                },
            )
        )
        me = await client.get_me()
        assert me["id"] == 123456
        assert me["is_bot"] is True
        assert me["username"] == "endpoint_bot"


class TestGetChatMeta:
    @respx.mock
    async def test_get_chat_meta_fetches_username_and_type(self, client: TelegramBotClient) -> None:
        route = respx.get(url=GET_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": -1003579184587,
                        "type": "supergroup",
                        "username": "ctms22_course",
                    },
                },
            )
        )

        meta = await client.get_chat_meta(-1003579184587)

        assert meta.username == "ctms22_course"
        assert meta.type == "supergroup"
        assert route.call_count == 1
        assert "chat_id=-1003579184587" in str(route.calls[0].request.url)

    @respx.mock
    async def test_get_chat_meta_normalizes_missing_username(
        self, client: TelegramBotClient
    ) -> None:
        respx.get(url=GET_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"id": -1003579184587, "type": "supergroup"},
                },
            )
        )

        meta = await client.get_chat_meta(-1003579184587)

        assert meta.username is None
        assert meta.type == "supergroup"

    @respx.mock
    async def test_get_chat_meta_caches_per_client(self, client: TelegramBotClient) -> None:
        route = respx.get(url=GET_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "id": -1003579184587,
                        "type": "supergroup",
                        "username": "ctms22_course",
                    },
                },
            )
        )

        first = await client.get_chat_meta(-1003579184587)
        second = await client.get_chat_meta(-1003579184587)

        assert first == second
        assert route.call_count == 1


class TestBuildFileUrl:
    def test_build_file_url(self, client: TelegramBotClient) -> None:
        url = client.build_file_url("documents/file_42.pdf")
        assert url == f"{API_BASE}/file/bot{BOT_TOKEN}/documents/file_42.pdf"


class TestTelegramErrorDetection:
    @respx.mock
    async def test_unauthorized_401_raises(self, client: TelegramBotClient) -> None:
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
        with pytest.raises(TokenExpiredError):
            await client.get_me()

    @respx.mock
    async def test_api_error_not_found(self, client: TelegramBotClient) -> None:
        """Telegram returns 200 with ok=false for some errors."""
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": False,
                    "error_code": 404,
                    "description": "Not Found",
                },
            )
        )
        with pytest.raises(PlatformUnavailableError, match="Not Found"):
            await client.get_updates()

    @respx.mock
    async def test_rate_limit_429_raises(self, client: TelegramBotClient) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 30},
                },
            )
        )
        with pytest.raises(RateLimitError):
            await client.get_updates()

    @respx.mock
    async def test_server_500_raises(self, client: TelegramBotClient) -> None:
        respx.get(url=GET_UPDATES_URL).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with pytest.raises(PlatformUnavailableError, match="Server error 500"):
            await client.get_updates()


class TestRetry:
    @respx.mock
    async def test_retries_on_500(self, retry_client: TelegramBotClient) -> None:
        route = respx.get(url=GET_UPDATES_URL).mock(
            side_effect=[
                httpx.Response(500, text="Error"),
                httpx.Response(500, text="Error"),
                httpx.Response(200, json={"ok": True, "result": []}),
            ]
        )
        updates = await retry_client.get_updates()
        assert updates == []
        assert route.call_count == 3

    @respx.mock
    async def test_retries_exhausted_raises(self, retry_client: TelegramBotClient) -> None:
        respx.get(url=GET_UPDATES_URL).mock(return_value=httpx.Response(500, text="Error"))
        with pytest.raises(PlatformUnavailableError):
            await retry_client.get_updates()

    @respx.mock
    async def test_retries_on_429(self, retry_client: TelegramBotClient) -> None:
        route = respx.get(url=GET_UPDATES_URL).mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 1},
                    },
                ),
                httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]}),
            ]
        )
        updates = await retry_client.get_updates()
        assert len(updates) == 1
        assert route.call_count == 2

    @respx.mock
    async def test_retries_on_connection_error(self, retry_client: TelegramBotClient) -> None:
        route = respx.get(url=GET_UPDATES_URL).mock(
            side_effect=[
                httpx.ConnectError("refused"),
                httpx.Response(200, json={"ok": True, "result": []}),
            ]
        )
        updates = await retry_client.get_updates()
        assert updates == []
        assert route.call_count == 2


class TestContextManager:
    async def test_async_context_manager(self) -> None:
        async with TelegramBotClient(
            bot_token=BOT_TOKEN,
            api_base_url=API_BASE,
            requests_per_second=0,
        ) as client:
            assert client is not None
