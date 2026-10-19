"""PostgresContentSink: upsert, batch, and exists against IngestedItem."""

from __future__ import annotations

import pytest
from ingestion.daemon.ports.content_sink import PostgresContentSink
from ingestion.schemas import ContentType, NormalizedItem

# transaction=True forces per-test table truncation instead of savepoint
# rollback. Django's async ORM runs ``aupdate_or_create`` in a separate
# thread whose savepoint doesn't always participate in pytest-django's
# default wrapper, so writes leak across tests when Postgres is reachable.
pytestmark = pytest.mark.django_db(transaction=True)


def _make_item(external_id: str = "moodle_forum_1", **overrides: object) -> NormalizedItem:
    defaults: dict[str, object] = {
        "external_id": external_id,
        "platform": "moodle",
        "content_type": ContentType.ANNOUNCEMENT,
        "title": "Welcome",
        "body": "Hi all",
        "course_name": "CS101",
        "course_external_id": "moodle_course_8740",
        "author": "prof@example.com",
        "source_url": "https://moodle.example/forum/1",
        "raw_payload": {"id": 1},
    }
    defaults.update(overrides)
    return NormalizedItem(**defaults)  # type: ignore[arg-type]


async def test_exists_returns_false_when_missing() -> None:
    sink = PostgresContentSink()
    assert await sink.exists("moodle", "moodle_forum_1") is False


async def test_upsert_inserts_new_row_and_returns_id() -> None:
    from core.models import IngestedItem

    sink = PostgresContentSink()
    item = _make_item()
    row_id = await sink.upsert(item)

    assert await sink.exists("moodle", "moodle_forum_1") is True
    row = await IngestedItem.objects.aget(platform="moodle", external_id="moodle_forum_1")
    assert str(row.id) == row_id
    assert row.course_external_id == "moodle_course_8740"
    assert row.source_url == "https://moodle.example/forum/1"
    assert row.payload["title"] == "Welcome"
    assert row.payload["body"] == "Hi all"
    assert row.raw_payload == {"id": 1}


async def test_upsert_updates_existing_row_in_place() -> None:
    from core.models import IngestedItem

    sink = PostgresContentSink()
    original_id = await sink.upsert(_make_item(title="v1"))
    updated_id = await sink.upsert(_make_item(title="v2"))

    assert original_id == updated_id
    assert await IngestedItem.objects.filter(platform="moodle").acount() == 1
    row = await IngestedItem.objects.aget(platform="moodle", external_id="moodle_forum_1")
    assert row.payload["title"] == "v2"


async def test_upsert_batch_returns_ids_in_input_order() -> None:
    from core.models import IngestedItem

    sink = PostgresContentSink()
    items = [_make_item(external_id=f"moodle_forum_{i}", title=f"t{i}") for i in range(3)]
    ids = await sink.upsert_batch(items)
    assert len(ids) == 3
    assert len(set(ids)) == 3
    for item, row_id in zip(items, ids, strict=True):
        row = await IngestedItem.objects.aget(external_id=item.external_id)
        assert str(row.id) == row_id


async def test_upsert_distinct_external_ids_yield_separate_rows() -> None:
    from core.models import IngestedItem

    sink = PostgresContentSink()
    await sink.upsert(_make_item(external_id="moodle_forum_a"))
    await sink.upsert(_make_item(external_id="moodle_forum_b"))

    assert await IngestedItem.objects.acount() == 2


async def test_upsert_writes_adapter_and_schema_versions() -> None:
    from core.models import IngestedItem
    from ingestion.schemas import PAYLOAD_SCHEMA_VERSION

    sink = PostgresContentSink(adapter_version="test-adapter-9.9.9")
    await sink.upsert(_make_item())
    row = await IngestedItem.objects.aget(external_id="moodle_forum_1")
    assert row.schema_version == PAYLOAD_SCHEMA_VERSION
    assert row.adapter_version == "test-adapter-9.9.9"
