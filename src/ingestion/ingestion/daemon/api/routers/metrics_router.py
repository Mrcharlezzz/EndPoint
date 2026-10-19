"""``GET /metrics`` — Prometheus exposition for Grafana / DataDog scrapes.

The endpoint is public (Prometheus convention; never authenticated) and
emits the canonical text exposition format ``text/plain; version=0.0.4;
charset=utf-8`` so any compliant scraper can ingest it. We hand-roll the
format instead of pulling ``prometheus_client`` to keep the daemon
dependency surface tight; the format is small and stable, the metric
list short, and the only escaping needed is for label values (none of
ours carry special characters today).

Snapshots are cached for ``_SNAPSHOT_TTL_SECONDS`` so a 1-second scrape
interval doesn't pummel SQLite. The cache is global (one snapshot per
process), keyed on monotonic time, and rebuilt when stale. Trade-off:
during a real spike a scrape can serve up to one second of stale data.
That's acceptable for ops dashboards (alerts use 1-5 min windows
anyway); a tighter SLA would warrant a dedicated metric collection
loop and a real prometheus_client integration.

Metric naming follows Prometheus conventions:
- All names are lowercase snake_case prefixed ``endpoint_daemon_``
- Counters end in ``_total``
- Durations end in ``_seconds``
- Gauges and counters use bare numeric units; histograms summarised
  with ``_count`` / ``_sum`` / quantile labels.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite
from fastapi import APIRouter, Response

from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.logging import get_logger
from ingestion.daemon.ports.sync_logger import KIND_CREDENTIAL_REFRESH

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter(tags=["metrics"])

_log = get_logger(__name__)

# Process start time captured ONCE at import. Wall-clock seconds since
# epoch is fine here: the metric only needs to survive a few seconds of
# clock skew between scrapes.
_PROCESS_START_MONOTONIC = time.monotonic()

# Prometheus exposition content type. Pinned exactly per the spec —
# scrapers content-negotiate against this string.
_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Snapshot cache TTL. 1s matches the most aggressive scrape interval
# operators run; tighter and we'd hammer SQLite, looser and metrics
# would lag visibly behind reality.
_SNAPSHOT_TTL_SECONDS = 1.0

# Histogram-style quantiles emitted for sync duration. Keep it small —
# more quantiles linearly inflate the scrape body without much insight
# beyond p50 / p95 / p99 in practice.
_DURATION_QUANTILES = (0.5, 0.95, 0.99)

# Cap on history rows scanned for the duration histogram. The spec's
# "last 100 syncs" is a balance between recency (we want the histogram
# to reflect current behaviour, not the past hour's outliers) and the
# query cost on a large sync_profiles table.
_HISTOGRAM_SAMPLE_SIZE = 100


@dataclass(frozen=True)
class _Snapshot:
    """One frozen Prometheus exposition body, plus the wall-clock when
    it was generated. Cached at module scope keyed by sqlite_path."""

    body: str
    captured_at_monotonic: float


# Cache keyed by sqlite_path string so multiple test apps using
# different DBs don't clobber each other. In production there's only
# one daemon process / DB so the dict has at most one entry.
_LAST_SNAPSHOT: dict[str, _Snapshot] = {}


@router.get(
    "/metrics",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
async def metrics(ctx: ContextDep) -> Response:
    """Return the Prometheus exposition text for the current snapshot.

    Cached for ``_SNAPSHOT_TTL_SECONDS`` to absorb tight scrape
    intervals. Content-Type is pinned to the canonical string so no
    middleware accidentally rewrites it (e.g. to ``text/plain``
    without the ``version=`` parameter, which makes some scrapers
    fall back to JSON parsing).
    """
    sqlite_key = str(ctx.config.sqlite_path)
    now = time.monotonic()
    cached = _LAST_SNAPSHOT.get(sqlite_key)
    if cached is not None and (now - cached.captured_at_monotonic) < _SNAPSHOT_TTL_SECONDS:
        body = cached.body
    else:
        body = await _build_snapshot(
            sqlite_path=ctx.config.sqlite_path,
            worker_capacity=ctx.config.scheduler.max_concurrent_syncs,
        )
        _LAST_SNAPSHOT[sqlite_key] = _Snapshot(body=body, captured_at_monotonic=now)
    return Response(content=body, media_type=_PROMETHEUS_CONTENT_TYPE)


async def _build_snapshot(
    *,
    sqlite_path: Path,
    worker_capacity: int,
) -> str:
    """Run every metric query, format the result, return the full body.

    Each query is independent so a single failed table doesn't blank
    every metric. We log + skip on individual failures rather than
    propagate; a Prometheus scrape that returns 500 hides the rest of
    the metrics, which is strictly worse than reporting partial data.
    """
    counters = await _read_counters(sqlite_path)
    duration_stats = await _read_duration_stats(sqlite_path)

    active = counters.get("active_syncs", 0)
    workers_available = max(0, worker_capacity - active)
    uptime_seconds = max(0.0, time.monotonic() - _PROCESS_START_MONOTONIC)

    parts: list[str] = []
    _emit_gauge(
        parts,
        name="endpoint_daemon_active_syncs",
        help_text="Currently in-flight syncs (sync_history rows in 'running' state).",
        value=float(active),
    )
    _emit_gauge(
        parts,
        name="endpoint_daemon_queue_depth",
        help_text="Pending force-sync requests awaiting scheduler pickup.",
        value=float(counters.get("queue_depth", 0)),
    )
    _emit_gauge(
        parts,
        name="endpoint_daemon_dead_letter_count",
        help_text="(user, platform) pairs currently suppressed by the dead-letter tracker.",
        value=float(counters.get("dead_letter_count", 0)),
    )
    _emit_counter(
        parts,
        name="endpoint_daemon_refresh_failures_total",
        help_text=(
            "Total credential-refresh syncs that ended in 'failed' state since process start."
        ),
        value=float(counters.get("refresh_failures", 0)),
    )
    _emit_counter(
        parts,
        name="endpoint_daemon_items_ingested_total",
        help_text="Total items ingested (items_new + items_updated) across all completed syncs.",
        value=float(counters.get("items_ingested", 0)),
    )
    _emit_gauge(
        parts,
        name="endpoint_daemon_workers_available",
        help_text="Worker pool capacity minus current active syncs.",
        value=float(workers_available),
    )
    _emit_gauge(
        parts,
        name="endpoint_daemon_uptime_seconds",
        help_text="Seconds since the daemon process started.",
        value=uptime_seconds,
    )
    _emit_duration_histogram(
        parts,
        name="endpoint_daemon_sync_duration_seconds",
        help_text=(
            f"Sync duration distribution over the last {_HISTOGRAM_SAMPLE_SIZE} completed syncs."
        ),
        durations=duration_stats,
    )
    # Trailing newline matters: Prometheus is permissive but several
    # client libraries (and the official text format spec) recommend it.
    return "".join(parts) + "\n"


def _emit_gauge(parts: list[str], *, name: str, help_text: str, value: float) -> None:
    """Append HELP, TYPE, and a single sample line for a gauge."""
    parts.append(f"# HELP {name} {help_text}\n")
    parts.append(f"# TYPE {name} gauge\n")
    parts.append(f"{name} {_format_float(value)}\n")


def _emit_counter(parts: list[str], *, name: str, help_text: str, value: float) -> None:
    """Append HELP, TYPE, and a single sample line for a counter."""
    parts.append(f"# HELP {name} {help_text}\n")
    parts.append(f"# TYPE {name} counter\n")
    parts.append(f"{name} {_format_float(value)}\n")


def _emit_duration_histogram(
    parts: list[str],
    *,
    name: str,
    help_text: str,
    durations: list[float],
) -> None:
    """Emit a Prometheus summary-style block: ``_count``, ``_sum``,
    and a quantile label per ``_DURATION_QUANTILES``.

    Real Prometheus histograms require pre-declared bucket boundaries
    that align across scrapes; summaries are simpler and fit the
    "last N samples" model better. The TYPE is declared as ``summary``
    so dashboards render the quantile labels correctly.
    """
    parts.append(f"# HELP {name} {help_text}\n")
    parts.append(f"# TYPE {name} summary\n")
    if not durations:
        parts.append(f"{name}_count 0\n")
        parts.append(f"{name}_sum 0\n")
        for q in _DURATION_QUANTILES:
            parts.append(f'{name}{{quantile="{q}"}} NaN\n')
        return
    sorted_durs = sorted(durations)
    parts.append(f"{name}_count {len(sorted_durs)}\n")
    parts.append(f"{name}_sum {_format_float(sum(sorted_durs))}\n")
    for q in _DURATION_QUANTILES:
        parts.append(f'{name}{{quantile="{q}"}} {_format_float(_quantile(sorted_durs, q))}\n')


def _quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation quantile of a presorted list.

    Matches numpy's default; faster than pulling numpy and avoids a
    new dependency. Empty input is the caller's responsibility (the
    histogram emitter handles that case before getting here).
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lower_idx = int(pos)
    upper_idx = min(lower_idx + 1, len(sorted_values) - 1)
    fraction = pos - lower_idx
    lower = sorted_values[lower_idx]
    upper = sorted_values[upper_idx]
    return lower + (upper - lower) * fraction


def _format_float(value: float) -> str:
    """Format a float for Prometheus exposition.

    Integer-valued floats render without decimals so a counter
    increment of one stays visually clean. NaN is the literal token
    Prometheus expects for missing-quantile samples.
    """
    if value != value:  # NaN check (NaN != NaN by IEEE 754)
        return "NaN"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


async def _read_counters(sqlite_path: Path) -> dict[str, int]:
    """Run every count-style query in one connection.

    Each statement is wrapped in its own ``try`` so a missing table
    (e.g. tests built against a partially-migrated DB) doesn't blank
    the whole scrape. Returned dict maps short keys to integer values;
    missing keys default to ``0`` at the call site.
    """
    out: dict[str, int] = {}
    try:
        async with aiosqlite.connect(sqlite_path) as conn:
            conn.row_factory = aiosqlite.Row
            out["active_syncs"] = await _safe_scalar_int(
                conn,
                "SELECT COUNT(*) FROM sync_history WHERE state = 'running'",
            )
            out["queue_depth"] = await _safe_scalar_int(
                conn,
                "SELECT COUNT(*) FROM force_sync_requests",
            )
            out["dead_letter_count"] = await _safe_scalar_int(
                conn,
                "SELECT COUNT(*) FROM dead_letter_state WHERE suppressed_until IS NOT NULL",
            )
            out["refresh_failures"] = await _safe_scalar_int(
                conn,
                "SELECT COUNT(*) FROM sync_history WHERE state = 'failed' AND sync_kind = ?",
                params=(KIND_CREDENTIAL_REFRESH,),
            )
            # COALESCE flattens NULL (empty table) to 0 so the SUM
            # doesn't propagate a NULL through to the metric body.
            out["items_ingested"] = await _safe_scalar_int(
                conn,
                "SELECT COALESCE(SUM(items_new), 0) + COALESCE(SUM(items_updated), 0) "
                "FROM sync_profiles",
            )
    except Exception:
        _log.warning("metrics.counters_query_failed", exc_info=True)
    return out


async def _read_duration_stats(sqlite_path: Path) -> list[float]:
    """Pull the most recent ``_HISTOGRAM_SAMPLE_SIZE`` durations.

    Joined against ``sync_history`` so we can ORDER BY ``started_at``
    (the profiles row carries no timestamp of its own). NULL durations
    are filtered out to keep the quantile calculation honest.
    """
    durations: list[float] = []
    try:
        async with aiosqlite.connect(sqlite_path) as conn:
            cursor = await conn.execute(
                """
                SELECT p.total_duration_seconds
                FROM sync_profiles p
                JOIN sync_history h ON h.id = p.sync_id
                WHERE p.total_duration_seconds IS NOT NULL
                ORDER BY h.started_at DESC
                LIMIT ?
                """,
                (_HISTOGRAM_SAMPLE_SIZE,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        for row in rows:
            value = row[0]
            if value is None:
                continue
            try:
                durations.append(float(value))
            except (TypeError, ValueError):
                continue
    except Exception:
        _log.warning("metrics.duration_query_failed", exc_info=True)
    return durations


async def _safe_scalar_int(
    conn: aiosqlite.Connection,
    sql: str,
    *,
    params: tuple[object, ...] = (),
) -> int:
    """Run a single COUNT/SUM query and return an int. Defaults to 0.

    Missing tables raise ``OperationalError`` which we treat as "no
    rows yet" — a partially-migrated test DB shouldn't kill the whole
    scrape. The error is logged at WARN with the offending SQL so an
    operator watching the metric pipeline sees "couldn't read X
    table" rather than a silent zero on the dashboard. We deliberately
    keep the swallow (a 500 here would blank every metric) and rely on
    the log channel to surface real DB problems.
    """
    try:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
    except aiosqlite.Error:
        _log.warning("metrics.safe_scalar_query_failed", sql=sql, exc_info=True)
        return 0
    if row is None or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        _log.warning(
            "metrics.safe_scalar_value_unparseable",
            sql=sql,
            raw_value_type=type(row[0]).__name__,
        )
        return 0


def reset_snapshot_cache_for_tests() -> None:
    """Test hook: clear the module-level cache so consecutive test
    requests don't see stale snapshots between fixtures.

    Public (no leading underscore) so the tester slice can import it
    without a private-symbol warning. Not meant for production code —
    the cache is process-scoped and self-evicting via TTL.
    """
    _LAST_SNAPSHOT.clear()
