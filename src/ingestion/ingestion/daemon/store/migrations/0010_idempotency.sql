-- Slice 5 part B: server-side idempotency cache for mutating HTTP requests.
--
-- The IdempotencyMiddleware reads an optional ``Idempotency-Key`` header
-- on POST / PUT / PATCH / DELETE requests, looks up the (key, method, path)
-- triple in this table, and either returns the cached response (replay)
-- or runs the handler and stores the response for the next retry.
--
-- The composite primary key (key, method, path) lets two different
-- endpoints reuse the same key value safely — e.g. a force-sync and a
-- pause both keyed by the same client-supplied UUID land on disjoint
-- rows. Without the composite, the second request would mistakenly
-- replay the first's response.
--
-- ``response_body`` is text (not blob) because every daemon response is
-- JSON or text/plain — both round-trip safely through TEXT. If a future
-- endpoint returns binary the column type can be promoted.
--
-- ``expires_at`` carries the wall-clock UTC ISO-8601 timestamp at which
-- the row stops being valid for replay. The TTL prune loop (see
-- observability_loops.run_idempotency_prune_loop) runs hourly and
-- deletes everything where ``expires_at < now``.

CREATE TABLE IF NOT EXISTS idempotency_log (
    key TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    -- Hash of the request body so a replay with the SAME key but a
    -- DIFFERENT body can be detected and rejected with 422 (per
    -- the slice 5 contract; the spec leaves this undefined and we
    -- pin the strict-conflict behaviour).
    body_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    -- Stored verbatim so the replay byte-matches the original. Empty
    -- body is allowed (some endpoints return 204 / empty 200).
    response_body TEXT NOT NULL,
    -- Outbound headers preserved as a JSON dict so the replay can
    -- carry the same Content-Type, Set-Cookie, etc. Stored as text
    -- to keep the schema simple.
    response_headers_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (key, method, path)
);

-- The TTL prune loop filters by ``expires_at < ?`` so an index here
-- keeps the prune from scanning the table.
CREATE INDEX IF NOT EXISTS idx_idempotency_expires_at ON idempotency_log(expires_at);
