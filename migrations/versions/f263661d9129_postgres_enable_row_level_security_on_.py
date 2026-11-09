"""postgres: enable row level security on all tables

Supabase publishes every table in the `public` schema through its auto-generated
REST API (PostgREST), reachable with the project's *public* anon key. Reelm never
uses that API: the backend connects as the database owner, which bypasses RLS.
Enabling RLS with no policies therefore blocks the public API completely while
leaving the backend untouched.

Any NEW table added later needs the same treatment (see README).

Revision ID: f263661d9129
Revises: 34aed98ac0d8
Create Date: 2026-11-09 21:15:07.530127
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f263661d9129"
down_revision: str | Sequence[str] | None = "34aed98ac0d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = [
    "titles",
    "genres",
    "title_genres",
    "seasons",
    "episodes",
    "videos",
    "alembic_version",
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite has no RLS (and no public API)
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
