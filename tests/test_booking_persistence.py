"""HTTP booking persistence tests. All writes are confined to TEST_DATABASE_URL."""
import json
import os
import threading
import uuid
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from db.connection import apply_schema, check_db_health, get_db_cursor
from booking_api_server import BookingAPIHandler

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/nimbus_test_db")
INTERNAL_KEY = os.environ.get("INTERNAL_SERVICE_SECRET", "nimbus_internal_secret_key_2026")


@pytest.fixture(scope="module")
def booking_http_server():
    if "test" not in TEST_DB_URL.lower():
        pytest.fail("Booking API tests require a dedicated TEST_DATABASE_URL.")
    healthy, error = check_db_health(TEST_DB_URL)
    assert healthy, f"Test PostgreSQL unavailable: {error}"
    apply_schema(TEST_DB_URL)
    os.environ["DATABASE_URL"] = TEST_DB_URL
    server = HTTPServer(("127.0.0.1", 0), BookingAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {"server": server, "thread": thread, "url": f"http://127.0.0.1:{server.server_port}"}
    yield state
    state["server"].shutdown()
    state["server"].server_close()
    state["thread"].join(timeout=2)


def _request(base, method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(base + path, data=body, method=method, headers={
        "Content-Type": "application/json",
        "X-Internal-Service-Key": INTERNAL_KEY,
    })
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_create_get_update_restart_and_duplicate_are_database_backed(booking_http_server):
    suffix = uuid.uuid4().hex[:10].upper()
    booking_ref = f"TEST-BK-{suffix}"
    email = f"pax-{suffix.lower()}@example.test"
    payload = {
        "bookingRef": booking_ref,
        "customer": "Persistence Test Passenger",
        "email": email,
        "flightNo": "MR201",
        "origin": "CCU",
        "destination": "DEL",
        "flightDate": "2026-11-19",
        "amount": 5200,
        "status": "CONFIRMED",
    }
    try:
        base = booking_http_server["url"]
        status, response = _request(base, "POST", "/api/booking/bookings", payload)
        assert status == 201
        assert response["booking"]["bookingRef"] == booking_ref
        assert response["booking"]["email"] == email
        with get_db_cursor(TEST_DB_URL, commit=False) as (cur, _):
            cur.execute("""SELECT b.booking_ref, b.user_id, b.status, u.email
                           FROM bookings b JOIN users u ON u.user_id = b.user_id
                           WHERE b.booking_ref = %s""", (booking_ref,))
            row = cur.fetchone()
        assert row and row["user_id"] and row["email"] == email

        status, listing = _request(base, "GET", "/api/booking/bookings")
        assert status == 200 and any(b["bookingRef"] == booking_ref for b in listing["bookings"])
        status, updated = _request(base, "POST", f"/api/booking/bookings/{booking_ref}", {"status": "CANCELLED"})
        assert status == 200 and updated["booking"]["status"] == "CANCELLED"

        # Simulate a backend restart: the new server process/handler reads the same PostgreSQL row.
        old_server, old_thread = booking_http_server["server"], booking_http_server["thread"]
        old_server.shutdown(); old_server.server_close(); old_thread.join(timeout=2)
        server = HTTPServer(("127.0.0.1", 0), BookingAPIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        booking_http_server.update(server=server, thread=thread, url=f"http://127.0.0.1:{server.server_port}")
        status, after_restart = _request(booking_http_server["url"], "GET", f"/api/booking/bookings/{booking_ref}")
        assert status == 200 and after_restart["status"] == "CANCELLED"

        status, duplicate = _request(booking_http_server["url"], "POST", "/api/booking/bookings", payload)
        assert status == 409 and "error" in duplicate
    finally:
        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("DELETE FROM bookings WHERE booking_ref = %s", (booking_ref,))
            cur.execute("DELETE FROM users WHERE email = %s", (email,))


def test_invalid_booking_does_not_create_customer_or_booking(booking_http_server):
    suffix = uuid.uuid4().hex[:10].upper()
    booking_ref = f"TEST-BK-{suffix}"
    email = f"bad-{suffix.lower()}@example.test"
    payload = {
        "bookingRef": booking_ref, "customer": "Invalid Passenger", "email": email,
        "flightNo": "MR201", "origin": "CCU", "destination": "DEL",
        "flightDate": "2026-11-19", "amount": -1, "status": "CONFIRMED",
    }
    status, _ = _request(booking_http_server["url"], "POST", "/api/booking/bookings", payload)
    assert status == 400
    with get_db_cursor(TEST_DB_URL, commit=False) as (cur, _):
        cur.execute("SELECT 1 FROM bookings WHERE booking_ref = %s", (booking_ref,))
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM users WHERE email = %s", (email,))
        assert cur.fetchone() is None
