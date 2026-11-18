"""
pytest configuration and session-wide fixtures for Nimbus-Hackathon.
Ensures PostgreSQL schema & seed are initialized and background API microservices
are running on ports 8001, 8002, 8003 for live HTTP test suites.
"""

import os
import sys
import time
import socket
import threading
from http.server import HTTPServer
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from db.connection import apply_schema, check_db_health, get_database_url
from db.seed import seed_bookings, seed_tickets, seed_payments, get_db_cursor
from booking_api_server import BookingAPIHandler
from airline_api_server import AirlineAPIHandler
from payment_api_server import PaymentAPIHandler


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="session", autouse=True)
def ensure_db_and_live_services():
    """
    Session fixture:
    1. Verifies database connectivity, applies schema, and seeds default development DB.
    2. Starts Booking (8001), Airline (8002), and Payment (8003) API servers
       in daemon threads if they are not already listening.
    """
    test_url = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/nimbus_test_db")
    dev_url = get_database_url()
    if "test" not in test_url.lower():
        pytest.fail("Automated tests must use a dedicated TEST_DATABASE_URL, never DATABASE_URL.")
    os.environ["DATABASE_URL"] = test_url
    healthy, msg = check_db_health(test_url)
    if healthy:
        apply_schema(test_url)
        with get_db_cursor(test_url) as (cur, _):
            seed_bookings(cur)
            seed_tickets(cur)
            seed_payments(cur)

    # Start API servers if not already listening
    servers = [
        (8001, BookingAPIHandler),
        (8002, AirlineAPIHandler),
        (8003, PaymentAPIHandler),
    ]

    active_servers = []
    for port, handler in servers:
        if not _is_port_open("127.0.0.1", port):
            try:
                srv = HTTPServer(("127.0.0.1", port), handler)
                t = threading.Thread(target=srv.serve_forever, daemon=True)
                t.start()
                active_servers.append(srv)
            except OSError:
                pass

    # Brief delay to allow sockets to bind and listen
    time.sleep(0.5)

    yield active_servers

    for srv in active_servers:
        srv.shutdown()
