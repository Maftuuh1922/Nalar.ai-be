"""add research_reports table

Revision ID: d4a7c81e9f30
Revises: c3f9a41d5b27
Create Date: 2026-07-26

Tabel penyimpan hasil fitur Riset Mendalam.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7c81e9f30"
down_revision: Union[str, None] = "c3f9a41d5b27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_reports",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(length=500), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("depth", sa.String(length=20), nullable=False, server_default="standar"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("progress_step", sa.String(length=255), nullable=False, server_default="Menunggu antrean"),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outline", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("content_markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_research_reports_user_id", "research_reports", ["user_id"])
    op.create_index("ix_research_reports_status", "research_reports", ["status"])


def downgrade() -> None:
    op.drop_index("ix_research_reports_status", table_name="research_reports")
    op.drop_index("ix_research_reports_user_id", table_name="research_reports")
    op.drop_table("research_reports")
