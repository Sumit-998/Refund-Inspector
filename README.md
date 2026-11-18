# Nimbus-Hackathon

Nimbus reconciles PostgreSQL backed booking, airline, and payment records through four local services. The Admin Resolution Hub uses database backed sessions and Argon2 password hashes; state changing Admin requests require CSRF tokens.

## Services and URLs

| Service | Port | URL |
| --- | ---: | --- |
| Resolution Hub and Admin UI | 8000 | http://127.0.0.1:8000/ |
| Booking API | 8001 | http://127.0.0.1:8001 |
| Airline API | 8002 | http://127.0.0.1:8002 |
| Payment API | 8003 | http://127.0.0.1:8003 |

Login page: http://127.0.0.1:8000/login.html. Admin credentials are configured with `ADMIN_EMAIL` and `ADMIN_PASSWORD` before first startup. Customer contact details remain in the `users` and `bookings` tables.

## PostgreSQL and startup

Set `DATABASE_URL` to the development database and `TEST_DATABASE_URL` to a separate test database. Defaults are `postgresql://postgres@localhost:5432/nimbus_db` and `postgresql://postgres@localhost:5432/nimbus_test_db`.

```bash
sudo systemctl start postgresql
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python run.py
```

The launcher applies idempotent schema migrations without resetting existing tables, bootstraps the Admin, and starts all four services. Development seed records are added idempotently; set `--no-seed` to avoid loading them.

## Booking lifecycle

Create a passenger booking from `/booking.html` or the Admin dashboard. The browser submits to the authenticated Resolution Hub, which coordinates the Booking, Airline, and Payment APIs. It creates the customer/user and booking in one Booking API transaction, then creates an airline ticket and payment with deterministic references. All three rows must read back successfully before the Hub reports success. Repeating a request with the same booking reference and data is idempotent; conflicting data for that reference returns HTTP 409. A partial create is compensated through the owning service APIs.

The Admin dashboard reads joined records from the three APIs after loading and refresh. It displays booking, airline, and payment states and offers only valid payment actions. State transitions are:

- Airline: `PENDING` → `AUTHORIZED` → `CONFIRMED`, or `PENDING`/`AUTHORIZED` → `REJECTED`.
- Payment: `PENDING` → `AUTHORIZED` → `CONFIRMED`, or `PENDING`/`AUTHORIZED` → `REJECTED`.
- Booking: remains `PENDING` during authorization, then moves to `CONFIRMED` or `FAILED` on the final decision.

Authorization is a separate step and does not finalize payment. Confirm/reject and deletion are coordinated by the Hub using service APIs and compensating operations if a downstream request fails. Deletion removes booking, airline, and payment rows; evidence/refund history and customer accounts are retained, with their booking/payment foreign keys set to NULL by the existing schema.

Migration `004_booking_payment_lifecycle.sql` expands the existing airline/payment status checks and adds payment authorization/decision timestamps. It preserves existing rows and legacy status values, and startup applies it idempotently.

## API routes

- Booking service: `GET /api/booking/bookings`, `GET /api/booking/bookings/{bookingRef}`, `POST /api/booking/bookings`, `POST /api/booking/bookings/{bookingRef}`, internal `DELETE /api/booking/bookings/{bookingRef}`.
- Airline service: `GET /api/airline/tickets`, ticket detail/create/update routes under `/api/airline/tickets`, internal ticket `DELETE`.
- Payment service: `GET /api/payment/transactions`, transaction detail/create/update routes under `/api/payment/transactions`, internal `POST /{paymentRef}/authorize|confirm|reject`, internal transaction `DELETE`.
- Resolution Hub: `/auth/login`, `/auth/me`, `/auth/logout`, `/api/status`, `/api/reconcile/*`, `GET/POST /api/bookings`, `GET/DELETE /api/bookings/{bookingRef}`, and `POST /api/bookings/{bookingRef}/payment/{authorize|confirm|reject}`.

Service APIs require a valid Admin session or `INTERNAL_SERVICE_SECRET`; writes to lifecycle records are restricted to the Hub's internal service calls. Hub Admin writes require the existing session and CSRF validation.

## Tests

Run the suite against the dedicated PostgreSQL test database:

```bash
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/nimbus_test_db PYTHONPATH=. .venv/bin/pytest -v
```

Do not point automated tests at the development database. Existing tests and the booking persistence tests preserve test DB records except their uniquely prefixed test rows.
