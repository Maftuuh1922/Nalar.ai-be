"""add content_format to co_writer_documents

Revision ID: g2h3i4j5k6l7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-05 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "g2h3i4j5k6l7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    # Penanda apakah isi draf sudah dikonversi ke LaTeX. Draf lama lahir
    # sebagai "markdown"; get_document mengkonversinya sekali lalu menyetel
    # "latex". Dokumen baru langsung "latex".
    with op.batch_alter_table("co_writer_documents") as batch_op:
        batch_op.add_column(
            sa.Column(
                "content_format",
                sa.String(16),
                nullable=False,
                server_default="markdown",
            )
        )


def downgrade():
    with op.batch_alter_table("co_writer_documents") as batch_op:
        batch_op.drop_column("content_format")
