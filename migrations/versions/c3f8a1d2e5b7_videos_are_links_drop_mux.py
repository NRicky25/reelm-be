"""videos are links (Cloudflare R2): drop the Mux columns

Videos are now encoded with reelm-encoder and hosted on Cloudflare R2; a video
row is just a link + duration. This drops everything that only Mux used:
status, upload_id, asset_id, playback_id, error_message and the "pending
upload" target columns.

Refuses to run while a Mux video still exists (it would stop playing): replace
it with an R2 link in the admin first, then delete the Mux video.

Revision ID: c3f8a1d2e5b7
Revises: a7c1d9e2b4f0
Create Date: 2026-11-10 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3f8a1d2e5b7"
down_revision: str | Sequence[str] | None = "a7c1d9e2b4f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MUX_COLUMNS = ("status", "upload_id", "asset_id", "playback_id", "error_message")


def upgrade() -> None:
    bind = op.get_bind()
    left = bind.execute(
        sa.text("SELECT count(*) FROM videos WHERE provider = 'mux'")
    ).scalar()
    if left:
        raise RuntimeError(
            f"{left} video(s) still use Mux and would stop playing. In the admin, give "
            "each of their episodes/movies an R2 video (Videos -> New, provider "
            "'external'), delete the Mux video, then run this migration again."
        )
    with op.batch_alter_table("videos", schema=None) as batch_op:
        batch_op.drop_constraint("fk_videos_pending_title", type_="foreignkey")
        batch_op.drop_constraint("fk_videos_pending_episode", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_videos_pending_title_id"))
        batch_op.drop_index(batch_op.f("ix_videos_pending_episode_id"))
        batch_op.drop_column("pending_title_id")
        batch_op.drop_column("pending_episode_id")
        for column in MUX_COLUMNS:  # unique constraints on these go with them
            batch_op.drop_column(column)
        # language codes of the subtitle files next to the playlist, e.g. ["en"]
        batch_op.add_column(
            sa.Column(
                "subtitles",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
                ),
                server_default=sa.text("'[]'"),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("videos", schema=None) as batch_op:
        batch_op.drop_column("subtitles")
        batch_op.add_column(
            sa.Column(
                "status",
                sa.Enum(
                    "waiting",
                    "ready",
                    "errored",
                    name="videostatus",
                    native_enum=False,
                    length=20,
                ),
                server_default="waiting",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("upload_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(sa.Column("asset_id", sa.String(length=100), nullable=True))
        batch_op.add_column(
            sa.Column("playback_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        batch_op.create_unique_constraint("uq_videos_upload_id", ["upload_id"])
        batch_op.create_unique_constraint("uq_videos_asset_id", ["asset_id"])
        batch_op.add_column(
            sa.Column("pending_episode_id", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column("pending_title_id", sa.String(length=40), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_videos_pending_episode_id"),
            ["pending_episode_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_videos_pending_title_id"), ["pending_title_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_videos_pending_episode", "episodes", ["pending_episode_id"], ["id"],
            ondelete="SET NULL", use_alter=True,
        )  # fmt: skip
        batch_op.create_foreign_key(
            "fk_videos_pending_title", "titles", ["pending_title_id"], ["id"],
            ondelete="SET NULL", use_alter=True,
        )  # fmt: skip
    # existing rows are links, which were always playable: mark them ready
    op.execute("UPDATE videos SET status = 'ready'")
