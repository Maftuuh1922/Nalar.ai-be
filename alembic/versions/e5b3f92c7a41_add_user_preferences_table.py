"""add user_preferences table

Revision ID: e5b3f92c7a41
Revises: d4a7c81e9f30
Create Date: 2026-07-26

Menyimpan setelan tiap user yang sebelumnya hanya tampil sebagai kontrol kosong
di modal Pengaturan.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5b3f92c7a41"
down_revision: Union[str, None] = "d4a7c81e9f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("chat_temperature", sa.Float(), nullable=False, server_default="0.7"),
        sa.Column("chat_max_tokens", sa.Integer(), nullable=False, server_default="8000"),
        sa.Column("history_limit", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("enable_web_tools", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enable_document_tools", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enable_suggestions", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("chunk_size", sa.Integer(), nullable=False, server_default="512"),
        sa.Column("chunk_overlap", sa.Integer(), nullable=False, server_default="64"),
        sa.Column("retrieval_top_k", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("request_timeout", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("proxy_url", sa.String(length=500), nullable=True),
        sa.Column("bypass_proxy_local", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("research_default_depth", sa.String(length=20), nullable=False, server_default="standar"),
        sa.Column("default_quiz_questions", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("custom_instructions", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_user_preferences_user_id", "user_preferences", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_preferences_user_id", table_name="user_preferences")
    op.drop_table("user_preferences")
