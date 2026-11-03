"""
Bug #509: manual briefing regenerates were not metered.

The seven `*/generate` routes call the system model through
`call_ai_with_tools`, which meters each call via `briefing_week.meter_ai_call`.
That hook used to write a row only inside the scheduled weekly run. The fix
adds a `briefing_week.user_metering(user_id)` scope, opened by the
`@meter_user_ai` route decorator from `current_user`.

These tests drive the real routes and the real Anthropic tool loop against a
mocked client. The section generators are stubbed with a single real
`call_ai_with_tools` call (their data prep is irrelevant here); one test runs
the real `generate_section_review` end to end, and the synthesis route runs its
real generator.

Isolation: other modules leave MagicMocks in sys.modules['services'], so
usage_metering is loaded from its file path (same pattern as
test_us507_model_switch.py).
"""

import importlib.util
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'
sys.path.insert(0, str(SIGNALTRACKERS_DIR))

# Import before any patch.dict(sys.modules); see test_us507_model_switch.py.
import dashboard  # noqa: E402
import ai_summary  # noqa: E402
import briefing_week  # noqa: E402
from dashboard import app  # noqa: E402
from extensions import db  # noqa: E402
from models import User  # noqa: E402
from models.ai_usage import AIUsageRecord  # noqa: E402

REQUESTED = 'claude-opus-5-5'
SERVED = 'claude-opus-5-5-20261101'

# Distinct, non-round per-turn usage: (input, output, cache_read, cache_write).
TURNS = [(7, 311, 0, 5638), (3, 389, 5638, 3720), (2, 2707, 9358, 3768)]
SUM_IN, SUM_OUT, SUM_CR, SUM_CW = (sum(t[i] for t in TURNS) for i in range(4))
# Opus 5.5 at $4 in / $20 out / $0.20 cache hit / $5 cache write, per MTok.
EXPECTED_COST = (Decimal(SUM_IN) * 4 + Decimal(SUM_OUT) * 20
                 + Decimal(SUM_CR) * Decimal('0.20') + Decimal(SUM_CW) * 5) / Decimal(1_000_000)

# route -> (generator name in dashboard, data-prep functions to stub)
SECTION_ROUTES = {
    'ai-summary': ('generate_weekly_summary', ['generate_market_summary', 'calculate_top_movers']),
    'crypto-summary': ('generate_crypto_summary', ['generate_crypto_market_summary']),
    'equity-summary': ('generate_equity_summary', ['generate_equity_market_summary']),
    'rates-summary': ('generate_rates_summary', ['generate_rates_market_summary']),
    'dollar-summary': ('generate_dollar_summary', ['generate_dollar_market_summary']),
    'credit-summary': ('generate_credit_summary', ['generate_credit_market_summary']),
}
ALL_ROUTES = list(SECTION_ROUTES) + ['market-conditions-synthesis']


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, SIGNALTRACKERS_DIR / relative_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resp(content, usage, stop_reason='end_turn', model=SERVED):
    i, o, cr, cw = usage
    return SimpleNamespace(
        stop_reason=stop_reason, stop_details=None, model=model, content=list(content),
        usage=SimpleNamespace(input_tokens=i, output_tokens=o,
                              cache_read_input_tokens=cr, cache_creation_input_tokens=cw))


def _tool_use(tid):
    return SimpleNamespace(type='tool_use', name='search_web', id=tid, input={'query': 'x'})


def _text(text):
    return SimpleNamespace(type='text', text=text)


def three_turn_loop():
    """Two search turns, then the answer, each with its own usage."""
    return [
        _resp([_tool_use('t1')], TURNS[0], stop_reason='tool_use'),
        _resp([_tool_use('t2')], TURNS[1], stop_reason='tool_use'),
        _resp([_text('Credit spreads tightened this week.')], TURNS[2]),
    ]


def _stub_generator(client):
    """A generator reduced to its one AI call. Like the real ones, it drops
    `usage`/`model` from its return value, so the route cannot meter off it."""
    def gen(*_args, **_kwargs):
        result = ai_summary.call_ai_with_tools(client, 'system', 'user', log_prefix='[test]')
        if not result['success']:
            return {'success': False, 'summary': None, 'structured': None, 'error': result['error']}
        return {'success': True, 'summary': result['content'], 'structured': None, 'error': None}
    return gen


@pytest.fixture
def env():
    """Logged-in test client with a mocked Anthropic client behind every route.

    Yields a namespace: post(route, **kw), client, records(), user_id, um
    (the usage_metering module in use), http (the test client).
    """
    with patch.dict(sys.modules):
        sys.modules.pop('services', None)
        um = _load('services.usage_metering', 'services/usage_metering.py')
        sys.modules['services.usage_metering'] = um

        client = MagicMock()
        old = {k: app.config.get(k) for k in ('TESTING', 'WTF_CSRF_ENABLED')}
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

        patches = [
            patch.object(ai_summary, 'get_ai_provider', return_value='anthropic'),
            patch.object(ai_summary, 'is_tavily_configured', return_value=False),
            patch.object(ai_summary, 'execute_search_function', return_value='{"results": []}'),
            patch.object(ai_summary, '_dump_prompt_to_file'),
            patch.object(ai_summary, 'ANTHROPIC_MODEL', REQUESTED),
            # Synthesis route runs its real generator.
            patch.object(dashboard, 'get_ai_client', return_value=(client, None)),
            patch.object(dashboard, 'get_dashboard_data', return_value={}),
            patch.object(dashboard, 'calculate_market_statuses', return_value={
                k: 'Neutral' for k in
                ('credit', 'equities', 'rates', 'volatility', 'dollar', 'liquidity')}),
            patch.object(dashboard, 'save_market_synthesis'),
        ]
        for gen_name, data_fns in SECTION_ROUTES.values():
            patches.append(patch.object(dashboard, gen_name, _stub_generator(client)))
            for fn in data_fns:
                patches.append(patch.object(dashboard, fn, return_value=''))

        with app.app_context():
            db.drop_all()
            db.create_all()
            user = User(username='bug509', email='bug509@example.com')
            user.set_password('TestPass123')
            other = User(username='bug509b', email='bug509b@example.com')
            other.set_password('TestPass123')
            db.session.add_all([user, other])
            db.session.commit()
            uid, other_id = user.id, other.id
            try:
                for p in patches:
                    p.start()
                http = app.test_client()
                with http.session_transaction() as sess:
                    sess['_user_id'] = str(uid)
                    sess['_fresh'] = True

                yield SimpleNamespace(
                    post=lambda route, **kw: http.post(f'/api/{route}/generate', **kw),
                    client=client,
                    records=lambda: AIUsageRecord.query.all(),
                    user_id=uid, other_id=other_id, um=um, http=http,
                )
            finally:
                for p in reversed(patches):
                    p.stop()
                db.session.remove()
                db.drop_all()
                db.create_all()
                app.config.update(old)


# ---------------------------------------------------------------------------
# Every regenerate route writes exactly one correct row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('route', ALL_ROUTES)
class TestRegenerateIsMetered:
    def test_one_row_summed_served_model_and_cost(self, env, route):
        env.client.beta.messages.create.side_effect = three_turn_loop()

        resp = env.post(route)

        assert resp.status_code == 200, resp.get_json()
        assert env.client.beta.messages.create.call_count == 3
        rows = env.records()
        assert len(rows) == 1
        row = rows[0]
        assert row.user_id == env.user_id
        assert row.interaction_type == 'briefing_regenerate'
        assert row.model == SERVED
        assert (row.input_tokens, row.output_tokens,
                row.cache_read_tokens, row.cache_creation_tokens) == (SUM_IN, SUM_OUT, SUM_CR, SUM_CW)
        assert Decimal(row.estimated_cost) == EXPECTED_COST

    def test_refusal_after_billed_calls_is_still_metered(self, env, route):
        loop = three_turn_loop()
        loop[2] = _resp([], TURNS[2], stop_reason='refusal')
        env.client.beta.messages.create.side_effect = loop

        resp = env.post(route)

        assert resp.status_code == 500
        rows = env.records()
        assert len(rows) == 1
        assert rows[0].input_tokens == SUM_IN
        assert rows[0].user_id == env.user_id

    def test_exception_mid_loop_writes_no_row(self, env, route):
        env.client.beta.messages.create.side_effect = [
            _resp([_tool_use('t1')], TURNS[0], stop_reason='tool_use'),
            RuntimeError('connection reset'),
        ]

        resp = env.post(route)

        assert resp.status_code == 500
        assert resp.get_json()['status'] == 'error'
        assert env.records() == []

    def test_anonymous_post_is_rejected_and_not_metered(self, env, route):
        env.client.beta.messages.create.side_effect = three_turn_loop()
        anon = app.test_client()

        resp = anon.post(f'/api/{route}/generate')

        assert resp.status_code in (302, 401)
        env.client.beta.messages.create.assert_not_called()
        assert env.records() == []

    def test_user_id_in_body_is_ignored(self, env, route):
        env.client.beta.messages.create.side_effect = three_turn_loop()

        env.post(route, json={'user_id': env.other_id})

        assert [r.user_id for r in env.records()] == [env.user_id]


def test_real_section_generator_is_metered(env):
    """No stub: the real generate_section_review path, rates section."""
    env.client.beta.messages.create.side_effect = three_turn_loop()
    rates = dict(ai_summary.SECTION_REVIEWS['rates'], save=MagicMock(), recent=lambda days: [])
    with patch.dict(ai_summary.SECTION_REVIEWS, {'rates': rates}), \
         patch.object(dashboard, 'generate_rates_summary', ai_summary.generate_rates_summary), \
         patch.object(ai_summary, 'get_ai_client', return_value=(env.client, None)), \
         patch.object(ai_summary, '_get_stored_news_context', return_value=None), \
         patch.object(ai_summary.weekly_review, 'week_in_numbers', return_value=''), \
         patch.object(ai_summary.weekly_review, 'calendar_instructions', return_value=''):
        resp = env.post('rates-summary')

    assert resp.status_code == 200, resp.get_json()
    rates['save'].assert_called_once()
    rows = env.records()
    assert len(rows) == 1
    assert (rows[0].user_id, rows[0].model, rows[0].output_tokens) == (env.user_id, SERVED, SUM_OUT)


def test_metering_failure_does_not_fail_regenerate(env):
    env.client.beta.messages.create.side_effect = three_turn_loop()
    with patch.object(env.um.db.session, 'commit', side_effect=RuntimeError('db down')):
        resp = env.post('rates-summary')
    assert resp.status_code == 200
    assert env.records() == []


def test_portfolio_and_other_routes_are_not_wrapped():
    """Only the seven regenerate routes open a user scope. The portfolio route
    meters itself, so wrapping it too would write two rows."""
    # Every layer copies __name__/__qualname__ via functools.wraps, but not
    # __code__, so identify the meter_user_ai layer by its code object.
    meter_code = dashboard.meter_user_ai(lambda: None).__code__

    def has_meter(fn):
        while fn is not None:
            if fn.__code__ is meter_code:
                return True
            fn = getattr(fn, '__wrapped__', None)
        return False
    wrapped = {r.rule for r in app.url_map.iter_rules() if has_meter(app.view_functions[r.endpoint])}
    assert wrapped == {f'/api/{r}/generate' for r in ALL_ROUTES}


# ---------------------------------------------------------------------------
# meter_ai_call scoping
# ---------------------------------------------------------------------------

class TestMeterScope:
    USAGE = {'input_tokens': 10, 'output_tokens': 20,
             'cache_read_tokens': None, 'cache_creation_tokens': None}

    def _rows(self, env):
        return [(r.user_id, r.interaction_type) for r in env.records()]

    def test_outside_any_scope_is_noop(self, env):
        briefing_week.meter_ai_call(self.USAGE, REQUESTED)
        assert env.records() == []

    def test_user_scope_writes_user_row(self, env):
        with briefing_week.user_metering(env.user_id):
            briefing_week.meter_ai_call(self.USAGE, REQUESTED)
        assert self._rows(env) == [(env.user_id, 'briefing_regenerate')]

    def test_scope_is_reset_after_block(self, env):
        with briefing_week.user_metering(env.user_id):
            pass
        briefing_week.meter_ai_call(self.USAGE, REQUESTED)
        assert env.records() == []

    def test_scheduled_run_wins_no_double_metering(self, env):
        from datetime import date
        with briefing_week.user_metering(env.user_id), \
             briefing_week.scheduled_run(date(2026, 11, 2), date(2026, 11, 6)):
            briefing_week.meter_ai_call(self.USAGE, REQUESTED)
        assert self._rows(env) == [(None, 'scheduled_briefing')]

    def test_scheduled_run_alone_unchanged(self, env):
        from datetime import date
        env.client.beta.messages.create.side_effect = three_turn_loop()
        with briefing_week.scheduled_run(date(2026, 11, 2), date(2026, 11, 6)):
            ai_summary.call_ai_with_tools(env.client, 's', 'u')
        rows = env.records()
        assert [(r.user_id, r.interaction_type, r.model, r.output_tokens) for r in rows] == \
            [(None, 'scheduled_briefing', SERVED, SUM_OUT)]
        assert Decimal(rows[0].estimated_cost) == EXPECTED_COST

    def test_no_model_writes_nothing(self, env):
        with briefing_week.user_metering(env.user_id):
            briefing_week.meter_ai_call(self.USAGE, None)
        assert env.records() == []

    def test_openai_path_metered_in_user_scope(self, env):
        oa = MagicMock()
        oa.chat.completions.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45),
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(tool_calls=None, content='Answer.'))])
        with briefing_week.user_metering(env.user_id):
            result = ai_summary.call_ai_with_tools(oa, 's', 'u', provider='openai')
        assert result['success']
        rows = env.records()
        assert [(r.user_id, r.model, r.input_tokens, r.output_tokens) for r in rows] == \
            [(env.user_id, ai_summary.OPENAI_MODEL, 123, 45)]

    def test_new_type_is_valid(self):
        assert 'briefing_regenerate' in AIUsageRecord.VALID_INTERACTION_TYPES
        assert briefing_week.REGENERATE_INTERACTION_TYPE == 'briefing_regenerate'
