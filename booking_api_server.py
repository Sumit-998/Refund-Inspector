"""
Booking API Server — Port 8001
Backed by PostgreSQL via psycopg 3.
Preserves all existing API contracts, HTTP methods, and JSON field names.
"""

import json
import math
import os
import sys
import re
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# Ensure repository root is on sys.path
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from db.connection import get_db_cursor, check_db_health, apply_schema
from core.auth import check_admin_or_internal_from_handler

HOST = "127.0.0.1"
PORT = int(os.environ.get("BOOKING_PORT", 8001))

VALID_STATUSES = {"CONFIRMED", "CANCELLED", "REFUND_INITIATED", "REFUNDED", "PENDING", "FAILED"}

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _row_to_api(row: dict) -> dict:
    """Convert a DB row (snake_case) to the original camelCase API shape."""
    return {
        "bookingRef":  row["booking_ref"],
        "customer":    row["customer_name"],
        "email":       row.get("email", ""),
        "airlinePnr":  row.get("airline_pnr", ""),
        "flightNo":    row["flight_no"],
        "origin":      row["origin"],
        "destination": row["destination"],
        "flightDate":  row["flight_date"],
        "amount":      float(row["amount"]),
        "currency":    row.get("currency", "INR"),
        "paymentRef":  row.get("payment_ref", ""),
        "status":      row["status"],
        "bookedAt":    row["booked_at"],
        "refundId":    row.get("refund_id", ""),
    }


def _db_list_bookings(status_filter: str | None) -> list[dict]:
    sql = """
        SELECT booking_ref, customer_name, email, airline_pnr, flight_no,
               origin, destination, flight_date, amount, currency,
               payment_ref, status, booked_at, refund_id
        FROM bookings
    """
    params = ()
    if status_filter:
        sql += " WHERE status = %s"
        params = (status_filter.upper(),)
    sql += " ORDER BY created_at"
    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)
        return [_row_to_api(r) for r in cur.fetchall()]


def _db_get_booking(booking_ref: str) -> dict | None:
    sql = """
        SELECT booking_ref, customer_name, email, airline_pnr, flight_no,
               origin, destination, flight_date, amount, currency,
               payment_ref, status, booked_at, refund_id
        FROM bookings WHERE booking_ref = %s
    """
    with get_db_cursor() as (cur, _):
        cur.execute(sql, (booking_ref,))
        row = cur.fetchone()
        return _row_to_api(row) if row else None


def _db_delete_booking(booking_ref: str) -> dict | None:
    with get_db_cursor() as (cur, _):
        cur.execute("""
            DELETE FROM bookings WHERE booking_ref = %s
            RETURNING booking_ref, customer_name, email, airline_pnr, flight_no,
                      origin, destination, flight_date, amount, currency,
                      payment_ref, status, booked_at, refund_id
        """, (booking_ref,))
        row = cur.fetchone()
        return _row_to_api(row) if row else None


def _db_create_booking(data: dict) -> dict:
    """Create a booking and its customer atomically; duplicate refs are not updates."""
    bref = str(data.get("bookingRef") or f"BK-{uuid.uuid4().hex[:12].upper()}").strip().upper()
    customer = str(data.get("customer", data.get("customer_name", ""))).strip()
    email = str(data.get("email", "")).strip().lower()
    flight_no = str(data.get("flightNo", data.get("flight_no", ""))).strip().upper()
    origin = str(data.get("origin", "")).strip().upper()
    destination = str(data.get("destination", "")).strip().upper()
    flight_date = str(data.get("flightDate", data.get("flight_date", ""))).strip()
    if not all((customer, email, flight_no, origin, destination, flight_date)):
        raise ValueError("customer, email, flightNo, origin, destination, and flightDate are required.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("A valid customer email is required to associate the booking with a user.")
    if origin == destination:
        raise ValueError("origin and destination must be different.")
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        raise ValueError("amount must be a valid non-negative number.")
    if amount < 0:
        raise ValueError("amount must be non-negative.")
    if not math.isfinite(amount) or amount > 99999999.99:
        raise ValueError("amount must be a finite value within the supported range.")
    if not re.fullmatch(r"[A-Z0-9-]{3,50}", bref):
        raise ValueError("bookingRef must contain 3-50 letters, numbers, or hyphens.")
    status = str(data.get("status", "CONFIRMED")).upper()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    sql = """
        INSERT INTO bookings (
            booking_ref, user_id, customer_name, email, airline_pnr, flight_no,
            origin, destination, flight_date, amount, currency,
            payment_ref, status, refund_id, booked_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        RETURNING booking_ref
    """
    with get_db_cursor() as (cur, _):
        cur.execute("""
            INSERT INTO users (full_name, email)
            VALUES (%s, %s)
            ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
            RETURNING user_id
        """, (customer, email))
        user_id = cur.fetchone()["user_id"]
        cur.execute(sql, (
            bref, user_id, customer, email,
            data.get("airlinePnr") or None, flight_no, origin, destination,
            flight_date, amount, str(data.get("currency") or "INR").upper(),
            data.get("paymentRef") or None, status,
            data.get("refundId") or None,
            str(data.get("bookedAt") or datetime.now().strftime("%Y-%m-%d %H:%M")),
        ))
        cur.fetchone()
        cur.execute("""
            SELECT booking_ref, customer_name, email, airline_pnr, flight_no,
                   origin, destination, flight_date, amount, currency,
                   payment_ref, status, booked_at, refund_id
            FROM bookings WHERE booking_ref = %s
        """, (bref,))
        saved = cur.fetchone()
    return _row_to_api(saved)


def _db_update_booking(booking_ref: str, data: dict) -> dict | None:
    """Update a booking and keep its linked customer record consistent."""
    existing = _db_get_booking(booking_ref)
    if existing is None:
        return None
    merged = {**existing, **data}
    customer = str(merged.get("customer", merged.get("customer_name", ""))).strip()
    email = str(merged.get("email", "")).strip().lower()
    status = str(merged.get("status", "CONFIRMED")).upper()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")
    if not customer or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("A customer name and valid contact email are required.")
    try:
        amount = float(merged.get("amount", 0))
    except (TypeError, ValueError):
        raise ValueError("amount must be a valid non-negative number.")
    if amount < 0 or not math.isfinite(amount) or amount > 99999999.99:
        raise ValueError("amount must be a finite non-negative value within the supported range.")

    with get_db_cursor() as (cur, _):
        cur.execute("""
            INSERT INTO users (full_name, email) VALUES (%s, %s)
            ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
            RETURNING user_id
        """, (customer, email))
        user_id = cur.fetchone()["user_id"]
        cur.execute("""
            UPDATE bookings SET user_id = %s, customer_name = %s, email = %s,
                airline_pnr = %s, flight_no = %s, origin = %s, destination = %s,
                flight_date = %s, amount = %s, currency = %s, payment_ref = %s,
                status = %s, refund_id = %s, booked_at = %s
            WHERE booking_ref = %s
        """, (
            user_id, customer, email,
            merged.get("airlinePnr") or merged.get("airline_pnr"),
            str(merged.get("flightNo", merged.get("flight_no", ""))),
            str(merged.get("origin", "")), str(merged.get("destination", "")),
            str(merged.get("flightDate", merged.get("flight_date", ""))),
            amount, str(merged.get("currency", "INR")),
            merged.get("paymentRef") or merged.get("payment_ref"), status,
            merged.get("refundId") or merged.get("refund_id"),
            str(merged.get("bookedAt", merged.get("booked_at", ""))), booking_ref,
        ))
        cur.execute("""
            SELECT booking_ref, customer_name, email, airline_pnr, flight_no,
                   origin, destination, flight_date, amount, currency,
                   payment_ref, status, booked_at, refund_id
            FROM bookings WHERE booking_ref = %s
        """, (booking_ref,))
        saved = cur.fetchone()
    return _row_to_api(saved)


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class BookingAPIHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):  # suppress per-request console noise
        pass

    def send_json(self, status, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin", "")
        allowed = {"http://127.0.0.1:8000", "http://localhost:8000"}
        if origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Internal-Service-Key, X-CSRF-Token")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        allowed = {"http://127.0.0.1:8000", "http://localhost:8000"}
        if origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Internal-Service-Key, X-CSRF-Token")
        self.end_headers()

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # POST handlers
    # ------------------------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Public health check
        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "booking"})
            return

        # Enforce server-side authorization: requires Admin session or internal service key
        authorized, principal = check_admin_or_internal_from_handler(self)
        if not authorized:
            self.send_json(401, {"error": "Unauthorized. Active Admin session or internal authorization required."})
            return
        data = self._read_json_body()
        if path == "/api/booking/bookings" and not (principal or {}).get("is_internal"):
            self.send_json(403, {"error": "Create bookings through the authenticated Resolution Hub workflow."})
            return
        if path.startswith("/api/booking/bookings/") and not (principal or {}).get("is_internal"):
            requested_status = str(data.get("status", "")).upper()
            if requested_status in {"PENDING", "CONFIRMED", "FAILED"}:
                self.send_json(403, {"error": "Booking lifecycle status changes must be coordinated by the Resolution Hub."})
                return
        if principal and not principal.get("is_internal"):
            csrf = self.headers.get("X-CSRF-Token", "")
            import secrets
            if not csrf or not secrets.compare_digest(csrf, principal.get("csrf_token", "")):
                self.send_json(403, {"error": "Forbidden. Invalid or missing CSRF token."})
                return

        # POST /api/booking/bookings — create; duplicate booking references return 409
        if path == "/api/booking/bookings":
            try:
                booking = _db_create_booking(data)
            except ValueError as err:
                self.send_json(400, {"error": str(err)})
                return
            except Exception as err:
                if getattr(err, "sqlstate", None) == "23505":
                    self.send_json(409, {"error": "Booking reference or airline PNR already exists."})
                    return
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            self.send_json(201, {"api": "API 1", "status": "created", "booking": booking})
            return

        # POST /api/booking/bookings/<ref>  — update by bookingRef in path
        prefix = "/api/booking/bookings/"
        if path.startswith(prefix):
            bref = path[len(prefix):].upper()
            try:
                updated = _db_update_booking(bref, data)
            except ValueError as err:
                self.send_json(400, {"error": str(err)})
                return
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if updated is None:
                self.send_json(404, {"error": "Booking not found"})
                return
            self.send_json(200, {"api": "API 2", "status": "updated", "booking": updated})
            return

        self.send_json(404, {"error": "Unknown endpoint"})

    # ------------------------------------------------------------------
    # GET handlers
    # ------------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "booking"})
            return

        # Enforce server-side authorization: requires Admin session or internal service key
        authorized, _ = check_admin_or_internal_from_handler(self)
        if not authorized:
            self.send_json(401, {"error": "Unauthorized. Active Admin session or internal authorization required."})
            return

        # API 1: List bookings
        if path == "/api/booking/bookings":
            status_filter = params.get("status", [None])[0]
            try:
                results = _db_list_bookings(status_filter)
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return
            self.send_json(200, {
                "api": "API 1",
                "count": len(results),
                "bookings": results
            })
            return

        # API 2: Get one booking
        prefix = "/api/booking/bookings/"
        if path.startswith(prefix):
            booking_ref = path[len(prefix):].upper()
            try:
                booking = _db_get_booking(booking_ref)
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if booking is None:
                self.send_json(404, {
                    "api": "API 2",
                    "error": "Booking not found"
                })
                return

            amount = booking["amount"]
            fee = 299 if amount > 5000 else 199
            tax = round((amount - fee) * 0.18)
            base = amount - fee - tax

            self.send_json(200, {
                "api": "API 2",
                **booking,
                "fareBreakdown": {
                    "baseFare": base,
                    "taxes": tax,
                    "convenienceFee": fee,
                    "total": amount
                }
            })
            return

        self.send_json(404, {"error": "Unknown endpoint"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        authorized, principal = check_admin_or_internal_from_handler(self)
        if not authorized or not (principal or {}).get("is_internal"):
            self.send_json(401, {"error": "Internal service authorization required."})
            return
        prefix = "/api/booking/bookings/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "Unknown endpoint"})
            return
        try:
            booking = _db_delete_booking(path[len(prefix):].upper())
        except Exception as err:
            if getattr(err, "sqlstate", None) == "23503":
                self.send_json(409, {"error": "Booking still has dependent airline or payment records."})
                return
            self.send_json(500, {"error": f"Database error: {err}"})
            return
        if booking is None:
            self.send_json(404, {"error": "Booking not found"})
            return
        self.send_json(200, {"status": "deleted", "booking": booking})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Verify database connectivity before accepting requests
    healthy, msg = check_db_health()
    if not healthy:
        print(f"FATAL: Cannot connect to database: {msg}")
        sys.exit(1)
    print(f"Database OK: {msg[:60]}")

    # Apply schema idempotently
    try:
        apply_schema()
    except Exception as err:
        print(f"FATAL: Schema initialization failed: {err}")
        sys.exit(1)

    server = HTTPServer((HOST, PORT), BookingAPIHandler)
    print(f"Booking API running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Booking API.")
        server.server_close()
