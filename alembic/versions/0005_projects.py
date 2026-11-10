"""projects table

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10

Projects used to be folders with a project.yaml; now their settings live in the database.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'projects',
        sa.Column('id', sa.Text(), nullable=False, comment='The slug; also the prefix of its source ids'),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('type', sa.Text(), nullable=False),
        sa.Column('timezone', sa.Text(), server_default='UTC', nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"),
                  nullable=False, comment='Optional event_start, app_packages and field_mappings'),
        sa.Column('push_token_hash', sa.Text(), nullable=False,
                  comment='SHA-256 of the bearer token; the token is shown once'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_projects')),
    )


def downgrade() -> None:
    op.drop_table('projects')
