-- ── Observability Dashboard — Phase 1: tables ────────────────────────────
-- Schema follows IACM_Dashboard_Architecture_Plan.docx §4.2 + §3.1.
--
-- Storage strategy (per plan §3.2):
--   metrics_requests       — raw per-request rows, kept 7 days, pruned daily.
--   metrics_requests_h     — rollup-by-hour, kept 90 days. Built from raw.
--   metrics_requests_d     — rollup-by-day,  kept 365 days. Built from hourly.
--   metrics_system         — CPU/RAM/Disk snapshots (every 60s).
--   metrics_db             — slow-query snapshots from pg_stat_statements.
--   alert_rules + alerts   — alert engine state (Phase 4 uses these).
--
-- Every table is keyed by `id` (uuid) so future inserts via supabase-py
-- don't depend on auto-increment behaviour. Multi-tenant separation is
-- implicit: requests carry user_id + (denormalised) company_id; the admin
-- dashboard intentionally crosses tenants.
--
-- Idempotent: IF NOT EXISTS on tables and indexes.

-- ── raw per-request log (~7-day retention) ───────────────────────────────
CREATE TABLE IF NOT EXISTS metrics_requests (
    id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    method          TEXT    NOT NULL,
    path            TEXT    NOT NULL,    -- normalised (route template, not raw URL)
    status          INT     NOT NULL,    -- HTTP status code
    duration_ms     INT     NOT NULL,    -- server-measured wall time
    user_id         UUID,
    company_id      UUID,
    role            TEXT,                -- snapshot at request time
    ip              INET,
    user_agent      TEXT,
    request_id      TEXT                 -- correlation id for logs
);
CREATE INDEX IF NOT EXISTS metrics_requests_ts_idx    ON metrics_requests (ts DESC);
CREATE INDEX IF NOT EXISTS metrics_requests_path_idx  ON metrics_requests (path, ts DESC);
CREATE INDEX IF NOT EXISTS metrics_requests_status_idx
    ON metrics_requests (status) WHERE status >= 400;

-- ── hourly rollup (~90-day retention) ────────────────────────────────────
-- Pre-computed so dashboard queries over weeks stay <100ms.
CREATE TABLE IF NOT EXISTS metrics_requests_h (
    hour            TIMESTAMPTZ NOT NULL,  -- truncated to the hour
    path            TEXT NOT NULL,
    method          TEXT NOT NULL,
    count           INT  NOT NULL,
    errors_4xx      INT  NOT NULL DEFAULT 0,
    errors_5xx      INT  NOT NULL DEFAULT 0,
    p50_ms          INT,
    p95_ms          INT,
    p99_ms          INT,
    avg_ms          INT,
    PRIMARY KEY (hour, path, method)
);
CREATE INDEX IF NOT EXISTS metrics_requests_h_hour_idx
    ON metrics_requests_h (hour DESC);

-- ── daily rollup (~365-day retention) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS metrics_requests_d (
    day             DATE NOT NULL,
    path            TEXT NOT NULL,
    method          TEXT NOT NULL,
    count           INT  NOT NULL,
    errors_4xx      INT  NOT NULL DEFAULT 0,
    errors_5xx      INT  NOT NULL DEFAULT 0,
    p95_ms          INT,
    avg_ms          INT,
    PRIMARY KEY (day, path, method)
);

-- ── system resource snapshots ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS metrics_system (
    id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    cpu_pct         NUMERIC(5,2),
    ram_pct         NUMERIC(5,2),
    ram_used_mb     INT,
    disk_pct        NUMERIC(5,2),
    disk_free_gb    NUMERIC(8,2),
    process_count   INT,
    uptime_sec      BIGINT
);
CREATE INDEX IF NOT EXISTS metrics_system_ts_idx ON metrics_system (ts DESC);

-- ── slow-query snapshots (from pg_stat_statements) ───────────────────────
CREATE TABLE IF NOT EXISTS metrics_db (
    id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    queryid         BIGINT,
    query           TEXT,
    calls           BIGINT,
    total_time_ms   NUMERIC,
    mean_time_ms    NUMERIC,
    rows            BIGINT,
    cache_hit_ratio NUMERIC(5,4)        -- 0..1
);
CREATE INDEX IF NOT EXISTS metrics_db_ts_idx ON metrics_db (ts DESC);

-- ── alert rules (Phase 4 wires the engine; the table lives here so the
-- schema is complete in one migration) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS alert_rules (
    id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT    NOT NULL UNIQUE,
    metric          TEXT    NOT NULL,        -- e.g. 'api.p95_ms', 'sys.cpu_pct'
    operator        TEXT    NOT NULL CHECK (operator IN ('>', '<', '>=', '<=', '=', '!=')),
    threshold       NUMERIC NOT NULL,
    duration_sec    INT     NOT NULL DEFAULT 60,   -- "must hold for ≥ N seconds"
    severity        TEXT    NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    channels        JSONB   NOT NULL DEFAULT '["websocket"]'::jsonb,  -- email/telegram/sms/...
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── alert events (firings) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         UUID    REFERENCES alert_rules(id) ON DELETE SET NULL,
    fired_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    severity        TEXT    NOT NULL,
    metric_value    NUMERIC,
    message         TEXT,
    status          TEXT    NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'acknowledged', 'snoozed', 'resolved')),
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by UUID,
    resolved_at     TIMESTAMPTZ,
    snoozed_until   TIMESTAMPTZ,
    context         JSONB                -- arbitrary detail (top offenders, samples)
);
CREATE INDEX IF NOT EXISTS alerts_fired_idx  ON alerts (fired_at DESC);
CREATE INDEX IF NOT EXISTS alerts_active_idx ON alerts (status) WHERE status = 'active';
