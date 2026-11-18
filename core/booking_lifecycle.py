"""Resolution Hub saga for booking, airline, and payment service operations."""
from contextlib import contextmanager
import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from core.auth import INTERNAL_SERVICE_SECRET
from core.integration import BOOKING_API_URL, AIRLINE_API_URL, PAYMENT_API_URL
from db.connection import get_connection


class LifecycleError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def _call(base: str, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        base + path,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Service-Key": INTERNAL_SERVICE_SECRET,
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
            return response.status, data
    except HTTPError as err:
        try:
            data = json.loads(err.read().decode("utf-8"))
        except Exception:
            data = {}
        raise LifecycleError(err.code, data.get("error") or data.get("detail") or f"Service request failed ({err.code}).") from err
    except (URLError, TimeoutError, OSError) as err:
        raise LifecycleError(503, f"Service at {base} is unavailable: {err}") from err


def _get_optional(base: str, path: str) -> dict | None:
    try:
        return _call(base, path)[1]
    except LifecycleError as err:
        if err.status_code == 404:
            return None
        raise


@contextmanager
def _booking_lock(booking_ref: str):
    """Serialize Hub lifecycle operations for a booking across concurrent requests."""
    lock_key = f"nimbus-booking:{booking_ref.upper()}"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 8000))", (lock_key,))
        conn.commit()
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 8000))", (lock_key,))
            conn.commit()
        finally:
            conn.close()


def _service_refs(booking_ref: str) -> tuple[str, str]:
    digest = hashlib.sha256(booking_ref.upper().encode()).hexdigest()[:18].upper()
    return f"MR-{digest}", f"PAY-{digest}"


def _record_exists(base: str, path: str) -> dict | None:
    return _get_optional(base, path)


def _same_booking(existing: dict, requested: dict) -> bool:
    fields = ("customer", "email", "flightNo", "origin", "destination", "flightDate", "amount", "currency")
    for field in fields:
        left, right = existing.get(field), requested.get(field)
        if field == "amount":
            try:
                if round(float(left), 2) != round(float(right), 2):
                    return False
            except (TypeError, ValueError):
                return False
        elif str(left or "").strip().lower() != str(right or "").strip().lower():
            return False
    return True


def _aggregate(booking: dict, tickets: list[dict], payments: list[dict]) -> dict:
    ref = booking["bookingRef"]
    ticket = next((t for t in tickets if t.get("bookingRef") == ref and (not booking.get("airlinePnr") or t.get("pnr") == booking["airlinePnr"])), None)
    payment = next((p for p in payments if p.get("bookingRef") == ref and (not booking.get("paymentRef") or p.get("paymentRef") == booking["paymentRef"])), None)
    return {
        **booking,
        "airline": ticket,
        "airlineStatus": ticket.get("status", "MISSING") if ticket else "MISSING",
        "payment": payment,
        "paymentRef": payment.get("paymentRef") if payment else booking.get("paymentRef", ""),
        "paymentAmount": payment.get("amount") if payment else None,
        "paymentStatus": payment.get("status", "MISSING") if payment else "MISSING",
    }


def list_booking_lifecycle() -> list[dict]:
    try:
        bookings = _call(BOOKING_API_URL, "/api/booking/bookings")[1]["bookings"]
        tickets = _call(AIRLINE_API_URL, "/api/airline/tickets")[1]["tickets"]
        payments = _call(PAYMENT_API_URL, "/api/payment/transactions")[1]["transactions"]
    except LifecycleError as err:
        raise LifecycleError(503, f"Could not load a consistent booking, airline, and payment view: {err}") from err
    return [_aggregate(b, tickets, payments) for b in bookings]


def create_booking_lifecycle(data: dict) -> dict:
    booking_ref = str(data.get("bookingRef") or "").strip().upper()
    if not booking_ref:
        booking_ref = f"BK-{hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16].upper()}"
    pnr, payment_ref = _service_refs(booking_ref)
    with _booking_lock(booking_ref):
        booking_path = f"/api/booking/bookings/{quote(booking_ref)}"
        airline_path = f"/api/airline/tickets/{quote(pnr)}"
        payment_path = f"/api/payment/transactions/{quote(payment_ref)}"
        initial_booking = _record_exists(BOOKING_API_URL, booking_path)
        initial_ticket = _record_exists(AIRLINE_API_URL, airline_path)
        initial_payment = _record_exists(PAYMENT_API_URL, payment_path)

        booking_data = {
            **data,
            "bookingRef": booking_ref,
            "airlinePnr": pnr,
            "paymentRef": payment_ref,
            "status": "PENDING",
        }
        if initial_booking and not _same_booking(initial_booking, booking_data):
            raise LifecycleError(409, f"Booking reference {booking_ref} already exists with different booking data.")

        try:
            if not initial_booking:
                _call(BOOKING_API_URL, "/api/booking/bookings", "POST", booking_data)
                initial_booking = None
            else:
                booking_data = initial_booking

            ticket_data = {
                "pnr": pnr,
                "bookingRef": booking_ref,
                "passenger": data["customer"],
                "flightNo": data["flightNo"],
                "origin": data["origin"],
                "destination": data["destination"],
                "departure": data["flightDate"],
                "fare": data["amount"],
                "status": "PENDING",
                "paymentRef": payment_ref,
                "currency": data.get("currency", "INR"),
            }
            if initial_ticket:
                same_ticket = (
                    initial_ticket.get("bookingRef") == booking_ref
                    and initial_ticket.get("passenger") == data["customer"]
                    and initial_ticket.get("flightNo") == data["flightNo"]
                    and initial_ticket.get("origin") == data["origin"]
                    and initial_ticket.get("destination") == data["destination"]
                    and round(float(initial_ticket.get("fare", 0)), 2) == round(float(data["amount"]), 2)
                )
                if not same_ticket:
                    raise LifecycleError(409, f"Airline reference {pnr} already exists with different ticket data.")
            else:
                _call(AIRLINE_API_URL, "/api/airline/tickets", "POST", ticket_data)

            payment_data = {
                "paymentRef": payment_ref,
                "bookingRef": booking_ref,
                "airlinePnr": pnr,
                "payer": data["customer"],
                "method": data.get("paymentMethod", "UPI"),
                "amount": data["amount"],
                "currency": data.get("currency", "INR"),
                "status": "PENDING",
                "paidAt": "",
                "email": data.get("email"),
            }
            if initial_payment:
                if initial_payment.get("bookingRef") != booking_ref or round(float(initial_payment.get("amount", 0)), 2) != round(float(data["amount"]), 2):
                    raise LifecycleError(409, f"Payment reference {payment_ref} already exists with different payment data.")
            else:
                _call(PAYMENT_API_URL, "/api/payment/transactions", "POST", payment_data)
        except Exception as err:
            compensation_errors = []
            # Reverse only records that were absent before this request. This also
            # handles a timeout after a downstream INSERT committed.
            if not initial_payment:
                try:
                    _call(PAYMENT_API_URL, payment_path, "DELETE")
                except LifecycleError as cleanup_err:
                    if cleanup_err.status_code != 404:
                        compensation_errors.append(f"payment cleanup: {cleanup_err}")
            if not initial_ticket:
                try:
                    _call(AIRLINE_API_URL, airline_path, "DELETE")
                except LifecycleError as cleanup_err:
                    if cleanup_err.status_code != 404:
                        compensation_errors.append(f"airline cleanup: {cleanup_err}")
            if initial_booking is None:
                try:
                    _call(BOOKING_API_URL, booking_path, "DELETE")
                except LifecycleError as cleanup_err:
                    if cleanup_err.status_code != 404:
                        compensation_errors.append(f"booking cleanup: {cleanup_err}")
            if compensation_errors:
                raise LifecycleError(503, f"Booking synchronization failed ({err}); compensation incomplete: {'; '.join(compensation_errors)}") from err
            if isinstance(err, LifecycleError):
                raise LifecycleError(err.status_code, f"Booking synchronization failed and was rolled back: {err}") from err
            raise LifecycleError(503, f"Booking synchronization failed and was rolled back: {err}") from err

        rows = list_booking_lifecycle()
        saved = next((row for row in rows if row["bookingRef"] == booking_ref), None)
        if not saved or saved["airlineStatus"] == "MISSING" or saved["paymentStatus"] == "MISSING":
            raise LifecycleError(503, "Booking was created but synchronized records could not be read back from all services.")
        return saved


def _restore_booking_status(booking_ref: str, status: str):
    _call(BOOKING_API_URL, f"/api/booking/bookings/{quote(booking_ref)}", "POST", {"status": status})


def _restore_airline_status(pnr: str, status: str):
    ticket = _call(AIRLINE_API_URL, f"/api/airline/tickets/{quote(pnr)}")[1]
    _call(AIRLINE_API_URL, f"/api/airline/tickets/{quote(pnr)}", "POST", {**ticket, "status": status})


def change_payment_lifecycle(booking_ref: str, action: str) -> dict:
    if action not in {"authorize", "confirm", "reject"}:
        raise LifecycleError(404, "Unknown payment action.")
    booking_ref = booking_ref.upper()
    with _booking_lock(booking_ref):
        rows = list_booking_lifecycle()
        current = next((row for row in rows if row["bookingRef"] == booking_ref), None)
        if not current:
            raise LifecycleError(404, "Booking not found.")
        payment = current["payment"]
        ticket = current["airline"]
        if not payment or not ticket:
            raise LifecycleError(409, "Payment and airline records are required before a payment decision.")
        pstatus = payment["status"]
        expected = {"authorize": {"PENDING", "AUTHORIZED"}, "confirm": {"AUTHORIZED", "CONFIRMED"}, "reject": {"PENDING", "AUTHORIZED", "REJECTED"}}[action]
        if pstatus not in expected:
            raise LifecycleError(409, f"Cannot {action} payment in {pstatus} state.")
        old_booking_status = current["status"]
        old_airline_status = ticket["status"]
        target_booking = {"authorize": old_booking_status, "confirm": "CONFIRMED", "reject": "FAILED"}[action]
        target_airline = {"authorize": "AUTHORIZED", "confirm": "CONFIRMED", "reject": "REJECTED"}[action]
        target_payment = {"authorize": "AUTHORIZED", "confirm": "CONFIRMED", "reject": "REJECTED"}[action]

        changed_booking = changed_ticket = False
        try:
            if old_booking_status != target_booking:
                _restore_booking_status(booking_ref, target_booking)
                changed_booking = True
            if old_airline_status != target_airline:
                _restore_airline_status(ticket["pnr"], target_airline)
                changed_ticket = True
            _call(PAYMENT_API_URL, f"/api/payment/transactions/{quote(payment['paymentRef'])}/{action}", "POST", {})
        except Exception as err:
            compensation_errors = []
            try:
                latest_payment = _get_optional(PAYMENT_API_URL, f"/api/payment/transactions/{quote(payment['paymentRef'])}")
                if latest_payment and latest_payment.get("status") != pstatus:
                    _call(PAYMENT_API_URL, f"/api/payment/transactions/{quote(payment['paymentRef'])}", "POST", {**latest_payment, "status": pstatus})
            except Exception as cleanup_err:
                compensation_errors.append(f"payment status restore: {cleanup_err}")
            if changed_ticket:
                try:
                    _restore_airline_status(ticket["pnr"], old_airline_status)
                except Exception as cleanup_err:
                    compensation_errors.append(f"airline status restore: {cleanup_err}")
            if changed_booking:
                try:
                    _restore_booking_status(booking_ref, old_booking_status)
                except Exception as cleanup_err:
                    compensation_errors.append(f"booking status restore: {cleanup_err}")
            if compensation_errors:
                raise LifecycleError(503, f"Payment decision failed ({err}); status compensation incomplete: {'; '.join(compensation_errors)}") from err
            if isinstance(err, LifecycleError):
                raise LifecycleError(err.status_code, f"Payment decision failed; related status changes were rolled back: {err}") from err
            raise LifecycleError(503, f"Payment decision failed; related status changes were rolled back: {err}") from err

        refreshed = list_booking_lifecycle()
        result = next((row for row in refreshed if row["bookingRef"] == booking_ref), None)
        if not result or result["paymentStatus"] != target_payment:
            raise LifecycleError(503, "Payment decision did not persist consistently across services.")
        return result


def delete_booking_lifecycle(booking_ref: str) -> dict:
    booking_ref = booking_ref.upper()
    with _booking_lock(booking_ref):
        rows = list_booking_lifecycle()
        current = next((row for row in rows if row["bookingRef"] == booking_ref), None)
        if not current:
            raise LifecycleError(404, "Booking not found.")
        booking = {key: value for key, value in current.items() if key not in {"airline", "airlineStatus", "payment", "paymentAmount", "paymentStatus"}}
        # Capture every dependent row, including legacy bookings with multiple
        # airline records or payment attempts.
        tickets = [t for t in _call(AIRLINE_API_URL, "/api/airline/tickets")[1]["tickets"] if t.get("bookingRef") == booking_ref]
        payments = [p for p in _call(PAYMENT_API_URL, "/api/payment/transactions")[1]["transactions"] if p.get("bookingRef") == booking_ref]
        try:
            for ticket in tickets:
                _call(AIRLINE_API_URL, f"/api/airline/tickets/{quote(ticket['pnr'])}", "DELETE")
            for payment in payments:
                _call(PAYMENT_API_URL, f"/api/payment/transactions/{quote(payment['paymentRef'])}", "DELETE")
            _call(BOOKING_API_URL, f"/api/booking/bookings/{quote(booking_ref)}", "DELETE")
        except Exception as err:
            compensation_errors = []
            # Restore the parent first if a response was lost after its DELETE committed.
            try:
                if _get_optional(BOOKING_API_URL, f"/api/booking/bookings/{quote(booking_ref)}") is None:
                    _call(BOOKING_API_URL, "/api/booking/bookings", "POST", booking)
            except Exception as cleanup_err:
                compensation_errors.append(f"booking restore: {cleanup_err}")
            for ticket in tickets:
                try:
                    if _get_optional(AIRLINE_API_URL, f"/api/airline/tickets/{quote(ticket['pnr'])}") is None:
                        _call(AIRLINE_API_URL, "/api/airline/tickets", "POST", ticket)
                except Exception as cleanup_err:
                    compensation_errors.append(f"airline {ticket['pnr']} restore: {cleanup_err}")
            for payment in payments:
                try:
                    if _get_optional(PAYMENT_API_URL, f"/api/payment/transactions/{quote(payment['paymentRef'])}") is None:
                        _call(PAYMENT_API_URL, "/api/payment/transactions", "POST", payment)
                except Exception as cleanup_err:
                    compensation_errors.append(f"payment {payment['paymentRef']} restore: {cleanup_err}")
            if compensation_errors:
                raise LifecycleError(503, f"Booking deletion failed ({err}); restore incomplete: {'; '.join(compensation_errors)}") from err
            if isinstance(err, LifecycleError):
                raise LifecycleError(err.status_code, f"Booking deletion failed; prior deletes were restored: {err}") from err
            raise LifecycleError(503, f"Booking deletion failed; prior deletes were restored: {err}") from err

        return {"bookingRef": booking_ref, "deleted": True}
