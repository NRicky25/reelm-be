"""outro: where the end credits start, on titles (movies) and episodes

Enables the player's "Next Episode" / "Skip Credits" button. Nullable: existing
rows simply have no outro.

Revision ID: d4e9b2c7a1f3
Revises: c3f8a1d2e5b7
Create Date: 2026-11-11 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e9b2c7a1f3"
down_revision: str | Sequence[str] | None = "c3f8a1d2e5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("titles", "episodes")


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("outro_start_sec", sa.Integer(), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("outro_start_sec")
