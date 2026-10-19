-- Discriminator on ``sync_history`` so credential-refresh runs can share the
-- same table as content syncs (the V6 Credentials page's "Refresh activity"
-- tab reuses the V2 Syncs UI and filters by kind).
--
-- Default ``content_sync`` keeps every existing row — written before this
-- migration landed — in the correct bucket without a backfill pass.

ALTER TABLE sync_history ADD COLUMN sync_kind TEXT NOT NULL DEFAULT 'content_sync';

CREATE INDEX IF NOT EXISTS ix_sync_history_kind_time
    ON sync_history (sync_kind, started_at DESC);
