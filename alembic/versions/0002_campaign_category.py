"""campaign category

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Nullable, so existing campaigns stay valid."""
    op.add_column('campaigns', sa.Column('category', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('campaigns', 'category')
