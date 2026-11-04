"""partial index for rows still waiting for an embedding

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04

The embedding loop looks for `embedding IS NULL` every few seconds; the HNSW index cannot serve that.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ix_log_events_unembedded', 'log_events', ['id'], unique=False, postgresql_where=sa.text('embedding IS NULL'))


def downgrade() -> None:
    op.drop_index('ix_log_events_unembedded', table_name='log_events')
