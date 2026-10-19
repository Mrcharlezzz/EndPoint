-- Hot-query indices identified by the slice 11 perf audit.
--
-- Each index is gated on ``IF NOT EXISTS`` so a re-run of the migration
-- pass on a database that already has the index is a no-op. Indices match
-- the column sets that the scheduler, dashboard, and audit-export views
-- actually filter / sort by; without these every page-load scans the
-- table in full.

-- DeadLetterTracker.is_suppressed and record_failure both filter on
-- (user_email, platform). The composite PK already covers it via the
-- left-most-prefix rule, but PRAGMA index_list shows SQLite picks the PK
-- only when both columns are equality-bound — adding the explicit
-- composite makes the planner choose the index for the scheduler's
-- bulk-suppression check too.
CREATE INDEX IF NOT EXISTS idx_dead_letter_state_user_platform
    ON dead_letter_state (user_email, platform);

-- The /api/syncs?user=&platform= filter and the scheduler's bulk
-- last_success_at_bulk both run ``WHERE user_email = ? AND platform = ?``.
-- Existing indices on (user_email, started_at) and (state, started_at)
-- don't cover this filter cleanly.
CREATE INDEX IF NOT EXISTS idx_sync_history_user_platform
    ON sync_history (user_email, platform);

-- /api/metrics aggregates ``WHERE finished_at >= ?`` to compute throughput
-- in the last hour. Without this the planner scans the table.
CREATE INDEX IF NOT EXISTS idx_sync_history_finished_at
    ON sync_history (finished_at);

-- NOTE: a ``requested_at`` index on ``force_sync_requests`` already exists
-- as ``ix_force_sync_requested`` (see 0006_actions.sql:18). The audit-spec
-- name ``idx_force_sync_requests_queued_at`` was redundant and has been
-- intentionally omitted to avoid two B-trees on the same column. Callers
-- that ORDER BY requested_at are already covered by the existing index.

-- Audit-export filters by actor when an admin drills into "what did
-- user X do?". Previously the only indices were (action, timestamp) and
-- (target, timestamp), forcing a scan when filtering by actor alone.
CREATE INDEX IF NOT EXISTS idx_audit_log_actor
    ON audit_log (actor);
