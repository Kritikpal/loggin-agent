"""memories

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table('memories',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('key', sa.String(length=60), nullable=True),
    sa.Column('subject', sa.String(length=60), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('evidence', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('replaces_id', sa.Uuid(), nullable=True),
    sa.Column('conflicts_with_id', sa.Uuid(), nullable=True),
    sa.Column('source_conversation_id', sa.Uuid(), nullable=True),
    sa.Column('content_hash', sa.String(length=32), nullable=False),
    sa.Column('search', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', coalesce(subject, '') || ' ' || content)", persisted=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('preference', 'observation')", name=op.f('ck_memories_kind_allowed')),
    sa.CheckConstraint("status IN ('active', 'conflicted', 'superseded', 'deleted')", name=op.f('ck_memories_status_allowed')),
    sa.CheckConstraint("(kind = 'preference') = (key IS NOT NULL)", name=op.f('ck_memories_key_iff_preference')),
    sa.ForeignKeyConstraint(['replaces_id'], ['memories.id'], name=op.f('fk_memories_replaces_id_memories')),
    sa.ForeignKeyConstraint(['conflicts_with_id'], ['memories.id'], name=op.f('fk_memories_conflicts_with_id_memories')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memories'))
    )
    op.create_index('uq_memories_active_key', 'memories', ['key'], unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_index('uq_memories_live_hash', 'memories', ['content_hash'], unique=True, postgresql_where=sa.text("status IN ('active', 'conflicted')"))
    op.create_index('ix_memories_search', 'memories', ['search'], postgresql_using='gin')
    op.create_index('ix_memories_content_trgm', 'memories', ['content'], postgresql_using='gin', postgresql_ops={'content': 'gin_trgm_ops'})
    op.create_index('ix_memories_kind_status', 'memories', ['kind', 'status'])


def downgrade() -> None:
    op.drop_table('memories')  # the pg_trgm extension is left in place: it is harmless and may be shared
