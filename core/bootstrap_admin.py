"""
core/bootstrap_admin.py — Secure Admin Account Bootstrap Script

Responsibilities:
1. Reads initial Admin credentials from environment variables (ADMIN_EMAIL, ADMIN_PASSWORD).
2. Hashes password securely using Argon2.
3. Checks if the Admin account already exists in PostgreSQL.
4. If exists: safely skips creation to NEVER unintentionally overwrite an existing password.
5. If not exists: securely inserts the new Admin record.
6. Does NOT expose any public registration endpoints.
7. Callable programmatically from run.py or via CLI:
   python -m core.bootstrap_admin
"""

import os
import sys
from typing import Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO_ROOT, ".env"))
except ImportError:
    pass

from db.connection import get_db_cursor, check_db_health, apply_schema, get_database_url
from core.auth import hash_password


DEFAULT_ADMIN_EMAIL = "admin@nimbus.local"


def bootstrap_admin(
    email: Optional[str] = None,
    password: Optional[str] = None,
    database_url: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Safely creates the initial Admin account if it does not already exist.
    Never overwrites existing Admin credentials.
    Returns (created_or_existed: bool, message: str).
    """
    admin_email = email or os.environ.get("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL)
    admin_password = password or os.environ.get("ADMIN_PASSWORD")

    if not admin_password:
        return False, "ADMIN_PASSWORD environment variable is not set. Please define ADMIN_PASSWORD in .env"

    admin_email = admin_email.strip().lower()

    # Ensure schema is applied first
    apply_schema(database_url=database_url)

    with get_db_cursor(database_url=database_url, commit=True) as (cur, _):
        # 1. Check if admin with this email already exists
        cur.execute("SELECT admin_id FROM admins WHERE LOWER(email) = %s;", (admin_email,))
        existing = cur.fetchone()

        if existing:
            return True, f"Admin account '{admin_email}' already exists (ID: {existing['admin_id']}). Password was NOT overwritten."

        # 2. Hash password using Argon2
        hashed = hash_password(admin_password)

        # 3. Insert new admin record
        cur.execute("""
            INSERT INTO admins (email, password_hash)
            VALUES (%s, %s)
            RETURNING admin_id;
        """, (admin_email, hashed))
        new_row = cur.fetchone()
        new_id = new_row["admin_id"]

        return True, f"Admin account '{admin_email}' successfully initialized (ID: {new_id})."


def main():
    db_url = get_database_url()
    print(f"Connecting to database: {db_url}")

    healthy, msg = check_db_health(db_url)
    if not healthy:
        print(f"FATAL: Database is unreachable: {msg}")
        sys.exit(1)

    success, msg = bootstrap_admin(database_url=db_url)
    if success:
        print(f"SUCCESS: {msg}")
        sys.exit(0)
    else:
        print(f"ERROR: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
