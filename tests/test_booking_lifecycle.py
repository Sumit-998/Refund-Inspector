"""Cross-service booking lifecycle tests; all rows are isolated in TEST_DATABASE_URL."""
import os
import threading
import uuid
from urllib.request import Request, urlopen
from unittest.mock import patch
from http.server import HTTPServer

import pytest
from fastapi.testclient import TestClient

from core.fastapi_app import app
import core.booking_lifecycle as lifecycle
from db.connection import get_db_cursor
from booking_api_server import BookingAPIHandler
from airline_api_server import AirlineAPIHandler
from payment_api_server import PaymentAPIHandler

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/nimbus_test_db")
ADMIN_EMAIL = "admin@nimbus.local"
ADMIN_PASSWORD = "NimbusAdmin2026!Secure"


@pytest.fixture
def admin_client():
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    csrf = client.cookies.get("csrf_token")
    return client, {"X-CSRF-Token": csrf}


def _payload():
    suffix = uuid.uuid4().hex[:10].upper()
    return {
        "bookingRef": f"TEST-BK-{suffix}",
        "customer": "Lifecycle Passenger",
        "email": f"lifecycle-{suffix.lower()}@example.test",
        "flightNo": "MR201",
        "origin": "CCU",
        "destination": "DEL",
        "flightDate": "2026-12-10",
        "amount": 5150.0,
        "currency": "INR",
        "paymentMethod": "UPI",
    }


def _counts(booking_ref):
    with get_db_cursor(TEST_DB_URL, commit=False) as (cur, _):
        cur.execute("SELECT COUNT(*) AS n FROM bookings WHERE booking_ref=%s", (booking_ref,))
        bookings = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM airline_tickets WHERE booking_ref=%s", (booking_ref,))
        tickets = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM payments WHERE booking_ref=%s", (booking_ref,))
        payments = cur.fetchone()["n"]
    return bookings, tickets, payments


def _cleanup(booking_ref, email):
    with get_db_cursor(TEST_DB_URL) as (cur, _):
        cur.execute("DELETE FROM refund_cases WHERE case_identifier LIKE %s", (f"TEST-LC-{booking_ref}%",))
        cur.execute("DELETE FROM evidence_records WHERE external_reference = %s", (booking_ref,))
        cur.execute("DELETE FROM payments WHERE booking_ref = %s", (booking_ref,))
        cur.execute("DELETE FROM airline_tickets WHERE booking_ref = %s", (booking_ref,))
        cur.execute("DELETE FROM bookings WHERE booking_ref = %s", (booking_ref,))
        cur.execute("DELETE FROM users WHERE email = %s", (email,))


def _create(client, headers, payload):
    response = client.post("/api/bookings", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_create_sync_decide_duplicate_and_restart_states(admin_client):
    client, headers = admin_client
    payload = _payload()
    ref = payload["bookingRef"]
    try:
        created = _create(client, headers, payload)
        assert created["bookingRef"] == ref
        assert created["status"] == "PENDING"
        assert created["airlineStatus"] == "PENDING"
        assert created["paymentStatus"] == "PENDING"
        assert created["payment"]["amount"] == payload["amount"]
        assert _counts(ref) == (1, 1, 1)
        ticket_list = lifecycle._call(lifecycle.AIRLINE_API_URL, "/api/airline/tickets")[1]["tickets"]
        payment_list = lifecycle._call(lifecycle.PAYMENT_API_URL, "/api/payment/transactions")[1]["transactions"]
        assert [row for row in ticket_list if row["bookingRef"] == ref] == [created["airline"]]
        assert [row for row in payment_list if row["bookingRef"] == ref] == [created["payment"]]

        # Retrying the same create is idempotent and preserves the current rows.
        duplicate = _create(client, headers, payload)
        assert duplicate["paymentRef"] == created["paymentRef"]
        assert _counts(ref) == (1, 1, 1)

        authorized = client.post(f"/api/bookings/{ref}/payment/authorize", headers=headers)
        assert authorized.status_code == 200, authorized.text
        assert authorized.json()["paymentStatus"] == "AUTHORIZED"
        assert authorized.json()["airlineStatus"] == "AUTHORIZED"
        assert authorized.json()["status"] == "PENDING"

        confirmed = client.post(f"/api/bookings/{ref}/payment/confirm", headers=headers)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["paymentStatus"] == "CONFIRMED"
        assert confirmed.json()["airlineStatus"] == "CONFIRMED"
        assert confirmed.json()["status"] == "CONFIRMED"
        invalid_reject = client.post(f"/api/bookings/{ref}/payment/reject", headers=headers)
        assert invalid_reject.status_code == 409

        # A fresh list read after the service processes have already committed is the refresh/restart source.
        saved = client.get("/api/bookings")
        assert saved.status_code == 200
        row = next(item for item in saved.json()["bookings"] if item["bookingRef"] == ref)
        assert (row["paymentStatus"], row["airlineStatus"], row["status"]) == ("CONFIRMED", "CONFIRMED", "CONFIRMED")
    finally:
        _cleanup(ref, payload["email"])


def test_rejection_blocks_confirmation_and_delete_preserves_history(admin_client):
    client, headers = admin_client
    payload = _payload()
    ref = payload["bookingRef"]
    try:
        _create(client, headers, payload)
        rejected = client.post(f"/api/bookings/{ref}/payment/reject", headers=headers)
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["paymentStatus"] == "REJECTED"
        assert rejected.json()["airlineStatus"] == "REJECTED"
        assert rejected.json()["status"] == "FAILED"
        assert client.post(f"/api/bookings/{ref}/payment/confirm", headers=headers).status_code == 409

        with get_db_cursor(TEST_DB_URL) as (cur, _):
            cur.execute("""INSERT INTO evidence_records (booking_ref, payment_ref, source_type, external_reference, evidence_type)
                           VALUES (%s, %s, 'test', %s, 'booking_snapshot')""", (ref, rejected.json()["paymentRef"], ref))
            cur.execute("""INSERT INTO refund_cases (case_identifier, booking_ref, payment_ref, case_status)
                           VALUES (%s, %s, %s, 'PENDING')""", (f"TEST-LC-{ref}", ref, rejected.json()["paymentRef"]))

        deleted = client.delete(f"/api/bookings/{ref}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert _counts(ref) == (0, 0, 0)
        ticket_list = lifecycle._call(lifecycle.AIRLINE_API_URL, "/api/airline/tickets")[1]["tickets"]
        payment_list = lifecycle._call(lifecycle.PAYMENT_API_URL, "/api/payment/transactions")[1]["transactions"]
        assert not any(row["bookingRef"] == ref for row in ticket_list)
        assert not any(row["bookingRef"] == ref for row in payment_list)
        with get_db_cursor(TEST_DB_URL, commit=False) as (cur, _):
            cur.execute("SELECT booking_ref, payment_ref FROM evidence_records WHERE external_reference=%s", (ref,))
            evidence = cur.fetchone()
            cur.execute("SELECT booking_ref, payment_ref FROM refund_cases WHERE case_identifier=%s", (f"TEST-LC-{ref}",))
            refund = cur.fetchone()
        assert evidence and evidence["booking_ref"] is None and evidence["payment_ref"] is None
        assert refund and refund["booking_ref"] is None and refund["payment_ref"] is None
    finally:
        _cleanup(ref, payload["email"])


def test_failed_delete_restores_previously_deleted_ticket(admin_client):
    client, headers = admin_client
    payload = _payload()
    ref = payload["bookingRef"]
    original_call = lifecycle._call
    try:
        _create(client, headers, payload)

        def fail_payment_delete(base, path, method="GET", payload=None):
            if base == lifecycle.PAYMENT_API_URL and method == "DELETE":
                raise lifecycle.LifecycleError(503, "simulated payment service failure")
            return original_call(base, path, method, payload)

        with patch.object(lifecycle, "_call", side_effect=fail_payment_delete):
            response = client.delete(f"/api/bookings/{ref}", headers=headers)
        assert response.status_code == 503
        assert _counts(ref) == (1, 1, 1)
    finally:
        _cleanup(ref, payload["email"])


def test_booking_lifecycle_survives_service_restarts(admin_client, ensure_db_and_live_services):
    client, headers = admin_client
    active_servers = ensure_db_and_live_services
    payload = _payload()
    ref = payload["bookingRef"]
    handlers = [BookingAPIHandler, AirlineAPIHandler, PaymentAPIHandler]
    ports = [8001, 8002, 8003]
    try:
        created = _create(client, headers, payload)
        assert _counts(ref) == (1, 1, 1)
        if len(active_servers) != 3:
            pytest.skip("Restart verification needs all three API servers to be owned by the test fixture.")

        # Restart each HTTP service process boundary while retaining the PostgreSQL DB.
        for index, (port, handler) in enumerate(zip(ports, handlers)):
            active_servers[index].shutdown()
            active_servers[index].server_close()
            restarted = HTTPServer(("127.0.0.1", port), handler)
            thread = threading.Thread(target=restarted.serve_forever, daemon=True)
            thread.start()
            active_servers[index] = restarted

        saved = client.get("/api/bookings")
        assert saved.status_code == 200
        row = next(item for item in saved.json()["bookings"] if item["bookingRef"] == ref)
        assert (row["airlineStatus"], row["paymentStatus"], row["status"]) == (
            created["airlineStatus"], created["paymentStatus"], created["status"]
        )
        assert _counts(ref) == (1, 1, 1)
    finally:
        _cleanup(ref, payload["email"])


def test_lifecycle_routes_require_admin_and_csrf():
    client = TestClient(app, raise_server_exceptions=False)
    payload = _payload()
    assert client.post("/api/bookings", json=payload).status_code == 401
    authenticated = TestClient(app, raise_server_exceptions=False)
    assert authenticated.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 200
    # Remove the CSRF cookie as well as the header; the existing verifier accepts
    # either source for same-origin requests.
    authenticated.cookies.delete("csrf_token")
    assert authenticated.post("/api/bookings", json=payload).status_code == 403


def test_airline_and_payment_frontends_use_live_service_apis():
    root = os.path.dirname(os.path.dirname(__file__))
    airline = open(os.path.join(root, "frontend", "airline.html"), encoding="utf-8").read()
    payment = open(os.path.join(root, "frontend", "payment.html"), encoding="utf-8").read()
    assert '"/api/airline/tickets"' in airline
    assert '"/api/payment/transactions"' in payment
    assert "credentials: \"include\"" in airline and "credentials: \"include\"" in payment
    assert 'let TICKETS = []' in airline and 'let TRANSACTIONS = []' in payment
    assert "using local memory state" not in airline + payment
    assert '"MR1001"' not in airline and '"PAY-90001"' not in payment
    assert "authorizedAt" in payment and "decisionAt" in payment


def test_service_apis_allow_authenticated_dashboard_origin(admin_client):
    client, _ = admin_client
    cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
    origin = "http://127.0.0.1:8000"
    for url in (
        "http://127.0.0.1:8002/api/airline/tickets",
        "http://127.0.0.1:8003/api/payment/transactions",
    ):
        request = Request(url, headers={"Origin": origin, "Cookie": cookie})
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.headers.get("Access-Control-Allow-Origin") == origin
            assert response.headers.get("Access-Control-Allow-Credentials") == "true"
