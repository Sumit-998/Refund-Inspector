"""
db/seed.py — Optional, idempotent seed script for development.

Loads the original demo records from the three API servers into PostgreSQL.
Safe to run multiple times: uses INSERT ... ON CONFLICT DO NOTHING.

WARNING: Do NOT run this against production or the test database.
         This populates the development (nimbus_db) database only.

Usage:
    .venv/bin/python db/seed.py
"""

import os
import sys
import json

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from db.connection import get_db_cursor, check_db_health, apply_schema, get_database_url

# Canonical booking records (from original booking_api_server.py demo data)
SEED_BOOKINGS = [
    ("BK-20001", "Aarav Sharma",  "aarav@example.com",  "MR1001", "MR201", "CCU", "DEL", "2026-10-10", 5400.00, "INR", "PAY-90001", "CONFIRMED",        "DEMO-REF-BK-20001", "2026-10-01 10:12"),
    ("BK-20002", "Priya Das",     "priya@example.com",  "MR1002", "MR305", "CCU", "BOM", "2026-10-10", 6200.00, "INR", "PAY-90002", "CONFIRMED",        "DEMO-REF-BK-20002", "2026-10-01 11:40"),
    ("BK-20003", "Rohan Mehta",   "rohan@example.com",  "MR1003", "MR118", "DEL", "BLR", "2026-10-10", 7100.00, "INR", "PAY-90003", "CONFIRMED",        "DEMO-REF-BK-20003", "2026-10-02 09:05"),
    ("BK-20004", "Ananya Roy",    "ananya@example.com", "MR1004", "MR410", "BOM", "CCU", "2026-10-10", 5800.00, "INR", "PAY-90004", "CANCELLED",        "DEMO-REF-BK-20004", "2026-10-02 14:30"),
    ("BK-20005", "Vikram Singh",  "vikram@example.com", "MR1005", "MR222", "BLR", "HYD", "2026-10-10", 3200.00, "INR", "PAY-90005", "REFUND_INITIATED", "DEMO-REF-BK-20005", "2026-10-03 08:20"),
    ("BK-20006", "Sneha Iyer",    "sneha@example.com",  "MR1006", "MR201", "CCU", "DEL", "2026-10-10", 5400.00, "INR", "PAY-90006", "CONFIRMED",        "DEMO-REF-BK-20006", "2026-10-03 16:45"),
    ("BK-20007", "Kabir Khan",    "kabir@example.com",  "MR1007", "MR509", "HYD", "CCU", "2026-10-10", 5100.00, "INR", "PAY-90007", "CONFIRMED",        "DEMO-REF-BK-20007", "2026-10-04 12:00"),
    ("BK-20008", "Meera Nair",    "meera@example.com",  "MR1008", "MR118", "DEL", "BLR", "2026-10-10", 7100.00, "INR", "PAY-90008", "CONFIRMED",        "DEMO-REF-BK-20008", "2026-10-04 19:10"),
    ("BK-20009", "Rahul Verma",   "rahul@example.com",  "MR1009", "MR201", "CCU", "DEL", "2026-10-10", 5400.00, "INR", "PAY-90009", "CONFIRMED",        "DEMO-REF-BK-20009", "2026-10-05 07:55"),
    # BK-20010 is referenced by payment PAY-90010 but not in original booking list;
    # it's included here so FK constraints succeed.
    ("BK-20010", "Isha Patel",    "isha@example.com",   "MR1010", "MR305", "BOM", "CCU", "2026-10-10", 4500.00, "INR", "PAY-90010", "CONFIRMED",        "DEMO-REF-BK-20010", "2026-10-05 10:29"),
]

SEED_TICKETS = [
    ("MR1001", "BK-20001", "Aarav Sharma",  "MR201", "CCU", "DEL", "2026-10-10 06:15", 5400.00, "CONFIRMED",  "12A", 15, "PAY-90001", "INR"),
    ("MR1002", "BK-20002", "Priya Das",     "MR305", "CCU", "BOM", "2026-10-11 09:40", 6200.00, "CHECKED_IN", "14C", 20, "PAY-90002", "INR"),
    ("MR1003", "BK-20003", "Rohan Mehta",   "MR118", "DEL", "BLR", "2026-10-12 18:05", 7100.00, "CONFIRMED",  "7F",  15, "PAY-90003", "INR"),
    ("MR1004", "BK-20004", "Ananya Roy",    "MR410", "BOM", "CCU", "2026-10-13 13:25", 5800.00, "CANCELLED",  "—",    0, "PAY-90004", "INR"),
    ("MR1005", "BK-20005", "Vikram Singh",  "MR222", "BLR", "HYD", "2026-10-14 07:50", 3200.00, "REFUNDED",   "—",    0, "PAY-90005", "INR"),
    ("MR1006", "BK-20006", "Sneha Iyer",    "MR201", "CCU", "DEL", "2026-10-10 06:15", 5400.00, "CONFIRMED",  "12B", 25, "PAY-90006", "INR"),
    ("MR1007", "BK-20007", "Kabir Khan",    "MR509", "HYD", "CCU", "2026-10-15 21:10", 4900.00, "CONFIRMED",  "3D",  15, "PAY-90007", "INR"),
    ("MR1008", "BK-20008", "Meera Nair",    "MR118", "DEL", "BLR", "2026-10-12 18:05", 7100.00, "CHECKED_IN", "8A",  20, "PAY-90008", "INR"),
]

METHODS = ["UPI", "Credit Card", "Debit Card", "Net Banking"]

SEED_PAYMENT_ROWS = [
    ("PAY-90001", "BK-20001", "MR1001", "Aarav Sharma",  5400.00, "SUCCESS",  "2026-10-01 10:13", 0.00),
    ("PAY-90002", "BK-20002", "MR1002", "Priya Das",     6200.00, "SUCCESS",  "2026-10-01 11:41", 0.00),
    ("PAY-90003", "BK-20003", "MR1003", "Rohan Mehta",   7100.00, "SUCCESS",  "2026-10-02 09:06", 0.00),
    ("PAY-90004", "BK-20004", "MR1004", "Ananya Roy",    5800.00, "SUCCESS",  "2026-10-02 14:31", 0.00),
    ("PAY-90005", "BK-20005", "MR1005", "Vikram Singh",  3200.00, "REFUNDED", "2026-10-03 08:21", 3200.00),
    ("PAY-90006", "BK-20006", "MR1006", "Sneha Iyer",    5400.00, "FAILED",   "2026-10-03 16:46", 0.00),
    ("PAY-90007", "BK-20007", "MR1007", "Kabir Khan",    5100.00, "SUCCESS",  "2026-10-04 12:01", 0.00),
    ("PAY-90008", "BK-20008", "MR1008", "Meera Nair",    7100.00, "SUCCESS",  "2026-10-04 19:11", 0.00),
    ("PAY-90009", "BK-20009", "MR1009", "Rahul Verma",   5400.00, "SUCCESS",  "2026-10-05 07:56", 0.00),
    ("PAY-90010", "BK-20010", "MR1010", "Isha Patel",    4500.00, "SUCCESS",  "2026-10-05 10:30", 0.00),
]


def seed_users(cur) -> int:
    """
    Seeds customer users from SEED_BOOKINGS and links bookings.user_id.
    """
    user_sql = """
        INSERT INTO users (full_name, email)
        VALUES (%s, %s)
        ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
        RETURNING user_id
    """
    count = 0
    for row in SEED_BOOKINGS:
        bref, cust_name, email = row[0], row[1], row[2]
        if email:
            cur.execute(user_sql, (cust_name, email))
            res = cur.fetchone()
            uid = res["user_id"] if isinstance(res, dict) else res[0]
            cur.execute(
                "UPDATE bookings SET user_id = %s WHERE booking_ref = %s AND (user_id IS NULL OR user_id != %s)",
                (uid, bref, uid)
            )
            count += 1
    return count


def seed_bookings(cur) -> int:
    sql = """
        INSERT INTO bookings (
            booking_ref, customer_name, email, airline_pnr, flight_no,
            origin, destination, flight_date, amount, currency,
            payment_ref, status, refund_id, booked_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (booking_ref) DO NOTHING
    """
    count = 0
    for row in SEED_BOOKINGS:
        cur.execute(sql, row)
        count += cur.rowcount
    seed_users(cur)
    return count


def seed_tickets(cur) -> int:
    sql = """
        INSERT INTO airline_tickets (
            pnr, booking_ref, passenger, flight_no, origin, destination,
            departure, fare, status, seat, baggage_kg, payment_ref, currency
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (pnr) DO NOTHING
    """
    count = 0
    for row in SEED_TICKETS:
        cur.execute(sql, row)
        count += cur.rowcount
    return count


def seed_payments(cur) -> int:
    sql = """
        INSERT INTO payments (
            payment_ref, booking_ref, airline_pnr, payer_name, method,
            amount, currency, status, paid_at, refunded_amount,
            gateway_txn_id, email
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (payment_ref) DO NOTHING
    """
    count = 0
    for i, row in enumerate(SEED_PAYMENT_ROWS):
        pref, bref, apnr, payer, amount, status, paid_at, refunded = row
        method = METHODS[i % len(METHODS)]
        gateway = f"ZP{7300000 + i * 137}"
        email = payer.split()[0].lower() + "@example.com"
        cur.execute(sql, (
            pref, bref, apnr, payer, method,
            amount, "INR", status, paid_at, refunded,
            gateway, email,
        ))
        count += cur.rowcount
    return count


def main():
    url = get_database_url()
    # Safety guard: refuse to seed the test database
    if "test" in url.lower():
        print("ERROR: Refusing to seed a test database. Set DATABASE_URL to the dev database.")
        sys.exit(1)

    print(f"Seeding database: {url}")
    print("This script uses INSERT ... ON CONFLICT DO NOTHING and is safe to re-run.\n")

    healthy, msg = check_db_health()
    if not healthy:
        print(f"ERROR: Cannot connect to database: {msg}")
        sys.exit(1)

    apply_schema()
    print("Schema ensured.\n")

    with get_db_cursor() as (cur, _):
        b_count = seed_bookings(cur)
        t_count = seed_tickets(cur)
        p_count = seed_payments(cur)

    print(f"Bookings inserted:     {b_count} (of {len(SEED_BOOKINGS)} total, duplicates skipped)")
    print(f"Airline tickets inserted: {t_count} (of {len(SEED_TICKETS)} total, duplicates skipped)")
    print(f"Payments inserted:     {p_count} (of {len(SEED_PAYMENT_ROWS)} total, duplicates skipped)")
    print("\nSeed completed successfully.")


if __name__ == "__main__":
    main()
