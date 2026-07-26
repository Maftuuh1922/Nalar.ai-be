"""Jadikan quizzes.document_id opsional (kuis topik bebas).

Revision ID: c3f9a41d5b27
Revises: b7c1e2f45a10
Create Date: 2026-07-26

"""

from alembic import op
import sqlalchemy as sa

revision = "c3f9a41d5b27"
down_revision = "b7c1e2f45a10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite tidak mendukung ALTER COLUMN langsung; batch mode membuat tabel baru.
    with op.batch_alter_table("quizzes") as batch_op:
        batch_op.alter_column("document_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    # Kuis tanpa dokumen dibuang karena kolomnya kembali wajib.
    op.execute("DELETE FROM quizzes WHERE document_id IS NULL")
    with op.batch_alter_table("quizzes") as batch_op:
        batch_op.alter_column("document_id", existing_type=sa.Uuid(), nullable=False)
