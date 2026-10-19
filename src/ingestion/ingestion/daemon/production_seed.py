"""Production seed export/import helpers for shared Postgres + daemon SQLite.

The production seed is a curated course-data snapshot, not a full clone.
Only the shared dashboard-visible tables and the daemon's ``sync_cursors``
table move through the artifact; user/session/credential/runtime tables are
left out and must stay empty on import.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor, execute_batch, execute_values

from ingestion.daemon.config import DEFAULT_SQLITE_PATH
from ingestion.daemon.store.local import apply_migrations

ARTIFACT_FORMAT_VERSION: Final[int] = 1
SYNC_CURSOR_COLUMNS: Final[tuple[str, ...]] = (
    "user_email",
    "platform",
    "scope",
    "cursor_json",
    "updated_at",
)
POSTGRES_TABLES: Final[tuple[str, ...]] = (
    "courses",
    "course_platform_mappings",
    "ingested_items",
    "base_classified_items",
    "assignments",
    "announcements",
    "events",
    "materials",
    "canonical_entities",
    "links",
    "attachments",
)
DAEMON_TABLES: Final[tuple[str, ...]] = ("sync_cursors",)
POSTGRES_EXCLUDED_TABLES: Final[tuple[str, ...]] = (
    "platform_credentials",
    "presentation_api_users",
    "presentation_api_sessions",
    "presentation_api_user_course_selections",
    "presentation_api_item_read_state",
    "classification_logs",
    "dedup_logs",
)
DAEMON_RUNTIME_EMPTY_TABLES: Final[tuple[str, ...]] = (
    "sync_history",
    "user_schedule_overrides",
    "dead_letter_state",
    "sync_profiles",
    "logs",
    "alert_firings",
    "audit_log",
    "force_sync_requests",
    "idempotency_log",
)
DAEMON_PRESERVED_TABLES: Final[tuple[str, ...]] = (
    "schema_migrations",
    "config_overlay",
    "alert_rules",
)
POSTGRES_EXPORT_ORDER: Final[dict[str, tuple[str, ...]]] = {
    "courses": ("id",),
    "course_platform_mappings": ("id",),
    "ingested_items": ("id",),
    "base_classified_items": ("id",),
    "assignments": ("baseclassifieditem_ptr_id",),
    "announcements": ("baseclassifieditem_ptr_id",),
    "events": ("baseclassifieditem_ptr_id",),
    "materials": ("baseclassifieditem_ptr_id",),
    "canonical_entities": ("id",),
    "links": ("id",),
    "attachments": ("id",),
}


class SeedValidationError(RuntimeError):
    """Raised when a seed artifact or target state violates the contract."""


@dataclass(frozen=True)
class TableBatch:
    table: str
    rows: list[dict[str, object]]


def sanitize_postgres_row(table: str, row: dict[str, object]) -> dict[str, object]:
    """Return a sanitized copy of an exported Postgres row."""
    sanitized = dict(row)
    if table == "ingested_items":
        sanitized["fetched_by_user_email"] = None
        sanitized["raw_payload"] = {}
    return sanitized


def validate_artifact(payload: object) -> dict[str, object]:
    """Validate top-level artifact shape and manifest counts."""
    if not isinstance(payload, dict):
        raise SeedValidationError("seed artifact must be a JSON object")

    format_version = payload.get("format_version")
    if format_version != ARTIFACT_FORMAT_VERSION:
        raise SeedValidationError(
            f"unsupported seed format_version {format_version!r}; "
            f"expected {ARTIFACT_FORMAT_VERSION}"
        )

    postgres = _validate_section(payload.get("postgres"), "postgres", POSTGRES_TABLES)
    daemon = _validate_section(payload.get("daemon"), "daemon", DAEMON_TABLES)
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise SeedValidationError("seed artifact missing manifest object")

    manifest_postgres = _validate_manifest_counts(
        manifest.get("postgres"), "postgres", POSTGRES_TABLES
    )
    manifest_daemon = _validate_manifest_counts(manifest.get("daemon"), "daemon", DAEMON_TABLES)

    for table, rows in postgres.items():
        expected = manifest_postgres[table]
        if expected != len(rows):
            raise SeedValidationError(
                f"manifest mismatch for postgres.{table}: expected {expected}, found {len(rows)}"
            )
    for table, rows in daemon.items():
        expected = manifest_daemon[table]
        if expected != len(rows):
            raise SeedValidationError(
                f"manifest mismatch for daemon.{table}: expected {expected}, found {len(rows)}"
            )

    return payload


def prepare_postgres_import(
    postgres_section: object,
) -> tuple[list[TableBatch], list[tuple[object, object]]]:
    """Prepare ordered Postgres batches plus delayed base->canonical backfills."""
    if not isinstance(postgres_section, dict):
        raise SeedValidationError("postgres section must be an object")

    batches: list[TableBatch] = []
    canonical_backfills: list[tuple[object, object]] = []

    for table in POSTGRES_TABLES:
        raw_rows = postgres_section.get(table, [])
        if not isinstance(raw_rows, list):
            raise SeedValidationError(f"postgres.{table} must be a list")
        if not raw_rows:
            continue

        rows = [_validate_row_object(table, row) for row in raw_rows]
        if table == "base_classified_items":
            sanitized_rows: list[dict[str, object]] = []
            for row in rows:
                next_row = dict(row)
                canonical_id = next_row.get("canonical_entity_id")
                if canonical_id is not None:
                    base_id = next_row.get("id")
                    if base_id is None:
                        raise SeedValidationError(
                            "base_classified_items row missing id for canonical backfill"
                        )
                    canonical_backfills.append((base_id, canonical_id))
                    next_row["canonical_entity_id"] = None
                sanitized_rows.append(next_row)
            rows = sanitized_rows

        batches.append(TableBatch(table=table, rows=rows))

    return batches, canonical_backfills


def export_sqlite_seed_rows(sqlite_path: Path) -> list[dict[str, object]]:
    """Read ordered ``sync_cursors`` rows from the daemon SQLite store."""
    if not sqlite_path.exists():
        raise SeedValidationError(f"daemon sqlite database not found: {sqlite_path}")

    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT user_email, platform, scope, cursor_json, updated_at
            FROM sync_cursors
            ORDER BY user_email, platform, scope
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def import_sqlite_seed_rows(sqlite_path: Path, rows: list[dict[str, object]]) -> None:
    """Apply daemon migrations, validate clean runtime state, and load cursors."""
    apply_migrations(sqlite_path)

    conn = sqlite3.connect(str(sqlite_path))
    try:
        _validate_sqlite_tables_empty(
            conn,
            ("sync_cursors", *DAEMON_RUNTIME_EMPTY_TABLES),
        )

        with conn:
            conn.execute("DELETE FROM sync_cursors")
            if rows:
                ordered_values = [
                    tuple(row.get(column) for column in SYNC_CURSOR_COLUMNS) for row in rows
                ]
                conn.executemany(
                    """
                    INSERT INTO sync_cursors
                    (user_email, platform, scope, cursor_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ordered_values,
                )

        inserted = _count_sqlite_table(conn, "sync_cursors")
        if inserted != len(rows):
            raise SeedValidationError(
                f"sync_cursors import count mismatch: expected {len(rows)}, found {inserted}"
            )
        _validate_sqlite_tables_empty(conn, DAEMON_RUNTIME_EMPTY_TABLES)
    finally:
        conn.close()


def clear_sqlite_seed_rows(sqlite_path: Path) -> None:
    """Best-effort rollback for fresh-only imports when Postgres commit fails."""
    if not sqlite_path.exists():
        return

    conn = sqlite3.connect(str(sqlite_path))
    try:
        with conn:
            conn.execute("DELETE FROM sync_cursors")
    finally:
        conn.close()


def export_seed_artifact(sqlite_path: Path) -> dict[str, object]:
    """Build an in-memory production seed artifact."""
    sync_cursors = export_sqlite_seed_rows(sqlite_path)

    conn = _connect_postgres()
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            postgres = {table: _fetch_postgres_rows(cur, table) for table in POSTGRES_TABLES}
            excluded_counts = {
                table: _count_postgres_table(cur, table) for table in POSTGRES_EXCLUDED_TABLES
            }
    finally:
        conn.close()

    artifact = {
        "format_version": ARTIFACT_FORMAT_VERSION,
        "created_at": _utc_now_iso(),
        "source": {
            "postgres": {
                "database": os.environ["POSTGRES_DB"],
                "host": os.environ.get("PG_HOST", "localhost"),
            },
            "daemon": {
                "sqlite_path": str(sqlite_path),
            },
            "excluded_row_counts": {
                "postgres": excluded_counts,
            },
        },
        "manifest": {
            "postgres": {table: len(rows) for table, rows in postgres.items()},
            "daemon": {"sync_cursors": len(sync_cursors)},
        },
        "postgres": postgres,
        "daemon": {"sync_cursors": sync_cursors},
    }
    validate_artifact(artifact)
    return cast("dict[str, object]", artifact)


def import_seed_artifact(artifact_path: Path, sqlite_path: Path) -> dict[str, object]:
    """Import a validated artifact into fresh Postgres + daemon SQLite state."""
    _verify_sha256_if_present(artifact_path)
    artifact = _load_artifact(artifact_path)
    postgres_section = artifact["postgres"]
    daemon_section = artifact["daemon"]

    if not isinstance(daemon_section, dict):
        raise SeedValidationError("daemon section must be an object")
    raw_sync_cursors = daemon_section.get("sync_cursors")
    if not isinstance(raw_sync_cursors, list):
        raise SeedValidationError("daemon.sync_cursors must be a list")
    sync_cursors = [_validate_row_object("sync_cursors", row) for row in raw_sync_cursors]

    batches, canonical_backfills = prepare_postgres_import(postgres_section)
    conn = _connect_postgres()
    sqlite_loaded = False
    try:
        with conn.cursor() as cur:
            _validate_postgres_tables_empty(cur, (*POSTGRES_TABLES, *POSTGRES_EXCLUDED_TABLES))
            for batch in batches:
                _insert_postgres_rows(cur, batch.table, batch.rows)
            _apply_canonical_backfills(cur, canonical_backfills)
            _validate_postgres_counts(
                cur,
                artifact["manifest"],
            )
            _validate_postgres_tables_empty(cur, POSTGRES_EXCLUDED_TABLES)

        import_sqlite_seed_rows(sqlite_path, sync_cursors)
        sqlite_loaded = True
        conn.commit()
    except Exception:
        conn.rollback()
        if sqlite_loaded:
            clear_sqlite_seed_rows(sqlite_path)
        raise
    finally:
        conn.close()

    return artifact


def main_export(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for the compressed seed artifact (.json.gz).",
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=_default_sqlite_path(),
        help=f"Path to the daemon SQLite file (default: {_default_sqlite_path()}).",
    )
    args = parser.parse_args(argv)

    artifact = export_seed_artifact(args.sqlite_path)
    output_path = args.output or _default_output_path()
    _write_artifact(output_path, artifact)
    sha_path = _write_sha256(output_path)

    print(f"Production seed written: {output_path}")
    print(f"SHA-256 written: {sha_path}")
    _print_counts("postgres", artifact["manifest"], POSTGRES_TABLES)
    _print_counts("daemon", artifact["manifest"], DAEMON_TABLES)
    return 0


def main_import(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Path to the seed artifact (.json.gz).")
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=_default_sqlite_path(),
        help=f"Path to the daemon SQLite file (default: {_default_sqlite_path()}).",
    )
    args = parser.parse_args(argv)

    artifact = import_seed_artifact(args.artifact, args.sqlite_path)
    print(f"Production seed imported: {args.artifact}")
    _print_counts("postgres", artifact["manifest"], POSTGRES_TABLES)
    _print_counts("daemon", artifact["manifest"], DAEMON_TABLES)
    return 0


def _default_sqlite_path() -> Path:
    override = os.environ.get("ENDPOINT_DAEMON__SQLITE_PATH")
    if override:
        return Path(override)
    return DEFAULT_SQLITE_PATH


def _default_output_path() -> Path:
    repo_root = Path(__file__).resolve().parents[4]
    target_dir = repo_root / "data" / "seeds" / "production"
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return target_dir / f"{timestamp}-production-seed.json.gz"


def _connect_postgres() -> Any:
    missing = [
        name
        for name in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
        if not os.environ.get(name)
    ]
    if missing:
        joined = ", ".join(missing)
        raise SeedValidationError(f"missing required Postgres environment variables: {joined}")

    return psycopg2.connect(
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ.get("PG_HOST", "localhost"),
        port=int(os.environ.get("PG_PORT", "5432")),
    )


def _validate_section(
    section: object,
    name: str,
    tables: tuple[str, ...],
) -> dict[str, list[dict[str, object]]]:
    if not isinstance(section, dict):
        raise SeedValidationError(f"seed artifact missing {name} object")

    expected = set(tables)
    actual = set(section.keys())
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"missing {missing}")
        if extra:
            parts.append(f"unexpected {extra}")
        raise SeedValidationError(f"{name} section has wrong tables: {', '.join(parts)}")

    validated: dict[str, list[dict[str, object]]] = {}
    for table in tables:
        rows = section[table]
        if not isinstance(rows, list):
            raise SeedValidationError(f"{name}.{table} must be a list")
        validated[table] = [_validate_row_object(f"{name}.{table}", row) for row in rows]
    return validated


def _validate_manifest_counts(
    payload: object,
    name: str,
    tables: tuple[str, ...],
) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise SeedValidationError(f"manifest.{name} must be an object")

    expected = set(tables)
    actual = set(payload.keys())
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"missing {missing}")
        if extra:
            parts.append(f"unexpected {extra}")
        raise SeedValidationError(f"manifest.{name} has wrong tables: {', '.join(parts)}")

    counts: dict[str, int] = {}
    for table in tables:
        value = payload[table]
        if not isinstance(value, int):
            raise SeedValidationError(f"manifest.{name}.{table} must be an integer")
        counts[table] = value
    return counts


def _validate_row_object(label: str, row: object) -> dict[str, object]:
    if not isinstance(row, dict):
        raise SeedValidationError(f"{label} rows must be objects")
    return dict(row)


def _fetch_postgres_rows(
    cur: RealDictCursor,
    table: str,
) -> list[dict[str, object]]:
    order_by = POSTGRES_EXPORT_ORDER[table]
    query = sql.SQL("SELECT * FROM {table} ORDER BY {order_by}").format(
        table=sql.Identifier(table),
        order_by=sql.SQL(", ").join(sql.Identifier(column) for column in order_by),
    )
    cur.execute(query)
    fetched = cur.fetchall()
    rows: list[dict[str, object]] = []
    for row in fetched:
        json_ready = {key: _json_ready(value) for key, value in dict(row).items()}
        rows.append(sanitize_postgres_row(table, json_ready))
    return rows


def _count_postgres_table(cur: Any, table: str) -> int:
    query = sql.SQL("SELECT COUNT(*) FROM {table}").format(table=sql.Identifier(table))
    cur.execute(query)
    row = cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, Mapping):
        return int(next(iter(row.values())))
    if isinstance(row, Sequence) and not isinstance(row, str | bytes | bytearray):
        return int(row[0])
    return int(row)


def _validate_postgres_tables_empty(cur: Any, tables: tuple[str, ...]) -> None:
    for table in tables:
        count = _count_postgres_table(cur, table)
        if count != 0:
            raise SeedValidationError(
                f"fresh-only import requires empty table: {table} ({count} rows)"
            )


def _insert_postgres_rows(cur: Any, table: str, rows: list[dict[str, object]]) -> None:
    if not rows:
        return

    columns = list(rows[0].keys())
    for row in rows[1:]:
        if list(row.keys()) != columns:
            raise SeedValidationError(f"inconsistent columns for postgres.{table}")

    values = [tuple(_pg_value(row[column]) for column in columns) for row in rows]
    statement = sql.SQL("INSERT INTO {table} ({columns}) VALUES %s").format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    execute_values(cur, statement.as_string(cur), values, page_size=500)


def _apply_canonical_backfills(cur: Any, backfills: list[tuple[object, object]]) -> None:
    if not backfills:
        return

    statement = sql.SQL("UPDATE {table} SET canonical_entity_id = %s WHERE id = %s").format(
        table=sql.Identifier("base_classified_items")
    )
    execute_batch(
        cur,
        statement.as_string(cur),
        [(canonical_id, base_id) for base_id, canonical_id in backfills],
        page_size=500,
    )


def _validate_postgres_counts(cur: Any, manifest: object) -> None:
    if not isinstance(manifest, dict):
        raise SeedValidationError("manifest must be an object")
    postgres_manifest = manifest.get("postgres")
    counts = _validate_manifest_counts(postgres_manifest, "postgres", POSTGRES_TABLES)
    for table, expected in counts.items():
        actual = _count_postgres_table(cur, table)
        if actual != expected:
            raise SeedValidationError(
                f"postgres import count mismatch for {table}: expected {expected}, found {actual}"
            )


def _validate_sqlite_tables_empty(conn: sqlite3.Connection, tables: tuple[str, ...]) -> None:
    for table in tables:
        count = _count_sqlite_table(conn, table)
        if count != 0:
            raise SeedValidationError(
                f"fresh-only import requires empty table: {table} ({count} rows)"
            )


def _count_sqlite_table(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _write_artifact(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_sha256(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sha_path = path.with_suffix(path.suffix + ".sha256")
    sha_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return sha_path


def _verify_sha256_if_present(path: Path) -> None:
    sha_path = path.with_suffix(path.suffix + ".sha256")
    if not sha_path.exists():
        return

    expected = sha_path.read_text(encoding="utf-8").strip().split()[0]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise SeedValidationError(f"sha256 mismatch for artifact: {path}")


def _load_artifact(path: Path) -> dict[str, object]:
    if not path.exists():
        raise SeedValidationError(f"seed artifact not found: {path}")

    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_artifact(payload)


def _pg_value(value: object) -> object:
    if isinstance(value, (dict, list)):
        return Json(value)
    return value


def _json_ready(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    return value


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _print_counts(section: str, manifest: object, tables: tuple[str, ...]) -> None:
    if not isinstance(manifest, dict):
        return
    counts = manifest.get(section)
    if not isinstance(counts, dict):
        return
    for table in tables:
        print(f"{section}.{table}: {counts.get(table, 0)}")
