from __future__ import annotations

from ingestion.daemon import source_url_backfill


def _telegram_message_ids(external_id: str) -> tuple[str, str] | None:
    assert hasattr(source_url_backfill, "telegram_message_ids")
    return source_url_backfill.telegram_message_ids(external_id)


def _build_telegram_source_url(external_id: str) -> str | None:
    assert hasattr(source_url_backfill, "build_telegram_source_url")
    return source_url_backfill.build_telegram_source_url(external_id)


def test_telegram_message_ids_parse_positive_chat_id() -> None:
    assert _telegram_message_ids("tg_3579184587_56") == ("3579184587", "56")


def test_telegram_message_ids_parse_private_supergroup_chat_id() -> None:
    assert _telegram_message_ids("tg_-1003709134149_184") == ("-1003709134149", "184")


def test_telegram_message_ids_reject_non_telegram_external_ids() -> None:
    assert _telegram_message_ids("teams_csa_123") is None
    assert _telegram_message_ids("tg_3579184587_") is None
    assert _telegram_message_ids("tg_chat_56") is None
    assert _telegram_message_ids("") is None


def test_build_telegram_source_url_uses_internal_chat_id() -> None:
    assert _build_telegram_source_url("tg_3579184587_56") == "https://t.me/c/3579184587/56"


def test_build_telegram_source_url_strips_supergroup_prefix() -> None:
    assert _build_telegram_source_url("tg_-1003709134149_184") == "https://t.me/c/3709134149/184"


def test_build_telegram_source_url_rejects_invalid_external_id() -> None:
    assert _build_telegram_source_url("tg_chat_56") is None
