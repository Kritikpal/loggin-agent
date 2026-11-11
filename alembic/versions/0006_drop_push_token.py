"""Projects no longer have a push token: the API is open for now.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("projects", "push_token_hash")


def downgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("push_token_hash", sa.Text(), nullable=False, server_default="", comment="SHA-256 of the bearer token"),
    )
