"""postgres: revoke the Supabase Data API roles' access to our tables

RLS (previous migration) already blocks the public API (anon / authenticated
roles) from reading or writing rows. This removes their privileges entirely, so:
- a table added later is NOT exposed by default, even if someone forgets RLS;
- table and column names aren't visible through the API's schema introspection.

Reelm's backend connects as the database owner and never uses the Data API.
Only runs on Postgres where those roles exist (i.e. Supabase).

Revision ID: a7c1d9e2b4f0
Revises: f263661d9129
Create Date: 2026-11-10 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7c1d9e2b4f0"
down_revision: str | Sequence[str] | None = "f263661d9129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = "anon, authenticated"
KINDS = ("TABLES", "SEQUENCES", "FUNCTIONS")


def _supabase() -> bool:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    found = bind.exec_driver_sql(
        "SELECT count(*) FROM pg_roles WHERE rolname IN ('anon', 'authenticated')"
    ).scalar()
    return found == 2


def upgrade() -> None:
    if not _supabase():
        return
    for kind in KINDS:
        op.execute(f"REVOKE ALL ON ALL {kind} IN SCHEMA public FROM {ROLES}")
        # objects created later by us (the owner) get no grants for these roles
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON {kind} FROM {ROLES}"
        )


def downgrade() -> None:
    # Deliberately does nothing: re-opening the public API is never wanted.
    pass
