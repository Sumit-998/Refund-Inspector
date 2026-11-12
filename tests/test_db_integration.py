"""
tests/test_db_integration.py
PostgreSQL integration tests for Nimbus-Hackathon.

Uses an isolated test database (nimbus_test_db).
Never modifies or truncates the development database.

Run with:
    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/nimbus_test_db \
    PYTHONPATH=. .venv/bin/pytest tests/test_db_integration.py -v
"""

import os
import sys
import json
import uuid
import pytest
from decimal import Decimal

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Always use the test database for these tests
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres@localhost:5432/nimbus_test_db"
)

from db.connection import (
    get_db_cursor,
    check_db_health,
    apply_schema,
    execute_query,
    get_database_url,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def prepare_test_db():
    """Apply schema to test DB once per session."""
    healthy, msg = check_db_health(TEST_DB_URL)
    assert healthy, f"Test DB not reachable: {msg}"
    apply_schema(TEST_DB_URL)
    yield
    # Cleanup: remove test data after all tests in the session
    with get_db_cursor(TEST_DB_URL) as (cur, _):
        cur.execute("DELETE FROM refund_cases WHERE case_identifier LIKE 'TEST-%'")
        cur.execute("DELETE FROM evidence_records WHERE source_type = 'test'")
        cur.execute("DELETE FROM payments WHERE payment_ref LIKE 'TEST-PAY-%'")
        cur.execute("DELETE FROM airline_tickets WHERE pnr LIKE 'TEST-MR%'")
        cur.execute("DELETE FROM bookings WHERE booking_ref LIKE 'TEST-BK-%'")


def _uid() -> str:
    """Short unique suffix for test identifiers."""
    return uuid.uuid4().hex[:8].upper()


# ---------------------------------------------------------------------------
# 1. Database connection tests
# ---------------------------------------------------------------------------

class TestDatabaseConnection:

    def test_valid_connection_succeeds(self):
        """Database connection succeeds with valid configuration."""
        healthy, msg = check_db_health(TEST_DB_URL)
        assert healthy, f"Expected healthy connection, got: {msg}"
        assert "PostgreSQL" in msg

    def test_invalid_connection_fails_clearly(self):
        """Invalid connection string produces clear error, not silent fallback."""
        healthy, msg = check_db_health("postgresql://postgres@localhost:9999/nonexistent_db")
        assert not healthy
        assert len(msg) > 0

    def test_missing_url_uses_env_default(self):
        """get_database_url() returns env var when set."""
        os.environ["DATABASE_URL"] = "postgresql://postgres@localhost:5432/nimbus_db"
        url = get_database_url()
        assert "nimbus_db" in url


# ---------------------------------------------------------------------------
# 2. Booking CRUD tests
# ---------------------------------------------------------------------------

class TestBookings:

    def _insert_booking(self, bref=None, status="CONFIRMED", amount="5000.00"):
        bref = bref or f"TEST-BK-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO bookings (booking_ref, customer_name, email, flight_no,
                    origin, destination, flight_date, amount, status, booked_at)
                VALUES (%s, 'Test User', 'test@example.com', 'MR001',
                    'CCU', 'DEL', '2026-11-01', %s, %s, '2026-10-31 10:00')
            """, (bref, amount, status))
        return bref

    def test_booking_creation_persists(self):
        """Booking creation and retrieval work correctly."""
        bref = self._insert_booking()
        rows = execute_query(
            "SELECT booking_ref, status, amount FROM bookings WHERE booking_ref = %s",
            (bref,), fetch="all", database_url=TEST_DB_URL, commit=False
        )
        assert len(rows) == 1
        assert rows[0]["booking_ref"] == bref
        assert rows[0]["status"] == "CONFIRMED"

    def test_booking_update_persists(self):
        """Booking status updates persist and are reflected in subsequent reads."""
        bref = self._insert_booking()
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute(
                "UPDATE bookings SET status = %s WHERE booking_ref = %s",
                ("CANCELLED", bref)
            )
        row = execute_query(
            "SELECT status FROM bookings WHERE booking_ref = %s",
            (bref,), fetch="one", database_url=TEST_DB_URL, commit=False
        )
        assert row["status"] == "CANCELLED"

    def test_duplicate_booking_ref_rejected(self):
        """Duplicate primary key on bookings is rejected by DB."""
        bref = self._insert_booking()
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO bookings (booking_ref, customer_name, flight_no,
                        origin, destination, flight_date, amount, status, booked_at)
                    VALUES (%s, 'Dup', 'MR002', 'CCU', 'DEL', '2026-11-01', 100, 'CONFIRMED', '2026-10-31 10:00')
                """, (bref,))

    def test_invalid_booking_status_rejected(self):
        """CHECK constraint rejects invalid status values."""
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO bookings (booking_ref, customer_name, flight_no,
                        origin, destination, flight_date, amount, status, booked_at)
                    VALUES (%s, 'Bad', 'MR003', 'CCU', 'DEL', '2026-11-01', 100, 'BOGUS_STATUS', '2026-10-31 10:00')
                """, (f"TEST-BK-{_uid()}",))

    def test_negative_amount_rejected(self):
        """CHECK constraint rejects negative booking amounts."""
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO bookings (booking_ref, customer_name, flight_no,
                        origin, destination, flight_date, amount, status, booked_at)
                    VALUES (%s, 'Neg', 'MR003', 'CCU', 'DEL', '2026-11-01', -1, 'CONFIRMED', '2026-10-31 10:00')
                """, (f"TEST-BK-{_uid()}",))

    def test_booking_list_query(self):
        """List query with status filter returns correct subset."""
        bref_c = self._insert_booking(status="CONFIRMED")
        bref_r = self._insert_booking(status="REFUNDED")
        confirmed = execute_query(
            "SELECT booking_ref FROM bookings WHERE booking_ref IN (%s, %s) AND status = 'CONFIRMED'",
            (bref_c, bref_r), fetch="all", database_url=TEST_DB_URL, commit=False
        )
        assert any(r["booking_ref"] == bref_c for r in confirmed)
        assert not any(r["booking_ref"] == bref_r for r in confirmed)


# ---------------------------------------------------------------------------
# 3. Airline tickets tests
# ---------------------------------------------------------------------------

class TestAirlineTickets:

    def _insert_booking(self, bref=None):
        bref = bref or f"TEST-BK-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO bookings (booking_ref, customer_name, flight_no,
                    origin, destination, flight_date, amount, status, booked_at)
                VALUES (%s, 'Ticket Holder', 'MR100',
                    'CCU', 'DEL', '2026-11-01', 5000, 'CONFIRMED', '2026-10-31 10:00')
            """, (bref,))
        return bref

    def test_ticket_creation_persists(self):
        """Airline record creation persists and is retrievable."""
        bref = self._insert_booking()
        pnr = f"TEST-MR{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO airline_tickets (pnr, booking_ref, passenger, flight_no,
                    origin, destination, departure, fare, status)
                VALUES (%s, %s, 'Test Pax', 'MR100', 'CCU', 'DEL', '2026-11-01 06:00', 5000, 'CONFIRMED')
            """, (pnr, bref))
        row = execute_query(
            "SELECT pnr, status FROM airline_tickets WHERE pnr = %s",
            (pnr,), fetch="one", database_url=TEST_DB_URL, commit=False
        )
        assert row is not None
        assert row["status"] == "CONFIRMED"

    def test_ticket_fk_violation_rejected(self):
        """Airline ticket with non-existent booking_ref is rejected."""
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO airline_tickets (pnr, booking_ref, passenger, flight_no,
                        origin, destination, departure, fare, status)
                    VALUES (%s, 'NONEXISTENT-BK', 'Pax', 'MR100', 'A', 'B', '2026-11-01 06:00', 100, 'CONFIRMED')
                """, (f"TEST-MR{_uid()}",))

    def test_invalid_ticket_status_rejected(self):
        """CHECK constraint rejects invalid airline ticket status."""
        bref = self._insert_booking()
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO airline_tickets (pnr, booking_ref, passenger, flight_no,
                        origin, destination, departure, fare, status)
                    VALUES (%s, %s, 'Pax', 'MR100', 'A', 'B', '2026-11-01 06:00', 100, 'INVALID')
                """, (f"TEST-MR{_uid()}", bref))


# ---------------------------------------------------------------------------
# 4. Payment tests
# ---------------------------------------------------------------------------

class TestPayments:

    def _insert_booking(self, bref=None):
        bref = bref or f"TEST-BK-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO bookings (booking_ref, customer_name, flight_no,
                    origin, destination, flight_date, amount, status, booked_at)
                VALUES (%s, 'Payment Holder', 'MR200',
                    'BOM', 'DEL', '2026-11-01', 6000, 'CONFIRMED', '2026-10-31 10:00')
            """, (bref,))
        return bref

    def _insert_payment(self, bref, pref=None, amount=6000, status="SUCCESS", refunded=0):
        pref = pref or f"TEST-PAY-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                    amount, status, paid_at, refunded_amount)
                VALUES (%s, %s, 'Test Payer', 'UPI', %s, %s, '2026-10-31 10:05', %s)
            """, (pref, bref, amount, status, refunded))
        return pref

    def test_payment_creation_persists(self):
        bref = self._insert_booking()
        pref = self._insert_payment(bref)
        row = execute_query(
            "SELECT payment_ref, status FROM payments WHERE payment_ref = %s",
            (pref,), fetch="one", database_url=TEST_DB_URL, commit=False
        )
        assert row["payment_ref"] == pref
        assert row["status"] == "SUCCESS"

    def test_payment_fk_violation_rejected(self):
        """Payment with non-existent booking_ref is rejected."""
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                        amount, status, paid_at, refunded_amount)
                    VALUES (%s, 'GHOST-BK', 'Payer', 'UPI', 100, 'SUCCESS', '2026-10-31 10:05', 0)
                """, (f"TEST-PAY-{_uid()}",))

    def test_invalid_payment_status_rejected(self):
        bref = self._insert_booking()
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                        amount, status, paid_at, refunded_amount)
                    VALUES (%s, %s, 'Payer', 'UPI', 100, 'INVALID_STATUS', '2026-10-31 10:05', 0)
                """, (f"TEST-PAY-{_uid()}", bref))

    def test_refunded_amount_exceeding_amount_rejected(self):
        """refunded_amount > amount violates CHECK constraint."""
        bref = self._insert_booking()
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                        amount, status, paid_at, refunded_amount)
                    VALUES (%s, %s, 'Payer', 'UPI', 1000, 'REFUNDED', '2026-10-31 10:05', 5000)
                """, (f"TEST-PAY-{_uid()}", bref))

    def test_negative_refunded_amount_rejected(self):
        bref = self._insert_booking()
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                        amount, status, paid_at, refunded_amount)
                    VALUES (%s, %s, 'Payer', 'UPI', 1000, 'SUCCESS', '2026-10-31 10:05', -50)
                """, (f"TEST-PAY-{_uid()}", bref))

    def test_invalid_method_rejected(self):
        bref = self._insert_booking()
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                        amount, status, paid_at, refunded_amount)
                    VALUES (%s, %s, 'Payer', 'CRYPTO', 1000, 'SUCCESS', '2026-10-31 10:05', 0)
                """, (f"TEST-PAY-{_uid()}", bref))

    def test_duplicate_gateway_txn_id_rejected(self):
        """UNIQUE constraint on gateway_txn_id."""
        bref1 = self._insert_booking()
        bref2 = self._insert_booking()
        txn_id = f"ZP_TEST_{_uid()}"
        self._insert_payment(bref1)  # insert without custom txn_id first
        pref1 = f"TEST-PAY-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                    amount, status, paid_at, refunded_amount, gateway_txn_id)
                VALUES (%s, %s, 'Payer1', 'UPI', 1000, 'SUCCESS', '2026-10-31 10:05', 0, %s)
            """, (pref1, bref1, txn_id))
        with pytest.raises(Exception):
            pref2 = f"TEST-PAY-{_uid()}"
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                        amount, status, paid_at, refunded_amount, gateway_txn_id)
                    VALUES (%s, %s, 'Payer2', 'UPI', 2000, 'SUCCESS', '2026-10-31 10:05', 0, %s)
                """, (pref2, bref2, txn_id))


# ---------------------------------------------------------------------------
# 5. Evidence records tests
# ---------------------------------------------------------------------------

class TestEvidenceRecords:

    def _insert_booking(self):
        bref = f"TEST-BK-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO bookings (booking_ref, customer_name, flight_no,
                    origin, destination, flight_date, amount, status, booked_at)
                VALUES (%s, 'Evi Holder', 'MR300', 'CCU', 'DEL', '2026-11-01', 5000, 'CONFIRMED', '2026-10-31 10:00')
            """, (bref,))
        return bref

    def test_evidence_creation_persists(self):
        """Evidence records can be stored and queried."""
        bref = self._insert_booking()
        payload = {"test": True, "amount": 5000}
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO evidence_records (booking_ref, source_type, external_reference,
                    evidence_type, payload)
                VALUES (%s, 'test', %s, 'Booking Platform', %s::jsonb)
                RETURNING evidence_id
            """, (bref, f"REF-{_uid()}", json.dumps(payload)))
            row = cur.fetchone()
        assert row["evidence_id"] > 0

    def test_evidence_null_booking_ref_allowed(self):
        """Evidence records with no booking_ref (nullable FK) are allowed."""
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO evidence_records (booking_ref, source_type, external_reference,
                    evidence_type, payload)
                VALUES (NULL, 'test', 'NO-BREF', 'Airline', '{}'::jsonb)
                RETURNING evidence_id
            """)
            row = cur.fetchone()
        assert row["evidence_id"] > 0

    def test_evidence_jsonb_payload_queryable(self):
        """JSONB payload can be queried with operators."""
        bref = self._insert_booking()
        ext_ref = f"JSONB-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO evidence_records (booking_ref, source_type, external_reference,
                    evidence_type, payload)
                VALUES (%s, 'test', %s, 'Payment Provider', '{"status": "REFUNDED", "amount": 3200}'::jsonb)
            """, (bref, ext_ref))
        rows = execute_query(
            "SELECT evidence_id FROM evidence_records WHERE payload->>'status' = 'REFUNDED' AND external_reference = %s",
            (ext_ref,), fetch="all", database_url=TEST_DB_URL, commit=False
        )
        assert len(rows) > 0


# ---------------------------------------------------------------------------
# 6. Refund cases tests
# ---------------------------------------------------------------------------

class TestRefundCases:

    def _insert_booking(self):
        bref = f"TEST-BK-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO bookings (booking_ref, customer_name, flight_no,
                    origin, destination, flight_date, amount, status, booked_at)
                VALUES (%s, 'Refund Holder', 'MR400', 'CCU', 'DEL', '2026-11-01', 7000, 'CONFIRMED', '2026-10-31 10:00')
            """, (bref,))
        return bref

    def test_refund_case_persists(self):
        """Refund/reconciliation cases persist correctly."""
        bref = self._insert_booking()
        case_id = f"TEST-CASE-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO refund_cases (case_identifier, booking_ref, case_status,
                    expected_amount, actual_amount, discrepancy_amount, reconciliation_result)
                VALUES (%s, %s, 'COMPLETED', 7000.00, 7000.00, 0.00, '{"status": "MATCHED"}'::jsonb)
                RETURNING refund_case_id
            """, (case_id, bref))
            row = cur.fetchone()
        assert row["refund_case_id"] > 0

    def test_invalid_case_status_rejected(self):
        bref = self._insert_booking()
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO refund_cases (case_identifier, booking_ref, case_status,
                        expected_amount, actual_amount, discrepancy_amount, reconciliation_result)
                    VALUES (%s, %s, 'TOTALLY_MADE_UP', 100, 100, 0, '{}'::jsonb)
                """, (f"TEST-CASE-{_uid()}", bref))

    def test_negative_expected_amount_rejected(self):
        bref = self._insert_booking()
        with pytest.raises(Exception):
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                cur.execute("""
                    INSERT INTO refund_cases (case_identifier, booking_ref, case_status,
                        expected_amount, actual_amount, discrepancy_amount, reconciliation_result)
                    VALUES (%s, %s, 'PENDING', -100, 0, 0, '{}'::jsonb)
                """, (f"TEST-CASE-{_uid()}", bref))


# ---------------------------------------------------------------------------
# 7. Transaction rollback tests
# ---------------------------------------------------------------------------

class TestTransactions:

    def test_rollback_on_error(self):
        """Multi-table transaction rolls back completely when an operation fails."""
        bref = f"TEST-BK-{_uid()}"
        pref = f"TEST-PAY-{_uid()}"
        initial_count = execute_query(
            "SELECT COUNT(*) AS cnt FROM bookings WHERE booking_ref = %s",
            (bref,), fetch="one", database_url=TEST_DB_URL, commit=False
        )["cnt"]

        try:
            with get_db_cursor(TEST_DB_URL) as (cur, _):
                # Insert booking (valid)
                cur.execute("""
                    INSERT INTO bookings (booking_ref, customer_name, flight_no,
                        origin, destination, flight_date, amount, status, booked_at)
                    VALUES (%s, 'Rollback', 'MR999', 'A', 'B', '2026-11-01', 1000, 'CONFIRMED', '2026-10-31 10:00')
                """, (bref,))
                # Insert payment with invalid status — triggers CHECK constraint failure
                cur.execute("""
                    INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                        amount, status, paid_at, refunded_amount)
                    VALUES (%s, %s, 'Payer', 'UPI', 1000, 'INVALID', '2026-10-31 10:05', 0)
                """, (pref, bref))
        except Exception:
            pass  # Expected

        # Booking should NOT have been committed (rolled back with the payment)
        after_count = execute_query(
            "SELECT COUNT(*) AS cnt FROM bookings WHERE booking_ref = %s",
            (bref,), fetch="one", database_url=TEST_DB_URL, commit=False
        )["cnt"]
        assert after_count == initial_count, "Transaction should have rolled back the booking insert"


# ---------------------------------------------------------------------------
# 8. Parameterized SQL (injection safety)
# ---------------------------------------------------------------------------

class TestParameterizedQueries:

    def test_sql_injection_safely_escaped(self):
        """Parameterized queries must prevent injection — injected string is literal data."""
        malicious_ref = "'; DROP TABLE bookings; --"
        rows = execute_query(
            "SELECT booking_ref FROM bookings WHERE booking_ref = %s",
            (malicious_ref,), fetch="all", database_url=TEST_DB_URL, commit=False
        )
        # Should return zero rows (not execute a DROP)
        assert rows == [] or len(rows) == 0

        # Confirm bookings table still exists
        health, _ = check_db_health(TEST_DB_URL)
        assert health
        count = execute_query(
            "SELECT COUNT(*) AS cnt FROM bookings",
            fetch="one", database_url=TEST_DB_URL, commit=False
        )
        assert count["cnt"] >= 0


# ---------------------------------------------------------------------------
# 9. JOIN and aggregate queries
# ---------------------------------------------------------------------------

class TestJoinAndAggregateQueries:

    def test_join_bookings_payments(self):
        """JOIN across bookings and payments returns correct data."""
        bref = f"TEST-BK-{_uid()}"
        pref = f"TEST-PAY-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO bookings (booking_ref, customer_name, flight_no,
                    origin, destination, flight_date, amount, status, booked_at)
                VALUES (%s, 'Join Tester', 'MR500', 'CCU', 'DEL', '2026-11-01', 5000, 'CONFIRMED', '2026-10-31 10:00')
            """, (bref,))
            cur.execute("""
                INSERT INTO payments (payment_ref, booking_ref, payer_name, method,
                    amount, status, paid_at, refunded_amount)
                VALUES (%s, %s, 'Join Tester', 'UPI', 5000, 'SUCCESS', '2026-10-31 10:05', 0)
            """, (pref, bref))

        rows = execute_query("""
            SELECT b.booking_ref, b.customer_name, p.payment_ref, p.amount, p.status
            FROM bookings b
            JOIN payments p ON b.booking_ref = p.booking_ref
            WHERE b.booking_ref = %s
        """, (bref,), fetch="all", database_url=TEST_DB_URL, commit=False)

        assert len(rows) == 1
        assert rows[0]["booking_ref"] == bref
        assert rows[0]["payment_ref"] == pref

    def test_aggregate_sum_count(self):
        """COUNT and SUM aggregates work across related tables."""
        result = execute_query("""
            SELECT COUNT(*) AS cnt, SUM(p.amount) AS total
            FROM payments p
            JOIN bookings b ON p.booking_ref = b.booking_ref
        """, fetch="one", database_url=TEST_DB_URL, commit=False)
        assert result is not None
        assert int(result["cnt"]) >= 0

    def test_reconciliation_stats_query(self):
        """Grouped reconciliation stats query works correctly."""
        rows = execute_query("""
            SELECT case_status, COUNT(*) AS cnt, SUM(expected_amount) AS total_expected
            FROM refund_cases
            GROUP BY case_status
            ORDER BY cnt DESC
        """, fetch="all", database_url=TEST_DB_URL, commit=False)
        # Should return list (possibly empty) without error
        assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# 10. API compatibility tests
# ---------------------------------------------------------------------------

class TestAPICompatibility:
    """
    These tests verify that the database helpers used by the API servers
    return the expected camelCase field names and data shapes.
    """

    def test_booking_api_field_names(self):
        """Booking rows returned by DB use expected API field names."""
        bref = f"TEST-BK-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO bookings (booking_ref, customer_name, flight_no,
                    origin, destination, flight_date, amount, status, booked_at)
                VALUES (%s, 'API Tester', 'MR600', 'CCU', 'DEL', '2026-11-01', 5000, 'CONFIRMED', '2026-10-31 10:00')
            """, (bref,))

        row = execute_query(
            "SELECT * FROM bookings WHERE booking_ref = %s",
            (bref,), fetch="one", database_url=TEST_DB_URL, commit=False
        )
        # Verify the DB columns that the API layer converts to camelCase exist
        assert "booking_ref" in row
        assert "customer_name" in row
        assert "flight_no" in row
        assert "status" in row
        assert "amount" in row

    def test_booking_amount_is_decimal(self):
        """Monetary values stored as NUMERIC to avoid float precision issues."""
        bref = f"TEST-BK-{_uid()}"
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""
                INSERT INTO bookings (booking_ref, customer_name, flight_no,
                    origin, destination, flight_date, amount, status, booked_at)
                VALUES (%s, 'Decimal Test', 'MR601', 'CCU', 'DEL', '2026-11-01', 12345.67, 'CONFIRMED', '2026-10-31 10:00')
            """, (bref,))
        row = execute_query(
            "SELECT amount FROM bookings WHERE booking_ref = %s",
            (bref,), fetch="one", database_url=TEST_DB_URL, commit=False
        )
        # psycopg returns Decimal for NUMERIC columns
        assert isinstance(row["amount"], Decimal)
        assert row["amount"] == Decimal("12345.67")


# ---------------------------------------------------------------------------
# 11. Reconciliation pipeline tests (unit-level, no live HTTP)
# ---------------------------------------------------------------------------

class TestReconciliationPipeline:

    def test_scenario_a_completed(self):
        """Scenario A: Completed Refund gives COMPLETED state."""
        from resolution_api_server import run_test_scenario
        case = run_test_scenario("A")
        assert case.state["normalized"] == "COMPLETED"
        assert not case.state["blocker"]["blocked"]
        assert case.reconciliation["status"] == "MATCHED"

    def test_scenario_b_blocked(self):
        """Scenario B: SLA exceeded gives BLOCKED state."""
        from resolution_api_server import run_test_scenario
        case = run_test_scenario("B")
        assert case.state["normalized"] == "BLOCKED"
        assert case.state["blocker"]["blocked"]

    def test_scenario_c_processing(self):
        """Scenario C: Within SLA gives PROCESSING_NORMALLY."""
        from resolution_api_server import run_test_scenario
        case = run_test_scenario("C")
        assert case.state["normalized"] == "PROCESSING_NORMALLY"

    def test_scenario_d_unresolved(self):
        """Scenario D: Conflicting refs give UNRESOLVED."""
        from resolution_api_server import run_test_scenario
        case = run_test_scenario("D")
        assert case.state["normalized"] == "UNRESOLVED"
        assert case.reconciliation["review_required"]

    def test_custom_records_resolution(self):
        """Custom evidence records produce a valid ApplicationReadyCase."""
        from resolution_api_server import run_custom_records
        records = [
            {"source_organization": "Booking Platform", "source_record_id": "BK-TEST-DB",
             "booking_ref": "BK-DB-100", "passenger": "DB Test", "refund_amount": 500.0,
             "source_status": "REFUND_REQUESTED"},
            {"source_organization": "Airline", "source_record_id": "AIR-TEST-DB",
             "booking_ref": "BK-DB-100", "passenger": "DB Test", "refund_amount": 500.0,
             "source_status": "REFUND_AUTHORIZED"},
        ]
        case = run_custom_records(records)
        assert case.reconciliation["status"] == "MATCHED"
        assert case.meta.case_id is not None
