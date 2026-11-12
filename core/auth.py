"""
core/auth.py — Complete Admin Authentication and Session Management for Nimbus

Features:
- Secure password hashing using Argon2 (argon2-cffi).
- Cryptographically secure session tokens (secrets.token_urlsafe).
- Hashed session token storage in PostgreSQL (SHA-256).
- Server-side session verification, expiration, and invalidation.
- Rate limiting on login attempts to prevent brute-force attacks.
- CSRF token generation and double-submit header validation.
- Reusable FastAPI authentication dependencies.
- Helpers for microservice BaseHTTPRequestHandler authorization.
- Zero sensitive tokens or passwords exposed in logs or API responses.
"""

import os
import sys
import time
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from http.cookies import SimpleCookie

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

from db.connection import get_db_cursor, get_database_url

# Password hasher configuration using Argon2id
_ph = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=1,
    hash_len=32,
)

# Configuration from environment
SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "nimbus_admin_session")
SESSION_EXPIRE_HOURS = int(os.environ.get("SESSION_EXPIRE_HOURS", 24))
INTERNAL_SERVICE_SECRET = os.environ.get("INTERNAL_SERVICE_SECRET", "nimbus_internal_secret_key_2026")
IS_PRODUCTION = os.environ.get("ENVIRONMENT", "development").lower() == "production"

# Rate Limiter for Login Attempts
# In-memory store: { ip_or_email: [(timestamp, success_bool)] }
_LOGIN_ATTEMPTS: Dict[str, list] = {}
MAX_FAILED_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# Password Hashing & Verification
# ---------------------------------------------------------------------------

def hash_password(plain_password: str) -> str:
    """Hashes a plaintext password using Argon2id."""
    if not plain_password:
        raise ValueError("Password cannot be empty")
    return _ph.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verifies a plaintext password against an Argon2 hash."""
    if not plain_password or not password_hash:
        return False
    try:
        return _ph.verify(password_hash, plain_password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:
        return False


def hash_session_token(token: str) -> str:
    """Hashes a session token using SHA-256 before database storage or lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

def check_login_rate_limit(client_identifier: str) -> Tuple[bool, int]:
    """
    Checks if a client has exceeded the failed login rate limit.
    Returns (is_allowed, retry_after_seconds).
    """
    now = time.time()
    attempts = _LOGIN_ATTEMPTS.get(client_identifier, [])
    # Keep only attempts within the window
    recent = [t for t in attempts if now - t < RATE_LIMIT_WINDOW_SECONDS]
    _LOGIN_ATTEMPTS[client_identifier] = recent

    if len(recent) >= MAX_FAILED_ATTEMPTS:
        retry_after = int(RATE_LIMIT_WINDOW_SECONDS - (now - recent[0]))
        return False, max(1, retry_after)
    return True, 0


def record_login_attempt(client_identifier: str, success: bool) -> None:
    """Records a login attempt for rate limiting."""
    now = time.time()
    if success:
        _LOGIN_ATTEMPTS.pop(client_identifier, None)
    else:
        attempts = _LOGIN_ATTEMPTS.get(client_identifier, [])
        attempts.append(now)
        _LOGIN_ATTEMPTS[client_identifier] = attempts


# ---------------------------------------------------------------------------
# Session Database Operations
# ---------------------------------------------------------------------------

def create_admin_session(
    admin_id: int,
    duration_hours: Optional[int] = None,
    database_url: Optional[str] = None,
) -> Tuple[str, str, datetime]:
    """
    Creates a new server-managed session in PostgreSQL.
    Stores only the SHA-256 token_hash in the database.
    Returns:
        (raw_session_token, csrf_token, expires_at)
    """
    duration = duration_hours or SESSION_EXPIRE_HOURS
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_session_token(raw_token)
    csrf_token = secrets.token_hex(16)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=duration)

    with get_db_cursor(database_url=database_url, commit=True) as (cur, _):
        cur.execute("""
            INSERT INTO admin_sessions (admin_id, token_hash, csrf_token, expires_at)
            VALUES (%s, %s, %s, %s)
            RETURNING session_id;
        """, (admin_id, token_hash, csrf_token, expires_at))

    return raw_token, csrf_token, expires_at


def validate_session_token(
    raw_token: str,
    database_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Validates a raw session token against PostgreSQL.
    Rejects invalid, missing, or expired sessions.
    Returns session & admin dictionary or None.
    """
    if not raw_token or not isinstance(raw_token, str):
        return None

    token_hash = hash_session_token(raw_token.strip())

    try:
        with get_db_cursor(database_url=database_url, commit=False) as (cur, _):
            cur.execute("""
                SELECT s.session_id, s.admin_id, s.csrf_token, s.expires_at,
                       a.email, a.created_at AS admin_created_at
                FROM admin_sessions s
                JOIN admins a ON s.admin_id = a.admin_id
                WHERE s.token_hash = %s
                  AND s.expires_at > CURRENT_TIMESTAMP;
            """, (token_hash,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "session_id": row["session_id"],
                "admin_id": row["admin_id"],
                "csrf_token": row["csrf_token"],
                "expires_at": row["expires_at"],
                "email": row["email"],
                "admin_created_at": row["admin_created_at"],
            }
    except Exception:
        return None


def invalidate_session(
    raw_token: str,
    database_url: Optional[str] = None,
) -> bool:
    """Invalidates (deletes) a session token from PostgreSQL upon logout."""
    if not raw_token:
        return False
    token_hash = hash_session_token(raw_token.strip())
    try:
        with get_db_cursor(database_url=database_url, commit=True) as (cur, _):
            cur.execute("DELETE FROM admin_sessions WHERE token_hash = %s;", (token_hash,))
            return cur.rowcount > 0
    except Exception:
        return False


def cleanup_expired_sessions(database_url: Optional[str] = None) -> int:
    """Removes all expired sessions from PostgreSQL."""
    try:
        with get_db_cursor(database_url=database_url, commit=True) as (cur, _):
            cur.execute("DELETE FROM admin_sessions WHERE expires_at <= CURRENT_TIMESTAMP;")
            return cur.rowcount
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Admin Verification against Database
# ---------------------------------------------------------------------------

def authenticate_admin(
    email: str,
    password: str,
    database_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Validates Admin credentials against the admins table using Argon2.
    Returns Admin dict on success or None on failure.
    """
    if not email or not password:
        return None

    clean_email = email.strip().lower()

    try:
        with get_db_cursor(database_url=database_url, commit=False) as (cur, _):
            cur.execute("""
                SELECT admin_id, email, password_hash, created_at
                FROM admins
                WHERE LOWER(email) = %s;
            """, (clean_email,))
            row = cur.fetchone()
            if not row:
                return None

            if verify_password(password, row["password_hash"]):
                return {
                    "admin_id": row["admin_id"],
                    "email": row["email"],
                    "created_at": row["created_at"],
                }
            return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP & Cookie Helpers for BaseHTTPRequestHandler
# ---------------------------------------------------------------------------

def get_cookie_from_headers(headers: Any, cookie_name: str) -> Optional[str]:
    """Extracts a specific cookie value from HTTP request headers."""
    cookie_header = headers.get("Cookie") or headers.get("cookie")
    if not cookie_header:
        return None
    try:
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(cookie_name)
        return morsel.value if morsel else None
    except Exception:
        return None


def check_admin_auth_from_handler(handler: Any) -> Optional[Dict[str, Any]]:
    """
    Extracts session cookie or Authorization Bearer header from BaseHTTPRequestHandler
    and validates against PostgreSQL. Returns Admin dict if valid, else None.
    """
    # 1. Check Cookie
    token = get_cookie_from_headers(handler.headers, SESSION_COOKIE_NAME)

    # 2. Check Bearer Authorization header
    if not token:
        auth_header = handler.headers.get("Authorization") or handler.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    if not token:
        return None

    return validate_session_token(token)


def check_admin_or_internal_from_handler(handler: Any) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Checks if request has a valid Admin session OR a valid internal service key.
    Returns (authorized: bool, admin_or_internal_info: Optional[dict]).
    """
    # 1. Check Internal Service Key
    internal_key = (
        handler.headers.get("X-Internal-Service-Key")
        or handler.headers.get("x-internal-service-key")
    )
    if internal_key and secrets.compare_digest(internal_key, INTERNAL_SERVICE_SECRET):
        return True, {"is_internal": True}

    # 2. Check Admin session
    admin = check_admin_auth_from_handler(handler)
    if admin:
        return True, admin

    return False, None


# ---------------------------------------------------------------------------
# FastAPI Dependencies & Reusable Middleware
# ---------------------------------------------------------------------------

try:
    from fastapi import Request, HTTPException, Security, Depends
    from fastapi.security import APIKeyCookie, APIKeyHeader

    cookie_sec = APIKeyCookie(name=SESSION_COOKIE_NAME, auto_error=False)
    bearer_sec = APIKeyHeader(name="Authorization", auto_error=False)
    internal_sec = APIKeyHeader(name="X-Internal-Service-Key", auto_error=False)

    async def get_current_admin(request: Request) -> Dict[str, Any]:
        """
        FastAPI dependency: enforces active Admin session.
        Checks cookie first, then Bearer token.
        Raises 401 if unauthorized.
        """
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            auth = request.headers.get("Authorization")
            if auth and auth.startswith("Bearer "):
                token = auth[7:].strip()

        if not token:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized. Active Admin session required.",
            )

        admin = validate_session_token(token)
        if not admin:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized. Session is invalid, expired, or revoked.",
            )
        return admin

    async def verify_csrf_token(
        request: Request,
        admin: Dict[str, Any] = Depends(get_current_admin),
    ) -> None:
        """
        FastAPI dependency: enforces CSRF protection on state-changing requests.
        """
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            provided_csrf = (
                request.headers.get("X-CSRF-Token")
                or request.headers.get("x-csrf-token")
                or request.cookies.get("csrf_token")
            )
            expected_csrf = admin.get("csrf_token")
            if not provided_csrf or not expected_csrf or not secrets.compare_digest(provided_csrf, expected_csrf):
                raise HTTPException(
                    status_code=403,
                    detail="Forbidden. Invalid or missing CSRF token.",
                )

    async def require_admin_or_internal(request: Request) -> Dict[str, Any]:
        """
        FastAPI dependency: allows valid Admin session OR internal service key.
        """
        internal_key = (
            request.headers.get("X-Internal-Service-Key")
            or request.headers.get("x-internal-service-key")
        )
        if internal_key and secrets.compare_digest(internal_key, INTERNAL_SERVICE_SECRET):
            return {"is_internal": True}

        return await get_current_admin(request)

except ImportError:
    # If fastapi is not loaded in current thread/scope
    pass
