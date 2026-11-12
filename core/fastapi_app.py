"""
core/fastapi_app.py — Production FastAPI Application for Nimbus Resolution Hub

Integrates:
- Admin Authentication (/auth/login, /auth/logout, /auth/me)
- Server-Side Authorization dependencies
- Reconciliation Pipeline APIs
- HTML Customer Email Notification System & Management APIs
- Static UI Serving (index.html, login.html, etc.)
- Strict CSRF protection and rate limiting
"""

import os
import sys
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import (
    FastAPI,
    Request,
    Response,
    HTTPException,
    Depends,
    Query,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

from db.connection import get_db_cursor, check_db_health, apply_schema
from core.auth import (
    authenticate_admin,
    create_admin_session,
    validate_session_token,
    invalidate_session,
    check_login_rate_limit,
    record_login_attempt,
    get_current_admin,
    verify_csrf_token,
    require_admin_or_internal,
    SESSION_COOKIE_NAME,
    IS_PRODUCTION,
)
from core.bootstrap_admin import bootstrap_admin
from core.integration import (
    fetch_booking_data,
    fetch_airline_data,
    fetch_payment_data,
)
from core.email_service import (
    list_available_templates,
    get_case_customer_info,
    preview_email,
    send_customer_email,
    get_case_email_history,
)

# Resolution server pipeline functions
from resolution_api_server import (
    run_pipeline_for_booking,
    run_test_scenario,
    run_custom_records,
)

FRONTEND_DIR = os.path.join(_REPO_ROOT, "frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle startup: verifies database, applies schema, bootstraps admin account."""
    if os.environ.get("SKIP_SCHEMA_INIT", "").lower() not in ("1", "true", "yes"):
        healthy, msg = check_db_health()
        if healthy:
            try:
                apply_schema()
                # Run bootstrap to ensure admin exists
                bootstrap_admin()
            except Exception as err:
                print(f"Warning during startup initialization: {err}", file=sys.stderr)
    yield


app = FastAPI(
    title="Nimbus Resolution Hub",
    description="Cross-Organizational Flight Refund Resolution Hub & Administrative Portal",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS Middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str


class ReconcileLiveRequest(BaseModel):
    bookingRef: str


class ReconcileScenarioRequest(BaseModel):
    scenario: str = "A"


class ReconcileCustomRequest(BaseModel):
    records: List[Dict[str, Any]] = []


class EmailPreviewRequest(BaseModel):
    refund_case_id: Any
    template_name: str
    additional_instructions: Optional[str] = ""
    required_documents: Optional[List[str]] = None
    custom_subject: Optional[str] = None


class EmailSendRequest(BaseModel):
    refund_case_id: Any
    template_name: str
    additional_instructions: Optional[str] = ""
    required_documents: Optional[List[str]] = None
    custom_subject: Optional[str] = None
    confirmed_recipient_email: Optional[str] = None


# ---------------------------------------------------------------------------
# Public Health Check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Health"])
def health_check():
    """Public health check endpoint."""
    return {"status": "ok", "service": "resolution"}


# ---------------------------------------------------------------------------
# Admin Authentication Endpoints
# ---------------------------------------------------------------------------

@app.post("/auth/login", tags=["Authentication"])
async def admin_login(payload: LoginRequest, request: Request, response: Response):
    """
    Admin-only login.
    - Validates email and password using Argon2.
    - Applies rate limiting against brute-force attempts.
    - Sets HttpOnly SameSite session cookie with SHA-256 hashed DB token.
    - Sets CSRF token cookie for double-submit protection.
    """
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{client_ip}:{payload.email.strip().lower()}"

    # 1. Rate limiting check
    allowed, retry_after = check_login_rate_limit(rate_key)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Please try again in {retry_after} seconds.",
        )

    # 2. Authenticate admin credentials
    admin = authenticate_admin(payload.email, payload.password)
    if not admin:
        record_login_attempt(rate_key, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # 3. Successful login: reset rate limiter
    record_login_attempt(rate_key, success=True)

    # 4. Generate cryptographically secure session and store token hash in DB
    raw_token, csrf_token, expires_at = create_admin_session(admin["admin_id"])

    # 5. Set session cookie (HttpOnly, SameSite=Lax, Secure in production)
    max_age_sec = 24 * 3600
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max_age_sec,
        httponly=True,
        secure=IS_PRODUCTION,
        samesite="lax",
        path="/",
    )

    # Set CSRF cookie (Readable by frontend JavaScript for header attachment)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        max_age=max_age_sec,
        httponly=False,
        secure=IS_PRODUCTION,
        samesite="lax",
        path="/",
    )

    return {
        "success": True,
        "message": "Admin authentication successful.",
        "admin": {
            "admin_id": admin["admin_id"],
            "email": admin["email"],
        },
        "csrf_token": csrf_token,
    }


@app.post("/auth/logout", tags=["Authentication"])
async def admin_logout(request: Request, response: Response):
    """
    Invalidates the current Admin session in PostgreSQL and clears authentication cookies.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()

    if token:
        invalidate_session(token)

    # Clear session & CSRF cookies
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie("csrf_token", path="/")

    return {"success": True, "message": "Admin logged out successfully."}


@app.get("/auth/me", tags=["Authentication"])
async def admin_me(admin: Dict[str, Any] = Depends(get_current_admin)):
    """
    Verifies the active Admin session and returns authenticated Admin profile.
    Rejects expired or invalid sessions with 401.
    """
    return {
        "authenticated": True,
        "admin": {
            "admin_id": admin["admin_id"],
            "email": admin["email"],
            "created_at": admin["admin_created_at"],
        },
        "csrf_token": admin["csrf_token"],
    }


# ---------------------------------------------------------------------------
# Reconcile & Microservice APIs (Protected)
# ---------------------------------------------------------------------------

@app.get("/api/status", tags=["Reconciliation"])
async def get_microservices_status(auth: Dict[str, Any] = Depends(require_admin_or_internal)):
    """Health & record count status for all 3 connected microservices."""
    status_data = {
        "booking_service": False,
        "airline_service": False,
        "payment_service": False,
        "bookings_count": 0,
        "airline_count": 0,
        "payment_count": 0,
        "available_bookings": [],
    }

    try:
        b = fetch_booking_data()
        status_data["booking_service"] = True
        status_data["bookings_count"] = len(b)
        status_data["available_bookings"] = [x["bookingRef"] for x in b]
    except Exception:
        pass

    try:
        a = fetch_airline_data()
        status_data["airline_service"] = True
        status_data["airline_count"] = len(a)
    except Exception:
        pass

    try:
        p = fetch_payment_data()
        status_data["payment_service"] = True
        status_data["payment_count"] = len(p)
    except Exception:
        pass

    return status_data


@app.post("/api/reconcile/live", tags=["Reconciliation"])
async def reconcile_live_post(
    payload: ReconcileLiveRequest,
    auth: Dict[str, Any] = Depends(require_admin_or_internal),
    csrf: None = Depends(verify_csrf_token),
):
    """Executes live multi-party reconciliation for a given booking reference."""
    if not payload.bookingRef:
        raise HTTPException(status_code=400, detail="Missing bookingRef in request body.")
    try:
        app_case = run_pipeline_for_booking(payload.bookingRef.upper())
        return app_case.model_dump(mode="json")
    except Exception as err:
        raise HTTPException(status_code=400, detail=str(err))


@app.get("/api/reconcile/live", tags=["Reconciliation"])
async def reconcile_live_get(
    bookingRef: Optional[str] = Query(None),
    auth: Dict[str, Any] = Depends(require_admin_or_internal),
):
    """Executes live reconciliation via GET query parameter."""
    if not bookingRef:
        raise HTTPException(status_code=400, detail="Missing bookingRef query parameter.")
    try:
        app_case = run_pipeline_for_booking(bookingRef.upper())
        return app_case.model_dump(mode="json")
    except Exception as err:
        raise HTTPException(status_code=400, detail=str(err))


@app.post("/api/reconcile/scenario", tags=["Reconciliation"])
async def reconcile_scenario(
    payload: ReconcileScenarioRequest,
    auth: Dict[str, Any] = Depends(require_admin_or_internal),
    csrf: None = Depends(verify_csrf_token),
):
    """Executes preset test scenarios (Scenario A, B, C, D)."""
    try:
        app_case = run_test_scenario(payload.scenario)
        return {
            "scenario": payload.scenario,
            "case": app_case.model_dump(mode="json"),
        }
    except Exception as err:
        raise HTTPException(status_code=400, detail=str(err))


@app.post("/api/reconcile/custom", tags=["Reconciliation"])
async def reconcile_custom(
    payload: ReconcileCustomRequest,
    auth: Dict[str, Any] = Depends(require_admin_or_internal),
    csrf: None = Depends(verify_csrf_token),
):
    """Executes reconciliation for interactive custom evidence records."""
    try:
        app_case = run_custom_records(payload.records)
        return app_case.model_dump(mode="json")
    except Exception as err:
        raise HTTPException(status_code=400, detail=str(err))


@app.get("/api/reconcile/cases", tags=["Reconciliation"])
async def list_persisted_cases(auth: Dict[str, Any] = Depends(require_admin_or_internal)):
    """Lists persisted refund cases from PostgreSQL."""
    try:
        with get_db_cursor() as (cur, _):
            cur.execute("""
                SELECT rc.refund_case_id, rc.case_identifier, rc.booking_ref, rc.payment_ref,
                       rc.case_status, rc.expected_amount, rc.actual_amount, rc.discrepancy_amount,
                       rc.review_required, rc.reason, rc.created_at,
                       b.customer_name, b.email AS customer_email
                FROM refund_cases rc
                LEFT JOIN bookings b ON rc.booking_ref = b.booking_ref
                ORDER BY rc.created_at DESC
                LIMIT 100;
            """)
            cases = cur.fetchall()
        return {
            "count": len(cases),
            "cases": [dict(c) for c in cases],
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Database error: {err}")


# ---------------------------------------------------------------------------
# HTML Email Notification & Management Endpoints (Admin-Only)
# ---------------------------------------------------------------------------

@app.get("/api/email/templates", tags=["Email"])
async def get_email_templates(admin: Dict[str, Any] = Depends(get_current_admin)):
    """Returns all available HTML email templates."""
    return {"templates": list_available_templates()}


@app.get("/api/email/case-customer/{case_id}", tags=["Email"])
async def get_email_case_customer(
    case_id: str,
    admin: Dict[str, Any] = Depends(get_current_admin),
):
    """Fetches verified customer identity and refund details for a specific case."""
    info = get_case_customer_info(case_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"Case or booking '{case_id}' not found.")
    return info


@app.post("/api/email/preview", tags=["Email"])
async def preview_customer_email(
    payload: EmailPreviewRequest,
    admin: Dict[str, Any] = Depends(get_current_admin),
):
    """
    Renders email preview with database information.
    Does NOT send an email or alter database state.
    """
    try:
        res = preview_email(
            case_id_or_identifier=payload.refund_case_id,
            template_name=payload.template_name,
            additional_instructions=payload.additional_instructions or "",
            required_documents=payload.required_documents,
            custom_subject=payload.custom_subject,
        )
        return res
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Preview rendering error: {err}")


@app.post("/api/email/send", tags=["Email"])
async def send_email_endpoint(
    payload: EmailSendRequest,
    admin: Dict[str, Any] = Depends(get_current_admin),
    csrf: None = Depends(verify_csrf_token),
):
    """
    Dispatches customer email notification via SMTP and logs in PostgreSQL.
    Enforces strict recipient validation against the associated refund case.
    """
    try:
        res = send_customer_email(
            case_id_or_identifier=payload.refund_case_id,
            template_name=payload.template_name,
            additional_instructions=payload.additional_instructions or "",
            required_documents=payload.required_documents,
            custom_subject=payload.custom_subject,
            confirmed_recipient_email=payload.confirmed_recipient_email,
        )
        return res
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Email dispatch error: {exc}")


@app.get("/api/email/history/{case_id}", tags=["Email"])
async def get_email_history(
    case_id: str,
    admin: Dict[str, Any] = Depends(get_current_admin),
):
    """Chronological email delivery audit logs for a refund case."""
    history = get_case_email_history(case_id)
    return {"count": len(history), "logs": history}


# ---------------------------------------------------------------------------
# Static Web UI & Route Serving
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def serve_root(request: Request):
    """
    Root route: checks active Admin session.
    If authenticated, serves index.html; otherwise redirects to /login.html.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    admin = validate_session_token(token) if token else None
    if not admin:
        return RedirectResponse(url="/login.html", status_code=302)

    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h1>Nimbus Resolution Hub</h1><p>index.html not found.</p>")


@app.get("/login.html", response_class=HTMLResponse, tags=["UI"])
async def serve_login():
    """Serves the Admin login page."""
    login_path = os.path.join(FRONTEND_DIR, "login.html")
    if os.path.isfile(login_path):
        return FileResponse(login_path, media_type="text/html")
    return HTMLResponse("<h1>Admin Login</h1><p>login.html not found.</p>")


@app.get("/{page}.html", response_class=HTMLResponse, tags=["UI"])
async def serve_page(page: str, request: Request):
    """
    Serves frontend HTML pages (index.html, booking.html, airline.html, payment.html).
    Enforces Admin authentication on index.html.
    """
    if page == "index":
        token = request.cookies.get(SESSION_COOKIE_NAME)
        admin = validate_session_token(token) if token else None
        if not admin:
            return RedirectResponse(url="/login.html", status_code=302)

    page_file = f"{page}.html"
    file_path = os.path.join(FRONTEND_DIR, page_file)
    if os.path.isfile(file_path):
        return FileResponse(file_path, media_type="text/html")
    raise HTTPException(status_code=404, detail=f"Page '{page}.html' not found.")


# Mount frontend directory for static assets (styles, scripts, icons)
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
