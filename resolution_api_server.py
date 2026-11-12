import json
import os
import sys
import mimetypes
import signal
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.integration import (
    BOOKING_API_URL,
    AIRLINE_API_URL,
    PAYMENT_API_URL,
    fetch_booking_data,
    fetch_airline_data,
    fetch_payment_data,
    fetch_payment_detail,
    convert_to_booking_record,
    convert_to_airline_record,
    convert_to_payment_record,
    adapt_booking,
    adapt_airline,
    adapt_payment,
    convert_to_pipeline_record,
)
from core.models.input_evidence import EvidenceRecord, SourceOrganization
from core.pipeline import create_refund_case
from person3_orchestration.orchestrator import CaseOrchestrator
from db.connection import get_db_cursor, check_db_health, apply_schema

HOST = "127.0.0.1"
PORT = int(os.environ.get("RESOLUTION_PORT", 8000))
FRONTEND_DIR = os.path.join(_REPO_ROOT, "frontend")


# ---------------------------------------------------------------------------
# Database persistence helpers for evidence and reconciliation results
# ---------------------------------------------------------------------------

def _persist_evidence_records(records, booking_ref=None, payment_ref=None, source_name="resolution"):
    """
    Persists a list of EvidenceRecord (core.models.input_evidence) to the
    evidence_records table.  Silently ignores DB errors so reconciliation
    logic is never blocked by persistence issues.
    """
    try:
        with get_db_cursor() as (cur, _):
            for rec in records:
                payload = {
                    "source_organization": str(getattr(rec, "source_organization", "")),
                    "source_record_id":    str(getattr(rec, "source_record_id", "")),
                    "source_status":       str(getattr(rec, "source_status", "")),
                    "customer_name":       getattr(rec, "customer_name", None),
                    "booking_ref":         getattr(rec, "booking_ref", booking_ref),
                    "flight_number":       getattr(rec, "flight_number", None),
                    "flight_date":         getattr(rec, "flight_date", None),
                    "refund_amount":       getattr(rec, "refund_amount", None),
                    "transaction_id":      getattr(rec, "transaction_id", None),
                    "timestamp":           str(getattr(rec, "timestamp", "")),
                }
                # Resolve booking_ref from evidence record or param
                eff_bref = getattr(rec, "booking_ref", None) or booking_ref
                eff_pref = getattr(rec, "transaction_id", None) or payment_ref

                # Only use FK values that actually exist in DB to avoid FK violation
                validated_bref = None
                validated_pref = None
                if eff_bref:
                    cur.execute("SELECT 1 FROM bookings WHERE booking_ref = %s", (eff_bref,))
                    if cur.fetchone():
                        validated_bref = eff_bref
                if eff_pref and eff_pref.startswith("PAY-"):
                    cur.execute("SELECT 1 FROM payments WHERE payment_ref = %s", (eff_pref,))
                    if cur.fetchone():
                        validated_pref = eff_pref

                cur.execute("""
                    INSERT INTO evidence_records
                        (booking_ref, payment_ref, source_type, external_reference,
                         evidence_type, payload)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """, (
                    validated_bref,
                    validated_pref,
                    source_name,
                    str(getattr(rec, "source_record_id", "")),
                    str(getattr(rec, "source_organization", "")),
                    json.dumps(payload),
                ))
    except Exception:
        pass  # Persistence errors must not block reconciliation logic


def _persist_refund_case(app_case, booking_ref=None, payment_ref=None):
    """
    Persists the ApplicationReadyCase result as a refund_cases row.
    Returns the new refund_case_id or None on failure.
    """
    try:
        case_id = app_case.meta.case_id
        state = app_case.state.get("normalized", "UNRESOLVED")
        rec_status = app_case.reconciliation.get("status", "UNRESOLVED")
        jev = app_case.jev_decision_assessment

        # Map normalized state to valid case_status enum
        status_map = {
            "COMPLETED": "COMPLETED",
            "BLOCKED": "BLOCKED",
            "PROCESSING_NORMALLY": "PROCESSING_NORMALLY",
            "CONFLICTING": "CONFLICTING",
            "UNRESOLVED": "UNRESOLVED",
        }
        case_status = status_map.get(state, "UNRESOLVED")

        # Extract amounts from raw refund case if available
        raw = app_case.raw_refund_case
        expected_amount = float(getattr(raw, "refund_amount", None) or 0)
        actual_amount = 0.0
        if hasattr(raw, "source_records"):
            actuals = [
                float(r.refund_amount)
                for r in raw.source_records
                if getattr(r, "refund_amount", None) is not None
            ]
            actual_amount = max(actuals) if actuals else expected_amount

        discrepancy = round(expected_amount - actual_amount, 2)

        result_payload = {
            "reconciliation_status": rec_status,
            "confidence_score": app_case.reconciliation.get("confidence_score"),
            "review_required": app_case.reconciliation.get("review_required", False),
            "jev_summary": jev.summary if jev else None,
            "jev_recommended_action": jev.recommended_action if jev else None,
            "jev_escalation_priority": jev.escalation_priority if jev else None,
        }

        # Validate FK references
        validated_bref = None
        validated_pref = None
        with get_db_cursor() as (cur, _):
            if booking_ref:
                cur.execute("SELECT 1 FROM bookings WHERE booking_ref = %s", (booking_ref,))
                if cur.fetchone():
                    validated_bref = booking_ref
            if payment_ref:
                cur.execute("SELECT 1 FROM payments WHERE payment_ref = %s", (payment_ref,))
                if cur.fetchone():
                    validated_pref = payment_ref

            cur.execute("""
                INSERT INTO refund_cases
                    (case_identifier, booking_ref, payment_ref, case_status,
                     expected_amount, actual_amount, discrepancy_amount,
                     reason, reconciliation_result, review_required)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (case_identifier) DO UPDATE SET
                    case_status           = EXCLUDED.case_status,
                    expected_amount       = EXCLUDED.expected_amount,
                    actual_amount         = EXCLUDED.actual_amount,
                    discrepancy_amount    = EXCLUDED.discrepancy_amount,
                    reason                = EXCLUDED.reason,
                    reconciliation_result = EXCLUDED.reconciliation_result,
                    review_required       = EXCLUDED.review_required,
                    updated_at            = CURRENT_TIMESTAMP
                RETURNING refund_case_id
            """, (
                case_id,
                validated_bref,
                validated_pref,
                case_status,
                expected_amount,
                actual_amount,
                discrepancy,
                getattr(jev, "summary", None) if jev else None,
                json.dumps(result_payload),
                app_case.reconciliation.get("review_required", False),
            ))
            row = cur.fetchone()
            return int(row["refund_case_id"]) if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pipeline functions (unchanged logic, now with persistence hooks)
# ---------------------------------------------------------------------------

def run_pipeline_for_booking(booking_ref: str):
    """
    Fetches live microservice data for a given bookingRef,
    passes it through Person 1 adapters, Person 2 pipeline,
    and Person 3 orchestrator + Jev decision engine.
    Results are persisted to evidence_records and refund_cases.
    """
    raw_bookings = fetch_booking_data()
    raw_airline = fetch_airline_data()
    raw_payments = fetch_payment_data()

    b_raw = [r for r in raw_bookings if r.get("bookingRef") == booking_ref]
    a_raw = [r for r in raw_airline if r.get("bookingRef") == booking_ref]
    p_raw = [r for r in raw_payments if r.get("bookingRef") == booking_ref]

    pipeline_records = []

    # Booking adapter
    for r in b_raw:
        source_rec = convert_to_booking_record(r)
        adapted = adapt_booking(source_rec)
        pipeline_records.append(convert_to_pipeline_record(adapted, "booking"))

    # Airline adapter
    for r in a_raw:
        source_rec = convert_to_airline_record(r)
        adapted = adapt_airline(source_rec)
        adapted.booking_information["booking_id"] = r.get("bookingRef")
        pipeline_records.append(convert_to_pipeline_record(adapted, "airline"))

    # Payment adapter
    for r in p_raw:
        pref = r.get("paymentRef")
        detail = fetch_payment_detail(pref) if pref else {}
        source_rec = convert_to_payment_record(r, detail)
        adapted = adapt_payment(source_rec)
        adapted.booking_information["booking_id"] = r.get("bookingRef")
        pipeline_records.append(convert_to_pipeline_record(adapted, "payment"))

    if not pipeline_records:
        raise ValueError(f"No records found across services for bookingRef '{booking_ref}'")

    # Person 2 Pipeline
    refund_case = create_refund_case(pipeline_records)

    # Person 3 Orchestrator
    orchestrator = CaseOrchestrator()
    app_case = orchestrator.evaluate_refund_case(refund_case)

    # Persist evidence and result (non-blocking)
    payment_ref = p_raw[0].get("paymentRef") if p_raw else None
    _persist_evidence_records(pipeline_records, booking_ref=booking_ref, payment_ref=payment_ref, source_name="live_pipeline")
    _persist_refund_case(app_case, booking_ref=booking_ref, payment_ref=payment_ref)

    return app_case


def run_test_scenario(scenario_key: str):
    """Executes preset scenarios matching core/test_cases.py."""
    t0 = datetime(2026, 10, 1, 10, 0)
    key = scenario_key.strip().upper()

    if key == "A":
        # Scenario A: Completed Refund
        records = [
            EvidenceRecord(source_organization=SourceOrganization.BOOKING_PLATFORM, source_record_id="BK-1", source_status="REFUND_REQUESTED", passenger="John Doe", booking_ref="BK123", refund_amount=250.0, timestamp=t0),
            EvidenceRecord(source_organization=SourceOrganization.AIRLINE, source_record_id="AIR-1", source_status="REFUND_AUTHORIZED", passenger="John Doe", booking_ref="BK123", refund_amount=250.0, timestamp=t0 + timedelta(minutes=30)),
            EvidenceRecord(source_organization=SourceOrganization.PAYMENT_PROVIDER, source_record_id="PAY-1", source_status="COMPLETED", passenger="John Doe", booking_ref="BK123", refund_amount=250.0, timestamp=t0 + timedelta(hours=2)),
            EvidenceRecord(source_organization=SourceOrganization.ISSUING_BANK, source_record_id="BNK-1", source_status="CREDITED", passenger="John Doe", booking_ref="BK123", refund_amount=250.0, timestamp=t0 + timedelta(hours=5))
        ]
    elif key == "B":
        # Scenario B: Blocked Refund (SLA Exceeded)
        records = [
            EvidenceRecord(source_organization=SourceOrganization.BOOKING_PLATFORM, source_record_id="BK-2", source_status="REFUND_REQUESTED", passenger="Jane Smith", booking_ref="BK456", refund_amount=180.0, timestamp=t0),
            EvidenceRecord(source_organization=SourceOrganization.AIRLINE, source_record_id="AIR-2", source_status="REFUND_AUTHORIZED", passenger="Jane Smith", booking_ref="BK456", refund_amount=180.0, timestamp=t0 + timedelta(minutes=30)),
            EvidenceRecord(source_organization=SourceOrganization.PAYMENT_PROVIDER, source_record_id="PAY-2", source_status="REFUND_INITIATED", passenger="Jane Smith", booking_ref="BK456", refund_amount=180.0, timestamp=t0 + timedelta(days=3))
        ]
    elif key == "C":
        # Scenario C: Normal Processing within SLA
        records = [
            EvidenceRecord(source_organization=SourceOrganization.AIRLINE, source_record_id="AIR-3", source_status="REFUND_AUTHORIZED", passenger="Alice N", booking_ref="BK789", refund_amount=300.0, timestamp=t0),
            EvidenceRecord(source_organization=SourceOrganization.PAYMENT_PROVIDER, source_record_id="PAY-3", source_status="PROCESSING", passenger="Alice N", booking_ref="BK789", refund_amount=300.0, timestamp=t0 + timedelta(minutes=15))
        ]
    elif key == "D":
        # Scenario D: Unresolved Match
        records = [
            EvidenceRecord(source_organization=SourceOrganization.BOOKING_PLATFORM, source_record_id="BK-4", source_status="REFUND_REQUESTED", passenger="Bob Wright", booking_ref="BK999", refund_amount=500.0, timestamp=t0),
            EvidenceRecord(source_organization=SourceOrganization.AIRLINE, source_record_id="AIR-4", source_status="REFUND_AUTHORIZED", passenger="Charlie Brown", booking_ref="BK000", refund_amount=100.0, timestamp=t0)
        ]
    else:
        raise ValueError(f"Unknown scenario key: {scenario_key}")

    refund_case = create_refund_case(records)
    orchestrator = CaseOrchestrator()
    app_case = orchestrator.evaluate_refund_case(refund_case)

    # Persist scenario evidence and result
    _persist_evidence_records(records, source_name=f"scenario_{key}")
    _persist_refund_case(app_case)

    return app_case


def run_custom_records(raw_records: list):
    """
    Parses custom evidence records submitted from the frontend,
    and runs them through create_refund_case and CaseOrchestrator.
    """
    parsed_records = []
    org_map = {
        "booking": SourceOrganization.BOOKING_PLATFORM,
        "booking platform": SourceOrganization.BOOKING_PLATFORM,
        "airline": SourceOrganization.AIRLINE,
        "payment": SourceOrganization.PAYMENT_PROVIDER,
        "payment provider": SourceOrganization.PAYMENT_PROVIDER,
        "bank": SourceOrganization.ISSUING_BANK,
        "issuing bank": SourceOrganization.ISSUING_BANK,
    }

    for item in raw_records:
        org_raw = str(item.get("source_organization", "booking platform")).strip().lower()
        org = org_map.get(org_raw, SourceOrganization.BOOKING_PLATFORM)

        # Parse timestamp
        ts_raw = item.get("timestamp")
        if isinstance(ts_raw, str) and ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                try:
                    ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M")
                except ValueError:
                    ts = datetime.now()
        else:
            ts = datetime.now()

        amt = item.get("refund_amount")
        if amt is not None and str(amt).strip() != "":
            try:
                amt = float(amt)
            except (ValueError, TypeError):
                amt = None
        else:
            amt = None

        rec = EvidenceRecord(
            source_organization=org,
            source_record_id=str(item.get("source_record_id") or "REC-1"),
            source_status=str(item.get("source_status") or "PENDING").upper(),
            passenger=item.get("passenger") or item.get("customer_name"),
            booking_ref=item.get("booking_ref"),
            flight=item.get("flight") or item.get("flight_number"),
            flight_date=item.get("flight_date"),
            refund_amount=amt,
            transaction_id=item.get("transaction_id"),
            timestamp=ts
        )
        parsed_records.append(rec)

    if not parsed_records:
        raise ValueError("At least one evidence record must be provided.")

    refund_case = create_refund_case(parsed_records)
    orchestrator = CaseOrchestrator()
    app_case = orchestrator.evaluate_refund_case(refund_case)

    # Persist custom evidence and result
    _persist_evidence_records(parsed_records, source_name="custom")
    _persist_refund_case(app_case)

    return app_case


# ---------------------------------------------------------------------------
# HTTP Handler (all original endpoints preserved)
# ---------------------------------------------------------------------------

class ResolutionHubHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def send_json(self, status, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "resolution"})
            return

        # Health & Microservices status
        if path == "/api/status":
            status_data = {
                "booking_service": False,
                "airline_service": False,
                "payment_service": False,
                "bookings_count": 0,
                "airline_count": 0,
                "payment_count": 0,
                "available_bookings": []
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

            self.send_json(200, status_data)
            return

        # Live reconciliation via query param
        if path == "/api/reconcile/live":
            booking_ref = params.get("bookingRef", [None])[0]
            if not booking_ref:
                self.send_json(400, {"error": "Missing 'bookingRef' parameter."})
                return
            try:
                app_case = run_pipeline_for_booking(booking_ref.upper())
                self.send_json(200, app_case.model_dump(mode="json"))
            except Exception as e:
                self.send_json(400, {"error": str(e)})
            return

        # List persisted refund cases
        if path == "/api/reconcile/cases":
            try:
                with get_db_cursor() as (cur, _):
                    cur.execute("""
                        SELECT case_identifier, booking_ref, payment_ref, case_status,
                               expected_amount, actual_amount, discrepancy_amount,
                               review_required, created_at
                        FROM refund_cases ORDER BY created_at DESC LIMIT 100
                    """)
                    cases = cur.fetchall()
                self.send_json(200, {
                    "count": len(cases),
                    "cases": [dict(c) for c in cases]
                })
            except Exception as err:
                self.send_json(500, {"error": str(err)})
            return

        # Static file serving from frontend/
        req_path = path.lstrip("/")
        if not req_path or req_path == "":
            req_path = "index.html"

        file_path = os.path.join(FRONTEND_DIR, req_path)
        # Security check: must remain inside FRONTEND_DIR
        if os.path.commonpath([FRONTEND_DIR, os.path.abspath(file_path)]) == FRONTEND_DIR and os.path.isfile(file_path):
            mime_type, _ = mimetypes.guess_type(file_path)
            if not mime_type:
                mime_type = "text/plain"
            if mime_type.startswith("text/") or mime_type in ["application/javascript", "application/json"]:
                mime_type += "; charset=utf-8"

            with open(file_path, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_json(404, {"error": f"File not found: {path}"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"

        try:
            payload = json.loads(raw_body)
        except Exception:
            payload = {}

        # 1. Live reconciliation for a booking
        if path == "/api/reconcile/live":
            booking_ref = payload.get("bookingRef")
            if not booking_ref:
                self.send_json(400, {"error": "Missing bookingRef in JSON body"})
                return
            try:
                app_case = run_pipeline_for_booking(booking_ref.upper())
                self.send_json(200, app_case.model_dump(mode="json"))
            except Exception as err:
                self.send_json(400, {"error": str(err)})
            return

        # 2. Preset test case scenarios (Scenario A, B, C, D)
        if path == "/api/reconcile/scenario":
            scenario = payload.get("scenario", "A")
            try:
                app_case = run_test_scenario(scenario)
                self.send_json(200, {
                    "scenario": scenario,
                    "case": app_case.model_dump(mode="json")
                })
            except Exception as err:
                self.send_json(400, {"error": str(err)})
            return

        # 3. Custom evidence inputs from frontend
        if path == "/api/reconcile/custom":
            records = payload.get("records", [])
            try:
                app_case = run_custom_records(records)
                self.send_json(200, app_case.model_dump(mode="json"))
            except Exception as err:
                self.send_json(400, {"error": str(err)})
            return

        self.send_json(404, {"error": "Unknown endpoint"})


if __name__ == "__main__":
    import uvicorn
    from core.bootstrap_admin import bootstrap_admin

    healthy, msg = check_db_health()
    if not healthy:
        print(f"FATAL: Cannot connect to database: {msg}")
        sys.exit(1)
    print(f"Database OK: {msg[:60]}")

    # Apply schema safely unless skipped by launcher
    if os.environ.get("SKIP_SCHEMA_INIT", "").lower() not in ("1", "true", "yes"):
        try:
            apply_schema()
            bootstrap_admin()
        except Exception as err:
            print(f"FATAL: Schema initialization failed: {err}")
            sys.exit(1)

    print("=" * 70)
    print(f"Nimbus Flight Refund Resolution Hub (FastAPI) running at:")
    print(f"  -> http://{HOST}:{PORT}/login.html   (Admin Login)")
    print(f"  -> http://{HOST}:{PORT}/             (Admin Resolution Dashboard)")
    print(f"  -> http://{HOST}:{PORT}/booking.html (Voyago Booking Portal)")
    print(f"  -> http://{HOST}:{PORT}/airline.html (Meridian Air Portal)")
    print(f"  -> http://{HOST}:{PORT}/payment.html (Zenpay Gateway Portal)")
    print("=" * 70)
    print("Press Ctrl+C to stop.")

    uvicorn.run("core.fastapi_app:app", host=HOST, port=PORT, log_level="warning")
