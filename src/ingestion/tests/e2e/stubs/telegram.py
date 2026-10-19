"""Telegram Bot API stub. Routes filled in in PR-2 as tests need them."""

from __future__ import annotations

from tests.e2e.stubs.base import StubServer


class TelegramStub(StubServer):
    def __init__(self) -> None:
        super().__init__(name="telegram")

    def _configure_routes(self) -> None:
        # Concrete routes added in PR-2. StubServer's catch-all handles
        # everything via push()/recorded_requests() for now.
        return
