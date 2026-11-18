-- Add lifecycle states used by the cross-service booking workflow.
-- Existing states and records remain valid.
ALTER TABLE airline_tickets
    DROP CONSTRAINT IF EXISTS airline_tickets_status_check;
ALTER TABLE airline_tickets
    ADD CONSTRAINT airline_tickets_status_check
    CHECK (status IN ('PENDING', 'AUTHORIZED', 'CONFIRMED', 'REJECTED', 'CHECKED_IN', 'CANCELLED', 'REFUNDED', 'ON_HOLD'));

ALTER TABLE payments
    DROP CONSTRAINT IF EXISTS payments_status_check;
ALTER TABLE payments
    ADD CONSTRAINT payments_status_check
    CHECK (status IN ('PENDING', 'AUTHORIZED', 'CONFIRMED', 'REJECTED', 'SUCCESS', 'REFUNDED', 'FAILED', 'PROCESSING', 'SETTLED'));

ALTER TABLE payments ADD COLUMN IF NOT EXISTS authorized_at TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS decision_at TIMESTAMPTZ;

INSERT INTO schema_migrations (version, description)
VALUES ('004_booking_payment_lifecycle', 'Add airline/payment decision states and payment decision timestamps')
ON CONFLICT (version) DO NOTHING;
