"""Tests for TelegramNormalizer — message/edited/forwarded → NormalizedItem."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from ingestion.adapters.telegram.normalizer import TelegramNormalizer
from ingestion.schemas import ContentType, FileRef, NormalizedItem


@pytest.fixture
def normalizer() -> TelegramNormalizer:
    return TelegramNormalizer(
        api_base_url="https://api.telegram.org",
        bot_token="TEST-TOKEN",
        adapter_version="1.0.0",
    )


def _make_message(
    message_id: int = 1,
    text: str = "Hello class!",
    chat_id: int = -1001234,
    chat_title: str = "CS101 Group",
    from_first: str = "John",
    from_last: str = "Doe",
    date: int = 1710288000,
    **extra: object,
) -> dict:
    msg: dict = {
        "message_id": message_id,
        "text": text,
        "date": date,
        "chat": {"id": chat_id, "title": chat_title, "type": "supergroup"},
        "from": {"id": 999, "first_name": from_first, "last_name": from_last},
    }
    msg.update(extra)
    return msg


class TestBasicMessage:
    def test_normalize_text_message(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message()
        item = normalizer.normalize(raw, source="message")

        assert isinstance(item, NormalizedItem)
        assert item.external_id == "tg_-1001234_1"
        assert item.platform == "telegram"
        assert item.content_type == ContentType.RAW_MESSAGE
        assert item.body == "Hello class!"
        assert item.author == "John Doe"
        assert item.course_name == "CS101 Group"
        assert item.course_external_id == "-1001234"
        assert item.source_url == "https://t.me/c/1234/1"

    def test_public_group_source_url_uses_username(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(message_id=78, chat_id=-1003579184587)
        chat_meta = SimpleNamespace(username="ctms22_course", type="supergroup")

        item = normalizer.normalize(raw, source="message", chat_meta=chat_meta)

        assert item.source_url == "https://t.me/ctms22_course/78"

    def test_private_group_source_url_uses_internal_chat_id(
        self, normalizer: TelegramNormalizer
    ) -> None:
        raw = _make_message(message_id=78, chat_id=-1003579184587)
        chat_meta = SimpleNamespace(username=None, type="supergroup")

        item = normalizer.normalize(raw, source="message", chat_meta=chat_meta)

        assert item.source_url == "https://t.me/c/3579184587/78"

    def test_source_url_none_without_chat_id(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(chat_id=0)

        item = normalizer.normalize(raw, source="message")

        assert item.source_url is None

    def test_published_at_from_date(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(date=1710288000)
        item = normalizer.normalize(raw, source="message")
        assert item.published_at == datetime(2024, 3, 13, 0, 0, tzinfo=UTC)

    def test_empty_text_uses_caption(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(text=None, caption="Photo of homework")
        # Remove the text key since we set it to None
        del raw["text"]
        raw["caption"] = "Photo of homework"
        item = normalizer.normalize(raw, source="message")
        assert item.body == "Photo of homework"

    def test_no_text_no_caption(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message()
        del raw["text"]
        item = normalizer.normalize(raw, source="message")
        assert item.body == ""

    def test_from_without_last_name(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(from_first="Alice")
        del raw["from"]["last_name"]
        item = normalizer.normalize(raw, source="message")
        assert item.author == "Alice"


class TestEditedMessage:
    def test_edited_message_same_external_id(self, normalizer: TelegramNormalizer) -> None:
        """Decision #17: edited_message uses same external_id for upsert."""
        raw = _make_message(message_id=42, text="Updated text")
        item = normalizer.normalize(raw, source="edited_message")
        assert item.external_id == "tg_-1001234_42"
        assert item.body == "Updated text"

    def test_edited_message_content_type(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message()
        item = normalizer.normalize(raw, source="edited_message")
        assert item.content_type == ContentType.RAW_MESSAGE


class TestForwardedMessage:
    def test_forwarded_original_author(self, normalizer: TelegramNormalizer) -> None:
        """Decision #19: Original author as author, forwarder in raw_payload."""
        raw = _make_message(
            from_first="Forwarder",
            from_last="Name",
            text="Important announcement",
        )
        raw["forward_origin"] = {
            "type": "user",
            "sender_user": {
                "id": 777,
                "first_name": "Original",
                "last_name": "Author",
            },
            "date": 1710200000,
        }
        item = normalizer.normalize(raw, source="message")
        assert item.author == "Original Author"

    def test_forwarded_forwarder_in_raw_payload(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(from_first="Forwarder", from_last="Name")
        raw["forward_origin"] = {
            "type": "user",
            "sender_user": {"id": 777, "first_name": "Original", "last_name": "Author"},
            "date": 1710200000,
        }
        item = normalizer.normalize(raw, source="message")
        payload = item.raw_payload
        assert payload["original"]["forward_origin"]["type"] == "user"

    def test_forwarded_hidden_user(self, normalizer: TelegramNormalizer) -> None:
        """forward_origin type=hidden_user — use sender_user_name."""
        raw = _make_message(from_first="Forwarder", from_last="Name")
        raw["forward_origin"] = {
            "type": "hidden_user",
            "sender_user_name": "Hidden Author",
            "date": 1710200000,
        }
        item = normalizer.normalize(raw, source="message")
        assert item.author == "Hidden Author"

    def test_forwarded_channel(self, normalizer: TelegramNormalizer) -> None:
        """forward_origin type=channel — use chat.title."""
        raw = _make_message(from_first="Forwarder", from_last="Name")
        raw["forward_origin"] = {
            "type": "channel",
            "chat": {"id": -100999, "title": "News Channel"},
            "date": 1710200000,
        }
        item = normalizer.normalize(raw, source="message")
        assert item.author == "News Channel"

    def test_no_forward_origin_uses_from(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(from_first="Direct", from_last="Sender")
        item = normalizer.normalize(raw, source="message")
        assert item.author == "Direct Sender"


class TestAttachments:
    def test_document_attachment(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(text="See attached")
        raw["document"] = {
            "file_id": "BQACAgIAAxk",
            "file_name": "homework.pdf",
            "mime_type": "application/pdf",
            "file_size": 2048,
        }
        item = normalizer.normalize(raw, source="message")
        assert len(item.attachments) == 1
        ref = item.attachments[0]
        assert isinstance(ref, FileRef)
        assert ref.filename == "homework.pdf"
        assert ref.content_type == "application/pdf"
        assert ref.size_bytes == 2048
        assert "BQACAgIAAxk" in ref.download_url

    def test_photo_attachment_largest(self, normalizer: TelegramNormalizer) -> None:
        """photo is an array of PhotoSize — pick the largest."""
        raw = _make_message(text="Photo")
        raw["photo"] = [
            {"file_id": "small_id", "width": 90, "height": 90, "file_size": 1000},
            {"file_id": "medium_id", "width": 320, "height": 320, "file_size": 5000},
            {"file_id": "large_id", "width": 800, "height": 800, "file_size": 20000},
        ]
        item = normalizer.normalize(raw, source="message")
        assert len(item.attachments) == 1
        ref = item.attachments[0]
        assert "large_id" in ref.download_url
        assert ref.is_media is True

    def test_video_attachment(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message()
        raw["video"] = {
            "file_id": "video_123",
            "file_name": "lecture.mp4",
            "mime_type": "video/mp4",
            "file_size": 50000,
        }
        item = normalizer.normalize(raw, source="message")
        assert len(item.attachments) == 1
        assert item.attachments[0].is_media is True

    def test_multiple_attachment_types(self, normalizer: TelegramNormalizer) -> None:
        """A message can have both document and photo (shouldn't happen, but handle)."""
        raw = _make_message()
        raw["document"] = {
            "file_id": "doc_id",
            "file_name": "notes.pdf",
            "mime_type": "application/pdf",
            "file_size": 1024,
        }
        raw["photo"] = [
            {"file_id": "photo_id", "width": 800, "height": 800, "file_size": 5000},
        ]
        item = normalizer.normalize(raw, source="message")
        assert len(item.attachments) == 2

    def test_no_attachments(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message()
        item = normalizer.normalize(raw, source="message")
        assert item.attachments == []


class TestRawPayloadEnvelope:
    def test_envelope_wrapping(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message()
        item = normalizer.normalize(raw, source="message")
        payload = item.raw_payload
        assert payload["source"] == "telegram_message"
        assert payload["ingestion_mode"] == "server_poll"
        assert payload["adapter_version"] == "1.0.0"
        assert "original" in payload

    def test_edited_message_source(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message()
        item = normalizer.normalize(raw, source="edited_message")
        assert item.raw_payload["source"] == "telegram_edited_message"


class TestHTMLStripping:
    def test_strip_html_entities(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message(text="Hello <b>world</b> &amp; friends")
        # Telegram can send HTML-formatted text
        item = normalizer.normalize(raw, source="message")
        # Body should strip any HTML tags (Decision #9)
        assert "<b>" not in item.body


class TestUnknownSource:
    def test_unknown_source_raises(self, normalizer: TelegramNormalizer) -> None:
        raw = _make_message()
        with pytest.raises(ValueError, match="Unknown source"):
            normalizer.normalize(raw, source="channel_post")
