"""
Tests for Bug #484: Opus 4.6 usage metering priced at 3x the actual rate.

`MODEL_PRICING['claude-opus-4-6']` carried the old Claude 3 Opus rate
($15/$75 per 1M tokens) instead of the published $5/$25, overstating every
recorded Opus 4.6 cost by exactly 3x across all four fields.

Two things are pinned here:

1. The corrected pricing row, plus regression guards that the fix stayed
   isolated to the Opus row.
2. The backfill SQL that reprices historical `ai_usage_records`.

**Every `cache_read` value is asserted as a literal published figure, never
as a percentage of `input`.** The 10% ratio holds for Opus 4.6 and Sonnet 4.6
by coincidence of those models' published rates; Claude Fable 5.1 publishes
cache reads at $0.25/MTok against a $10.00 input rate (2.5%). A blanket 10%
invariant would bake a 4x cache-read overstatement into any future row.
`cache_creation == 1.25 * input` does hold across the Anthropic rows and is
asserted as an invariant.
"""

import importlib.util
import logging
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'

sys.path.insert(0, str(SIGNALTRACKERS_DIR))

MIGRATION_PATH = (
    SIGNALTRACKERS_DIR
    / 'migrations/versions/i4b5c6d7e8f9_backfill_opus_4_6_estimated_cost.py'
)

OPUS = 'claude-opus-4-6'
# Dated snapshot / alias forms: priced from the Opus row via the prefix
# fallback in `_get_pricing`, but invisible to an equality-scoped backfill.
OPUS_SNAPSHOT = 'claude-opus-4-6-20260115'
OPUS_ALIAS = 'claude-opus-4-6-latest'
SONNET = 'claude-sonnet-4-6'
GPT = 'gpt-5.2'

# Published rates per 1M tokens, copied literally from the provider pricing pages.
EXPECTED_PRICING = {
    OPUS: {
        'input': Decimal('5.00'),
        'output': Decimal('25.00'),
        'cache_read': Decimal('0.50'),
        'cache_creation': Decimal('6.25'),
    },
    SONNET: {
        'input': Decimal('3.00'),
        'output': Decimal('15.00'),
        'cache_read': Decimal('0.30'),
        'cache_creation': Decimal('3.75'),
    },
    GPT: {
        'input': Decimal('2.50'),
        'output': Decimal('10.00'),
        'cache_read': Decimal('1.25'),
        'cache_creation': Decimal('0'),
    },
}

ANTHROPIC_MODELS = (OPUS, SONNET)


def load_module(name, path):
    """Load a module straight from its file path.

    Other test modules leave a MagicMock in sys.modules['services'], which
    breaks a plain `from services.usage_metering import ...` depending on test
    order. Loading by path sidesteps that.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope='module')
def metering():
    return load_module(
        'usage_metering_under_test', SIGNALTRACKERS_DIR / 'services/usage_metering.py'
    )


@pytest.fixture(scope='module')
def migration():
    return load_module('backfill_opus_pricing_under_test', MIGRATION_PATH)


# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('model', [OPUS, SONNET, GPT])
def test_pricing_row_matches_published_rates(metering, model):
    """Opus 4.6 is corrected; Sonnet 4.6 and gpt-5.2 must be untouched."""
    assert metering.MODEL_PRICING[model] == EXPECTED_PRICING[model]


def test_opus_is_not_still_at_the_claude_3_rate(metering):
    """The specific regression: $15/$75 was the Claude 3 Opus rate."""
    assert metering.MODEL_PRICING[OPUS]['input'] != Decimal('15.00')
    assert metering.MODEL_PRICING[OPUS]['output'] != Decimal('75.00')


def test_all_pricing_values_are_decimal(metering):
    """Floats would reintroduce rounding error into recorded cost."""
    tables = dict(metering.MODEL_PRICING, _default=metering._DEFAULT_PRICING)
    for model, row in tables.items():
        for field, value in row.items():
            assert isinstance(value, Decimal), f'{model}.{field} is {type(value).__name__}'


@pytest.mark.parametrize('model', ANTHROPIC_MODELS)
def test_anthropic_cache_creation_is_125_percent_of_input(metering, model):
    """Safe invariant: cache writes are consistently 1.25x input on Anthropic."""
    row = metering.MODEL_PRICING[model]
    assert row['cache_creation'] == row['input'] * Decimal('1.25')


@pytest.mark.parametrize('model', ANTHROPIC_MODELS)
def test_anthropic_cache_read_is_a_literal_published_figure(metering, model):
    """cache_read must be entered literally, never derived from input.

    Deliberately asserted as a constant rather than a ratio. The 10%-of-input
    relationship is a coincidence of these two models' published rates and is
    not an Anthropic rule -- see this module's docstring.
    """
    assert metering.MODEL_PRICING[model]['cache_read'] == EXPECTED_PRICING[model]['cache_read']


def test_default_pricing_is_the_sonnet_rate(metering):
    """Pins the fallback so any change to it is a deliberate one.

    Flagged by QA rather than blocking: a miss on an Opus-tier model
    under-reports, since the fallback is a mid-tier rate.
    """
    assert metering._DEFAULT_PRICING == EXPECTED_PRICING[SONNET]


# ---------------------------------------------------------------------------
# calculate_cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'kwargs,expected',
    [
        ({'input_tokens': 1_000_000, 'output_tokens': 0}, Decimal('5.00')),
        ({'input_tokens': 0, 'output_tokens': 1_000_000}, Decimal('25.00')),
        (
            {'input_tokens': 0, 'output_tokens': 0, 'cache_read_tokens': 1_000_000},
            Decimal('0.50'),
        ),
        (
            {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_tokens': 1_000_000},
            Decimal('6.25'),
        ),
    ],
)
def test_calculate_cost_per_token_type(metering, kwargs, expected):
    assert metering.calculate_cost(OPUS, **kwargs) == expected


def test_calculate_cost_sums_all_four_terms(metering):
    """No double-counting and no dropped term in the combined call."""
    counts = {
        'input_tokens': 120_000,
        'output_tokens': 8_000,
        'cache_read_tokens': 400_000,
        'cache_creation_tokens': 30_000,
    }
    combined = metering.calculate_cost(OPUS, **counts)

    individually = sum(
        metering.calculate_cost(OPUS, **{**dict.fromkeys(counts, 0), field: value})
        for field, value in counts.items()
    )
    assert combined == individually

    rates = EXPECTED_PRICING[OPUS]
    expected = (
        Decimal(counts['input_tokens']) * rates['input']
        + Decimal(counts['output_tokens']) * rates['output']
        + Decimal(counts['cache_read_tokens']) * rates['cache_read']
        + Decimal(counts['cache_creation_tokens']) * rates['cache_creation']
    ) / Decimal('1000000')
    assert combined == expected


@pytest.mark.parametrize(
    'kwargs',
    [
        {'input_tokens': None, 'output_tokens': None},
        {'input_tokens': 0, 'output_tokens': 0},
        {
            'input_tokens': None,
            'output_tokens': None,
            'cache_read_tokens': None,
            'cache_creation_tokens': None,
        },
    ],
)
def test_calculate_cost_handles_null_and_zero_tokens(metering, kwargs):
    """The documented nullable case returns zero rather than raising.

    `calculate_cost` uses falsy checks, so None and 0 take the same branch.
    """
    assert metering.calculate_cost(OPUS, **kwargs) == Decimal('0')


def test_calculate_cost_is_exact_at_very_large_token_counts(metering):
    """Decimal must not drift into float error or scientific notation."""
    cost = metering.calculate_cost(OPUS, 1_000_000_000, 1_000_000_000)
    assert cost == Decimal('30000.00')
    assert 'E' not in str(cost).upper()


def test_recorded_cost_is_one_third_of_the_old_figure(metering):
    """The whole bug in one assertion: the 3x overstatement is gone."""
    old_rates = {
        'input': Decimal('15.00'),
        'output': Decimal('75.00'),
        'cache_read': Decimal('1.50'),
        'cache_creation': Decimal('18.75'),
    }
    for field, old in old_rates.items():
        assert metering.MODEL_PRICING[OPUS][field] * 3 == old


# ---------------------------------------------------------------------------
# _get_pricing resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('model', [OPUS, SONNET, GPT])
def test_exact_match_wins(metering, model):
    """Exact lookup must not fall through to a prefix collision."""
    assert metering._get_pricing(model) is metering.MODEL_PRICING[model]


def test_prefix_match_resolves_extended_model_ids(metering):
    """A dated/extended variant falls back to its base row."""
    assert metering._get_pricing(f'{OPUS}-20260101') is metering.MODEL_PRICING[OPUS]


def test_exact_match_beats_prefix_for_overlapping_keys(metering):
    """Guards the collision #483 introduces when it adds more claude-* keys.

    Once two keys share a prefix, the longer ID must resolve to its own row
    rather than prefix-matching the shorter one. Their input/output rates may
    coincide while their cache rates differ, so a wrong match is silent.
    """
    base, extended = 'claude-test-5', 'claude-test-5-1'
    patched = dict(metering.MODEL_PRICING)
    patched[base] = {
        'input': Decimal('10.00'),
        'output': Decimal('50.00'),
        'cache_read': Decimal('1.00'),
        'cache_creation': Decimal('12.50'),
    }
    patched[extended] = {
        'input': Decimal('10.00'),
        'output': Decimal('50.00'),
        'cache_read': Decimal('0.25'),
        'cache_creation': Decimal('12.50'),
    }

    original = metering.MODEL_PRICING
    try:
        metering.MODEL_PRICING = patched
        assert metering._get_pricing(extended)['cache_read'] == Decimal('0.25')
        assert metering._get_pricing(base)['cache_read'] == Decimal('1.00')
    finally:
        metering.MODEL_PRICING = original


def test_unknown_model_falls_back_and_warns(metering, caplog):
    """A silent fallback is how this class of bug hides -- assert the log."""
    with caplog.at_level(logging.WARNING):
        pricing = metering._get_pricing('some-unlisted-model')

    assert pricing is metering._DEFAULT_PRICING
    assert any(
        'some-unlisted-model' in record.getMessage() and record.levelno == logging.WARNING
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Historical backfill
# ---------------------------------------------------------------------------


def _usage_table(metadata):
    return sa.Table(
        'ai_usage_records',
        metadata,
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('input_tokens', sa.Integer, nullable=True),
        sa.Column('output_tokens', sa.Integer, nullable=True),
        sa.Column('cache_read_tokens', sa.Integer, nullable=True),
        sa.Column('cache_creation_tokens', sa.Integer, nullable=True),
        sa.Column('model', sa.String(100), nullable=False),
        sa.Column('estimated_cost', sa.Numeric(precision=12, scale=8), nullable=False),
    )


@pytest.fixture
def seeded_db():
    """In-memory table holding rows written at the overstated Opus rates."""
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    table = _usage_table(metadata)
    metadata.create_all(engine)

    rows = [
        # Opus, full token data -> recomputed from tokens
        dict(
            id=1,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            model=OPUS,
            estimated_cost=Decimal('90.00'),
        ),
        # Opus, cache tokens only
        dict(
            id=2,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=1_000_000,
            cache_creation_tokens=1_000_000,
            model=OPUS,
            estimated_cost=Decimal('20.25'),
        ),
        # Opus, all token columns NULL -> /3 fallback path
        dict(
            id=3,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            model=OPUS,
            estimated_cost=Decimal('9.00'),
        ),
        # Sonnet, correctly priced -> must not be touched
        dict(
            id=4,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            model=SONNET,
            estimated_cost=Decimal('18.00'),
        ),
        # Dated snapshot ID -- charged at the Opus rates, so it must be
        # corrected even though it is not equal to the bare model ID.
        dict(
            id=5,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            model=OPUS_SNAPSHOT,
            estimated_cost=Decimal('90.00'),
        ),
        # Alias form, all token columns NULL -> /3.0 fallback, and the stored
        # cost does NOT divide evenly. Under integer division this row would
        # be silently zeroed, so it is the guard for the float divisor.
        dict(
            id=6,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_creation_tokens=None,
            model=OPUS_ALIAS,
            estimated_cost=Decimal('2.00'),
        ),
    ]
    with engine.begin() as conn:
        conn.execute(table.insert(), rows)
    return engine, table


def _costs(engine, table):
    with engine.connect() as conn:
        return {
            row.id: Decimal(str(row.estimated_cost))
            for row in conn.execute(sa.select(table.c.id, table.c.estimated_cost))
        }


def _apply(engine, migration, rates, factor):
    stmt, params = migration._rescale(rates, factor)
    with engine.begin() as conn:
        conn.execute(stmt, params)


def test_backfill_corrects_opus_rows(seeded_db, migration):
    engine, table = seeded_db
    _apply(engine, migration, migration.CORRECTED, migration.UPGRADE_FACTOR)

    costs = _costs(engine, table)
    assert costs[1] == Decimal('30.00')  # 1M in @ $5 + 1M out @ $25
    assert costs[2] == Decimal('6.75')  # 1M cache read @ $0.50 + 1M write @ $6.25
    assert costs[3] == Decimal('3.00')  # 9.00 / 3, no tokens to recompute from
    assert costs[5] == Decimal('30.00')  # dated snapshot ID, recomputed like id=1
    # 2.00 / 3.0 -- integer division would store 0 here
    assert costs[6] == Decimal('0.66666667')


def test_backfill_leaves_sonnet_rows_alone(seeded_db, migration):
    engine, table = seeded_db
    before = _costs(engine, table)[4]
    _apply(engine, migration, migration.CORRECTED, migration.UPGRADE_FACTOR)
    assert _costs(engine, table)[4] == before == Decimal('18.00')


def test_backfill_recompute_branch_is_idempotent(seeded_db, migration):
    """Rows with token data derive cost from tokens, so re-running is safe."""
    engine, table = seeded_db
    _apply(engine, migration, migration.CORRECTED, migration.UPGRADE_FACTOR)
    once = _costs(engine, table)
    _apply(engine, migration, migration.CORRECTED, migration.UPGRADE_FACTOR)
    twice = _costs(engine, table)

    for row_id in (1, 2, 4, 5):
        assert once[row_id] == twice[row_id]


def test_backfill_downgrade_restores_prior_values(seeded_db, migration):
    engine, table = seeded_db
    before = _costs(engine, table)

    _apply(engine, migration, migration.CORRECTED, migration.UPGRADE_FACTOR)
    _apply(engine, migration, migration.OVERSTATED, migration.DOWNGRADE_FACTOR)

    assert _costs(engine, table) == before


def test_backfill_is_scoped_by_model_prefix(migration):
    """Equality under-scopes: `_get_pricing` prefix-matches, so snapshot and
    alias IDs were charged at the Opus rates too. The filter must be LIKE."""
    stmt, params = migration._rescale(migration.CORRECTED, migration.UPGRADE_FACTOR)
    assert params == {'model_prefix': OPUS + '%'}
    assert 'WHERE model LIKE :model_prefix' in str(stmt)
    assert 'WHERE model = :model' not in str(stmt)


def test_backfill_prefix_matches_exactly_what_get_pricing_routes_to_opus(metering):
    """The LIKE prefix is not an approximation of the overcharged set -- it is
    that set. Any name the prefix matches must resolve to the Opus pricing row,
    and any correctly-priced model must not match."""
    opus_pricing = metering.MODEL_PRICING[OPUS]
    for name in (OPUS, OPUS_SNAPSHOT, OPUS_ALIAS):
        assert name.startswith(OPUS)
        assert metering._get_pricing(name) is opus_pricing
    for name in (SONNET, GPT):
        assert not name.startswith(OPUS)
        assert metering._get_pricing(name) is not opus_pricing


def test_backfill_corrects_snapshot_and_alias_rows(seeded_db, migration):
    """The regression QA caught: rows carrying a dated snapshot or alias ID
    were left 3x inflated by an equality-scoped backfill."""
    engine, table = seeded_db
    before = _costs(engine, table)
    _apply(engine, migration, migration.CORRECTED, migration.UPGRADE_FACTOR)
    after = _costs(engine, table)

    assert after[5] == before[5] / 3
    assert after[6] == Decimal('0.66666667')
    assert after[6] != before[6]


def test_backfill_divisor_is_float_form(migration):
    """`2 / 3` truncates to 0 on SQLite's INTEGER affinity -- the divisor must
    stay in float form or non-multiples of 3 are silently zeroed."""
    assert migration.UPGRADE_FACTOR == '/ 3.0'
    assert migration.DOWNGRADE_FACTOR == '* 3.0'


def test_backfill_uses_no_like_wildcards_in_the_prefix(migration):
    """`_` and `%` are LIKE wildcards. The prefix must contain neither, or it
    would need an ESCAPE clause to avoid over-matching."""
    literal = migration.MODEL_PREFIX[:-1]
    assert migration.MODEL_PREFIX.endswith('%')
    assert '%' not in literal and '_' not in literal
