-- Phase 5 action plumbing. Two tables back the force-sync and
-- emergency-stop actions; the scheduler checks them every tick so a
-- control-panel mutation takes effect on the same cadence as any other
-- overlay flip.

-- Requests queued by /api/actions/users/{email}/{platform}/force-sync.
-- Scheduler consumes the row and submits the candidate immediately,
-- bypassing the interval check, then deletes the row. A requeue lands
-- as a separate row with a later ``requested_at``.
CREATE TABLE IF NOT EXISTS force_sync_requests (
    user_email TEXT NOT NULL,
    platform TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (user_email, platform)
);

CREATE INDEX IF NOT EXISTS ix_force_sync_requested
    ON force_sync_requests (requested_at);
