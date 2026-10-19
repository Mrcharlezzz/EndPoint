-- Phase 4 observability: per-sync profiles, structured log persistence,
-- alert rules/firings, and the append-only audit log.

-- Aggregate phase timings for each completed sync. One row per sync_id.
-- Detailed per-event traces belong in the logs table and are filterable
-- by the same sync_id.
CREATE TABLE IF NOT EXISTS sync_profiles (
    sync_id TEXT PRIMARY KEY,
    user_email TEXT NOT NULL,
    platform TEXT NOT NULL,
    total_duration_seconds REAL,
    api_call_duration_seconds REAL,
    normalization_duration_seconds REAL,
    items_fetched INTEGER,
    items_new INTEGER,
    items_updated INTEGER,
    error_count INTEGER,
    per_scope_json TEXT,
    created_at TEXT NOT NULL
);

-- Every structlog record persisted. Writes via
-- :class:`SqliteLogHandler`. Retention is applied by a background job
-- (:func:`prune_logs_older_than`).
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    user_email TEXT,
    platform TEXT,
    sync_id TEXT,
    fields_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_logs_timestamp_level ON logs (timestamp DESC, level);
CREATE INDEX IF NOT EXISTS ix_logs_user_timestamp ON logs (user_email, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_logs_sync ON logs (sync_id);

-- Rule definitions for the alert engine. Rules can scope to a specific
-- user or platform (NULL == global).
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    metric TEXT NOT NULL,
    comparator TEXT NOT NULL,
    threshold REAL NOT NULL,
    window_seconds INTEGER,
    cooldown_seconds INTEGER NOT NULL DEFAULT 300,
    user_email TEXT,
    platform TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_firings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    fired_at TEXT NOT NULL,
    metric_value REAL NOT NULL,
    user_email TEXT,
    platform TEXT,
    FOREIGN KEY (rule_id) REFERENCES alert_rules(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_alert_firings_rule ON alert_firings (rule_id, fired_at DESC);

-- Append-only audit log. Every control-panel mutation writes an entry.
-- ``before_json`` and ``after_json`` are optional snapshots of the affected
-- record so UIs can diff.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    target TEXT,
    before_json TEXT,
    after_json TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_audit_action_time ON audit_log (action, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_audit_target_time ON audit_log (target, timestamp DESC);
