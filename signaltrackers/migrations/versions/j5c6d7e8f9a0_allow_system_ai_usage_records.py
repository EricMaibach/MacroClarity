"""Allow system AI usage records (nullable user_id)

The scheduled weekly AI review (US-490.1) is metered so a week's spend can be
compared before and after the move from daily briefings. Its calls belong to
no user, so `ai_usage_records.user_id` becomes nullable. Per-user quotas filter
on `user_id = :id`, and the admin top-users view inner-joins `users`, so NULL
rows never count against a user.

Revision ID: j5c6d7e8f9a0
Revises: i4b5c6d7e8f9
Create Date: 2026-10-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'j5c6d7e8f9a0'
down_revision = 'i4b5c6d7e8f9'
branch_labels = None
depends_on = None


def upgrade():
    # batch mode: SQLite cannot ALTER a column's nullability in place.
    with op.batch_alter_table('ai_usage_records') as batch_op:
        batch_op.alter_column('user_id', existing_type=sa.String(36), nullable=True)


def downgrade():
    # System rows have no user to restore, so they cannot survive NOT NULL.
    op.execute("DELETE FROM ai_usage_records WHERE user_id IS NULL")
    with op.batch_alter_table('ai_usage_records') as batch_op:
        batch_op.alter_column('user_id', existing_type=sa.String(36), nullable=False)
