-- =============================================================================
-- Nimbus-Hackathon Relational Database Schema
-- DBMS: PostgreSQL 14+ (tested on PostgreSQL 18.6 on CachyOS / Arch Linux)
-- Entities: bookings, airline_tickets, payments, evidence_records, refund_cases
-- =============================================================================

-- Schema Migrations Tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(50) PRIMARY KEY,
    description TEXT,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Automatic Timestamp Update Function
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- Entity 1: bookings (Voyago Booking Platform)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bookings (
    booking_ref VARCHAR(50) PRIMARY KEY,
    customer_name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    airline_pnr VARCHAR(50) UNIQUE,
    flight_no VARCHAR(50) NOT NULL,
    origin VARCHAR(10) NOT NULL,
    destination VARCHAR(10) NOT NULL,
    flight_date VARCHAR(50) NOT NULL,
    amount NUMERIC(10, 2) NOT NULL CHECK (amount >= 0),
    currency VARCHAR(3) NOT NULL DEFAULT 'INR',
    payment_ref VARCHAR(50),
    status VARCHAR(50) NOT NULL CHECK (status IN ('CONFIRMED', 'CANCELLED', 'REFUND_INITIATED', 'REFUNDED', 'PENDING', 'FAILED')),
    refund_id VARCHAR(100),
    booked_at VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE TRIGGER trg_bookings_updated_at
BEFORE UPDATE ON bookings
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- Supporting Entity: airline_tickets (Meridian Air Microservice)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS airline_tickets (
    pnr VARCHAR(50) PRIMARY KEY,
    booking_ref VARCHAR(50) NOT NULL REFERENCES bookings(booking_ref) ON UPDATE CASCADE ON DELETE RESTRICT,
    passenger VARCHAR(255) NOT NULL,
    flight_no VARCHAR(50) NOT NULL,
    origin VARCHAR(10) NOT NULL,
    destination VARCHAR(10) NOT NULL,
    departure VARCHAR(50) NOT NULL,
    fare NUMERIC(10, 2) NOT NULL CHECK (fare >= 0),
    status VARCHAR(50) NOT NULL CHECK (status IN ('CONFIRMED', 'CHECKED_IN', 'CANCELLED', 'REFUNDED', 'ON_HOLD')),
    seat VARCHAR(20) DEFAULT '—',
    baggage_kg INTEGER NOT NULL DEFAULT 0 CHECK (baggage_kg >= 0),
    payment_ref VARCHAR(50),
    currency VARCHAR(3) NOT NULL DEFAULT 'INR',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE TRIGGER trg_airline_tickets_updated_at
BEFORE UPDATE ON airline_tickets
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- Entity 2: payments (Zenpay Payment Gateway)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    payment_ref VARCHAR(50) PRIMARY KEY,
    booking_ref VARCHAR(50) NOT NULL REFERENCES bookings(booking_ref) ON UPDATE CASCADE ON DELETE RESTRICT,
    airline_pnr VARCHAR(50),
    payer_name VARCHAR(255) NOT NULL,
    method VARCHAR(50) NOT NULL CHECK (method IN ('UPI', 'Credit Card', 'Debit Card', 'Net Banking')),
    amount NUMERIC(10, 2) NOT NULL CHECK (amount >= 0),
    currency VARCHAR(3) NOT NULL DEFAULT 'INR',
    status VARCHAR(50) NOT NULL CHECK (status IN ('SUCCESS', 'REFUNDED', 'FAILED', 'PENDING', 'PROCESSING', 'SETTLED')),
    paid_at VARCHAR(50) NOT NULL,
    refunded_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.00 CHECK (refunded_amount >= 0 AND refunded_amount <= amount),
    gateway_txn_id VARCHAR(100) UNIQUE,
    email VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE TRIGGER trg_payments_updated_at
BEFORE UPDATE ON payments
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- Entity 3: evidence_records (Reconciliation Engine Audit Trail)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_records (
    evidence_id BIGSERIAL PRIMARY KEY,
    booking_ref VARCHAR(50) REFERENCES bookings(booking_ref) ON UPDATE CASCADE ON DELETE SET NULL,
    payment_ref VARCHAR(50) REFERENCES payments(payment_ref) ON UPDATE CASCADE ON DELETE SET NULL,
    source_type VARCHAR(50) NOT NULL,
    external_reference VARCHAR(100),
    evidence_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- -----------------------------------------------------------------------------
-- Entity 4: refund_cases (Reconciliation Outcomes & Decisions)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refund_cases (
    refund_case_id BIGSERIAL PRIMARY KEY,
    case_identifier VARCHAR(50) UNIQUE NOT NULL,
    booking_ref VARCHAR(50) REFERENCES bookings(booking_ref) ON UPDATE CASCADE ON DELETE SET NULL,
    payment_ref VARCHAR(50) REFERENCES payments(payment_ref) ON UPDATE CASCADE ON DELETE SET NULL,
    case_status VARCHAR(50) NOT NULL CHECK (case_status IN ('COMPLETED', 'BLOCKED', 'PROCESSING_NORMALLY', 'CONFLICTING', 'UNRESOLVED', 'PENDING')),
    expected_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.00 CHECK (expected_amount >= 0),
    actual_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.00 CHECK (actual_amount >= 0),
    discrepancy_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    reason TEXT,
    reconciliation_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE TRIGGER trg_refund_cases_updated_at
BEFORE UPDATE ON refund_cases
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- Indexes for High Performance & Query Optimization
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);
CREATE INDEX IF NOT EXISTS idx_bookings_airline_pnr ON bookings(airline_pnr);
CREATE INDEX IF NOT EXISTS idx_bookings_payment_ref ON bookings(payment_ref);

CREATE INDEX IF NOT EXISTS idx_airline_tickets_booking_ref ON airline_tickets(booking_ref);
CREATE INDEX IF NOT EXISTS idx_airline_tickets_status ON airline_tickets(status);

CREATE INDEX IF NOT EXISTS idx_payments_booking_ref ON payments(booking_ref);
CREATE INDEX IF NOT EXISTS idx_payments_airline_pnr ON payments(airline_pnr);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_gateway_txn_id ON payments(gateway_txn_id);

CREATE INDEX IF NOT EXISTS idx_evidence_booking_ref ON evidence_records(booking_ref);
CREATE INDEX IF NOT EXISTS idx_evidence_payment_ref ON evidence_records(payment_ref);
CREATE INDEX IF NOT EXISTS idx_evidence_source_type ON evidence_records(source_type);
CREATE INDEX IF NOT EXISTS idx_evidence_external_reference ON evidence_records(external_reference);
CREATE INDEX IF NOT EXISTS idx_evidence_payload_gin ON evidence_records USING gin (payload);

CREATE INDEX IF NOT EXISTS idx_refund_cases_booking_ref ON refund_cases(booking_ref);
CREATE INDEX IF NOT EXISTS idx_refund_cases_payment_ref ON refund_cases(payment_ref);
CREATE INDEX IF NOT EXISTS idx_refund_cases_case_status ON refund_cases(case_status);
CREATE INDEX IF NOT EXISTS idx_refund_cases_case_identifier ON refund_cases(case_identifier);

-- -----------------------------------------------------------------------------
-- Entity 5: admins (Nimbus Administrative Users)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admins (
    admin_id BIGSERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_admins_email ON admins(email);

-- -----------------------------------------------------------------------------
-- Entity 6: admin_sessions (Server-Managed Session Store)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_sessions (
    session_id BIGSERIAL PRIMARY KEY,
    admin_id BIGINT NOT NULL REFERENCES admins(admin_id) ON DELETE CASCADE,
    token_hash VARCHAR(128) UNIQUE NOT NULL,
    csrf_token VARCHAR(128) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_admin_sessions_token_hash ON admin_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin_id ON admin_sessions(admin_id);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires_at ON admin_sessions(expires_at);

-- -----------------------------------------------------------------------------
-- Entity 7: users (Customer Users Directory)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id BIGSERIAL PRIMARY KEY,
    full_name VARCHAR(255),
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Link users to bookings via foreign key
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(user_id) ON UPDATE CASCADE ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_bookings_user_id ON bookings(user_id);

-- -----------------------------------------------------------------------------
-- Entity 8: email_logs (Audit Trail for Customer Email Communications)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_logs (
    email_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(user_id) ON UPDATE CASCADE ON DELETE SET NULL,
    refund_case_id BIGINT REFERENCES refund_cases(refund_case_id) ON UPDATE CASCADE ON DELETE SET NULL,
    recipient_email VARCHAR(255) NOT NULL,
    subject VARCHAR(255) NOT NULL,
    template_name VARCHAR(100) NOT NULL,
    delivery_status VARCHAR(50) NOT NULL,
    error_message TEXT,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_email_logs_refund_case_id ON email_logs(refund_case_id);
CREATE INDEX IF NOT EXISTS idx_email_logs_user_id ON email_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_email_logs_delivery_status ON email_logs(delivery_status);

-- Record initial schema migration version
INSERT INTO schema_migrations (version, description)
VALUES ('001_initial_schema', 'Initial 5-entity relational schema with constraints and indexes')
ON CONFLICT (version) DO NOTHING;

INSERT INTO schema_migrations (version, description)
VALUES ('002_admin_auth_and_email', 'Admin auth, customer users directory, and email logging tables')
ON CONFLICT (version) DO NOTHING;
