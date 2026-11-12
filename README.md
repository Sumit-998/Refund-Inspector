# Nimbus-Hackathon — Multi-Platform Travel Refund Resolution & Admin Hub

Nimbus is an automated cross-platform travel refund reconciliation engine. It connects disparate source systems across booking platforms, airlines, payment gateways, and banking networks to deterministically resolve refund states, detect SLA breaches, identify operational blockers, and assign case ownership.

This repository integrates:
1. **Production-Ready PostgreSQL DBMS**: Full relational schema, connection pooling with modern `psycopg` 3 driver, and parameterized queries.
2. **Admin-Only Authentication & Authorization**: Argon2id password hashing, database-backed cryptographically secure sessions (stored as SHA-256 hashes), HttpOnly/SameSite cookies, CSRF protection, and microservice internal authentication.
3. **HTML Email Notification & Communications Hub**: Jinja2-powered branded customer email templates, SMTP dispatching with automated plain-text fallback, recipient identity verification, and PostgreSQL audit trail (`email_logs`).

---

## 1. Entity-Relationship (ER) Diagram

```mermaid
erDiagram
    USERS ||--o{ BOOKINGS : "1 : N (User owns multiple bookings)"
    USERS ||--o{ EMAIL_LOGS : "1 : N (User receives email communications)"
    BOOKINGS ||--o{ AIRLINE_TICKETS : "1 : N (Booking links to airline tickets)"
    BOOKINGS ||--o{ PAYMENTS : "1 : N (Booking links to payment transactions)"
    BOOKINGS ||--o{ EVIDENCE_RECORDS : "1 : N (Booking links to audit events)"
    BOOKINGS ||--o| REFUND_CASES : "1 : 1 (Booking links to refund case)"
    REFUND_CASES ||--o{ EMAIL_LOGS : "1 : N (Refund case has communication history)"
    ADMINS ||--o{ ADMIN_SESSIONS : "1 : N (Admin has active server sessions)"

    USERS {
        bigserial user_id PK "Surrogate user ID"
        varchar(255) full_name "Customer full name"
        varchar(255) email UK "Customer unique email address"
        varchar(50) phone "Optional contact phone number"
        timestamptz created_at "Account creation timestamp"
        timestamptz updated_at "Account update timestamp"
    }

    ADMINS {
        bigserial admin_id PK "Surrogate admin ID"
        varchar(255) email UK "Admin login email"
        varchar(255) password_hash "Argon2id password hash"
        timestamptz created_at "Admin account creation timestamp"
        timestamptz updated_at "Admin account update timestamp"
    }

    ADMIN_SESSIONS {
        bigserial session_id PK "Session record ID"
        bigint admin_id FK "Foreign key to admins.admin_id"
        varchar(64) token_hash UK "SHA-256 hash of random session token"
        varchar(64) csrf_token "CSRF double-submit token"
        timestamptz expires_at "Session expiration timestamp"
        timestamptz created_at "Session creation timestamp"
    }

    BOOKINGS {
        varchar(50) booking_ref PK "Unique booking reference code"
        bigint user_id FK "Foreign key to users.user_id"
        varchar(255) customer_name "Customer full name"
        varchar(255) email "Customer email"
        varchar(50) airline_pnr UK "Airline Passenger Name Record"
        varchar(50) flight_no "Flight number"
        varchar(10) origin "3-letter airport origin IATA"
        varchar(10) destination "3-letter airport destination IATA"
        varchar(50) flight_date "Scheduled flight date"
        numeric(10,2) amount "Booking amount (CHECK >= 0)"
        varchar(3) currency "Currency code (default INR)"
        varchar(50) payment_ref "Linked payment reference"
        varchar(50) status "Booking state (CONFIRMED, CANCELLED, etc.)"
        varchar(100) refund_id "Downstream refund identifier"
        varchar(50) booked_at "Booking timestamp"
        timestamptz created_at "Row creation timestamp"
        timestamptz updated_at "Row modification timestamp"
    }

    REFUND_CASES {
        bigserial refund_case_id PK "Surrogate autoincrement ID"
        varchar(50) case_identifier UK "Unique refund case identifier"
        varchar(50) booking_ref FK "Foreign key to bookings.booking_ref"
        varchar(50) payment_ref FK "Foreign key to payments.payment_ref"
        varchar(50) case_status "State machine status (COMPLETED, BLOCKED, etc.)"
        numeric(10,2) expected_amount "Expected refund amount (CHECK >= 0)"
        numeric(10,2) actual_amount "Actual refund amount (CHECK >= 0)"
        numeric(10,2) discrepancy_amount "Calculated amount difference"
        text reason "Explanation of case status and blockers"
        jsonb reconciliation_result "Complete serialized reconciliation result"
        boolean review_required "Flag for human or operational review"
        timestamptz created_at "Case generation timestamp"
        timestamptz updated_at "Case update timestamp"
    }

    EMAIL_LOGS {
        bigserial email_id PK "Surrogate email log ID"
        bigint user_id FK "Foreign key to users.user_id"
        bigint refund_case_id FK "Foreign key to refund_cases.refund_case_id"
        varchar(255) recipient_email "Recipient email address"
        varchar(500) subject "Subject line of email"
        varchar(100) template_name "Template key used (e.g. refund_approved)"
        varchar(50) delivery_status "Status (SENT, FAILED, SIMULATED)"
        text error_message "Error description if delivery failed"
        timestamptz sent_at "Dispatch timestamp"
        timestamptz created_at "Log creation timestamp"
    }
```

---

## 2. Authentication & Authorization Architecture

### Admin-Only Security Model
- **Single Authenticated Role**: Only Admins can log in and perform administrative actions. Customers, airline personnel, and payment gateways do not have login credentials or public registration portals.
- **Argon2id Hashing**: Passwords are never stored in plaintext. They are hashed using Argon2id (`time_cost=2`, `memory_cost=64MB`, `parallelism=1`).
- **Cryptographically Secure Sessions**:
  - Session tokens are generated using `secrets.token_urlsafe(32)`.
  - Only the **SHA-256 hash** of the session token is stored in the database (`admin_sessions.token_hash`).
  - Tokens are delivered to the browser in an `HttpOnly`, `SameSite=Lax` cookie (`nimbus_admin_session`), preventing XSS access.
- **CSRF Protection**:
  - Implements the double-submit cookie pattern.
  - A separate `csrf_token` cookie is set on login, and frontend JavaScript includes it in the `X-CSRF-Token` HTTP header for all non-GET requests (`POST /auth/logout`, `/api/reconcile/*`, `/api/email/*`).
  - State-changing requests without matching CSRF tokens are rejected with HTTP 403 Forbidden.
- **Rate Limiting**:
  - In-memory rate limiting throttles failed login attempts (max 5 failed attempts per 60 seconds per IP/email), returning HTTP 429 Too Many Requests.
- **Microservice-to-Microservice Authentication**:
  - Internal service calls (e.g., from the Resolution Hub to Voyago Booking, Meridian Air, and Zenpay) authenticate using a shared secret header: `X-Internal-Service-Key: <INTERNAL_SERVICE_SECRET>`.
  - Public endpoints are strictly limited to `/health` and login.

### Bootstrap Admin Account
Initial Admin accounts are created via an idempotent bootstrap script:
```bash
python core/bootstrap_admin.py
```
- Reads credentials from `ADMIN_EMAIL` and `ADMIN_PASSWORD` environment variables.
- **Safe & Idempotent**: If an Admin with that email already exists, the script exits cleanly without overwriting the existing password hash.

---

## 3. HTML Email Notification System

### Reusable Jinja2 Templates (`email_templates/`)
All templates inherit from `base.html` and feature responsive, Nimbus-branded dark-theme styling, header branding, details cards, and regulatory footers:
1. `base.html`: Common layout with security autoescaping and Nimbus visual identity.
2. `refund_received.html`: Acknowledges initial refund request submission.
3. `documents_required.html`: Requests specific verification documents (ID, bank statement, cancelled ticket).
4. `refund_under_review.html`: Informs the customer that multi-party audit is underway.
5. `refund_approved.html`: Confirms refund approval and queued disbursement amount.
6. `refund_rejected.html`: Explains why the refund could not be approved, citing reason.
7. `refund_completed.html`: Confirms successful transfer with payment reference.

### Delivery & Logging Service (`core/email_service.py`)
- **Recipient Verification**: Validates `confirmed_recipient_email` against the customer email linked to the refund case in PostgreSQL. Emails cannot be dispatched to arbitrary or mismatched addresses.
- **SMTP Support**: Sends multipart MIME emails (`text/html` + auto-generated `text/plain` fallback). Falls back to simulated delivery if SMTP credentials are not configured, enabling offline testing.
- **Audit Logging**: Logs every email attempt into PostgreSQL `email_logs` with delivery status (`SENT` or `FAILED`), timestamp, and error details.

---

## 4. Admin Dashboard Email Interface

The Admin Resolution Hub (`http://127.0.0.1:8000`) includes a dedicated **Customer Communications & Email Hub** tab:
1. **Case Selection**: Select any active refund case from the dropdown. The associated customer's name, email, booking reference, and phone number populate automatically.
2. **Template Selection**: Choose from 6 pre-built notification templates.
3. **Custom Instructions**: Add case-specific instructions or guidance.
4. **Live HTML Preview**: View a rendered iframe preview of the email with real database context.
5. **Confirmation Modal**: Explicit confirmation prompt displays recipient name and email before dispatching.
6. **Communication History**: View the real-time chronological audit trail of all previous emails sent for that case.

---

## 5. Environment Configuration

Copy `.env.example` to `.env` and configure your settings:
```bash
cp .env.example .env
```

Key environment variables:
```ini
# PostgreSQL Connections
DATABASE_URL=postgresql://postgres@localhost:5432/nimbus_db
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/nimbus_test_db

# Microservice Ports
BOOKING_PORT=8001
AIRLINE_PORT=8002
PAYMENT_PORT=8003
RESOLUTION_PORT=8000

# Admin Bootstrap Credentials
ADMIN_EMAIL=admin@nimbus.local
ADMIN_PASSWORD=NimbusAdmin2026!Secure

# Internal Service-to-Service Secret
INTERNAL_SERVICE_SECRET=nimbus_internal_secret_key_2026

# Session Security
SESSION_COOKIE_NAME=nimbus_admin_session
SESSION_EXPIRE_HOURS=24
ENVIRONMENT=development

# SMTP Email Configuration (Optional for real email delivery)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@example.com
SMTP_PASSWORD=your-app-password
SMTP_FROM_EMAIL=refunds@nimbus.local
SMTP_USE_TLS=true
```

---

## 6. Running the Complete Application

Use the unified launcher `run.py` to start all 4 services, verify PostgreSQL schema, and bootstrap the Admin account:

```bash
python run.py
```

### Services Started:
- **Admin Login**: `http://127.0.0.1:8000/login.html`
- **Admin Dashboard**: `http://127.0.0.1:8000/`
- **Voyago Booking API**: `http://127.0.0.1:8001`
- **Meridian Air API**: `http://127.0.0.1:8002`
- **Zenpay Payment API**: `http://127.0.0.1:8003`

---

## 7. Automated Testing

Run the comprehensive test suite with `pytest`:

```bash
.venv/bin/pytest -v
```

### Test Coverage (116 Passed Tests):
- **`tests/test_auth.py`** (25 tests):
  - Admin login success & session cookie verification.
  - Wrong password & unknown email rejection (HTTP 401).
  - Unauthenticated & invalid/expired session rejection on protected endpoints.
  - Session persistence across multiple requests.
  - Logout and post-logout access denial.
  - CSRF double-submit token verification.
  - Rate limiting (5 failed attempts -> HTTP 429).
  - Microservice `X-Internal-Service-Key` verification.
- **`tests/test_email.py`** (28 tests):
  - Jinja2 template loading and rendering for all 6 templates.
  - Dynamic placeholder substitution (passenger, booking ID, amount).
  - HTML escaping for XSS prevention.
  - Plain-text fallback generation.
  - Recipient identity mismatch protection.
  - Mocked SMTP delivery & failure logging.
  - Database audit logging in `email_logs`.
  - Email history retrieval endpoints.
- **`tests/test_db_integration.py`** (37 tests):
  - Relational schema, check constraints, foreign keys, and transactions.
- **`tests/test_live_http_pipeline.py`** (3 tests):
  - Live HTTP inter-service pipeline execution with internal service keys.
- **`tests/test_startup_concurrency.py`** (4 tests):
  - Advisory lock concurrency and launcher graceful shutdown.
- **`tests/test_orchestrator.py`**, **`tests/test_resolution_server.py`**, **`tests/test_adapters.py`**, **`core/test_cases.py`** (19 tests):
  - End-to-end reconciliation and deterministic resolution engines.
