"""add sfdt to co_writer_documents

Revision ID: h1j2k3l4m5n6
Revises: g2h3i4j5k6l7
Create Date: 2026-08-07 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "h1j2k3l4m5n6"
down_revision = "g2h3i4j5k6l7"
branch_labels = None
depends_on = None


def upgrade():
    # SFDT (format internal Syncfusion Document Editor, JSON) sebagai
    # representasi kerja utama editor ala Word. Dokumen lama (LaTeX/AST)
    # tetap tersimpan di `content`/`structured_content` sampai dikonversi.
    with op.batch_alter_table("co_writer_documents") as batch_op:
        batch_op.add_column(sa.Column("sfdt", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("co_writer_documents") as batch_op:
        batch_op.drop_column("sfdt")
