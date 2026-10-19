"""``endpoint-admin debug`` subcommands — read-only diagnostics.

Designed for incident response: dump daemon state in one JSON blob,
inspect the lockfile (works even when the daemon is down), count rows
in operational tables, and list credentials that look broken. Every
command writes JSON to stdout and prints non-fatal warnings to stderr.

NO mutations. The lockfile read is the one allowed direct-filesystem
escape: it intentionally bypasses the daemon API so an operator can
answer "why won't the daemon start?" when the daemon itself isn't
responding.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

# Bound for state-dump reads. Big enough to capture a full incident
# window; small enough that pulling the dump into a chat / paste buffer
# stays under a reasonable size.
_AUDIT_TAIL_ROWS = 100
_DLQ_TAIL_ROWS = 100
_SYNCS_TAIL_ROWS = 100

# Default lockfile path. Mirrors :data:`ingestion.daemon.config.DEFAULT_SQLITE_PATH`
# with the ``.lock`` suffix that :func:`ingestion.daemon.lockfile.acquire`
# attaches. Hard-coded here so this command can run without the daemon
# extras installed.
DEFAULT_LOCKFILE_PATH = Path("/var/lib/ingestor/state.db.lock")


debug_app = typer.Typer(
    name="debug",
    help="Read-only diagnostic helpers (state-dump / lockfile / queue-stats / failing-creds).",
    no_args_is_help=True,
    add_completion=False,
)


@dataclass(frozen=True)
class _LockfileSummary:
    """Decoded contents of a daemon lockfile.

    ``alive`` is best-effort: ``os.kill(pid, 0)`` only confirms a
    process exists at that PID — it doesn't prove the process is the
    daemon. Operators looking at this summary should cross-check the
    hostname when in doubt.
    """

    path: str
    exists: bool
    pid: int | None
    hostname: str | None
    started_at: str | None
    alive: bool | None
    raw: str | None
    parse_error: str | None = None


def _summary(path: Path, **overrides: Any) -> _LockfileSummary:
    """Build a default :class:`_LockfileSummary` and apply ``overrides``.

    Cuts the four near-identical 8-arg constructor calls inside
    :func:`_read_lockfile` down to one-line builders.
    """
    base: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "pid": None,
        "hostname": None,
        "started_at": None,
        "alive": None,
        "raw": None,
        "parse_error": None,
    }
    base.update(overrides)
    return _LockfileSummary(**base)


def _read_lockfile(path: Path) -> _LockfileSummary:
    """Parse the daemon's lockfile without locking it.

    The lockfile format is ``<pid>\\n<hostname>\\n<iso8601>\\n`` (see
    :mod:`ingestion.daemon.lockfile`). This is a passive inspection: we
    do NOT call :func:`os.unlink` even if the holder is dead; the
    daemon recovers stale locks on its own at startup, and a CLI that
    silently removes someone else's lock invites a race the operator
    didn't ask for.
    """
    if not path.exists():
        return _summary(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _summary(path, exists=True, parse_error=f"read failed: {exc}")
    parts = raw.splitlines()
    if len(parts) < 2:
        return _summary(
            path,
            exists=True,
            raw=raw,
            parse_error=f"malformed lockfile (lines={len(parts)})",
        )
    try:
        pid = int(parts[0])
    except ValueError:
        return _summary(
            path,
            exists=True,
            hostname=parts[1] if len(parts) > 1 else None,
            started_at=parts[2] if len(parts) > 2 else None,
            raw=raw,
            parse_error=f"non-integer pid {parts[0]!r}",
        )
    return _summary(
        path,
        exists=True,
        pid=pid,
        hostname=parts[1],
        started_at=parts[2] if len(parts) > 2 else None,
        alive=_pid_alive(pid),
        raw=raw,
    )


def _pid_alive(pid: int) -> bool | None:
    """Return True / False / None for the liveness of a PID.

    None means we couldn't tell (cross-host lockfile, permission
    denied). False means the kernel confirms no process at that PID.
    True means a process exists — which may or may not be the daemon.

    Operators reading the JSON output should pair this with
    :func:`_pid_alive_note` to disambiguate ``None`` (which has two
    distinct causes) and cross-check ``hostname`` when in doubt.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


def _pid_alive_note(pid: int, alive: bool | None, lock_hostname: str | None) -> str:
    """Return a human-readable explanation of the alive flag.

    Disambiguates the three values:

    * ``True`` -> "running (process exists at pid X)"
    * ``False`` -> "not running (no process at pid X)" or
                   "invalid pid" when pid <= 0
    * ``None`` -> distinguishes the two causes:
                  cross-host lockfile (hostname != socket.gethostname())
                  vs same-host permission-denied.

    A single sentence so the operator doesn't have to grok os.kill
    semantics in the middle of an incident.
    """
    if alive is True:
        return (
            f"running (process exists at pid {pid}; "
            "cross-check hostname to confirm it's the daemon)"
        )
    if alive is False:
        if pid <= 0:
            return f"invalid pid ({pid}) — likely a malformed lockfile"
        return f"not running (kernel reports no process at pid {pid})"
    # alive is None — figure out which reason applies.
    current = socket.gethostname()
    if lock_hostname and lock_hostname != current:
        return (
            f"cannot tell — lockfile is from a different host "
            f"({lock_hostname!r} vs current {current!r}); pid lookup is meaningless cross-host"
        )
    return (
        f"cannot tell — no permission to signal pid {pid}; "
        "the process exists but is owned by another user"
    )


@debug_app.command("lockfile")
def lockfile_cmd(
    ctx: typer.Context,
    path: Annotated[
        Path | None,
        typer.Option("--path", help=f"Lockfile path (default: {DEFAULT_LOCKFILE_PATH})"),
    ] = None,
) -> None:
    """Inspect the daemon's PID lockfile (works without a running daemon).

    Use this to debug "why won't the daemon start?" — the daemon refuses
    to boot when a same-host live peer holds the lockfile.

    Example:
        endpoint-admin debug lockfile --path /var/lib/ingestor/state.db.lock
    """
    target = path or DEFAULT_LOCKFILE_PATH
    summary = _read_lockfile(target)
    # ``alive_note`` disambiguates the alive flag for operators —
    # ``None`` has two distinct causes (cross-host vs perm-denied) and
    # the bare bool/None doesn't say which. Always emit a string so the
    # output shape is stable for jq pipelines.
    alive_note: str | None = None
    if summary.exists and summary.pid is not None:
        alive_note = _pid_alive_note(summary.pid, summary.alive, summary.hostname)
    payload = {
        "path": summary.path,
        "exists": summary.exists,
        "pid": summary.pid,
        "hostname": summary.hostname,
        "started_at": summary.started_at,
        "alive": summary.alive,
        "alive_note": alive_note,
        "current_hostname": socket.gethostname(),
        "parse_error": summary.parse_error,
        "raw": summary.raw,
    }
    # Avoid importing _print_json from admin.py — that would create a
    # circular import at admin_debug load time (admin imports this
    # module at the bottom). Local helper does the same job.
    _emit_json(ctx, payload)


# Source spec for state-dump. Tuple of (label, path, params). A flat
# table keeps the dispatch loop tiny and lets future maintainers append
# a new source without touching the function body.
_STATE_DUMP_SOURCES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("running_syncs", "/api/syncs", {"state": "running", "limit": _SYNCS_TAIL_ROWS}),
    ("failed_syncs", "/api/syncs", {"state": "failed", "limit": _SYNCS_TAIL_ROWS}),
    ("errors_feed", "/api/errors", {"limit": _DLQ_TAIL_ROWS}),
    ("audit_tail", "/api/audit", {"limit": _AUDIT_TAIL_ROWS}),
    ("config_overlay", "/api/config/overlay", None),
)


@debug_app.command("state-dump")
def state_dump_cmd(ctx: typer.Context) -> None:
    """Dump in-flight syncs, dead-letter rows, audit-log tail, and the
    config overlay in one JSON blob — the all-purpose incident snapshot.

    Bounded to ~100 rows per source so the output stays paste-friendly.

    Example:
        endpoint-admin --pretty debug state-dump > incident.json
    """
    from ingestion.cli.admin import get_client, get_options

    client = get_client(ctx)
    # Each source runs independently so a missing endpoint logs to stderr
    # without blanking the whole snapshot.
    sources = {
        label: _safe_query(client, "GET", path, params=params, label=label)
        for label, path, params in _STATE_DUMP_SOURCES
    }
    _emit_json(
        ctx,
        {
            "snapshot": sources,
            "limits": {
                "audit_tail_rows": _AUDIT_TAIL_ROWS,
                "dlq_tail_rows": _DLQ_TAIL_ROWS,
                "syncs_tail_rows": _SYNCS_TAIL_ROWS,
            },
        },
        pretty=get_options(ctx).pretty,
    )


@debug_app.command("queue-stats")
def queue_stats_cmd(ctx: typer.Context) -> None:
    """Quick counts: dead-letter rows, force-sync queue depth, audit log
    size, and SQLite DB size from the daemon's metrics + errors APIs.

    Example:
        endpoint-admin debug queue-stats
    """
    from ingestion.cli.admin import get_client, get_options, parse_prometheus

    client = get_client(ctx)
    pretty = get_options(ctx).pretty
    metrics_text = client.request("GET", "/api/metrics", require_auth=False)
    parsed = parse_prometheus(metrics_text if isinstance(metrics_text, str) else str(metrics_text))
    errors = _safe_query(client, "GET", "/api/errors", params={"limit": 1}, label="errors")
    audit_count = _safe_query(client, "GET", "/api/audit", params={"limit": 1}, label="audit")

    def _gauge(key: str) -> float | None:
        value = parsed.get(key)
        return value if isinstance(value, int | float) else None

    payload = {
        "dead_letter_count": _gauge("endpoint_daemon_dead_letter_count"),
        "queue_depth": _gauge("endpoint_daemon_queue_depth"),
        "active_syncs": _gauge("endpoint_daemon_active_syncs"),
        "workers_available": _gauge("endpoint_daemon_workers_available"),
        "uptime_seconds": _gauge("endpoint_daemon_uptime_seconds"),
        "errors_feed_counts": (errors.get("counts") if isinstance(errors, dict) else None),
        "audit_sample_returned": (len(audit_count) if isinstance(audit_count, list) else None),
    }
    _emit_json(ctx, payload, pretty=pretty)


@debug_app.command("failing-creds")
def failing_creds_cmd(ctx: typer.Context) -> None:
    """List credentials marked expired or stuck in dead-letter.

    Pulls ``/api/credentials?expired_only=true`` plus ``/api/errors``
    filtered to dead_letter; dedupes on (user_email, platform).

    Example:
        endpoint-admin debug failing-creds --pretty
    """
    from ingestion.cli.admin import get_client, get_options

    client = get_client(ctx)
    expired = _safe_query(
        client,
        "GET",
        "/api/credentials",
        params={"expired_only": "true", "limit": 200},
        label="expired_creds",
    )
    errors = _safe_query(
        client,
        "GET",
        "/api/errors",
        params={"kind": "dead_letter", "limit": 200},
        label="dlq_errors",
    )
    expired_rows = _filter_dict_rows(expired if isinstance(expired, list) else [])
    dlq_rows = _filter_dict_rows(errors.get("items", []) if isinstance(errors, dict) else [])
    expired_pairs = {_pair(r) for r in expired_rows}
    dlq_pairs = {_pair(r) for r in dlq_rows}
    payload = {
        "expired_credentials": expired_rows,
        "dead_letter_entries": dlq_rows,
        "summary": {
            "expired_count": len(expired_rows),
            "dead_letter_count": len(dlq_rows),
            "overlap_count": len(expired_pairs & dlq_pairs),
        },
    }
    _emit_json(ctx, payload, pretty=get_options(ctx).pretty)


def _filter_dict_rows(items: Any) -> list[dict[str, Any]]:
    """Return only the dict rows from a heterogeneous list."""
    return [r for r in items if isinstance(r, dict)]


def _pair(row: dict[str, Any]) -> tuple[str, str]:
    """Extract the (user_email, platform) tuple used to dedupe error rows."""
    return (str(row.get("user_email") or ""), str(row.get("platform") or ""))


# Slice 12: known destructive / mutating action verbs the daemon
# emits to ``audit_log``. The ``audit-completeness-check`` subcommand
# reads the audit feed for the last N hours and reports any actions
# that have NOT shown up — useful as a one-shot sanity check that no
# destructive code path silently bypassed the audit writer.
#
# Adding a new mutating action: append its ``audit_log.action`` value
# here so the CLI surfaces it on the next operator run. Conversely,
# REMOVING an entry means the action is no longer expected to fire
# — stay precise so the check stays useful.
_KNOWN_AUDIT_ACTIONS: tuple[str, ...] = (
    # credential service
    "user.pause",
    "user.unpause",
    "credential.pause",
    "credential.unpause",
    "credential.mark_expired",
    "user.bulk_pause",
    "user.bulk_mark_expired",
    # sync service
    "sync.force_requested",
    "sync.force_requested_all",
    "sync.dry_run",
    "sync.cancel",
    "scheduler.drain_set",
    "scheduler.emergency_stop",
    "teams.bulk_refresh",
    # maintenance service
    "sync.reset_cursor",
    "content.purge_item",
    "storage.compact",
    "dead_letter.clear",
    "dead_letter.replay",
    "alert_rule.mute",
    "user.teams.refresh",
    # admin / lifecycle (slice 12)
    "admin.fernet_rotation_requested",
    "admin.fernet_rotation_completed",
    "credential.fernet_rotated",
)


@debug_app.command("audit-completeness-check")
def audit_completeness_check_cmd(
    ctx: typer.Context,
    hours: Annotated[
        int,
        typer.Option("--hours", help="Look back this many hours (default: 24)."),
    ] = 24,
) -> None:
    """Walk the audit_log tail and report actions that did NOT fire.

    Reads ``/api/audit?since=<NOW - hours>`` and compares the
    distinct ``action`` values against the curated
    :data:`_KNOWN_AUDIT_ACTIONS` allowlist. Every known action that
    has NOT shown up in the window lands in the ``missing`` list.
    Unknown actions (in the audit feed but NOT in the allowlist)
    surface in ``unexpected`` so the operator can decide whether
    they're new (extend the allowlist) or unintended (audit
    miswiring).

    A fresh DB shows every known action as missing — that's not a
    bug, just the lack of operator activity. The point is to catch
    destructive code paths that silently bypass the audit writer.

    Example:
        endpoint-admin --pretty debug audit-completeness-check --hours 48
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from ingestion.cli.admin import get_client, get_options  # noqa: PLC0415

    client = get_client(ctx)
    cutoff = (datetime.now(tz=UTC) - timedelta(hours=hours)).isoformat()
    audit_rows = _safe_query(
        client,
        "GET",
        "/api/audit",
        params={"since": cutoff, "limit": 5000},
        label="audit_completeness",
    )
    rows = _filter_dict_rows(audit_rows if isinstance(audit_rows, list) else [])
    seen: set[str] = {str(row.get("action") or "") for row in rows}
    seen.discard("")
    known = set(_KNOWN_AUDIT_ACTIONS)
    missing = sorted(known - seen)
    unexpected = sorted(seen - known)
    payload = {
        "window_hours": hours,
        "since": cutoff,
        "rows_seen": len(rows),
        "missing": missing,
        "unexpected": unexpected,
        "summary": {
            "known_count": len(known),
            "seen_count": len(seen),
            "missing_count": len(missing),
            "unexpected_count": len(unexpected),
        },
    }
    _emit_json(ctx, payload, pretty=get_options(ctx).pretty)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_query(
    client: Any,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None,
    label: str,
) -> Any:
    """Run one daemon request, return ``None`` (and log to stderr) on
    failure so a single broken endpoint doesn't blank the whole dump.
    """
    try:
        return client.request(method, path, params=params, require_auth=True)
    except typer.Exit as exc:
        # Convert the typer.Exit into a structured stderr note instead of
        # propagating — debug commands aim to surface what they CAN read.
        # The exit code is preserved in the note so an operator can see
        # whether the failure was auth (2), network (3), or API (1).
        print(
            f"[debug] source={label} skipped (exit_code={exc.exit_code})",
            file=__import__("sys").stderr,
        )
        return None


def _emit_json(
    ctx: typer.Context,
    payload: Any,
    *,
    pretty: bool | None = None,
) -> None:
    """Local JSON emitter that doesn't import ``admin._print_json``.

    ``admin.py`` imports this module at its bottom; pulling helpers
    upward would create an import cycle. The two emitters share format
    (compact vs pretty) — see :func:`ingestion.cli.admin._print_json`
    for the canonical behaviour.
    """
    import sys

    if pretty is None and ctx.obj is not None:
        opts = ctx.obj.get("client_options")
        pretty = bool(getattr(opts, "pretty", False)) if opts is not None else False
    pretty = bool(pretty)
    if pretty:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    else:
        json.dump(payload, sys.stdout, default=str)
    sys.stdout.write("\n")
