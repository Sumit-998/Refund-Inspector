#!/usr/bin/env python3
"""
run.py — Unified Launcher for Nimbus-Hackathon API Microservices

Responsibilities:
1. Validates PostgreSQL connectivity.
2. Safely initializes relational schema exactly once before starting any servers.
3. Automatically seeds initial development records idempotently.
4. Spawns all four API microservices:
   - Voyago Booking API      (:8001)
   - Meridian Air API        (:8002)
   - Zenpay Payment API      (:8003)
   - Resolution Hub & UI     (:8000)
5. Streams and prefixes real-time logs from each microservice to stdout.
6. Catches Ctrl+C (SIGINT) and SIGTERM to terminate all child processes cleanly.
"""

import os
import sys
import time
import signal
import threading
import subprocess
from typing import List, Dict, Any, Optional

# Ensure repository root is on sys.path
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO_ROOT, ".env"))
except ImportError:
    pass

from db.connection import check_db_health, apply_schema, get_database_url, get_db_cursor
from db.seed import seed_bookings, seed_tickets, seed_payments
from core.bootstrap_admin import bootstrap_admin

# Terminal ANSI Color Codes for Prefixed Real-Time Logs
COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"
COLOR_LAUNCHER = "\033[1;34m"   # Bold Blue
COLOR_BOOKING = "\033[96m"      # Cyan
COLOR_AIRLINE = "\033[92m"      # Green
COLOR_PAYMENT = "\033[93m"      # Yellow
COLOR_RESOLUTION = "\033[95m"   # Magenta
COLOR_RED = "\033[91m"          # Red


def log_launcher(msg: str) -> None:
    print(f"{COLOR_LAUNCHER}[Launcher]      {COLOR_RESET} {msg}", flush=True)


def log_error(msg: str) -> None:
    print(f"{COLOR_RED}[Launcher ERROR]{COLOR_RESET} {msg}", flush=True)


SERVERS = [
    {
        "name": "Booking API",
        "script": "booking_api_server.py",
        "env_port": "BOOKING_PORT",
        "default_port": 8001,
        "tag": "[Booking   | {port}]",
        "color": COLOR_BOOKING,
    },
    {
        "name": "Airline API",
        "script": "airline_api_server.py",
        "env_port": "AIRLINE_PORT",
        "default_port": 8002,
        "tag": "[Airline   | {port}]",
        "color": COLOR_AIRLINE,
    },
    {
        "name": "Payment API",
        "script": "payment_api_server.py",
        "env_port": "PAYMENT_PORT",
        "default_port": 8003,
        "tag": "[Payment   | {port}]",
        "color": COLOR_PAYMENT,
    },
    {
        "name": "Resolution Hub",
        "script": "resolution_api_server.py",
        "env_port": "RESOLUTION_PORT",
        "default_port": 8000,
        "tag": "[Resolution| {port}]",
        "color": COLOR_RESOLUTION,
    },
]


class ServiceLauncher:
    def __init__(self, skip_seed: bool = False):
        self.skip_seed = skip_seed
        self.processes: List[subprocess.Popen] = []
        self.threads: List[threading.Thread] = []
        self.shutting_down = False
        self._lock = threading.Lock()

    def prepare_database(self) -> None:
        """
        Validates PostgreSQL connectivity, applies schema safely, and seeds demo data.
        """
        db_url = get_database_url()
        log_launcher(f"Checking PostgreSQL connectivity ({db_url})...")
        healthy, msg = check_db_health(db_url)
        if not healthy:
            log_error(f"Cannot connect to PostgreSQL database: {msg}")
            log_error("Please make sure PostgreSQL is running: systemctl start postgresql")
            sys.exit(1)

        log_launcher(f"Database reachable: {msg.splitlines()[0]}")

        # Safe schema initialization under PostgreSQL advisory lock
        log_launcher("Applying relational schema (idempotent advisory lock)...")
        try:
            apply_schema(database_url=db_url)
            log_launcher("Schema verified and ready.")
        except Exception as err:
            log_error(f"Schema initialization failed: {err}")
            sys.exit(1)

        # Ensure Admin account is bootstrapped from environment
        log_launcher("Ensuring Admin account is bootstrapped...")
        try:
            boot_ok, boot_msg = bootstrap_admin(database_url=db_url)
            log_launcher(boot_msg)
        except Exception as err:
            log_launcher(f"Notice: Admin bootstrap notice: {err}")

        # Idempotent demo data seeding for development
        if not self.skip_seed and "test" not in db_url.lower():
            log_launcher("Ensuring development seed records are loaded...")
            try:
                with get_db_cursor(database_url=db_url, commit=True) as (cur, _):
                    b_count = seed_bookings(cur)
                    t_count = seed_tickets(cur)
                    p_count = seed_payments(cur)
                log_launcher(f"Seed complete: {b_count} bookings, {t_count} tickets, {p_count} payments loaded/verified.")
            except Exception as err:
                log_launcher(f"Notice: Seed completed with warning: {err}")

    def _stream_output(self, proc: subprocess.Popen, tag: str, color: str) -> None:
        """
        Reads lines from a child process stdout pipe and prints them prefixed in real time.
        """
        assert proc.stdout is not None
        try:
            for raw_line in iter(proc.stdout.readline, b""):
                if not raw_line and proc.poll() is not None:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    print(f"{color}{tag}{COLOR_RESET} {line}", flush=True)
        except Exception:
            pass

    def start_servers(self) -> None:
        """
        Starts all four API microservices as child subprocesses with piped logs.
        """
        python_exe = sys.executable

        for srv in SERVERS:
            port = int(os.environ.get(srv["env_port"], srv["default_port"]))
            tag = srv["tag"].format(port=port)
            color = srv["color"]
            script_path = os.path.join(_REPO_ROOT, srv["script"])

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            # Schema already applied once by launcher; signal children to skip redundant init
            env["SKIP_SCHEMA_INIT"] = "1"
            env[srv["env_port"]] = str(port)

            log_launcher(f"Starting {srv['name']} on http://127.0.0.1:{port}...")

            proc = subprocess.Popen(
                [python_exe, script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=_REPO_ROOT,
                env=env,
                bufsize=0,
            )
            self.processes.append(proc)

            t = threading.Thread(
                target=self._stream_output,
                args=(proc, tag, color),
                daemon=True,
                name=f"Logger-{srv['name']}",
            )
            t.start()
            self.threads.append(t)

        log_launcher("=" * 60)
        log_launcher("All 4 services are starting up!")
        log_launcher("  - Voyago Booking:  http://127.0.0.1:8001")
        log_launcher("  - Meridian Air:    http://127.0.0.1:8002")
        log_launcher("  - Zenpay Payment:  http://127.0.0.1:8003")
        log_launcher("  - Resolution Hub:  http://127.0.0.1:8000")
        log_launcher("=" * 60)
        log_launcher("Press Ctrl+C to stop all servers cleanly.")

    def shutdown(self, signum: Optional[int] = None) -> None:
        """
        Shuts down all child processes gracefully.
        """
        with self._lock:
            if self.shutting_down:
                return
            self.shutting_down = True

        sig_name = signal.Signals(signum).name if signum else "manual"
        print("", flush=True)
        log_launcher(f"Received shutdown request ({sig_name}). Stopping all services...")

        # First request polite shutdown via SIGINT / SIGTERM to child processes
        for proc in self.processes:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGINT)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

        # Wait up to 3 seconds for clean termination
        timeout_sec = 3.0
        start_wait = time.time()
        for proc in self.processes:
            remaining = max(0.1, timeout_sec - (time.time() - start_wait))
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                # Force terminate if still running
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

        log_launcher("All microservice child processes terminated cleanly.")

    def run(self) -> int:
        """
        Main run loop with signal trapping.
        """
        def _handle_signal(signum, frame):
            self.shutdown(signum=signum)
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        self.prepare_database()
        self.start_servers()

        # Monitoring loop: if any child exits with non-zero exit code unexpectedly, trigger shutdown
        try:
            while not self.shutting_down:
                time.sleep(0.3)
                for i, proc in enumerate(self.processes):
                    code = proc.poll()
                    if code is not None and not self.shutting_down:
                        srv = SERVERS[i]
                        log_launcher(f"Service {srv['name']} exited unexpectedly with code {code}.")
                        self.shutdown()
                        return code if code != 0 else 1
        except KeyboardInterrupt:
            self.shutdown(signum=signal.SIGINT)

        return 0


def main():
    skip_seed = "--no-seed" in sys.argv
    launcher = ServiceLauncher(skip_seed=skip_seed)
    code = launcher.run()
    sys.exit(code)


if __name__ == "__main__":
    main()
