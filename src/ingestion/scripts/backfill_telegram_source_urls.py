"""Backfill source_url for processed Telegram message rows.

Usage:
    uv run python scripts/backfill_telegram_source_urls.py
    uv run python scripts/backfill_telegram_source_urls.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import psycopg

from ingestion.daemon.source_url_backfill import build_telegram_source_url


@dataclass(frozen=True)
class Candidate:
    id: str
    external_id: str
    source_url: str


def _pg_conninfo() -> str:
    host = os.environ.get("PG_HOST") or "127.0.0.1"
    port = os.environ.get("PG_PORT") or "5432"
    dbname = os.environ.get("POSTGRES_DB")
    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    missing = [
        name
        for name, value in (
            ("POSTGRES_DB", dbname),
            ("POSTGRES_USER", user),
            ("POSTGRES_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        joined = ", ".join(missing)
        msg = f"missing required database env vars: {joined}"
        raise RuntimeError(msg)
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def _fetch_candidates(conn: psycopg.Connection[object], *, limit: int | None) -> list[Candidate]:
    query = """
        SELECT id, external_id
        FROM ingested_items
        WHERE platform = 'telegram'
          AND processing_status = 'processed'
          AND (source_url IS NULL OR source_url = '')
        ORDER BY external_id
    """
    params: tuple[object, ...] = ()
    if limit is not None:
        query += " LIMIT %s"
        params = (limit,)

    candidates: list[Candidate] = []
    with conn.cursor() as cur:
        cur.execute(query, params)
        for row_id, external_id in cur.fetchall():
            source_url = build_telegram_source_url(str(external_id))
            if source_url is None:
                continue
            candidates.append(
                Candidate(
                    id=str(row_id),
                    external_id=str(external_id),
                    source_url=source_url,
                )
            )
    return candidates


def _apply_candidates(conn: psycopg.Connection[object], candidates: list[Candidate]) -> int:
    if not candidates:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE ingested_items
            SET source_url = %s
            WHERE id = %s
              AND platform = 'telegram'
              AND external_id LIKE %s
              AND (source_url IS NULL OR source_url = '')
            """,
            [
                (
                    candidate.source_url,
                    candidate.id,
                    "tg_%",
                )
                for candidate in candidates
            ],
        )
        return int(cur.rowcount or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write source_url values. Without this flag the script only previews.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the number of candidate rows, useful for a small dry run.",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit <= 0:
        print("--limit must be a positive integer", file=sys.stderr)
        return 2

    with psycopg.connect(_pg_conninfo()) as conn:
        candidates = _fetch_candidates(conn, limit=args.limit)
        print(f"candidate processed Telegram rows: {len(candidates)}")
        for candidate in candidates[:10]:
            print(f"{candidate.external_id} -> {candidate.source_url}")
        if not args.apply:
            print("dry run only; pass --apply to update ingested_items.source_url")
            return 0
        updated = _apply_candidates(conn, candidates)
        conn.commit()
        print(f"updated processed Telegram rows: {updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
