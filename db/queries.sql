-- ==============================================================================
-- Nimbus-Hackathon: Demonstrative Analytical SQL Queries
-- Demonstrating JOINs, Aggregations, Cardinalities, JSON Operations, and Auditing
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Multi-Table Cross-Entity JOIN
-- Demonstrates relationships across bookings, airline_tickets, payments, and refund_cases
-- ------------------------------------------------------------------------------
SELECT 
    b.booking_ref,
    b.customer_name,
    b.flight_no,
    b.status AS booking_status,
    b.amount AS booking_amount,
    t.pnr AS airline_pnr,
    t.status AS airline_status,
    p.payment_ref,
    p.status AS payment_status,
    p.amount AS payment_amount,
    rc.case_identifier,
    rc.case_status,
    rc.review_required
FROM bookings b
LEFT JOIN airline_tickets t ON b.booking_ref = t.booking_ref
LEFT JOIN payments p ON b.booking_ref = p.booking_ref
LEFT JOIN refund_cases rc ON b.booking_ref = rc.booking_ref
ORDER BY b.booked_at DESC;

-- ------------------------------------------------------------------------------
-- 2. Financial Aggregation & Reconciliation Metrics
-- Aggregates total booked value, successful captured payments, and settled refunds
-- grouped by booking status.
-- ------------------------------------------------------------------------------
SELECT 
    b.status AS booking_status,
    COUNT(DISTINCT b.booking_ref) AS total_bookings,
    SUM(b.amount) AS total_booking_value,
    COALESCE(SUM(p.amount) FILTER (WHERE p.status = 'SUCCESS'), 0.00) AS total_paid_success,
    COALESCE(SUM(p.refunded_amount), 0.00) AS total_refunded
FROM bookings b
LEFT JOIN payments p ON b.booking_ref = p.booking_ref
GROUP BY b.status
ORDER BY total_booking_value DESC;

-- ------------------------------------------------------------------------------
-- 3. 1:N Relationship & Payment Multiplicity Detection
-- Identifies bookings with multiple transactions or refund settlements
-- ------------------------------------------------------------------------------
SELECT 
    b.booking_ref,
    b.customer_name,
    b.amount AS booking_total,
    COUNT(p.payment_ref) AS transaction_count,
    STRING_AGG(p.payment_ref || ' (' || p.status || ': ' || p.amount || ')', ', ') AS transactions
FROM bookings b
JOIN payments p ON b.booking_ref = p.booking_ref
GROUP BY b.booking_ref, b.customer_name, b.amount
HAVING COUNT(p.payment_ref) >= 1;

-- ------------------------------------------------------------------------------
-- 4. Evidence Trail Audit per Booking (JSON Aggregation)
-- Aggregates evidence records into a chronological JSON audit trail per booking
-- ------------------------------------------------------------------------------
SELECT 
    b.booking_ref,
    b.customer_name,
    COUNT(e.evidence_id) AS evidence_event_count,
    jsonb_agg(
        jsonb_build_object(
            'source_type', e.source_type,
            'external_ref', e.external_reference,
            'evidence_type', e.evidence_type,
            'received_at', e.received_at,
            'payload', e.payload
        ) ORDER BY e.received_at ASC
    ) AS evidence_timeline
FROM bookings b
JOIN evidence_records e ON b.booking_ref = e.booking_ref
GROUP BY b.booking_ref, b.customer_name;

-- ------------------------------------------------------------------------------
-- 5. Operational Queue: Cases Requiring Human / Bank Review
-- Lists blocked, conflicting, or review-required refund cases
-- ------------------------------------------------------------------------------
SELECT 
    rc.case_identifier,
    rc.booking_ref,
    rc.case_status,
    rc.expected_amount,
    rc.actual_amount,
    rc.discrepancy_amount,
    rc.reason,
    rc.reconciliation_result->'blocker'->>'organization' AS blocker_org,
    rc.reconciliation_result->'next_owner'->>'organization' AS next_owner_org,
    rc.review_required
FROM refund_cases rc
WHERE rc.review_required = TRUE OR rc.case_status IN ('BLOCKED', 'CONFLICTING', 'UNRESOLVED')
ORDER BY rc.created_at DESC;

-- ------------------------------------------------------------------------------
-- 6. Discrepancy & Mismatch Detection
-- Finds mismatches between booking fare and payment transaction amounts
-- ------------------------------------------------------------------------------
SELECT 
    b.booking_ref,
    b.amount AS booking_amount,
    p.payment_ref,
    p.amount AS payment_amount,
    p.status AS payment_status,
    (b.amount - COALESCE(p.amount, 0)) AS amount_variance
FROM bookings b
LEFT JOIN payments p ON b.booking_ref = p.booking_ref
WHERE p.amount IS NOT NULL AND b.amount != p.amount;
