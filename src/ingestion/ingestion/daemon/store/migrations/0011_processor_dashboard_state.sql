CREATE TABLE IF NOT EXISTS processor_dashboard_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    baseline_at TEXT,
    updated_at TEXT NOT NULL
);
