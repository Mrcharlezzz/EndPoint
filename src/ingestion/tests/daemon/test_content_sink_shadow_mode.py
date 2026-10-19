"""Shadow-mode behaviour for :class:`PostgresContentSink`.

Lives in its own module (rather than ``test_ports_content_sink.py``) because
shadow mode never touches Postgres — the whole point is to bypass the
writer. Kept isolated so the Postgres-gated ``django_db(transaction=True)``
marker doesn't auto-skip these when the DB is unreachable.
"""

from __future__ import annotations

from ingestion.daemon.ports.content_sink import PostgresContentSink
from ingestion.schemas import ContentType, NormalizedItem


def _make_item(external_id: str = "moodle_forum_shadow") -> NormalizedItem:
    return NormalizedItem(  # type: ignore[arg-type]
        external_id=external_id,
        platform="moodle",
        content_type=ContentType.ANNOUNCEMENT,
        title="Welcome",
        body="Hi all",
        course_external_id="moodle_course_1",
        source_url="https://moodle.example/forum/1",
        raw_payload={"id": 1},
    )


async def test_upsert_under_shadow_returns_sentinel_without_db() -> None:
    """When the gate returns True the sink must not touch the ORM.

    The test never runs with django_db, so if the sink reached into
    ``IngestedItem.objects`` this would blow up with a connection error
    against Postgres rather than returning the sentinel.
    """
    sink = PostgresContentSink(is_shadow_mode=lambda: True)
    result = await sink.upsert(_make_item("moodle_forum_1"))
    assert result.startswith("shadow:")
    assert result == "shadow:moodle_forum_1"


async def test_upsert_batch_under_shadow_returns_sentinel_list() -> None:
    """Batch suppression returns one sentinel per input item, ordered."""
    sink = PostgresContentSink(is_shadow_mode=lambda: True)
    items = [_make_item(f"moodle_forum_{i}") for i in range(3)]
    results = await sink.upsert_batch(items)
    assert results == [
        "shadow:moodle_forum_0",
        "shadow:moodle_forum_1",
        "shadow:moodle_forum_2",
    ]


async def test_shadow_mode_emits_one_suppression_log_per_batch() -> None:
    """Every suppressed batch must emit exactly one log record. The
    dashboard's Errors/Ops pane uses this signal to visualise a shadow
    run in progress; per-item logging would flood it."""
    import ingestion.daemon.ports.content_sink as sink_module  # noqa: PLC0415

    records: list[dict[str, object]] = []

    class _Capture:
        def info(self, event: str, **kwargs: object) -> None:
            records.append({"event": event, **kwargs})

        # Unused by the sink but structlog BoundLogger carries them.
        def warning(self, *_args: object, **_kwargs: object) -> None:
            return

    original_log = sink_module._log
    sink_module._log = _Capture()  # type: ignore[assignment]
    try:
        sink = PostgresContentSink(is_shadow_mode=lambda: True)
        await sink.upsert_batch([_make_item("moodle_forum_x"), _make_item("moodle_forum_y")])
    finally:
        sink_module._log = original_log

    events = [r for r in records if r.get("event") == "shadow_mode_write_suppressed"]
    assert len(events) == 1
    assert events[0].get("item_count") == 2


async def test_exists_is_not_affected_by_shadow_mode() -> None:
    """``exists`` reads from Postgres; shadow mode doesn't suppress reads.

    The sink's ``exists`` is used by the pipeline's dedupe path — if we
    suppressed it, the pipeline would over-count 'new' items during a
    shadow run and distort the counters the V10 ops pane shows.
    """
    # A shadow-gated sink still calls through to the ORM on ``exists`` —
    # we don't exercise that path here (no Postgres) but do assert the
    # code path is unchanged: the constructor doesn't wrap exists.
    sink_shadow = PostgresContentSink(is_shadow_mode=lambda: True)
    sink_live = PostgresContentSink(is_shadow_mode=lambda: False)
    assert sink_shadow.exists.__qualname__ == sink_live.exists.__qualname__


async def test_default_is_shadow_mode_is_off() -> None:
    """Constructing with no gate must behave like the pre-shadow sink:
    ``is_shadow_mode()`` returns False, so writes flow normally when
    called. We only validate the gate here; the write path is covered
    by the Postgres-gated tests in ``test_ports_content_sink.py``."""
    sink = PostgresContentSink()
    assert sink._is_shadow_mode() is False


async def test_shadow_mode_is_polled_per_call() -> None:
    """Flipping the gate mid-run must change behaviour on the next write.

    Operators flip the overlay from the dashboard; the sink picks it up
    immediately without a restart. We simulate the flip via a mutable
    list so the second ``upsert`` observes the toggle.
    """
    state = {"shadow": False}
    sink = PostgresContentSink(is_shadow_mode=lambda: state["shadow"])

    # First call: gate is False. We'd hit Postgres — skip executing the
    # ``await`` here and only check the gate resolved to False.
    assert sink._is_shadow_mode() is False

    state["shadow"] = True
    assert sink._is_shadow_mode() is True
    result = await sink.upsert(_make_item("moodle_forum_flipped"))
    assert result.startswith("shadow:")
