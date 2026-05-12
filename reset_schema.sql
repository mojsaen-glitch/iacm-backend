-- IACM Database Schema Reset
-- Run this in Supabase Dashboard → SQL Editor
-- This drops and recreates all tables with the correct schema

-- Drop in reverse dependency order
DROP TABLE IF EXISTS assignments CASCADE;
DROP TABLE IF EXISTS notifications CASCADE;
DROP TABLE IF EXISTS messages CASCADE;
DROP TABLE IF EXISTS audit_log CASCADE;
DROP TABLE IF EXISTS "LeaveRequests" CASCADE;
DROP TABLE IF EXISTS leave_requests CASCADE;
DROP TABLE IF EXISTS documents CASCADE;
DROP TABLE IF EXISTS flights CASCADE;
DROP TABLE IF EXISTS crew CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS "Aircraft" CASCADE;
DROP TABLE IF EXISTS aircraft CASCADE;
DROP TABLE IF EXISTS "Routes" CASCADE;
DROP TABLE IF EXISTS routes CASCADE;
DROP TABLE IF EXISTS settings CASCADE;
DROP TABLE IF EXISTS companies CASCADE;

-- Companies
CREATE TABLE companies (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name_ar TEXT NOT NULL,
    name_en TEXT NOT NULL,
    code TEXT UNIQUE NOT NULL,
    icao_code TEXT,
    iata_code TEXT,
    country TEXT,
    primary_color TEXT,
    secondary_color TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    logo_path TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Users
CREATE TABLE users (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    email TEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    name_ar TEXT NOT NULL,
    name_en TEXT NOT NULL,
    role TEXT NOT NULL,
    company_id TEXT NOT NULL REFERENCES companies(id),
    crew_id TEXT,
    phone TEXT,
    avatar_path TEXT,
    is_active BOOLEAN DEFAULT true,
    is_superuser BOOLEAN DEFAULT false,
    last_login TIMESTAMPTZ,
    refresh_token TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Crew
CREATE TABLE crew (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    employee_id TEXT UNIQUE NOT NULL,
    full_name_ar TEXT NOT NULL,
    full_name_en TEXT NOT NULL,
    nickname TEXT,
    company_id TEXT NOT NULL REFERENCES companies(id),
    base TEXT NOT NULL,
    rank TEXT NOT NULL,
    operation_type TEXT DEFAULT 'short_haul',
    contract_type TEXT DEFAULT 'full_time',
    aircraft_qualifications TEXT,
    languages TEXT,
    status TEXT DEFAULT 'active',
    block_reason TEXT,
    blocked_by TEXT,
    blocked_on TIMESTAMPTZ,
    nationality TEXT,
    date_of_birth DATE,
    gender TEXT,
    join_date DATE,
    photo_path TEXT,
    email TEXT,
    phone TEXT,
    monthly_flight_hours FLOAT DEFAULT 0,
    yearly_flight_hours FLOAT DEFAULT 0,
    total_flight_hours FLOAT DEFAULT 0,
    last_28day_hours FLOAT DEFAULT 0,
    last_flight_date TIMESTAMPTZ,
    last_landing_time TIMESTAMPTZ,
    rest_hours_due FLOAT DEFAULT 0,
    available_from TIMESTAMPTZ,
    max_monthly_hours FLOAT DEFAULT 100,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Documents
CREATE TABLE documents (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    crew_id TEXT NOT NULL REFERENCES crew(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL,
    document_number TEXT,
    issue_date DATE,
    expiry_date DATE,
    issued_by TEXT,
    file_path TEXT,
    is_verified BOOLEAN DEFAULT false,
    verified_by TEXT,
    verified_at TIMESTAMPTZ,
    notes TEXT,
    last_reminder_sent TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Aircraft
CREATE TABLE aircraft (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    company_id TEXT NOT NULL REFERENCES companies(id),
    aircraft_type TEXT NOT NULL,
    registration TEXT UNIQUE NOT NULL,
    name TEXT,
    manufacturer TEXT,
    min_crew INT DEFAULT 2,
    max_crew INT DEFAULT 10,
    capacity INT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Routes
CREATE TABLE routes (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    company_id TEXT NOT NULL REFERENCES companies(id),
    origin_code TEXT NOT NULL,
    destination_code TEXT NOT NULL,
    flight_duration_hours FLOAT NOT NULL,
    is_international BOOLEAN DEFAULT false,
    required_rest_hours FLOAT DEFAULT 10,
    min_crew INT DEFAULT 2,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Flights
CREATE TABLE flights (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    flight_number TEXT NOT NULL,
    company_id TEXT NOT NULL REFERENCES companies(id),
    aircraft_id TEXT REFERENCES aircraft(id),
    origin_code TEXT NOT NULL,
    destination_code TEXT NOT NULL,
    departure_time TIMESTAMPTZ NOT NULL,
    arrival_time TIMESTAMPTZ NOT NULL,
    duration_hours FLOAT NOT NULL,
    crew_required INT DEFAULT 4,
    status TEXT DEFAULT 'scheduled',
    publish_status TEXT DEFAULT 'draft',
    delay_minutes INT DEFAULT 0,
    delay_reason TEXT,
    gate TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Assignments
CREATE TABLE assignments (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    flight_id TEXT NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    crew_id TEXT NOT NULL REFERENCES crew(id),
    assigned_by TEXT NOT NULL REFERENCES users(id),
    assignment_type TEXT DEFAULT 'regular',
    acknowledged BOOLEAN DEFAULT false,
    acknowledged_at TIMESTAMPTZ,
    is_override BOOLEAN DEFAULT false,
    override_reason TEXT,
    UNIQUE(flight_id, crew_id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Notifications
CREATE TABLE notifications (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    title_ar TEXT NOT NULL,
    title_en TEXT NOT NULL,
    body_ar TEXT,
    body_en TEXT,
    type TEXT NOT NULL,
    target_user_id TEXT REFERENCES users(id),
    company_id TEXT REFERENCES companies(id),
    related_flight_id TEXT REFERENCES flights(id),
    related_crew_id TEXT REFERENCES crew(id),
    is_read BOOLEAN DEFAULT false,
    read_at TIMESTAMPTZ,
    requires_acknowledge BOOLEAN DEFAULT false,
    is_acknowledged BOOLEAN DEFAULT false,
    acknowledged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Messages
CREATE TABLE messages (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    sender_id TEXT NOT NULL REFERENCES users(id),
    receiver_id TEXT NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,
    linked_flight_id TEXT REFERENCES flights(id),
    attachment_path TEXT,
    is_read BOOLEAN DEFAULT false,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Audit Log
CREATE TABLE audit_log (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    user_id TEXT REFERENCES users(id),
    user_name TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    before_data TEXT,
    after_data TEXT,
    ip_address TEXT,
    device_info TEXT,
    is_override BOOLEAN DEFAULT false,
    override_reason TEXT,
    company_id TEXT REFERENCES companies(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Leave Requests
CREATE TABLE leave_requests (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    crew_id TEXT NOT NULL REFERENCES crew(id),
    leave_type TEXT NOT NULL,
    from_date DATE NOT NULL,
    to_date DATE NOT NULL,
    reason TEXT,
    status TEXT DEFAULT 'pending',
    approved_by TEXT REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    rejection_reason TEXT,
    attachment_path TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Settings
CREATE TABLE settings (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    key TEXT NOT NULL,
    value TEXT,
    company_id TEXT NOT NULL REFERENCES companies(id),
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(key, company_id)
);

-- Indexes
CREATE INDEX idx_crew_company      ON crew(company_id);
CREATE INDEX idx_crew_status       ON crew(status);
CREATE INDEX idx_flights_company   ON flights(company_id);
CREATE INDEX idx_flights_departure ON flights(departure_time);
CREATE INDEX idx_assignments_flight ON assignments(flight_id);
CREATE INDEX idx_assignments_crew  ON assignments(crew_id);
CREATE INDEX idx_documents_crew    ON documents(crew_id);
CREATE INDEX idx_notifications_user ON notifications(target_user_id);
CREATE INDEX idx_users_email       ON users(email);

SELECT 'Schema reset complete!' AS result;
