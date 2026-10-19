"""Tests for MoodleRestClient — POST-based API, error detection, retry, rate limiting."""

from __future__ import annotations

import httpx
import pytest
import respx
from ingestion.adapters.moodle.client import MoodleRestClient
from ingestion.exceptions import (
    InsufficientScopeError,
    PlatformUnavailableError,
    RateLimitError,
    TokenExpiredError,
)

MOODLE_BASE = "https://moodle.test"
MOODLE_ENDPOINT = f"{MOODLE_BASE}/webservice/rest/server.php"


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def client() -> MoodleRestClient:
    return MoodleRestClient(
        base_url=MOODLE_BASE,
        token="test-token-123",
        max_retries=0,
        timeout=5,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )


@pytest.fixture
def retry_client() -> MoodleRestClient:
    return MoodleRestClient(
        base_url=MOODLE_BASE,
        token="test-token-123",
        max_retries=2,
        timeout=5,
        requests_per_second=0,
        sleep_func=_noop_sleep,
    )


class TestSuccessfulCalls:
    @respx.mock
    async def test_call_sends_post_with_token(self, client: MoodleRestClient) -> None:
        """Verify the client sends POST with wstoken, wsfunction, moodlewsrestformat."""
        route = respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(200, json=[{"id": 1, "fullname": "CS101"}])
        )

        await client.call("core_enrol_get_users_courses", userid=12059)

        assert route.called
        request = route.calls[0].request
        body = request.content.decode()
        assert "wstoken=test-token-123" in body
        assert "wsfunction=core_enrol_get_users_courses" in body
        assert "moodlewsrestformat=json" in body
        assert "userid=12059" in body

    @respx.mock
    async def test_call_returns_list(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}])
        )
        result = await client.call("core_enrol_get_users_courses", userid=1)
        assert isinstance(result, list)
        assert len(result) == 2

    @respx.mock
    async def test_call_returns_dict(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"courses": [], "warnings": []})
        )
        result = await client.call("mod_assign_get_assignments")
        assert isinstance(result, dict)
        assert "courses" in result

    @respx.mock
    async def test_extra_params_forwarded(self, client: MoodleRestClient) -> None:
        route = respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(200, json={"discussions": []})
        )
        await client.call("mod_forum_get_forum_discussions", forumid=42, perpage=10)
        body = route.calls[0].request.content.decode()
        assert "forumid=42" in body
        assert "perpage=10" in body


class TestMoodleErrorDetection:
    @respx.mock
    async def test_invalid_token_raises(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "exception": "moodle_exception",
                    "errorcode": "invalidtoken",
                    "message": "Invalid token",
                },
            )
        )
        with pytest.raises(TokenExpiredError, match="invalidtoken"):
            await client.call("core_webservice_get_site_info")

    @respx.mock
    async def test_access_exception_raises(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "exception": "webservice_access_exception",
                    "errorcode": "accessexception",
                    "message": "Access denied",
                },
            )
        )
        with pytest.raises(TokenExpiredError, match="accessexception"):
            await client.call("core_webservice_get_site_info")

    @respx.mock
    async def test_no_permission_raises(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "exception": "required_capability_exception",
                    "errorcode": "nopermissions",
                    "message": "No permission",
                },
            )
        )
        with pytest.raises(InsufficientScopeError, match="nopermissions"):
            await client.call("mod_assign_get_assignments")

    @respx.mock
    async def test_unknown_error_raises_platform_unavailable(
        self, client: MoodleRestClient
    ) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "exception": "dml_exception",
                    "errorcode": "dmlreadexception",
                    "message": "Database error",
                },
            )
        )
        with pytest.raises(PlatformUnavailableError, match="dmlreadexception"):
            await client.call("core_course_get_contents")


class TestHTTPErrors:
    @respx.mock
    async def test_server_500_raises(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with pytest.raises(PlatformUnavailableError, match="Server error 500"):
            await client.call("core_webservice_get_site_info")

    @respx.mock
    async def test_rate_limit_429_raises(self, client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(
            return_value=httpx.Response(429, text="Too Many Requests")
        )
        with pytest.raises(RateLimitError):
            await client.call("core_webservice_get_site_info")


class TestRetry:
    @respx.mock
    async def test_retries_on_500(self, retry_client: MoodleRestClient) -> None:
        route = respx.post(url__eq=MOODLE_ENDPOINT).mock(
            side_effect=[
                httpx.Response(500, text="Error"),
                httpx.Response(500, text="Error"),
                httpx.Response(200, json={"userid": 1}),
            ]
        )
        result = await retry_client.call("core_webservice_get_site_info")
        assert result == {"userid": 1}
        assert route.call_count == 3

    @respx.mock
    async def test_retries_exhausted_raises(self, retry_client: MoodleRestClient) -> None:
        respx.post(url__eq=MOODLE_ENDPOINT).mock(return_value=httpx.Response(500, text="Error"))
        with pytest.raises(PlatformUnavailableError):
            await retry_client.call("core_webservice_get_site_info")

    @respx.mock
    async def test_retries_on_429(self, retry_client: MoodleRestClient) -> None:
        route = respx.post(url__eq=MOODLE_ENDPOINT).mock(
            side_effect=[
                httpx.Response(429, text="Rate limited"),
                httpx.Response(200, json=[{"id": 1}]),
            ]
        )
        result = await retry_client.call("core_enrol_get_users_courses", userid=1)
        assert isinstance(result, list)
        assert route.call_count == 2


class TestMoodleErrorParsing:
    """Test _parse_response detects Moodle error-in-200 responses."""

    def _make_client(self) -> MoodleRestClient:
        return MoodleRestClient(
            base_url=MOODLE_BASE,
            token="tok",
            requests_per_second=0,
            sleep_func=_noop_sleep,
        )

    def _mock_response(self, json_data: dict | list) -> httpx.Response:
        import json

        return httpx.Response(200, text=json.dumps(json_data))

    def test_list_response_passes(self) -> None:
        client = self._make_client()
        result = client._parse_response(self._mock_response([{"id": 1}]), url="test")
        assert result == [{"id": 1}]

    def test_dict_without_errorcode_passes(self) -> None:
        client = self._make_client()
        result = client._parse_response(self._mock_response({"courses": []}), url="test")
        assert result == {"courses": []}

    def test_token_error_raises(self) -> None:
        client = self._make_client()
        with pytest.raises(TokenExpiredError):
            client._parse_response(
                self._mock_response({"errorcode": "invalidtoken", "message": "Bad token"}),
                url="test",
            )

    def test_permission_error_raises(self) -> None:
        client = self._make_client()
        with pytest.raises(InsufficientScopeError):
            client._parse_response(
                self._mock_response({"errorcode": "nopermissions", "message": "No access"}),
                url="test",
            )

    def test_generic_error_raises(self) -> None:
        client = self._make_client()
        with pytest.raises(PlatformUnavailableError):
            client._parse_response(
                self._mock_response({"errorcode": "somethingweird", "message": "Oops"}),
                url="test",
            )


class TestContextManager:
    async def test_async_context_manager(self) -> None:
        async with MoodleRestClient(
            base_url=MOODLE_BASE,
            token="tok",
            requests_per_second=0,
        ) as client:
            assert client is not None
