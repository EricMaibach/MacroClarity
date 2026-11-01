"""Fold briefing_frequency into the weekly review email toggle (US-490.5)

The email is now weekly, so `daily_briefing_enabled` is the only setting that
decides whether a user gets it. `briefing_frequency` and `briefing_time` stay
in the table but are no longer read.

Mapping: enabled = daily_briefing_enabled AND briefing_frequency IN
('daily', 'weekly'). Opt-out lives in `daily_briefing_enabled` (the unsubscribe
link and the settings toggle both clear it and leave the frequency at
'daily'), so mapping on the frequency alone would re-subscribe users who
unsubscribed. Only rows that are enabled with frequency 'off' (or anything
unknown) change: they are switched off.

Downgrade: intentionally changes nothing. The only rows the upgrade touches
are ones the old code never emailed (it skipped frequency 'off' whatever the
toggle said, and only queried frequency 'daily'), so with the old code those
rows behave identically after the upgrade. Turning them back on would be
guesswork, since a row that was (False, 'off') before cannot be told apart.

Revision ID: k6d7e8f9a0b1
Revises: j5c6d7e8f9a0
Create Date: 2026-10-31

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'k6d7e8f9a0b1'
down_revision = 'j5c6d7e8f9a0'
branch_labels = None
depends_on = None

ENABLED_FREQUENCIES = ('daily', 'weekly')


def disable_off_frequency_sql():
    """The upgrade's UPDATE, as (statement, params), runnable without Alembic."""
    statement = sa.text(
        "UPDATE alert_preferences SET daily_briefing_enabled = :off "
        "WHERE daily_briefing_enabled = :on "
        "AND (briefing_frequency IS NULL OR briefing_frequency NOT IN (:daily, :weekly))"
    )
    return statement, {'on': True, 'off': False,
                       'daily': ENABLED_FREQUENCIES[0], 'weekly': ENABLED_FREQUENCIES[1]}


def upgrade():
    conn = op.get_bind()
    if 'alert_preferences' not in sa.inspect(conn).get_table_names():
        return
    statement, params = disable_off_frequency_sql()
    conn.execute(statement, params)


def downgrade():
    # See the module docstring: the old code already treated every row this
    # upgrade changes as "no email", so there is nothing to restore.
    pass
