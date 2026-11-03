"""log event signatures

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03

Events indexed before this revision keep NULL signatures until they are indexed again.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('log_events', sa.Column(
        'signature', sa.Text(), nullable=True, comment="Id of the event's kind (app.ingestion.signature)"))
    op.add_column('log_events', sa.Column(
        'signature_template', sa.Text(), nullable=True, comment="The kind's text with variable parts as <*>"))
    op.create_index('ix_log_events_signature_time', 'log_events', ['signature', 'occurred_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_log_events_signature_time', table_name='log_events')
    op.drop_column('log_events', 'signature_template')
    op.drop_column('log_events', 'signature')
