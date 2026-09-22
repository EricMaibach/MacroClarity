"""Backfill overstated claude-opus-4-6 estimated_cost values

`MODEL_PRICING['claude-opus-4-6']` priced Opus 4.6 at the old Claude 3 Opus
rate ($15.00/$75.00 per 1M tokens) instead of the published $5.00/$25.00. All
four fields were overstated by exactly 3x, so every `ai_usage_records` row
written for that model carries a cost 3x too high.

The token columns that fed `calculate_cost()` are still on the row, so the
correction is deterministic: recompute from the stored token counts at the
corrected rates. Scoped strictly to `model = 'claude-opus-4-6'` -- the
`claude-sonnet-4-6` rows were always priced correctly and must not be touched.

Null guard: the token columns are nullable. A row with *all four* token counts
NULL has nothing to recompute from, so it falls back to dividing the stored
cost by 3, which is arithmetically identical given the uniform 3x error. The
recompute branch derives cost purely from the token columns and is therefore
idempotent; the /3 fallback is not, and relies on Alembic's version table to
run exactly once.

Revision ID: i4b5c6d7e8f9
Revises: h3a4b5c6d7e8
Create Date: 2026-09-22 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'i4b5c6d7e8f9'
down_revision = 'h3a4b5c6d7e8'
branch_labels = None
depends_on = None


MODEL = 'claude-opus-4-6'

# Corrected published rates (USD per 1M tokens)
CORRECTED = {'input': '5.00', 'output': '25.00', 'cache_read': '0.50', 'cache_creation': '6.25'}
# The rates that were wrongly applied, used to reverse the backfill
OVERSTATED = {'input': '15.00', 'output': '75.00', 'cache_read': '1.50', 'cache_creation': '18.75'}

_ALL_TOKENS_NULL = (
    'input_tokens IS NULL AND output_tokens IS NULL '
    'AND cache_read_tokens IS NULL AND cache_creation_tokens IS NULL'
)


def _rescale(rates, fallback_factor):
    """Build the UPDATE that reprices Opus 4.6 rows at the given rates."""
    return sa.text(
        f"""
        UPDATE ai_usage_records
           SET estimated_cost = CASE
                 WHEN {_ALL_TOKENS_NULL}
                 THEN estimated_cost {fallback_factor}
                 ELSE (COALESCE(input_tokens, 0) * {rates['input']}
                     + COALESCE(output_tokens, 0) * {rates['output']}
                     + COALESCE(cache_read_tokens, 0) * {rates['cache_read']}
                     + COALESCE(cache_creation_tokens, 0) * {rates['cache_creation']}
                      ) / 1000000
               END
         WHERE model = :model
        """
    ), {'model': MODEL}


def _run(rates, fallback_factor):
    conn = op.get_bind()
    if 'ai_usage_records' not in sa.inspect(conn).get_table_names():
        return
    stmt, params = _rescale(rates, fallback_factor)
    conn.execute(stmt, params)


def upgrade():
    # Overstated by 3x -> divide the unrecomputable rows by 3
    _run(CORRECTED, '/ 3')


def downgrade():
    # Restore the (incorrect) figures this migration replaced
    _run(OVERSTATED, '* 3')
