"""
Authentication & Authorization Tests
Tests for Admin login, session management, protected endpoints,
rate limiting, and internal service-key access.
"""
import os
import sys
import time
import hashlib
import secrets
import threading
import pytest
from unittest.mock import patch, MagicMock

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── FastAPI TestClient setup ─────────────────────────────────────────────────
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    """Import the FastAPI app (skips lifespan startup)."""
    from core.fastapi_app import app as fastapi_app
    return fastapi_app


@pytest.fixture(scope="module")
def client(app):
    """TestClient with no automatic redirect following."""
    return TestClient(app, raise_server_exceptions=False)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _login(client, email="admin@nimbus.local", password="NimbusAdmin2026!Secure"):
    """Perform a login and return the response."""
    return client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )


def _get_csrf(client):
    """Extract the csrf_token cookie value from the TestClient's cookie jar."""
    for cookie in client.cookies.jar:
        if cookie.name == "csrf_token":
            return cookie.value
    return ""


# ════════════════════════════════════════════════════════════════════════════
# Admin Login Tests
# ════════════════════════════════════════════════════════════════════════════

class TestAdminLogin:

    def test_login_success(self, client):
        """Valid credentials return 200 and set session cookie."""
        resp = _login(client)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("success") is True
        assert "admin_id" in body or "email" in body or "message" in body
        # Session cookie must be HttpOnly (not visible to JS)
        # TestClient exposes it; real browser won't via document.cookie
        assert "nimbus_admin_session" in client.cookies or any(
            c.name == "nimbus_admin_session" for c in client.cookies.jar
        )

    def test_login_wrong_password(self, client):
        """Wrong password returns 401."""
        resp = client.post(
            "/auth/login",
            json={"email": "admin@nimbus.local", "password": "WrongPassword!"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert "detail" in body or "error" in body

    def test_login_unknown_email(self, client):
        """Unknown email returns 401 (not 404 to avoid user enumeration)."""
        resp = client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "SomePassword!"},
        )
        assert resp.status_code == 401

    def test_login_missing_fields(self, client):
        """Login without email/password returns 422."""
        resp = client.post("/auth/login", json={})
        assert resp.status_code in (400, 422)

    def test_login_response_does_not_expose_hash(self, client):
        """Login response must not contain password_hash or token."""
        resp = _login(client)
        text = resp.text.lower()
        assert "password_hash" not in text
        assert "argon2" not in text


# ════════════════════════════════════════════════════════════════════════════
# Auth Me / Session Verification Tests
# ════════════════════════════════════════════════════════════════════════════

class TestAuthMe:

    def test_me_authenticated(self, client):
        """After login, GET /auth/me returns admin info."""
        _login(client)
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        # Response may be flat {email:...} or nested {admin: {email:...}}
        email = body.get("email") or (body.get("admin") or {}).get("email")
        assert email == "admin@nimbus.local", f"email not found in response: {body}"
        # Must not expose password hash
        assert "password_hash" not in str(body)

    def test_me_unauthenticated(self, client):
        """Without a session, GET /auth/me returns 401."""
        # Fresh client with no cookies
        from core.fastapi_app import app as fastapi_app
        fresh_client = TestClient(fastapi_app, raise_server_exceptions=False)
        resp = fresh_client.get("/auth/me")
        assert resp.status_code == 401


# ════════════════════════════════════════════════════════════════════════════
# Logout Tests
# ════════════════════════════════════════════════════════════════════════════

class TestLogout:

    def test_logout_success(self, client):
        """Login then logout returns 200."""
        _login(client)
        csrf = _get_csrf(client)
        resp = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

    def test_access_after_logout(self, client):
        """After logout, protected endpoints return 401."""
        from core.fastapi_app import app as fastapi_app
        c = TestClient(fastapi_app, raise_server_exceptions=False)
        _login(c)
        csrf = _get_csrf(c)
        c.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        resp = c.get("/auth/me")
        assert resp.status_code == 401

    def test_session_persistence_across_requests(self, client):
        """Session cookie persists and authenticates multiple requests."""
        from core.fastapi_app import app as fastapi_app
        c = TestClient(fastapi_app, raise_server_exceptions=False)
        _login(c)
        for _ in range(3):
            resp = c.get("/auth/me")
            assert resp.status_code == 200


# ════════════════════════════════════════════════════════════════════════════
# Protected API Endpoints — no auth
# ════════════════════════════════════════════════════════════════════════════

class TestProtectedEndpoints:

    def _fresh(self):
        from core.fastapi_app import app as fastapi_app
        return TestClient(fastapi_app, raise_server_exceptions=False)

    def test_live_reconcile_requires_auth(self):
        c = self._fresh()
        resp = c.post(
            "/api/reconcile/live",
            json={"bookingRef": "BK-20001"},
        )
        assert resp.status_code == 401

    def test_scenario_reconcile_requires_auth(self):
        c = self._fresh()
        resp = c.post(
            "/api/reconcile/scenario",
            json={"scenario": "clean_match"},
        )
        assert resp.status_code == 401

    def test_custom_reconcile_requires_auth(self):
        c = self._fresh()
        resp = c.post(
            "/api/reconcile/custom",
            json={"records": []},
        )
        assert resp.status_code == 401

    def test_cases_list_requires_auth(self):
        c = self._fresh()
        resp = c.get("/api/reconcile/cases")
        assert resp.status_code == 401

    def test_email_templates_requires_auth(self):
        c = self._fresh()
        resp = c.get("/api/email/templates")
        assert resp.status_code == 401

    def test_email_preview_requires_auth(self):
        c = self._fresh()
        resp = c.post(
            "/api/email/preview",
            json={"case_id": "RC-001", "template_name": "refund_received"},
        )
        assert resp.status_code == 401

    def test_email_send_requires_auth(self):
        c = self._fresh()
        resp = c.post(
            "/api/email/send",
            json={
                "case_id": "RC-001",
                "template_name": "refund_received",
                "confirmed_recipient_email": "test@example.com",
            },
        )
        assert resp.status_code == 401

    def test_public_health_does_not_require_auth(self):
        c = self._fresh()
        resp = c.get("/health")
        assert resp.status_code == 200

    def test_login_endpoint_is_public(self):
        c = self._fresh()
        # Just hitting login endpoint (bad creds) is 401 for creds, not 401 for session
        resp = c.post("/auth/login", json={"email": "x@x.com", "password": "bad"})
        # Should be 401 for bad credentials, not "session required"
        assert resp.status_code in (400, 401, 422)


# ════════════════════════════════════════════════════════════════════════════
# Expired / Invalid Session Tests
# ════════════════════════════════════════════════════════════════════════════

class TestSessionValidation:

    def test_invalid_session_token(self):
        """Tampered session cookie returns 401."""
        from core.fastapi_app import app as fastapi_app
        c = TestClient(fastapi_app, raise_server_exceptions=False)
        c.cookies.set("nimbus_admin_session", "totally_invalid_token_xyz")
        resp = c.get("/auth/me")
        assert resp.status_code == 401

    def test_expired_session(self):
        """An expired session token is rejected (mocked)."""
        from core import auth as auth_module

        original = auth_module.validate_session_token

        def mock_validate(token):
            # Simulate expired session
            return None

        with patch.object(auth_module, "validate_session_token", side_effect=mock_validate):
            from core.fastapi_app import app as fastapi_app
            c = TestClient(fastapi_app, raise_server_exceptions=False)
            c.cookies.set("nimbus_admin_session", "some_expired_token")
            resp = c.get("/auth/me")
            assert resp.status_code == 401


# ════════════════════════════════════════════════════════════════════════════
# Rate Limiting Tests
# ════════════════════════════════════════════════════════════════════════════

class TestRateLimiting:

    def test_rate_limiting_after_failed_attempts(self):
        """After 5 failed login attempts, further attempts return 429."""
        from core.fastapi_app import app as fastapi_app
        c = TestClient(fastapi_app, raise_server_exceptions=False)

        # Clear any existing rate limit for this IP
        from core import auth as auth_module
        auth_module._LOGIN_ATTEMPTS.clear()

        try:
            for i in range(5):
                c.post(
                    "/auth/login",
                    json={"email": "admin@nimbus.local", "password": f"wrong{i}"},
                )

            # 6th attempt should be rate-limited
            resp = c.post(
                "/auth/login",
                json={"email": "admin@nimbus.local", "password": "wrong6"},
            )
            assert resp.status_code == 429, f"Expected 429, got {resp.status_code}"
        finally:
            auth_module._LOGIN_ATTEMPTS.clear()


# ════════════════════════════════════════════════════════════════════════════
# Internal Service Key Tests (for microservice-to-microservice auth)
# ════════════════════════════════════════════════════════════════════════════

class TestInternalServiceKey:

    def test_internal_key_check_passes(self):
        """Internal service key header satisfies auth check."""
        from core.auth import check_admin_or_internal_from_handler

        internal_secret = os.environ.get("INTERNAL_SERVICE_SECRET", "nimbus-internal-dev-secret")

        mock_handler = MagicMock()
        mock_handler.headers = {
            "X-Internal-Service-Key": internal_secret
        }
        result = check_admin_or_internal_from_handler(mock_handler)
        ok = result[0] if isinstance(result, tuple) else result
        assert ok is True

    def test_wrong_internal_key_fails(self):
        """Wrong internal service key is rejected."""
        from core.auth import check_admin_or_internal_from_handler

        mock_handler = MagicMock()
        mock_handler.headers = {
            "X-Internal-Service-Key": "wrong-secret-key"
        }
        mock_handler.rfile = MagicMock()
        result = check_admin_or_internal_from_handler(mock_handler)
        ok = result[0] if isinstance(result, tuple) else result
        assert ok is False

    def test_no_auth_header_fails(self):
        """No auth header is rejected."""
        from core.auth import check_admin_or_internal_from_handler

        mock_handler = MagicMock()
        mock_handler.headers = {}
        mock_handler.rfile = MagicMock()
        result = check_admin_or_internal_from_handler(mock_handler)
        ok = result[0] if isinstance(result, tuple) else result
        assert ok is False
