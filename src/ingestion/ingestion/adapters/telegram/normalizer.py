"""Normalize raw Telegram Bot API messages into NormalizedItem.

Decision #17: edited_message uses same external_id for upsert.
Decision #19: Forwarded messages — original author as author, forwarder in raw_payload.
"""

from __future__ import annotations

from ingestion.normalize import decode_entities, make_envelope, strip_html, timestamp_to_datetime
from ingestion.schemas import (
    ContentType,
    FileRef,
    NormalizedItem,
)


class TelegramNormalizer:
    """Convert raw Telegram message dicts to NormalizedItem.

    Implements the Normalizer protocol from ports.
    """

    def __init__(
        self,
        api_base_url: str = "https://api.telegram.org",
        bot_token: str = "",
        adapter_version: str = "",
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._bot_token = bot_token
        self._adapter_version = adapter_version

    def normalize(self, raw: dict, **context: object) -> NormalizedItem:
        """Dispatch to source-specific normalizer."""
        source = str(context.get("source", ""))
        chat_meta = context.get("chat_meta")
        if source in ("message", "edited_message"):
            return self._normalize_message(raw, source=source, chat_meta=chat_meta)
        msg = f"Unknown source: {source!r}"
        raise ValueError(msg)

    def _normalize_message(
        self,
        raw: dict,
        source: str,
        chat_meta: object | None,
    ) -> NormalizedItem:
        message_id = raw.get("message_id", 0)
        chat = raw.get("chat", {})
        chat_id = chat.get("id", 0)
        chat_title = chat.get("title", "")

        external_id = f"tg_{chat_id}_{message_id}"

        text = raw.get("text") or raw.get("caption") or ""
        body = strip_html(text)

        author = self._resolve_author(raw)
        published_at = timestamp_to_datetime(raw.get("date"))
        attachments = self._extract_attachments(raw)
        source_url = _build_source_url(chat_id, message_id, chat_meta)

        return NormalizedItem(
            external_id=external_id,
            platform="telegram",
            content_type=ContentType.RAW_MESSAGE,
            title=None,
            body=body,
            course_name=decode_entities(chat_title) or None,
            course_external_id=str(chat_id) if chat_id else None,
            author=author or None,
            published_at=published_at,
            raw_payload=make_envelope(f"telegram_{source}", raw, self._adapter_version),
            attachments=attachments,
            source_url=source_url,
        )

    def _resolve_author(self, raw: dict) -> str:
        """Resolve message author, handling forwarded messages."""
        forward_origin = raw.get("forward_origin")

        if forward_origin:
            origin_type = forward_origin.get("type", "")

            if origin_type == "user":
                sender = forward_origin.get("sender_user", {})
                return _format_name(
                    sender.get("first_name", ""),
                    sender.get("last_name", ""),
                )

            if origin_type == "hidden_user":
                return forward_origin.get("sender_user_name", "")

            if origin_type == "channel":
                chat = forward_origin.get("chat", {})
                return chat.get("title", "")

            if origin_type == "chat":
                chat = forward_origin.get("chat", {})
                return chat.get("title", "")

        from_user = raw.get("from", {})
        return _format_name(
            from_user.get("first_name", ""),
            from_user.get("last_name", ""),
        )

    def _extract_attachments(self, raw: dict) -> list[FileRef]:
        """Extract FileRef objects from document, photo, and video fields."""
        attachments: list[FileRef] = []

        doc = raw.get("document")
        if doc:
            attachments.append(
                self._make_file_ref(
                    file_id=doc.get("file_id", ""),
                    filename=doc.get("file_name", "document"),
                    mime_type=doc.get("mime_type"),
                    file_size=doc.get("file_size"),
                    is_media=False,
                )
            )

        photos = raw.get("photo")
        if photos and isinstance(photos, list):
            largest_photo: dict | None = None
            largest_size = -1
            for candidate in photos:
                if not isinstance(candidate, dict):
                    continue
                size_raw = candidate.get("file_size")
                size = size_raw if isinstance(size_raw, int) else 0
                if size > largest_size:
                    largest_photo = candidate
                    largest_size = size
            if largest_photo is not None:
                attachments.append(
                    self._make_file_ref(
                        file_id=largest_photo.get("file_id", ""),
                        filename="photo.jpg",
                        mime_type="image/jpeg",
                        file_size=largest_photo.get("file_size"),
                        is_media=True,
                    )
                )

        video = raw.get("video")
        if video:
            attachments.append(
                self._make_file_ref(
                    file_id=video.get("file_id", ""),
                    filename=video.get("file_name", "video.mp4"),
                    mime_type=video.get("mime_type", "video/mp4"),
                    file_size=video.get("file_size"),
                    is_media=True,
                )
            )

        return attachments

    def _make_file_ref(
        self,
        file_id: str,
        filename: str,
        mime_type: str | None = None,
        file_size: int | None = None,
        is_media: bool = False,
    ) -> FileRef:
        """Create a FileRef using the Telegram getFile URL pattern.

        Note: download_url contains the bot token (acceptable, documented).
        """
        download_url = f"{self._api_base_url}/file/bot{self._bot_token}/{file_id}"
        return FileRef(
            external_id=f"tg_file_{file_id}",
            filename=filename,
            download_url=download_url,
            content_type=mime_type,
            size_bytes=file_size,
            is_media=is_media,
        )


def _format_name(first: str, last: str) -> str:
    parts = [p for p in (first, last) if p]
    return " ".join(parts)


def _build_source_url(
    chat_id: int,
    message_id: int,
    chat_meta: object | None,
) -> str | None:
    if not chat_id or not message_id:
        return None

    username = getattr(chat_meta, "username", None)
    if isinstance(username, str) and username:
        return f"https://t.me/{username}/{message_id}"

    raw_chat_id = str(chat_id)
    internal = raw_chat_id[4:] if raw_chat_id.startswith("-100") else raw_chat_id.lstrip("-")
    if not internal:
        return None
    return f"https://t.me/c/{internal}/{message_id}"
