"""
One-off CLI to create (or reset) an admin account in the `admins` table.

Usage:
    python scripts/create_admin.py <username> <password> [role]

    role defaults to 'superadmin' (full powers) so the bootstrap admin can
    approve users, delete accounts and run the engine. Pass 'support' for a
    limited admin who can only view/approve/enable/disable users.

Requires DATABASE_URL to be set in the environment (same Postgres/Supabase
instance the backend uses). Run this once after applying
sql/migration_v5_admin_approval.sql AND sql/migration_v12_admin_audit_2fa.sql
to bootstrap your first admin login.
"""
import asyncio
import os
import sys

import asyncpg
import bcrypt

VALID_ROLES = ("support", "superadmin")


async def main(username: str, password: str, role: str) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)
    if role not in VALID_ROLES:
        print(f"role must be one of {VALID_ROLES}", file=sys.stderr)
        sys.exit(1)

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            """
            insert into admins (username, password_hash, role)
            values ($1, $2, $3)
            on conflict (username) do update
                set password_hash = excluded.password_hash,
                    role          = excluded.role
            """,
            username, password_hash, role,
        )
        print(f"Admin '{username}' ({role}) created/updated successfully.")
    finally:
        await conn.close()


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print("Usage: python scripts/create_admin.py <username> <password> [role]", file=sys.stderr)
        sys.exit(1)
    role = sys.argv[3] if len(sys.argv) == 4 else "superadmin"
    asyncio.run(main(sys.argv[1], sys.argv[2], role))
