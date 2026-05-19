-- ─────────────────────────────────────────────────────────────────────
-- Sprint 2 — Payroll engine
--
-- Three tables that together let an admin:
--   1. Configure wage rates per rank (captain / FO / FA / engineer / ...)
--   2. Generate monthly payslips per crew member from flight hours
--   3. Lock months once paid so historical figures don't drift
--
-- All money fields are stored as NUMERIC(14,2) — never use floats for
-- currency in Postgres. NUMERIC keeps fractional dinars precise.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS wage_rates (
    id                          TEXT PRIMARY KEY,
    company_id                  TEXT REFERENCES companies(id) ON DELETE CASCADE,
    rank                        TEXT NOT NULL,            -- 'captain', 'fo', 'flight_attendant', 'engineer', ...
    currency                    TEXT NOT NULL DEFAULT 'IQD',
    base_monthly_salary         NUMERIC(14,2) NOT NULL DEFAULT 0,
    position_allowance_monthly  NUMERIC(14,2) NOT NULL DEFAULT 0,
    hour_rate                   NUMERIC(14,2) NOT NULL DEFAULT 0,   -- per flight hour
    international_hour_bonus    NUMERIC(14,2) NOT NULL DEFAULT 0,   -- extra per int'l hour
    night_hour_bonus            NUMERIC(14,2) NOT NULL DEFAULT 0,   -- extra per night hour (22:00-06:00)
    per_diem_domestic           NUMERIC(14,2) NOT NULL DEFAULT 0,   -- per overnight stay
    per_diem_international      NUMERIC(14,2) NOT NULL DEFAULT 0,
    notes                       TEXT,
    is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
    created_by                  TEXT REFERENCES users(id),
    updated_by                  TEXT REFERENCES users(id),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_wage_rates_company ON wage_rates(company_id);


-- A payslip is the snapshot of one crew member's compensation for one
-- calendar month. Once `finalized = TRUE` the row is read-only — re-runs
-- of the generator skip finalized rows (so accountants who already paid
-- can't have their figures rewritten).
CREATE TABLE IF NOT EXISTS payslips (
    id                          TEXT PRIMARY KEY,
    company_id                  TEXT REFERENCES companies(id) ON DELETE CASCADE,
    crew_id                     TEXT REFERENCES crew(id) ON DELETE CASCADE,
    month                       TEXT NOT NULL,             -- 'YYYY-MM'
    currency                    TEXT NOT NULL DEFAULT 'IQD',

    -- Inputs (from assignments + flights)
    total_flight_hours          NUMERIC(10,2) NOT NULL DEFAULT 0,
    domestic_hours              NUMERIC(10,2) NOT NULL DEFAULT 0,
    international_hours         NUMERIC(10,2) NOT NULL DEFAULT 0,
    night_hours                 NUMERIC(10,2) NOT NULL DEFAULT 0,
    sectors_flown               INT           NOT NULL DEFAULT 0,
    days_per_diem_domestic      INT           NOT NULL DEFAULT 0,
    days_per_diem_international INT           NOT NULL DEFAULT 0,

    -- Components
    base_salary                 NUMERIC(14,2) NOT NULL DEFAULT 0,
    position_allowance          NUMERIC(14,2) NOT NULL DEFAULT 0,
    hourly_pay                  NUMERIC(14,2) NOT NULL DEFAULT 0,
    international_bonus         NUMERIC(14,2) NOT NULL DEFAULT 0,
    night_bonus                 NUMERIC(14,2) NOT NULL DEFAULT 0,
    per_diem_total              NUMERIC(14,2) NOT NULL DEFAULT 0,
    other_additions             NUMERIC(14,2) NOT NULL DEFAULT 0,
    deductions                  NUMERIC(14,2) NOT NULL DEFAULT 0,
    tax                         NUMERIC(14,2) NOT NULL DEFAULT 0,

    gross_total                 NUMERIC(14,2) NOT NULL DEFAULT 0,
    net_total                   NUMERIC(14,2) NOT NULL DEFAULT 0,

    notes                       TEXT,
    finalized                   BOOLEAN NOT NULL DEFAULT FALSE,
    finalized_at                TIMESTAMPTZ,
    finalized_by                TEXT REFERENCES users(id),
    generated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (company_id, crew_id, month)
);

CREATE INDEX IF NOT EXISTS idx_payslips_company_month ON payslips(company_id, month);
CREATE INDEX IF NOT EXISTS idx_payslips_crew_month    ON payslips(crew_id, month);


-- Optional: track the lifecycle of a whole month (open / generated / paid).
-- The generator refuses to run on a 'paid' period to protect history.
CREATE TABLE IF NOT EXISTS payroll_periods (
    id            TEXT PRIMARY KEY,
    company_id    TEXT REFERENCES companies(id) ON DELETE CASCADE,
    month         TEXT NOT NULL,                            -- 'YYYY-MM'
    status        TEXT NOT NULL DEFAULT 'open',             -- 'open' | 'generated' | 'paid'
    generated_at  TIMESTAMPTZ,
    paid_at       TIMESTAMPTZ,
    paid_by       TEXT REFERENCES users(id),
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, month)
);


-- RLS — block direct anon access; reads/writes go through FastAPI which
-- already enforces role checks.
ALTER TABLE wage_rates       ENABLE ROW LEVEL SECURITY;
ALTER TABLE payslips         ENABLE ROW LEVEL SECURITY;
ALTER TABLE payroll_periods  ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wage_rates      FROM anon;
REVOKE ALL ON payslips        FROM anon;
REVOKE ALL ON payroll_periods FROM anon;


-- Keep updated_at fresh on PATCH (re-using the function we created for
-- om_articles is tempting but safer to declare a dedicated one — single
-- responsibility, no surprise dependencies).
CREATE OR REPLACE FUNCTION payroll_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS wage_rates_touch ON wage_rates;
CREATE TRIGGER wage_rates_touch
    BEFORE UPDATE ON wage_rates
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();

DROP TRIGGER IF EXISTS payslips_touch ON payslips;
CREATE TRIGGER payslips_touch
    BEFORE UPDATE ON payslips
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();

DROP TRIGGER IF EXISTS payroll_periods_touch ON payroll_periods;
CREATE TRIGGER payroll_periods_touch
    BEFORE UPDATE ON payroll_periods
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
