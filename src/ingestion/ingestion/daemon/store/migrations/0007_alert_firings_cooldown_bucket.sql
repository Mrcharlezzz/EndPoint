-- Phase 4 fix: close the cross-process cooldown race on alert firings.
--
-- The engine's cooldown check and firing insert run in two separate
-- aiosqlite connections. Two overlapping ticks (or a second daemon process
-- pointed at the same SQLite file) could both pass the check and insert
-- duplicate firings for the same breach. An in-process asyncio.Lock only
-- serialises one engine instance.
--
-- The bucket is ``floor(fired_at_epoch / cooldown_seconds)``. Every firing
-- computed against the same rule and cooldown window lands in the same
-- bucket, and the unique partial index rejects the second insert at the
-- SQLite layer. The engine catches the integrity error and treats it as
-- "already fired, skip", so the dedup is enforced regardless of how many
-- engine ticks, processes, or API-triggered insertions race.
--
-- Existing rows are preserved: ``cooldown_bucket`` is nullable, and the
-- partial index only applies where the bucket is NOT NULL. Historical
-- firings keep their row and never match the unique index.

ALTER TABLE alert_firings ADD COLUMN cooldown_bucket INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_firings_rule_bucket
    ON alert_firings (rule_id, cooldown_bucket)
    WHERE cooldown_bucket IS NOT NULL;
