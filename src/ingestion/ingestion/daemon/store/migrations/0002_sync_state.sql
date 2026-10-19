-- Phase 1: per-user cursors and sync run history. sync_history is also the
-- data source for the future sync-history UI (Phase 5+).

CREATE TABLE IF NOT EXISTS sync_cursors (
    user_email TEXT NOT NULL,
    platform TEXT NOT NULL,
    scope TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_email, platform, scope)
);

-- ``cursor_before`` and ``cursor_after`` stay NULL in Phase 1 because the
-- SyncLogger Protocol doesn't carry cursor snapshots today. Kept in the
-- schema so the sync-history UI and debugging tools can read them once the
-- Protocol grows a cursor argument in a later phase.
CREATE TABLE IF NOT EXISTS sync_history (
    id TEXT PRIMARY KEY,
    user_email TEXT NOT NULL,
    platform TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL,
    stats_json TEXT,
    error TEXT,
    cursor_before TEXT,
    cursor_after TEXT
);

CREATE INDEX IF NOT EXISTS ix_sync_history_user_time
    ON sync_history (user_email, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_sync_history_state
    ON sync_history (state, started_at DESC);
