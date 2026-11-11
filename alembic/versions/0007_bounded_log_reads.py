"""Index for bounded log reads: (source_id, occurred_at, id) serves 'one source, a time window, in order, paged'.

Also upper-cases stored levels so a level filter is an equality the index can use.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_log_events_source_time_id", "log_events", ["source_id", "occurred_at", "id"])
    op.drop_index("ix_log_events_source_time", table_name="log_events")  # the new index starts with the same columns
    op.execute("UPDATE log_events SET level = upper(level) WHERE level <> upper(level)")


def downgrade() -> None:
    op.create_index("ix_log_events_source_time", "log_events", ["source_id", "occurred_at"])
    op.drop_index("ix_log_events_source_time_id", table_name="log_events")
