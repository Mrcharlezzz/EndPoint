"""TelegramAdapter — orchestrates polling-based fetching from Telegram Bot API.

Decision #16: Privacy mode OFF required (documented prerequisite).
Decision #17: edited_message processed with same external_id.
Decision #18: Group mapping is Django's job — adapter passes chat.id + chat.title through.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ingestion.adapters.base import BaseAdapter
from ingestion.adapters.telegram.bot_client import TelegramBotClient
from ingestion.adapters.telegram.normalizer import TelegramNormalizer
from ingestion.exceptions import IngestionError, TokenExpiredError
from ingestion.schemas import (
    ErrorCode,
    FetchError,
    FetchResult,
    FetchScope,
    FileRef,
    NormalizedItem,
    ScopeStats,
    SyncStats,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ingestion.adapters.telegram.credentials import TelegramCredentials
    from ingestion.config import TelegramConfig
    from ingestion.ports import ProgressCallback

logger = logging.getLogger(__name__)

_GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})


class TelegramAdapter(BaseAdapter):
    """Adapter for Telegram via Bot API polling.

    Single scope: MESSAGES. Filters to group/supergroup chats only.
    """

    platform = "telegram"
    _supported_scopes = frozenset({FetchScope.MESSAGES})
    _required_credential_keys = ("bot_token",)

    def __init__(
        self,
        config: TelegramConfig,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(
            max_concurrent_requests=config.max_concurrent_requests,
            sleep_func=sleep_func,
        )
        self._config = config
        self._client: TelegramBotClient | None = None

    async def fetch(
        self,
        credentials: dict,
        cursor: dict | None = None,
        scope: frozenset[FetchScope] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FetchResult:
        """Fetch messages from Telegram groups via Bot API polling.

        credentials: {"bot_token": str}
        cursor: {"last_update_id": int} or None
        """
        validation_error = self._validate_credentials(credentials)
        if validation_error:
            return FetchResult(errors=[validation_error])

        effective_scopes = self._resolve_scopes(scope)
        if not effective_scopes:
            return FetchResult()

        creds: TelegramCredentials = credentials  # type: ignore[assignment]
        bot_token = creds["bot_token"]
        last_update_id = (cursor or {}).get("last_update_id", 0)

        all_items: list[NormalizedItem] = []
        all_files: list[FileRef] = []
        errors: list[FetchError] = []
        stats = ScopeStats()
        start_time = time.monotonic()

        normalizer = TelegramNormalizer(
            api_base_url=self._config.api_base_url,
            bot_token=bot_token,
            adapter_version="",
        )

        self._client = self._make_client(bot_token)

        try:
            if on_progress:
                on_progress("messages", "Polling for updates...")

            offset = last_update_id + 1 if last_update_id else 0

            updates = await self._client.get_updates(
                offset=offset if offset else None,
                timeout=self._config.poll_timeout,
                limit=self._config.max_updates,
            )

            if on_progress:
                on_progress("messages", f"{len(updates)} updates received")
                if updates:
                    on_progress("+items", str(len(updates)))

            max_update_id = last_update_id

            for update in updates:
                update_id = update.get("update_id", 0)
                if update_id > max_update_id:
                    max_update_id = update_id

                message = update.get("message")
                edited_message = update.get("edited_message")

                if message:
                    source = "message"
                    msg = message
                elif edited_message:
                    source = "edited_message"
                    msg = edited_message
                else:
                    continue

                chat = msg.get("chat", {})
                chat_type = chat.get("type", "")
                if chat_type not in _GROUP_CHAT_TYPES:
                    continue

                try:
                    chat_id = chat.get("id", 0)
                    chat_meta = (
                        await self._client.get_chat_meta(chat_id)
                        if isinstance(chat_id, int) and chat_id
                        else None
                    )
                    item = normalizer.normalize(msg, source=source, chat_meta=chat_meta)
                    all_items.append(item)
                    all_files.extend(item.attachments)
                    stats.items_fetched += 1
                except TokenExpiredError:
                    raise
                except IngestionError as exc:
                    errors.append(
                        FetchError(
                            code=ErrorCode.PLATFORM_UNAVAILABLE,
                            message=str(exc),
                            scope=f"messages/{update_id}",
                        )
                    )
                    stats.errors += 1
                except Exception as exc:
                    errors.append(
                        FetchError(
                            code=ErrorCode.NORMALIZATION_ERROR,
                            message=str(exc),
                            scope=f"messages/{update_id}",
                        )
                    )
                    stats.errors += 1

            new_cursor: dict | None = {"last_update_id": max_update_id}

        except TokenExpiredError as exc:
            errors.append(
                FetchError(
                    code=ErrorCode.TOKEN_EXPIRED,
                    message=str(exc),
                    recoverable=False,
                )
            )
            new_cursor = cursor
        except IngestionError as exc:
            errors.append(
                FetchError(
                    code=ErrorCode.UNKNOWN,
                    message=str(exc),
                    recoverable=True,
                )
            )
            new_cursor = cursor
        except Exception as exc:
            errors.append(
                FetchError(
                    code=ErrorCode.UNKNOWN,
                    message=f"Unexpected error: {exc}",
                    recoverable=True,
                )
            )
            new_cursor = cursor
        finally:
            await self._client.close()

        duration = time.monotonic() - start_time

        return FetchResult(
            items=all_items,
            files=all_files,
            cursor=new_cursor,
            errors=errors,
            stats=SyncStats(
                items_fetched=len(all_items),
                duration_seconds=duration,
                per_scope_stats={"messages": stats},
            ),
        )

    async def test_connection(self, credentials: dict) -> bool:
        """Validate bot token by calling getMe."""
        bot_token = credentials.get("bot_token", "")
        if not bot_token:
            return False
        client = self._make_client(bot_token)
        try:
            me = await client.get_me()
            return bool(me.get("id"))
        except Exception:
            logger.debug("Telegram test_connection failed", exc_info=True)
            return False
        finally:
            await client.close()

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def _make_client(self, bot_token: str) -> TelegramBotClient:
        return TelegramBotClient(
            bot_token=bot_token,
            api_base_url=self._config.api_base_url,
            max_retries=self._config.max_retries,
            timeout=self._config.request_timeout,
            requests_per_second=self._config.requests_per_second,
            sleep_func=self._sleep_func,
        )
