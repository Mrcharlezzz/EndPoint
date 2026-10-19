-- Phase 0 baseline: the config overlay is the only table a scaffolded daemon
-- needs to demonstrate a working SQLite round-trip. Later phases add
-- sync_cursors, sync_history, logs, audit, etc. in further migrations.

CREATE TABLE IF NOT EXISTS config_overlay (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
