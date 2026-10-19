from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_script() -> ModuleType:
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "backfill_telegram_source_urls.py"
    )
    spec = importlib.util.spec_from_file_location("backfill_telegram_source_urls", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[object, ...] | list[tuple[object, ...]]]] = []
        self.rowcount = 0

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.queries.append((query, params))

    def executemany(self, query: str, params: list[tuple[object, ...]]) -> None:
        self.queries.append((query, params))
        self.rowcount = len(params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FakeConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.cursor_instance = FakeCursor(rows)

    def cursor(self) -> FakeCursor:
        return self.cursor_instance


def test_fetch_candidates_builds_urls_for_missing_processed_telegram_rows() -> None:
    script = _load_script()
    conn = FakeConnection(
        [
            ("row-1", "tg_3579184587_56"),
            ("row-2", "tg_-1003709134149_184"),
            ("row-3", "tg_chat_56"),
        ]
    )

    candidates = script._fetch_candidates(conn, limit=10)

    assert [candidate.external_id for candidate in candidates] == [
        "tg_3579184587_56",
        "tg_-1003709134149_184",
    ]
    assert [candidate.source_url for candidate in candidates] == [
        "https://t.me/c/3579184587/56",
        "https://t.me/c/3709134149/184",
    ]
    query, params = conn.cursor_instance.queries[0]
    assert "platform = 'telegram'" in query
    assert "processing_status = 'processed'" in query
    assert "source_url IS NULL OR source_url = ''" in query
    assert params == (10,)


def test_apply_candidates_updates_only_missing_telegram_rows() -> None:
    script = _load_script()
    conn = FakeConnection([])
    candidates = [
        script.Candidate(
            id="row-1",
            external_id="tg_3579184587_56",
            source_url="https://t.me/c/3579184587/56",
        )
    ]

    updated = script._apply_candidates(conn, candidates)

    assert updated == 1
    query, params = conn.cursor_instance.queries[0]
    assert "platform = 'telegram'" in query
    assert "external_id LIKE %s" in query
    assert "source_url IS NULL OR source_url = ''" in query
    assert params == [("https://t.me/c/3579184587/56", "row-1", "tg_%")]
