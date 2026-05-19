-- ─────────────────────────────────────────────────────────────────────
-- Sprint 4 — Safety Management System (ICAO Annex 19)
--
-- Three tables that together let any employee file a safety report,
-- compliance/ops investigate and assess risk, then admin close it.
--
-- A safety report is intentionally low-friction: any logged-in user can
-- file one with just title + type. Everything else (risk score, actions,
-- closure) lives on related rows so the original report stays raw and
-- traceable for the auditor.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS safety_reports (
    id              TEXT PRIMARY KEY,
    company_id      TEXT REFERENCES companies(id) ON DELETE CASCADE,
    reporter_id     TEXT REFERENCES users(id),       -- nullable for confidential reports
    is_anonymous    BOOLEAN NOT NULL DEFAULT FALSE,  -- ICAO encourages just-culture / anon

    -- What happened
    report_type     TEXT NOT NULL DEFAULT 'occurrence',  -- 'incident' | 'hazard' | 'occurrence' | 'observation'
    title           TEXT NOT NULL,
    description     TEXT,
    location        TEXT,                  -- e.g. 'BGW', 'YI-AQY cockpit', 'crew rest'
    occurred_at     TIMESTAMPTZ,

    -- Optional links
    flight_id       TEXT REFERENCES flights(id) ON DELETE SET NULL,
    aircraft_id     TEXT REFERENCES aircraft(id) ON DELETE SET NULL,

    -- Triage / status
    status          TEXT NOT NULL DEFAULT 'open',  -- open | under_review | closed | rejected
    severity        TEXT,                  -- minor | major | critical (initial guess by reporter)
    immediate_action TEXT,                 -- what the reporter already did about it

    -- Lifecycle stamps
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by     TEXT REFERENCES users(id),
    reviewed_at     TIMESTAMPTZ,
    closed_by       TEXT REFERENCES users(id),
    closed_at       TIMESTAMPTZ,
    closure_notes   TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_safety_reports_company  ON safety_reports(company_id);
CREATE INDEX IF NOT EXISTS idx_safety_reports_status   ON safety_reports(status);
CREATE INDEX IF NOT EXISTS idx_safety_reports_reporter ON safety_reports(reporter_id);
CREATE INDEX IF NOT EXISTS idx_safety_reports_type     ON safety_reports(report_type);


-- ICAO 5×5 risk matrix:
--   likelihood: A=Frequent, B=Occasional, C=Remote, D=Improbable, E=Extremely improbable
--   severity:   1=Catastrophic, 2=Hazardous, 3=Major, 4=Minor, 5=Negligible
-- risk_score = simple cell index ('A1' .. 'E5'); the UI colours it.
CREATE TABLE IF NOT EXISTS risk_assessments (
    id              TEXT PRIMARY KEY,
    report_id       TEXT NOT NULL REFERENCES safety_reports(id) ON DELETE CASCADE,
    likelihood      TEXT NOT NULL,         -- 'A'..'E'
    severity        TEXT NOT NULL,         -- '1'..'5'
    risk_score      TEXT NOT NULL,         -- e.g. 'B2' — combo for the matrix
    rationale       TEXT,
    assessed_by     TEXT REFERENCES users(id),
    assessed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_risk_report ON risk_assessments(report_id);


-- Corrective / preventive / mitigating actions tied to a report.
CREATE TABLE IF NOT EXISTS safety_actions (
    id              TEXT PRIMARY KEY,
    report_id       TEXT NOT NULL REFERENCES safety_reports(id) ON DELETE CASCADE,
    action_type     TEXT NOT NULL DEFAULT 'corrective',  -- corrective | preventive | mitigating
    description     TEXT NOT NULL,
    assigned_to     TEXT REFERENCES users(id),
    due_date        DATE,
    status          TEXT NOT NULL DEFAULT 'open',         -- open | in_progress | done | cancelled
    completed_at    TIMESTAMPTZ,
    completed_by    TEXT REFERENCES users(id),
    notes           TEXT,
    created_by      TEXT REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_actions_report ON safety_actions(report_id);
CREATE INDEX IF NOT EXISTS idx_actions_status ON safety_actions(status);


-- RLS — service-role only. FastAPI gates row visibility (crew sees own,
-- safety/admin see all).
ALTER TABLE safety_reports     ENABLE ROW LEVEL SECURITY;
ALTER TABLE risk_assessments   ENABLE ROW LEVEL SECURITY;
ALTER TABLE safety_actions     ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON safety_reports   FROM anon;
REVOKE ALL ON risk_assessments FROM anon;
REVOKE ALL ON safety_actions   FROM anon;


-- Reuse the touch trigger from payroll
DROP TRIGGER IF EXISTS safety_reports_touch ON safety_reports;
CREATE TRIGGER safety_reports_touch BEFORE UPDATE ON safety_reports
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
DROP TRIGGER IF EXISTS safety_actions_touch ON safety_actions;
CREATE TRIGGER safety_actions_touch BEFORE UPDATE ON safety_actions
    FOR EACH ROW EXECUTE FUNCTION payroll_touch_updated_at();
