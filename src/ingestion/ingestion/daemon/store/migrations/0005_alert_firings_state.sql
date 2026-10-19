-- Phase 4: extend alert_firings so the UI can distinguish active from
-- resolved firings. The handoff doc's AlertFiring entity expects state,
-- resolved_at, and trigger_context_json.

ALTER TABLE alert_firings ADD COLUMN state TEXT NOT NULL DEFAULT 'active';
ALTER TABLE alert_firings ADD COLUMN resolved_at TEXT;
ALTER TABLE alert_firings ADD COLUMN trigger_context_json TEXT;

CREATE INDEX IF NOT EXISTS ix_alert_firings_state ON alert_firings (state, fired_at DESC);
