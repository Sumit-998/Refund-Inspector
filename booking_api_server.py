"""
Booking API Server — Port 8001
Backed by PostgreSQL via psycopg 3.
Preserves all existing API contracts, HTTP methods, and JSON field names.
"""

import json
import os
import sys
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


def _db_count_bookings() -> int:
    with get_db_cursor() as (cur, _):
        cur.execute("SELECT COUNT(*) AS cnt FROM bookings")
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0


def _db_upsert_booking(data: dict) -> tuple[dict, bool]:
    """
    Insert or update a booking.
    Returns (booking_dict, created:bool).
    The incoming data uses camelCase keys.
    """
    bref = str(data.get("bookingRef", "")).upper()

    # Provide defaults for new bookings
    if not bref:
        count = _db_count_bookings()
        bref = f"BK-{20000 + count + 1}"
        data["bookingRef"] = bref

    if "bookedAt" not in data or not data["bookedAt"]:
        data["bookedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    if "flightDate" not in data or not data["flightDate"]:
        data["flightDate"] = "2026-10-10"
    if "refundId" not in data or not data["refundId"]:
        data["refundId"] = f"DEMO-REF-{bref}"
    if "currency" not in data or not data["currency"]:
        data["currency"] = "INR"
    if "status" not in data or not data["status"]:
        data["status"] = "CONFIRMED"

    status = str(data.get("status", "CONFIRMED")).upper()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    sql = """
        INSERT INTO bookings (
            booking_ref, customer_name, email, airline_pnr, flight_no,
            origin, destination, flight_date, amount, currency,
            payment_ref, status, refund_id, booked_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        ON CONFLICT (booking_ref) DO UPDATE SET
            customer_name = EXCLUDED.customer_name,
            email         = COALESCE(EXCLUDED.email, bookings.email),
            airline_pnr   = COALESCE(EXCLUDED.airline_pnr, bookings.airline_pnr),
            flight_no     = EXCLUDED.flight_no,
            origin        = EXCLUDED.origin,
            destination   = EXCLUDED.destination,
            flight_date   = EXCLUDED.flight_date,
            amount        = EXCLUDED.amount,
            currency      = EXCLUDED.currency,
            payment_ref   = COALESCE(EXCLUDED.payment_ref, bookings.payment_ref),
            status        = EXCLUDED.status,
            refund_id     = COALESCE(EXCLUDED.refund_id, bookings.refund_id),
            booked_at     = EXCLUDED.booked_at,
            updated_at    = CURRENT_TIMESTAMP
        RETURNING booking_ref, (xmax = 0) AS inserted
    """
    params = (
        bref,
        str(data.get("customer", data.get("customer_name", ""))),
        data.get("email"),
        data.get("airlinePnr") or data.get("airline_pnr"),
        str(data.get("flightNo", data.get("flight_no", ""))),
        str(data.get("origin", "")),
        str(data.get("destination", "")),
        str(data["flightDate"]),
        float(data.get("amount", 0)),
        str(data.get("currency", "INR")),
        data.get("paymentRef") or data.get("payment_ref"),
        status,
        data.get("refundId") or data.get("refund_id"),
        str(data["bookedAt"]),
    )

    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)
        row = cur.fetchone()
        created = bool(row["inserted"]) if row else True

    booking = _db_get_booking(bref)
    return booking, created


def _db_update_booking(booking_ref: str, data: dict) -> dict | None:
    """Update fields on an existing booking. Raises if not found."""
    existing = _db_get_booking(booking_ref)
    if existing is None:
        return None

    # Merge incoming fields
    merged = {**existing, **data}
    merged["bookingRef"] = booking_ref  # must not change the PK

    status = str(merged.get("status", "CONFIRMED")).upper()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    sql = """
        UPDATE bookings SET
            customer_name = %s,
            email         = %s,
            airline_pnr   = %s,
            flight_no     = %s,
            origin        = %s,
            destination   = %s,
            flight_date   = %s,
            amount        = %s,
            currency      = %s,
            payment_ref   = %s,
            status        = %s,
            refund_id     = %s,
            booked_at     = %s
        WHERE booking_ref = %s
    """
    params = (
        str(merged.get("customer", merged.get("customer_name", ""))),
        merged.get("email"),
        merged.get("airlinePnr") or merged.get("airline_pnr"),
        str(merged.get("flightNo", merged.get("flight_no", ""))),
        str(merged.get("origin", "")),
        str(merged.get("destination", "")),
        str(merged.get("flightDate", merged.get("flight_date", ""))),
        float(merged.get("amount", 0)),
        str(merged.get("currency", "INR")),
        merged.get("paymentRef") or merged.get("payment_ref"),
        status,
        merged.get("refundId") or merged.get("refund_id"),
        str(merged.get("bookedAt", merged.get("booked_at", ""))),
        booking_ref,
    )
    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)

    return _db_get_booking(booking_ref)


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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Internal-Service-Key, X-CSRF-Token")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
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
        authorized, _ = check_admin_or_internal_from_handler(self)
        if not authorized:
            self.send_json(401, {"error": "Unauthorized. Active Admin session or internal authorization required."})
            return

        data = self._read_json_body()

        # POST /api/booking/bookings  — create or update by bookingRef in body
        if path == "/api/booking/bookings":
            try:
                booking, created = _db_upsert_booking(data)
            except ValueError as err:
                self.send_json(400, {"error": str(err)})
                return
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if created:
                self.send_json(201, {"api": "API 1", "status": "created", "booking": booking})
            else:
                self.send_json(200, {"api": "API 1", "status": "updated", "booking": booking})
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

        