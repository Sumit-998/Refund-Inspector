"""
Payment API Server — Port 8003 (Zenpay)
Backed by PostgreSQL via psycopg 3.
Preserves all existing API contracts (API 5 / API 6).
payments must reference an existing booking_ref in the bookings table.
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
PORT = int(os.environ.get("PAYMENT_PORT", 8003))
METHODS = ["UPI", "Credit Card", "Debit Card", "Net Banking"]
VALID_STATUSES = {"PENDING", "AUTHORIZED", "CONFIRMED", "REJECTED", "SUCCESS", "REFUNDED", "FAILED", "PROCESSING", "SETTLED"}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _row_to_api(row: dict) -> dict:
    """Convert DB row to original camelCase API shape."""
    return {
        "paymentRef":    row["payment_ref"],
        "bookingRef":    row["booking_ref"],
        "airlinePnr":    row.get("airline_pnr", ""),
        "payer":         row["payer_name"],
        "method":        row["method"],
        "amount":        float(row["amount"]),
        "currency":      row.get("currency", "INR"),
        "status":        row["status"],
        "paidAt":        row["paid_at"],
        "refundedAmount": float(row.get("refunded_amount") or 0),
        "gatewayTxnId":  row.get("gateway_txn_id", ""),
        "email":         row.get("email", ""),
        "authorizedAt":  row.get("authorized_at"),
        "decisionAt":    row.get("decision_at"),
    }


def _ensure_booking_exists(booking_ref: str) -> None:
    with get_db_cursor() as (cur, _):
        cur.execute("SELECT 1 FROM bookings WHERE booking_ref = %s", (booking_ref,))
        if not cur.fetchone():
            raise ValueError(
                f"Booking '{booking_ref}' does not exist. "
                "Create the booking first via the Booking API (port 8001)."
            )


def _db_count_payments() -> int:
    with get_db_cursor() as (cur, _):
        cur.execute("SELECT COUNT(*) AS cnt FROM payments")
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0


def _db_list_payments(status_filter: str | None) -> list[dict]:
    sql = """
        SELECT payment_ref, booking_ref, airline_pnr, payer_name, method,
               amount, currency, status, paid_at, refunded_amount,
               gateway_txn_id, email, authorized_at, decision_at
        FROM payments
    """
    params: tuple = ()
    if status_filter:
        sql += " WHERE status = %s"
        params = (status_filter.upper(),)
    sql += " ORDER BY created_at"
    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)
        return [_row_to_api(r) for r in cur.fetchall()]


def _db_get_payment(payment_ref: str) -> dict | None:
    sql = """
        SELECT payment_ref, booking_ref, airline_pnr, payer_name, method,
               amount, currency, status, paid_at, refunded_amount,
               gateway_txn_id, email, authorized_at, decision_at
        FROM payments WHERE payment_ref = %s
    """
    with get_db_cursor() as (cur, _):
        cur.execute(sql, (payment_ref,))
        row = cur.fetchone()
        return _row_to_api(row) if row else None


def _db_upsert_payment(data: dict) -> tuple[dict, bool]:
    pref = str(data.get("paymentRef", "")).upper()
    booking_ref = str(data.get("bookingRef", data.get("booking_ref", ""))).upper()

    if not pref:
        count = _db_count_payments()
        pref = f"PAY-{90000 + count + 1}"
        data["paymentRef"] = pref

    if not booking_ref:
        raise ValueError("bookingRef is required when creating a payment.")

    _ensure_booking_exists(booking_ref)

    status = str(data.get("status", "PENDING")).upper()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    amount = float(data.get("amount", 0))
    if amount < 0:
        raise ValueError("amount must not be negative.")

    refunded_amount = float(data.get("refundedAmount", data.get("refunded_amount", 0)) or 0)
    if refunded_amount < 0:
        raise ValueError("refundedAmount must not be negative.")
    if refunded_amount > amount:
        raise ValueError("refundedAmount cannot exceed amount.")

    # Derive method index from count for consistent assignment
    count = _db_count_payments()
    method = data.get("method") or METHODS[count % len(METHODS)]
    if method not in METHODS:
        raise ValueError(f"Invalid method '{method}'. Must be one of {METHODS}")

    gateway_txn_id = data.get("gatewayTxnId") or data.get("gateway_txn_id")

    sql = """
        INSERT INTO payments (
            payment_ref, booking_ref, airline_pnr, payer_name, method,
            amount, currency, status, paid_at, refunded_amount,
            gateway_txn_id, email, authorized_at, decision_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (payment_ref) DO NOTHING
        RETURNING payment_ref
    """
    params = (
        pref,
        booking_ref,
        data.get("airlinePnr") or data.get("airline_pnr"),
        str(data.get("payer", data.get("payer_name", ""))),
        method,
        amount,
        str(data.get("currency", "INR")),
        status,
        str(data.get("paidAt", data.get("paid_at", ""))),
        refunded_amount,
        gateway_txn_id,
        data.get("email"),
        data.get("authorizedAt"),
        data.get("decisionAt"),
    )
    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)
        row = cur.fetchone()
        created = bool(row)

    payment = _db_get_payment(pref)
    if payment and payment["bookingRef"] != booking_ref:
        raise ValueError(f"Payment reference '{pref}' is already assigned to another booking.")
    if payment and (payment["amount"] != amount or payment["currency"] != str(data.get("currency", "INR"))):
        raise ValueError(f"Payment reference '{pref}' already exists with different payment details.")
    return payment, created


def _db_transition_payment(payment_ref: str, action: str) -> dict | None:
    transitions = {
        "authorize": ("PENDING", "AUTHORIZED"),
        "confirm": ("AUTHORIZED", "CONFIRMED"),
        "reject": (("PENDING", "AUTHORIZED"), "REJECTED"),
    }
    if action not in transitions:
        raise ValueError("Unknown payment decision.")
    allowed_from, target = transitions[action]
    allowed_from = (allowed_from,) if isinstance(allowed_from, str) else allowed_from
    with get_db_cursor() as (cur, _):
        cur.execute("SELECT status FROM payments WHERE payment_ref = %s FOR UPDATE", (payment_ref,))
        row = cur.fetchone()
        if not row:
            return None
        current = row["status"]
        if current == target:
            return _db_get_payment(payment_ref)
        if current not in allowed_from:
            raise ValueError(f"Cannot {action} a payment in {current} state.")
        cur.execute("""
            UPDATE payments SET status = %s,
                authorized_at = CASE WHEN %s = 'AUTHORIZED' THEN CURRENT_TIMESTAMP ELSE authorized_at END,
                decision_at = CASE WHEN %s IN ('CONFIRMED', 'REJECTED') THEN CURRENT_TIMESTAMP ELSE decision_at END,
                paid_at = CASE WHEN %s = 'CONFIRMED' THEN to_char(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI') ELSE paid_at END
            WHERE payment_ref = %s
        """, (target, target, target, target, payment_ref))
    return _db_get_payment(payment_ref)


def _db_delete_payment(payment_ref: str) -> dict | None:
    with get_db_cursor() as (cur, _):
        cur.execute("""
            DELETE FROM payments WHERE payment_ref = %s
            RETURNING payment_ref, booking_ref, airline_pnr, payer_name, method,
                      amount, currency, status, paid_at, refunded_amount,
                      gateway_txn_id, email, authorized_at, decision_at
        """, (payment_ref,))
        row = cur.fetchone()
        return _row_to_api(row) if row else None


def _db_update_payment(payment_ref: str, data: dict) -> dict | None:
    existing = _db_get_payment(payment_ref)
    if existing is None:
        return None

    merged = {**existing, **data}
    merged["paymentRef"] = payment_ref

    status = str(merged.get("status", "PENDING")).upper()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    amount = float(merged.get("amount", 0))
    refunded_amount = float(merged.get("refundedAmount", merged.get("refunded_amount", 0)) or 0)
    if refunded_amount > amount:
        raise ValueError("refundedAmount cannot exceed amount.")

    sql = """
        UPDATE payments SET
            payer_name      = %s,
            method          = %s,
            amount          = %s,
            currency        = %s,
            status          = %s,
            paid_at         = %s,
            refunded_amount = %s,
            gateway_txn_id  = %s,
            email           = %s,
            authorized_at   = %s,
            decision_at     = %s
        WHERE payment_ref = %s
    """
    params = (
        str(merged.get("payer", merged.get("payer_name", ""))),
        str(merged.get("method", "UPI")),
        amount,
        str(merged.get("currency", "INR")),
        status,
        str(merged.get("paidAt", merged.get("paid_at", ""))),
        refunded_amount,
        merged.get("gatewayTxnId") or merged.get("gateway_txn_id"),
        merged.get("email"),
        merged.get("authorizedAt") or merged.get("authorized_at"),
        merged.get("decisionAt") or merged.get("decision_at"),
        payment_ref,
    )
    with get_db_cursor() as (cur, _):
        cur.execute(sql, params)

    return _db_get_payment(payment_ref)


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class PaymentAPIHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
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

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "payment"})
            return

        authorized, principal = check_admin_or_internal_from_handler(self)
        if not authorized:
            self.send_json(401, {"error": "Unauthorized. Active Admin session or internal authorization required."})
            return
        if principal and not principal.get("is_internal"):
            import secrets
            csrf = self.headers.get("X-CSRF-Token", "")
            if not csrf or not secrets.compare_digest(csrf, principal.get("csrf_token", "")):
                self.send_json(403, {"error": "Forbidden. Invalid or missing CSRF token."})
                return

        data = self._read_json_body()

        prefix = "/api/payment/transactions/"
        if path.startswith(prefix) and path.rsplit("/", 1)[-1] in {"authorize", "confirm", "reject"}:
            if not (principal or {}).get("is_internal"):
                self.send_json(401, {"error": "Lifecycle decisions must be coordinated by the Resolution Hub."})
                return
            payment_ref, action = path[len(prefix):].rsplit("/", 1)
            try:
                payment = _db_transition_payment(payment_ref.upper(), action)
            except ValueError as err:
                self.send_json(409, {"error": str(err)})
                return
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return
            if payment is None:
                self.send_json(404, {"error": "Payment not found"})
                return
            self.send_json(200, {"status": payment["status"].lower(), "transaction": payment})
            return

        if path == "/api/payment/transactions":
            try:
                payment, created = _db_upsert_payment(data)
            except ValueError as err:
                self.send_json(400, {"error": str(err)})
                return
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if created:
                self.send_json(201, {"api": "API 5", "status": "created", "transaction": payment})
            else:
                self.send_json(200, {"api": "API 5", "status": "updated", "transaction": payment})
            return

        prefix = "/api/payment/transactions/"
        if path.startswith(prefix):
            requested_status = str(data.get("status", "")).upper()
            if requested_status in {"AUTHORIZED", "CONFIRMED", "REJECTED"} and not (principal or {}).get("is_internal"):
                self.send_json(403, {"error": "Payment lifecycle decisions must be made through the Resolution Hub."})
                return
            pref = path[len(prefix):].upper()
            try:
                updated = _db_update_payment(pref, data)
            except ValueError as err:
                self.send_json(400, {"error": str(err)})
                return
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if updated is None:
                self.send_json(404, {"error": "Payment not found"})
                return
            self.send_json(200, {"api": "API 6", "status": "updated", "transaction": updated})
            return

        self.send_json(404, {"error": "Unknown endpoint"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        authorized, principal = check_admin_or_internal_from_handler(self)
        if not authorized or not (principal or {}).get("is_internal"):
            self.send_json(401, {"error": "Internal service authorization required."})
            return
        prefix = "/api/payment/transactions/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "Unknown endpoint"})
            return
        try:
            payment = _db_delete_payment(path[len(prefix):].upper())
        except Exception as err:
            self.send_json(500, {"error": f"Database error: {err}"})
            return
        if payment is None:
            self.send_json(404, {"error": "Payment not found"})
            return
        self.send_json(200, {"status": "deleted", "transaction": payment})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "payment"})
            return

        authorized, _ = check_admin_or_internal_from_handler(self)
        if not authorized:
            self.send_json(401, {"error": "Unauthorized. Active Admin session or internal authorization required."})
            return

        # API 5: list transactions
        if path == "/api/payment/transactions":
            status_filter = params.get("status", [None])[0]
            try:
                records = _db_list_payments(status_filter)
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return
            self.send_json(200, {
                "api": "API 5", "count": len(records), "transactions": records
            })
            return

        # API 6: transaction detail
        prefix = "/api/payment/transactions/"
        if path.startswith(prefix):
            payment_ref = path[len(prefix):].upper()
            try:
                txn = _db_get_payment(payment_ref)
            except Exception as err:
                self.send_json(500, {"error": f"Database error: {err}"})
                return

            if txn is None:
                self.send_json(404, {"api": "API 6", "error": "Payment not found"})
                return

            fee = 0 if txn["status"] == "FAILED" else round(txn["amount"] * 0.02)
            net = (
                txn["amount"] - fee if txn["status"] == "SUCCESS"
                else -fee if txn["status"] == "REFUNDED"
                else 0
            )
            self.send_json(200, {
                "api": "API 6", **txn,
                "gatewayFee": fee, "netSettlement": net
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

    server = HTTPServer((HOST, PORT), PaymentAPIHandler)
    print(f"Zenpay API running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")

    def _shutdown(signum, frame):
        print(f"\nShutting down Zenpay API (signal {signum}).")
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Zenpay API.")
        server.server_close()
