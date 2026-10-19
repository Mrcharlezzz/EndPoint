"""Reusable aiohttp-based stub server for faking external platform APIs.

Concrete stubs (Teams CSA, Teams Graph, Moodle, Telegram) subclass
``StubServer`` and register their routes in ``_configure_routes``. Tests
control the stub imperatively through ``push()`` (queue a canned response)
and ``recorded_requests()`` (inspect what the daemon actually called).
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from aiohttp import web


class StubServer:
    """Base class for per-platform stub servers.

    Each (method, path) pair has its own FIFO response queue. Responses are
    popped on match. An empty queue returns 404; re-pushing restores behavior.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int = 0
        self._responses: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
        self._requests: list[dict[str, Any]] = []
        self._configure_routes()
        # Catch-all fallback: registered last so concrete routes override.
        self._app.router.add_route("*", "/{tail:.*}", self._handle_any)

    @property
    def base_url(self) -> str:
        if self._port == 0:
            raise RuntimeError(f"StubServer({self.name}) not started")
        return f"http://127.0.0.1:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    def push(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json: Any = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Queue a canned response for the next request matching (method, path)."""
        self._responses[(method.upper(), path)].append(
            {
                "status": status,
                "json": json,
                "text": text,
                "headers": headers or {},
            }
        )

    def recorded_requests(self) -> list[dict[str, Any]]:
        """Return all requests seen so far (most recent last)."""
        return list(self._requests)

    def recorded_json(self, index: int = -1) -> Any:
        """Return the JSON-decoded body of a recorded request.

        Convenience for subclass tests to avoid ``json.loads(stub.recorded_requests()[i]["body"])``
        boilerplate. Index defaults to ``-1`` (the most recent request).
        """
        import json as _json

        return _json.loads(self._requests[index]["body"])

    def reset(self) -> None:
        """Clear response queues and recorded requests."""
        self._responses.clear()
        self._requests.clear()

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
            await self._site.start()
            addresses = self._runner.addresses  # public API, list[tuple]
            if not addresses:
                raise RuntimeError("stub server failed to bind")
            self._port = addresses[0][1]
        except BaseException:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            raise

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._port = 0

    def _configure_routes(self) -> None:
        """Hook for subclasses to register platform-specific routes."""

    async def _handle_any(self, request: web.Request) -> web.Response:
        body = await request.read()
        self._requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.rel_url.query),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        key = (request.method, request.path)
        queue = self._responses.get(key)
        if not queue:
            return web.json_response({"error": "no response queued"}, status=404)
        canned = queue.popleft()
        if canned["json"] is not None:
            return web.json_response(
                canned["json"], status=canned["status"], headers=canned["headers"]
            )
        return web.Response(
            text=canned["text"] or "",
            status=canned["status"],
            headers=canned["headers"],
        )
