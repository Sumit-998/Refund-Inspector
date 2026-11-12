"""
tests/test_startup_concurrency.py

Tests for:
1. Concurrent schema initialization across multiple threads and processes
   without 'tuple concurrently updated' or lock conflicts.
2. PostgreSQL advisory lock mutual exclusion during schema migrations.
3. run.py unified launcher process lifecycle: starts all four API servers,
   streams logs, and terminates all child processes cleanly on SIGINT (Ctrl+C).
"""

import os
import sys
import time
import signal
import socket
import urllib.request
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from db.connection import (
    apply_schema,
    get_database_url,
    get_db_cursor,
    SCHEMA_MIGRATION_LOCK_ID,
    CURRENT_SCHEMA_VERSION,
    REQUIRED_CORE_TABLES,
)

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres@localhost:5432/nimbus_test_db",
)


def _is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _wait_for_port(port: int, timeout: float = 8.0, host: str = "127.0.0.1") -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if _is_port_open(port, host):
            return True
        time.sleep(0.1)
    return False


def _wait_for_port_closed(port: int, timeout: float = 6.0, host: str = "127.0.0.1") -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if not _is_port_open(port, host):
            return True
        time.sleep(0.1)
    return not _is_port_open(port, host)


class TestConcurrentSchemaInitialization:
    """Verifies that apply_schema safely serializes and prevents tuple concurrently updated."""

    def test_concurrent_apply_schema_threads(self):
        """
        Spawns 10 concurrent threads all executing apply_schema on the test database.
        Must succeed without raising 'tuple concurrently updated' or deadlocks.
        """
        concurrency = 10
        results = []
        errors = []

        def _worker(thread_id: int):
            try:
                success = apply_schema(database_url=TEST_DB_URL)
                return thread_id, success, None
            except Exception as exc:
                return thread_id, False, exc

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(_worker, i) for i in range(concurrency)]
            for fut in as_completed(futures):
                tid, ok, err = fut.result()
                if ok:
                    results.append(tid)
                else:
                    errors.append((tid, err))

        assert len(errors) == 0, f"Concurrent apply_schema failed with errors: {errors}"
        assert len(results) == concurrency

        # Verify all tables exist and migration is recorded
        with get_db_cursor(database_url=TEST_DB_URL, commit=False) as (cur, _):
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = ANY(%s);
            """, (list(REQUIRED_CORE_TABLES),))
            cnt = cur.fetchone()["cnt"]
            assert cnt == len(REQUIRED_CORE_TABLES)

            cur.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s;",
                (CURRENT_SCHEMA_VERSION,),
            )
            assert cur.fetchone() is not None

    def test_concurrent_apply_schema_with_force(self):
        """
        Spawns 6 concurrent threads all executing apply_schema with force=True.
        Forces the DDL execution path under the advisory lock to confirm that
        advisory lock mutual exclusion completely avoids PostgreSQL catalog race conditions.
        """
        concurrency = 6
        results = []
        errors = []

        def _worker(thread_id: int):
            try:
                success = apply_schema(database_url=TEST_DB_URL, force=True)
                return thread_id, success, None
            except Exception as exc:
                return thread_id, False, exc

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(_worker, i) for i in range(concurrency)]
            for fut in as_completed(futures):
                tid, ok, err = fut.result()
                if ok:
                    results.append(tid)
                else:
                    errors.append((tid, err))

        assert len(errors) == 0, f"Concurrent forced apply_schema failed: {errors}"
        assert len(results) == concurrency

    def test_advisory_lock_prevents_simultaneous_execution(self):
        """
        Tests that pg_advisory_xact_lock effectively blocks a second transaction
        until the first transaction commits or rolls back.
        """
        lock_held = False
        second_acquired = False

        with get_db_cursor(database_url=TEST_DB_URL, commit=False) as (cur1, conn1):
            cur1.execute("SELECT pg_advisory_xact_lock(%s);", (SCHEMA_MIGRATION_LOCK_ID,))
            lock_held = True

            # In a second connection, try-lock should report false (lock held by conn1)
            with get_db_cursor(database_url=TEST_DB_URL, commit=False) as (cur2, _):
                cur2.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked;", (SCHEMA_MIGRATION_LOCK_ID,))
                res = cur2.fetchone()["locked"]
                assert res is False, "Second connection should NOT have acquired advisory lock while first holds it"

            # Rollback / exit conn1 releases lock
            conn1.rollback()

        # Now second connection should be able to acquire it
        with get_db_cursor(database_url=TEST_DB_URL, commit=False) as (cur3, conn3):
            cur3.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked;", (SCHEMA_MIGRATION_LOCK_ID,))
            res3 = cur3.fetchone()["locked"]
            assert res3 is True, "Second connection should acquire advisory lock after first released it"
            conn3.rollback()


class TestLauncherLifecycle:
    """Verifies that run.py launcher starts all 4 servers and cleanly shuts down on SIGINT."""

    def test_launcher_starts_and_terminates_cleanly_on_sigint(self):
        """
        Starts run.py with custom test ports (9001, 9002, 9003, 9000).
        Verifies HTTP /health responds on all 4 services.
        Sends SIGINT (Ctrl+C) to run.py.
        Verifies run.py exits with 0 and all 4 ports are released.
        """
        ports = {
            "BOOKING_PORT": 9001,
            "AIRLINE_PORT": 9002,
            "PAYMENT_PORT": 9003,
            "RESOLUTION_PORT": 9000,
        }

        # Ensure test ports are initially free
        for p in ports.values():
            assert not _is_port_open(p), f"Test port {p} is already in use before test run!"

        env = os.environ.copy()
        for k, v in ports.items():
            env[k] = str(v)
        env["DATABASE_URL"] = TEST_DB_URL
        env["PYTHONUNBUFFERED"] = "1"

        run_script = os.path.join(_REPO_ROOT, "run.py")

        # Launch run.py as a process group
        proc = subprocess.Popen(
            [sys.executable, run_script, "--no-seed"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=_REPO_ROOT,
            env=env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )

        try:
            # Wait for all 4 servers to be listening and healthy
            for name, port in ports.items():
                open_ok = _wait_for_port(port, timeout=8.0)
                assert open_ok, f"Server on port {port} ({name}) failed to start in time!"

                # Perform HTTP health check
                url = f"http://127.0.0.1:{port}/health"
                with urllib.request.urlopen(url, timeout=3.0) as resp:
                    assert resp.status == 200, f"Health check on {url} returned {resp.status}"

            # Simulate Ctrl+C by sending SIGINT to the launcher process group
            if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            else:
                proc.send_signal(signal.SIGINT)

            # Wait for launcher process to shut down cleanly
            try:
                exit_code = proc.wait(timeout=6.0)
                assert exit_code == 0, f"Launcher exited with unexpected exit code: {exit_code}"
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail("Launcher did not shut down within 6.0 seconds after SIGINT!")

            # Verify that all 4 server ports are closed and child processes terminated
            for name, port in ports.items():
                closed = _wait_for_port_closed(port, timeout=5.0)
                assert closed, f"Port {port} ({name}) remained open after launcher shutdown!"

        finally:
            # Cleanup safeguard in case of assertion failure
            if proc.poll() is None:
                try:
                    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
