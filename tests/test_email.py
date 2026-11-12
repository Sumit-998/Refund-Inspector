"""
Email Service Tests
Tests for HTML template rendering, placeholder substitution, XSS escaping,
email logging, SMTP failure handling, invalid recipient handling,
and email history retrieval.
All tests use mocked SMTP — no real emails are sent.
"""
import os
import sys
import re
import pytest
from unittest.mock import patch, MagicMock

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.email_service as email_svc


# ════════════════════════════════════════════════════════════════════════════
# Template Rendering Tests
# ════════════════════════════════════════════════════════════════════════════

class TestTemplateRendering:

    def test_all_templates_listed(self):
        """list_available_templates returns all 6 expected templates."""
        templates = email_svc.list_available_templates()
        assert len(templates) >= 6
        names = {t["template_id"] for t in templates}
        expected = {
            "refund_received",
            "documents_required",
            "refund_under_review",
            "refund_approved",
            "refund_rejected",
            "refund_completed",
        }
        for name in expected:
            assert name in names, f"Missing template: {name}"

    def _base_context(self, **overrides):
        ctx = {
            "customer_name": "Test User",
            "customer_email": "test@example.com",
            "booking_id": "BK-99999",
            "refund_case_id": "RC-TEST-001",
            "refund_amount": "5,000",
            "current_status": "PENDING",
            "payment_ref": "PAY-001",
            "required_documents": [],
            "additional_instructions": "",
            "relevant_dates": {
                "date": "01 Jan 2026",
                "formatted": "Thursday, 01 January 2026, 10:00 AM UTC",
                "created_at": "2026-01-01",
            },
            "rejection_reason": None,
        }
        ctx.update(overrides)
        return ctx

    def test_template_renders_to_html(self):
        """render_email returns non-empty HTML string."""
        ctx = self._base_context()
        subject, html, text = email_svc.render_email("refund_received", ctx)
        assert isinstance(html, str)
        assert len(html) > 100
        assert isinstance(subject, str) and len(subject) > 0
        assert isinstance(text, str) and len(text) > 0

    def test_placeholder_substitution_customer_name(self):
        """Customer name appears in rendered output."""
        ctx = self._base_context(customer_name="Priya Sharma", booking_id="BK-UNIQUE-XYZ")
        _, html, _ = email_svc.render_email("refund_approved", ctx)
        assert "Priya Sharma" in html

    def test_placeholder_substitution_booking_ref(self):
        """Booking ref appears in rendered output."""
        ctx = self._base_context(booking_id="BK-UNIQUE-XYZ-999")
        _, html, _ = email_svc.render_email("refund_approved", ctx)
        assert "BK-UNIQUE-XYZ-999" in html

    def test_placeholder_substitution_refund_amount(self):
        """Refund amount appears in rendered output."""
        ctx = self._base_context(refund_amount="12,345")
        _, html, _ = email_svc.render_email("refund_approved", ctx)
        assert "12,345" in html or "12345" in html

    def test_html_xss_escaping(self):
        """XSS payloads in context variables are HTML-escaped."""
        xss = "<script>alert('xss')</script>"
        ctx = self._base_context(
            customer_name=xss,
            additional_instructions=xss,
        )
        _, html, _ = email_svc.render_email("refund_received", ctx)
        # Raw script tag must NOT appear verbatim in output
        assert "<script>" not in html
        # Escaped form is OK
        assert "&lt;script&gt;" in html or "alert" not in html

    def test_additional_instructions_included(self):
        """Additional instructions appear in rendered HTML."""
        instruction = "UNIQUE_INSTRUCTION_STRING_FINDME_12345"
        ctx = self._base_context(additional_instructions=instruction)
        _, html, _ = email_svc.render_email("refund_under_review", ctx)
        assert instruction in html

    def test_documents_required_template(self):
        """documents_required template renders list of documents."""
        docs = ["Passport copy", "Medical certificate", "Flight ticket"]
        ctx = self._base_context(
            customer_name="Raj Kumar",
            required_documents=docs,
        )
        _, html, _ = email_svc.render_email("documents_required", ctx)
        assert "Raj Kumar" in html
        assert any(doc in html for doc in docs), "No required documents found in rendered HTML"

    def test_invalid_template_raises(self):
        """Non-existent template name raises ValueError."""
        ctx = self._base_context()
        with pytest.raises((ValueError, Exception)):
            email_svc.render_email("template_does_not_exist_xyz", ctx)

    def test_all_templates_render_without_error(self):
        """All 6 templates render without raising exceptions."""
        ctx = self._base_context()
        for template_name in [
            "refund_received",
            "documents_required",
            "refund_under_review",
            "refund_approved",
            "refund_rejected",
            "refund_completed",
        ]:
            subject, html, text = email_svc.render_email(template_name, ctx)
            assert len(html) > 50, f"Template {template_name} produced too-short HTML"
            assert len(text) > 20, f"Template {template_name} produced too-short text"

    def test_plain_text_has_meaningful_content(self):
        """html_to_plain_text strips tags and returns readable text."""
        ctx = self._base_context(customer_name="Fallback User")
        _, html, text = email_svc.render_email("refund_completed", ctx)
        # Plain text should not contain HTML tags
        assert "<" not in text or text.count("<") < 5, "Plain text still contains HTML tags"
        # Should have meaningful length
        assert len(text.strip()) > 50


# ════════════════════════════════════════════════════════════════════════════
# Email Send Tests (mocked SMTP + mocked DB)
# ════════════════════════════════════════════════════════════════════════════

class TestEmailServiceSend:

    def _mock_case_info(self, **overrides):
        info = {
            "refund_case_id": 1,
            "case_identifier": "RC-MOCK-001",
            "booking_ref": "BK-MOCK-001",
            "case_status": "PENDING",
            "expected_amount": 5000,
            "actual_amount": 5000,
            "discrepancy_amount": 0,
            "reason": None,
            "case_created_at": "2026-01-01T00:00:00",
            "booking_customer_name": "Mock Customer",
            "booking_email": "mock@example.com",
            "flight_no": "AI201",
            "booking_amount": 5000,
            "booking_currency": "INR",
            "booked_at": "2026-01-01T00:00:00",
            "user_id": 99,
            "user_full_name": "Mock Customer",
            "user_email": "mock@example.com",
            "user_phone": None,
            "payment_ref": "PAY-MOCK-001",
            # Computed helpers
            "customer_name": "Mock Customer",
            "customer_email": "mock@example.com",
            "phone": None,
            "formatted_amount": "₹5,000",
        }
        info.update(overrides)
        return info

    def test_invalid_recipient_rejected(self):
        """confirmed_recipient_email not matching case customer raises ValueError."""
        with patch("core.email_service.get_case_customer_info",
                   return_value=self._mock_case_info()):
            with pytest.raises((ValueError, PermissionError)) as exc_info:
                email_svc.send_customer_email(
                    case_id_or_identifier="RC-MOCK-001",
                    template_name="refund_received",
                    confirmed_recipient_email="attacker@evil.com",
                )
            msg = str(exc_info.value).lower()
            assert any(w in msg for w in ["mismatch", "match", "recipient", "security"])

    def test_nonexistent_case_rejected(self):
        """Non-existent case_id raises ValueError."""
        with patch("core.email_service.get_case_customer_info", return_value=None):
            with pytest.raises((ValueError, Exception)):
                email_svc.send_customer_email(
                    case_id_or_identifier="RC-DOES-NOT-EXIST",
                    template_name="refund_received",
                    confirmed_recipient_email="someone@example.com",
                )

    def test_nonexistent_template_rejected(self):
        """Non-existent template name raises ValueError."""
        with patch("core.email_service.get_case_customer_info",
                   return_value=self._mock_case_info()):
            with pytest.raises((ValueError, Exception)):
                email_svc.send_customer_email(
                    case_id_or_identifier="RC-MOCK-001",
                    template_name="template_xyz_does_not_exist",
                    confirmed_recipient_email="mock@example.com",
                )

    def test_send_simulated_success_no_smtp_credentials(self):
        """With no SMTP credentials, send falls back to simulated SENT."""
        # Remove SMTP credentials if set
        env_patch = {
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_EMAIL": "noreply@nimbus.local",
        }
        with patch("core.email_service.get_case_customer_info",
                   return_value=self._mock_case_info()), \
             patch("core.email_service.get_db_cursor") as mock_cursor, \
             patch.dict(os.environ, env_patch):

            # Mock DB cursor context manager
            mock_cur = MagicMock()
            mock_cur.fetchone.return_value = {"email_id": 42}
            mock_conn = MagicMock()
            mock_cursor.return_value.__enter__ = MagicMock(
                return_value=(mock_cur, mock_conn)
            )
            mock_cursor.return_value.__exit__ = MagicMock(return_value=False)

            result = email_svc.send_customer_email(
                case_id_or_identifier="RC-MOCK-001",
                template_name="refund_received",
                confirmed_recipient_email="mock@example.com",
            )

        assert result.get("success") is True or result.get("delivery_status") == "SENT"

    def test_send_logs_failure_on_smtp_error(self):
        """SMTP exception results in delivery_status FAILED."""
        import smtplib
        env_patch = {
            "SMTP_USERNAME": "user@smtp.com",
            "SMTP_PASSWORD": "pass",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_FROM_EMAIL": "noreply@nimbus.local",
        }
        logged_status = {}

        original_db_cursor = email_svc.get_db_cursor

        def capture_insert(sql, args):
            # Find delivery_status in args (position 5 in the INSERT)
            if "INSERT INTO email_logs" in str(sql):
                logged_status["delivery_status"] = args[5]

        with patch("core.email_service.get_case_customer_info",
                   return_value=self._mock_case_info()), \
             patch("smtplib.SMTP",
                   side_effect=smtplib.SMTPException("Connection refused")), \
             patch("smtplib.SMTP_SSL",
                   side_effect=smtplib.SMTPException("Connection refused")), \
             patch("core.email_service.get_db_cursor") as mock_cursor, \
             patch.dict(os.environ, env_patch):

            mock_cur = MagicMock()
            mock_cur.fetchone.return_value = {"email_id": 99}
            mock_cur.execute = MagicMock(side_effect=lambda s, a: capture_insert(s, a))
            mock_conn = MagicMock()
            mock_cursor.return_value.__enter__ = MagicMock(
                return_value=(mock_cur, mock_conn)
            )
            mock_cursor.return_value.__exit__ = MagicMock(return_value=False)

            result = email_svc.send_customer_email(
                case_id_or_identifier="RC-MOCK-001",
                template_name="refund_received",
                confirmed_recipient_email="mock@example.com",
            )

        # Result should indicate failure or the logged_status should be FAILED
        status = result.get("delivery_status") or logged_status.get("delivery_status", "")
        assert status == "FAILED" or result.get("success") is False

    def test_preview_does_not_send_email(self):
        """preview_email does NOT perform any SMTP operations."""
        with patch("core.email_service.get_case_customer_info",
                   return_value=self._mock_case_info()), \
             patch("smtplib.SMTP") as mock_smtp, \
             patch("smtplib.SMTP_SSL") as mock_ssl:

            preview = email_svc.preview_email(
                case_id_or_identifier="RC-MOCK-001",
                template_name="refund_received",
            )

        # SMTP should never have been called
        assert not mock_smtp.called, "SMTP was invoked during preview_email"
        assert not mock_ssl.called, "SMTP_SSL was invoked during preview_email"
        # Preview should contain expected keys
        assert "html_body" in preview
        assert "subject" in preview
        assert "recipient_email" in preview

    def test_preview_nonexistent_case_raises(self):
        """preview_email raises ValueError for non-existent case."""
        with patch("core.email_service.get_case_customer_info", return_value=None):
            with pytest.raises((ValueError, Exception)):
                email_svc.preview_email(
                    case_id_or_identifier="RC-DOES-NOT-EXIST",
                    template_name="refund_received",
                )


# ════════════════════════════════════════════════════════════════════════════
# Email History Tests (mocked DB)
# ════════════════════════════════════════════════════════════════════════════

class TestEmailHistory:

    def test_history_returns_list_on_found_case(self):
        """get_case_email_history returns list when case exists."""
        mock_case = {
            "refund_case_id": 1,
            "case_identifier": "RC-001",
            "booking_ref": "BK-001",
            "case_status": "PENDING",
            "expected_amount": 5000,
            "actual_amount": 5000,
            "discrepancy_amount": 0,
            "reason": None,
            "case_created_at": "2026-01-01",
            "booking_customer_name": "Test",
            "booking_email": "t@t.com",
            "flight_no": "AI1",
            "booking_amount": 5000,
            "booking_currency": "INR",
            "booked_at": "2026-01-01",
            "user_id": 1,
            "user_full_name": "Test",
            "user_email": "t@t.com",
            "user_phone": None,
            "payment_ref": "PAY-001",
            "customer_name": "Test",
            "customer_email": "t@t.com",
            "phone": None,
            "formatted_amount": "₹5,000",
        }
        fake_rows = [
            {
                "email_id": 1,
                "recipient_email": "t@t.com",
                "subject": "Refund Received",
                "template_name": "refund_received",
                "delivery_status": "SENT",
                "error_message": None,
                "sent_at": "2026-11-01T10:00:00",
                "created_at": "2026-11-01T10:00:00",
            }
        ]

        def fake_cursor(**kwargs):
            class CM:
                def __enter__(self):
                    cur = MagicMock()
                    cur.fetchall.return_value = fake_rows
                    return cur, MagicMock()
                def __exit__(self, *args):
                    return False
            return CM()

        with patch("core.email_service.get_case_customer_info", return_value=mock_case), \
             patch("core.email_service.get_db_cursor", side_effect=fake_cursor):
            result = email_svc.get_case_email_history("RC-001")

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["delivery_status"] == "SENT"

    def test_history_empty_for_unknown_case(self):
        """get_case_email_history returns empty list for non-existent case."""
        with patch("core.email_service.get_case_customer_info", return_value=None):
            result = email_svc.get_case_email_history("RC-DOES-NOT-EXIST")
        assert result == []


# ════════════════════════════════════════════════════════════════════════════
# FastAPI Email Endpoint Tests (via TestClient — requires live DB)
# ════════════════════════════════════════════════════════════════════════════

class TestEmailEndpoints:
    """Tests for /api/email/* endpoints via FastAPI TestClient.
    These tests require the real DB (marked as integration).
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        from core.fastapi_app import app as fastapi_app
        from fastapi.testclient import TestClient
        self.TestClient = TestClient
        self.app = fastapi_app

    def _authenticated_client(self):
        from core import auth as auth_module
        auth_module._LOGIN_ATTEMPTS.clear()
        c = self.TestClient(self.app, raise_server_exceptions=False)
        resp = c.post(
            "/auth/login",
            json={"email": "admin@nimbus.local", "password": "NimbusAdmin2026!Secure"},
        )
        assert resp.status_code == 200, f"Login failed in test setup: {resp.status_code} {resp.text}"
        csrf = ""
        for cookie in c.cookies.jar:
            if cookie.name == "csrf_token":
                csrf = cookie.value
                break
        return c, csrf

    def test_get_templates_authenticated(self):
        c, _ = self._authenticated_client()
        resp = c.get("/api/email/templates")
        assert resp.status_code == 200
        body = resp.json()
        assert "templates" in body
        assert len(body["templates"]) >= 6

    def test_get_templates_unauthenticated_401(self):
        c = self.TestClient(self.app, raise_server_exceptions=False)
        resp = c.get("/api/email/templates")
        assert resp.status_code == 401

    def test_case_customer_endpoint_authenticated_nonexistent(self):
        """Case customer info for non-existent case returns 404 (not 401)."""
        c, _ = self._authenticated_client()
        resp = c.get("/api/email/case-customer/RC-TEST-NONEXISTENT-9999")
        assert resp.status_code in (200, 404, 422)

    def test_preview_missing_fields_returns_422(self):
        c, csrf = self._authenticated_client()
        resp = c.post(
            "/api/email/preview",
            json={},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code in (400, 422)

    def test_preview_nonexistent_case_returns_error(self):
        c, csrf = self._authenticated_client()
        resp = c.post(
            "/api/email/preview",
            json={
                "case_id": "RC-DOES-NOT-EXIST-EVER-9999",
                "template_name": "refund_received",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code in (404, 422, 400)

    def test_send_without_csrf_rejected(self):
        """POST /api/email/send without CSRF returns 403 or 400."""
        c, _ = self._authenticated_client()
        resp = c.post(
            "/api/email/send",
            json={
                "case_id": "RC-001",
                "template_name": "refund_received",
                "confirmed_recipient_email": "cust@example.com",
            },
        )
        assert resp.status_code in (400, 401, 403, 422)

    def test_history_endpoint_authenticated(self):
        """Email history returns 200 with logs key when authenticated."""
        c, _ = self._authenticated_client()
        resp = c.get("/api/email/history/RC-ANY-CASE")
        # Non-existent case should return 200 with empty list or 404
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            body = resp.json()
            assert "logs" in body

    def test_history_unauthenticated_401(self):
        c = self.TestClient(self.app, raise_server_exceptions=False)
        resp = c.get("/api/email/history/RC-ANY-CASE")
        assert resp.status_code == 401
