"""
Airline API Server — Port 8002 (Meridian Air)
Backed by PostgreSQL via psycopg 3.
Preserves all existing API contracts (API 3 / API 4).
airline_tickets must reference a booking in the bookings table.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import signal
from db.connection import get_db_cursor, check_db_health, apply_schema
from core.auth import check_admin_or_internal_from_handler

HOST = "127.0.0.1"
PORT = int(os.environ.get("AIRLINE_PORT", 8002))

VALID_STATUSES = {"CONFIRMED", "CHECKED_IN", "CANCELLED", "REFUNDED", "ON_HOLD"}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _row_to_api(row: dict) -> dict:
    """Convert DB row to original camelCase API shape."""
    return {
        "pnr":        row["pnr"],
        "bookingRef": row["booking_ref"],
        "passenger":  row["passenger"],
        "flightNo":   row["flight_no"],
        "origin":     row["origin"],
        "destination": row["destination"],
        "departure":  row["departure"],
        "fare":       float(row["fare"]),
        "status":     row["status"],
        "seat":       row.get("seat", "—"),
        "baggageKg":  row.get("baggage_kg", 0),
        "paymentRef": row.get("payment_ref", ""),
        "currency":   row.get("currency", "INR"),
    }


def _ensure_booking_exists(booking_ref: str) -> None:
    """
    Ensures a matching booking row exists.
    Raises ValueError with a clear message if not found.
    """
    with get_db_cursor() as (cur, _):
        cur.execute("SELECT 1 FROM bookings WHERE booking_ref = %s", (booking_ref,))
        if not cur.fetchone():
            raise ValueError(
                f"Booking '{booking_ref}' does not exist in bookings table. "
                "Create the booking first via the Booking API (port 8001)."
            )


def _db_list_tickets(status_filter: str | None) -> list[dict]:
    sql = """
        SELECT pnr, booking_ref, passenger, flight_no, origin, destination,
               departure, fare, status, seat, baggage_kg, payment_ref, currency
        FROM airline_tickets
    """
    params: tuple = ()
    if status_filter:
        sql += " WHERE status = %s"
        params = (status_filter.upper(),)
    sql += " ORDER BY created_at"
    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)
        return [_row_to_api(r) for r in cur.fetchall()]


def _db_get_ticket(pnr: str) -> dict | None:
    sql = """
        SELECT pnr, booking_ref, passenger, flight_no, origin, destination,
               departure, fare, status, seat, baggage_kg, payment_ref, currency
        FROM airline_tickets WHERE pnr = %s
    """
    with get_db_cursor() as (cur, _):
        cur.execute(sql, (pnr,))
        row = cur.fetchone()
        return _row_to_api(row) if row else None


def _db_upsert_ticket(data: dict) -> tuple[dict, bool]:
    """
    Insert or update a ticket.  Always ensures the parent booking exists.
    Returns (ticket_dict, created:bool).
    """
    pnr = str(data.get("pnr", "")).upper()
    booking_ref = str(data.get("bookingRef", data.get("booking_ref", ""))).upper()

    if not pnr:
        # Generate a sequential PNR
        with get_db_cursor() as (cur, _):
            cur.execute("SELECT COUNT(*) AS cnt FROM airline_tickets")
            row = cur.fetchone()
            count = int(row["cnt"]) if row else 0
        pnr = f"MR{1001 + count}"
        data["pnr"] = pnr

    if not booking_ref:
        raise ValueError("bookingRef is required when creating a ticket.")

    # Enforce referential integrity at application layer too
    _ensure_booking_exists(booking_ref)

    status = str(data.get("status", "CONFIRMED")).upper()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    departure = data.get("departure", "2026-10-10 06:15")
    if hasattr(departure, "strftime"):
        departure = departure.strftime("%Y-%m-%d %H:%M")

    sql = """
        INSERT INTO airline_tickets (
            pnr, booking_ref, passenger, flight_no, origin, destination,
            departure, fare, status, seat, baggage_kg, payment_ref, currency
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (pnr) DO UPDATE SET
            booking_ref  = EXCLUDED.booking_ref,
            passenger    = EXCLUDED.passenger,
            flight_no    = EXCLUDED.flight_no,
            origin       = EXCLUDED.origin,
            destination  = EXCLUDED.destination,
            departure    = EXCLUDED.departure,
            fare         = EXCLUDED.fare,
            status       = EXCLUDED.status,
            seat         = COALESCE(EXCLUDED.seat, airline_tickets.seat),
            baggage_kg   = EXCLUDED.baggage_kg,
            payment_ref  = COALESCE(EXCLUDED.payment_ref, airline_tickets.payment_ref),
            currency     = EXCLUDED.currency,
            updated_at   = CURRENT_TIMESTAMP
        RETURNING pnr, (xmax = 0) AS inserted
    """
    params = (
        pnr,
        booking_ref,
        str(data.get("passenger", "")),
        str(data.get("flightNo", data.get("flight_no", ""))),
        str(data.get("origin", "")),
        str(data.get("destination", "")),
        str(departure),
        float(data.get("fare", 0)),
        status,
        data.get("seat", "—"),
        int(data.get("baggageKg", data.get("baggage_kg", 0))),
        data.get("paymentRef") or data.get("payment_ref"),
        str(data.get("currency", "INR")),
    )
    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)
        row = cur.fetchone()
        created = bool(row["inserted"]) if row else True

    ticket = _db_get_ticket(pnr)
    return ticket, created


def _db_update_ticket(pnr: str, data: dict) -> dict | None:
    existing = _db_get_ticket(pnr)
    if existing is None:
        return None

    merged = {**existing, **data}
    merged["pnr"] = pnr

    status = str(merged.get("status", "CONFIRMED")).upper()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    sql = """
        UPDATE airline_tickets SET
            passenger   = %s,
            flight_no   = %s,
            origin      = %s,
            destination = %s,
            departure   = %s,
            fare        = %s,
            status      = %s,
            seat        = %s,
            baggage_kg  = %s,
            payment_ref = %s,
            currency    = %s
        WHERE pnr = %s
    """
    params = (
        str(merged.get("passenger", "")),
        str(merged.get("flightNo", merged.get("flight_no", ""))),
        str(merged.get("origin", "")),
        str(merged.get("destination", "")),
        str(merged.get("departure", "")),
        float(merged.get("fare", 0)),
        status,
        merged.get("seat", "—"),
        int(merged.get("baggageKg", merged.get("baggage_kg", 0))),
        merged.get("paymentRef") or merged.get("payment_ref"),
        str(merged.get("currency", "INR")),
        pnr,
    )
    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)

    return _db_get_ticket(pnr)


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class AirlineAPIHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
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

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "airline"})
            return

        authorized, _ = check_admin_or_internal_from_handler(self)
        if not authorized:
            self.send_json(401, {"error": "Unauthorized. Active Admin session or internal authorization required."})
            return

        data = self._read_json_body()

        if path == "/api/airline/tickets":
            try:
                ticket, created = _db_upsert_ticket(data)
            except ValueError as err:
                self.send_json(400, {"error": str(err)})
                return
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if created:
                self.send_json(201, {"api": "API 3", "status": "created", "ticket": ticket})
            else:
                self.send_json(200, {"api": "API 3", "status": "updated", "ticket": ticket})
            return

        prefix = "/api/airline/tickets/"
        if path.startswith(prefix):
            pnr = path[len(prefix):].upper()
            try:
                updated = _db_update_ticket(pnr, data)
            except ValueError as err:
                self.send_json(400, {"error": str(err)})
                return
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if updated is None:
                self.send_json(404, {"error": "PNR not found"})
                return
            self.send_json(200, {"api": "API 4", "status": "updated", "ticket": updated})
            return

        self.send_json(404, {"error": "Unknown endpoint"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "airline"})
            return

        authorized, _ = check_admin_or_internal_from_handler(self)
        if not authorized:
            self.send_json(401, {"error": "Unauthorized. Active Admin session or internal authorization required."})
            return

        # API 3: list tickets
        if path == "/api/airline/tickets":
            status_filter = params.get("status", [None])[0]
            try:
                records = _db_list_tickets(status_filter)
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return
            self.send_json(200, {"api": "API 3", "count": len(records), "tickets": records})
            return

        # API 4: ticket detail
        prefix = "/api/airline/tickets/"
        if path.startswith(prefix):
            pnr = path[len(prefix):].upper()
            try:
                ticket = _db_get_ticket(pnr)
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if ticket is None:
                self.send_json(404, {"api": "API 4", "error": "PNR not found"})
                return
            base = round(ticket["fare"] * 0.82)
            taxes = ticket["fare"] - base
            self.send_json(200, {
                "api": "API 4",
                **ticket,
                "fareBreakdown": {"base": base, "taxes": taxes, "total": ticket["fare"]}
            })
            return

        self.send_json(404, {"error": "Unknown endpoint"})


if __name__ == "__main__":
    healthy, msg = check_db_health()
    if not healthy:
        print(f"FATAL: Cannot connect to database: {msg}")
        sys.exit(1)
    print(f"Database OK: {msg[:60]}")

    # Apply schema safely unless skipped by launcher
    if os.environ.get("SKIP_SCHEMA_INIT", "").lower() not in ("1", "true", "yes"):
        try:
            apply_schema()
        except Exception as err:
            print(f"FATAL: Schema initialization failed: {err}")
            sys.exit(1)

    server = HTTPServer((HOST, PORT), AirlineAPIHandler)
    print(f"Meridian Air API running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")

    def _shutdown(signum, frame):
        print(f"\nShutting down Meridian Air API (signal {signum}).")
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Meridian Air API.")
        server.server_close()
