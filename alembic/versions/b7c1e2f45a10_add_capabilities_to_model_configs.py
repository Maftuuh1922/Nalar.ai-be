"""add capabilities, provider_type, context_window to model_configs

Revision ID: b7c1e2f45a10
Revises: da0b2d7a7d7a
Create Date: 2026-07-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c1e2f45a10'
down_revision: Union[str, None] = 'da0b2d7a7d7a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'model_configs',
        sa.Column('capabilities', sa.Text(), nullable=False, server_default='["text"]'),
    )
    op.add_column(
        'model_configs',
        sa.Column('provider_type', sa.String(length=50), nullable=False, server_default='openai-compatible'),
    )
    op.add_column(
        'model_configs',
        sa.Column('context_window', sa.Integer(), nullable=False, server_default='65536'),
    )


def downgrade() -> None:
    op.drop_column('model_configs', 'context_window')
    op.drop_column('model_configs', 'provider_type')
    op.drop_column('model_configs', 'capabilities')
