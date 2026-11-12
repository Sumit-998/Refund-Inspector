"""
Database Connection and Utility Module for Nimbus-Hackathon
Using psycopg 3 (psycopg) with connection pooling and context managers.
"""

import os
import sys
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg
from psycopg.rows import dict_row

DEFAULT_DEV_DB = "postgresql://postgres@localhost:5432/nimbus_db"
DEFAULT_TEST_DB = "postgresql://postgres@localhost:5432/nimbus_test_db"


def get_database_url(url: Optional[str] = None) -> str:
    """
    Returns the configured database URL.
    Order of preference:
    1. Explicit url parameter passed to function
    2. DATABASE_URL environment variable
    3. Default local development database
    """
    if url:
        return url
    return os.environ.get("DATABASE_URL", DEFAULT_DEV_DB)


def get_connection(database_url: Optional[str] = None) -> psycopg.Connection:
    """
    Opens a new database connection using psycopg 3.
    Caller is responsible for closing or using with context manager.
    """
    conn_str = get_database_url(database_url)
    return psycopg.connect(conn_str)


@contextmanager
def get_db_cursor(database_url: Optional[str] = None, commit: bool = True):
    """
    Context manager providing a dictionary cursor and handling transactions cleanly.
    Automatically commits if commit=True and no exceptions are raised.
    Rolls back automatically upon any error.
    Closes both cursor and connection on exit.
    """
    conn_str = get_database_url(database_url)
    conn = psycopg.connect(conn_str)
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur, conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_db_health(database_url: Optional[str] = None) -> Tuple[bool, str]:
    """
    Verifies that the database is reachable and operational.
    Returns (True, version_string) on success or (False, error_message) on failure.
    """
    try:
        with get_db_cursor(database_url=database_url, commit=False) as (cur, _):
            cur.execute("SELECT version();")
            row = cur.fetchone()
            if row:
                return True, row.get("version", "PostgreSQL Connected")
            return True, "Connected"
    except Exception as err:
        return False, str(err)


def execute_query(
    sql: str,
    params: Optional[Union[Tuple, List, Dict]] = None,
    fetch: str = "all",
    database_url: Optional[str] = None,
    commit: bool = True,
) -> Any:
    """
    Executes a parameterized SQL query safely.
    fetch options:
      - 'all': returns all matching rows as dictionaries
      - 'one': returns the first matching row or None
      - 'none': returns rowcount
    """
    with get_db_cursor(database_url=database_url, commit=commit) as (cur, _):
        cur.execute(sql, params or ())
        if fetch == "all":
            return cur.fetchall()
        elif fetch == "one":
            return cur.fetchone()
        elif fetch == "none":
            return cur.rowcount
        else:
            raise ValueError(f"Unknown fetch mode: {fetch}")


SCHEMA_MIGRATION_LOCK_ID = 74291847129841
CURRENT_SCHEMA_VERSION = "002_admin_auth_and_email"
REQUIRED_CORE_TABLES = (
    "bookings", "airline_tickets", "payments", "evidence_records", "refund_cases",
    "admins", "admin_sessions", "users", "email_logs"
)


def apply_schema(
    database_url: Optional[str] = None,
    schema_file: Optional[str] = None,
    force: bool = False,
) -> bool:
    """
    Applies the schema file (db/schema.sql) idempotently and concurrency-safely.
    Uses a PostgreSQL transaction-level advisory lock (pg_advisory_xact_lock)
    to eliminate race conditions (such as 'tuple concurrently updated') when multiple
    services or test workers start simultaneously.
    Never uses destructive DROP commands.
    """
    if not schema_file:
        schema_file = os.path.join(os.path.dirname(__file__), "schema.sql")

    if not os.path.isfile(schema_file):
        raise FileNotFoundError(f"Schema file not found: {schema_file}")

    with open(schema_file, "r", encoding="utf-8") as f:
        sql = f.read()

    with get_db_cursor(database_url=database_url, commit=True) as (cur, conn):
        # 1. Acquire transaction-level advisory lock across database
        cur.execute("SELECT pg_advisory_xact_lock(%s);", (SCHEMA_MIGRATION_LOCK_ID,))

        # 2. Check if schema is already fully initialized
        if not force:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'public' AND table_name = 'schema_migrations'
                ) AS mig_exists;
            """)
            mig_res = cur.fetchone()
            if mig_res and mig_res.get("mig_exists"):
                cur.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s;",
                    (CURRENT_SCHEMA_VERSION,),
                )
                if cur.fetchone():
                    # Verify all core tables exist
                    cur.execute("""
                        SELECT COUNT(*) AS cnt FROM information_schema.tables 
                        WHERE table_schema = 'public' 
                        AND table_name = ANY(%s);
                    """, (list(REQUIRED_CORE_TABLES),))
                    row = cur.fetchone()
                    if row and row.get("cnt") == len(REQUIRED_CORE_TABLES):
                        return True

        # 3. Apply schema DDL under lock
        cur.execute(sql)

        # 4. Record migration version
        cur.execute("""
            INSERT INTO schema_migrations (version, description)
            VALUES (%s, %s)
            ON CONFLICT (version) DO NOTHING;
        """, (CURRENT_SCHEMA_VERSION, "Admin auth, customer users directory, and email logging tables"))

    return True


if __name__ == "__main__":
    url = get_database_url()
    print(f"Testing connection to: {url}")
    healthy, msg = check_db_health()
    if healthy:
        print(f"SUCCESS: {msg}")
        print("Applying schema if not exists...")
        apply_schema()
        print("Schema successfully applied!")
    else:
        print(f"FAILURE: Could not connect to database: {msg}")
        sys.exit(1)
