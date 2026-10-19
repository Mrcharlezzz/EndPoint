-- Phase 2: per-user schedule overrides and dead-letter suppression state.

CREATE TABLE IF NOT EXISTS user_schedule_overrides (
    user_email TEXT NOT NULL,
    platform TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'default',
    interval_seconds INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_email, platform, scope)
);

-- One row per (user, platform) tracks consecutive-failure counting. When a
-- sync succeeds the row resets to 0. When the counter hits the threshold the
-- ``suppressed_until`` timestamp is set; the scheduler skips this row until
-- ``now >= suppressed_until`` (or until the control panel calls
-- ``DeadLetterTracker.clear``).
CREATE TABLE IF NOT EXISTS dead_letter_state (
    user_email TEXT NOT NULL,
    platform TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_at TEXT,
    suppressed_until TEXT,
    PRIMARY KEY (user_email, platform)
);
