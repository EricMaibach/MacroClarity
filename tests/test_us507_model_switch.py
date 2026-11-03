"""
#507: system AI on Claude Opus 5.5, chatbot on Claude Sonnet 5.5.

Covers:
  - exact MODEL_PRICING rows for both new IDs (published figures, Decimal),
    including the identity check that catches a missing Sonnet 5.5 row (the
    prefix match would otherwise price it off the Sonnet 5 row silently);
  - hand-computed costs with non-round token counts;
  - config defaults and env overrides;
  - the real /api/chatbot route: explicit output_config.effort, thinking
    contract, text-only answer extraction, tool turns without text,
    thinking-only and refusal endings, and Sonnet 5.5 metering.

Pricing source: https://platform.claude.com/docs/en/about-claude/pricing
(checked 2026-11-02). Opus 5.5: $4 in / $5 5m-write / $0.20 hit / $20 out.
Sonnet 5.5: $2 in / $2.50 5m-write / $0.10 hit / $10 out.

Isolation: other modules leave MagicMocks in sys.modules['config'] and
['services'], so modules under test are loaded from their file paths.
"""

import importlib.util
import os
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'
sys.path.insert(0, str(SIGNALTRACKERS_DIR))

# Imported up front, before any patch.dict(sys.modules) below: usage_metering
# imports `extensions` and `models`, and if that first import happened inside a
# snapshot it would be discarded on exit, and dashboard would later build a
# second copy of every model class (SQLAlchemy then fails with
# "'InternalTraversal' object is not callable").
import dashboard  # noqa: E402
from dashboard import app  # noqa: E402
from extensions import db  # noqa: E402
from models import User  # noqa: E402
from models.ai_usage import AIUsageRecord  # noqa: E402

OPUS_55 = 'claude-opus-5-5'
SONNET_55 = 'claude-sonnet-5-5'


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, SIGNALTRACKERS_DIR / relative_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextmanager
def metering_module():
    with patch.dict(sys.modules):
        for cached in ('config', 'services'):
            sys.modules.pop(cached, None)
        yield _load('usage_metering_us507', 'services/usage_metering.py')


@contextmanager
def config_module(env=None, unset=()):
    environ = {k: v for k, v in os.environ.items() if k not in unset}
    environ.update(env or {})
    with patch.dict(os.environ, environ, clear=True):
        yield _load('config_us507', 'config.py')


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

class TestPricingRows:
    EXPECTED = {
        OPUS_55: {
            'input': Decimal('4.00'), 'output': Decimal('20.00'),
            'cache_read': Decimal('0.20'), 'cache_creation': Decimal('5.00'),
        },
        SONNET_55: {
            'input': Decimal('2.00'), 'output': Decimal('10.00'),
            'cache_read': Decimal('0.10'), 'cache_creation': Decimal('2.50'),
        },
    }

    @pytest.mark.parametrize('model', [OPUS_55, SONNET_55])
    def test_published_figures_exact(self, model):
        with metering_module() as um:
            row = um.MODEL_PRICING[model]
        assert row == self.EXPECTED[model]
        assert all(isinstance(v, Decimal) for v in row.values())

    @pytest.mark.parametrize('model', [OPUS_55, SONNET_55])
    def test_own_key_and_exact_row(self, model):
        """Sonnet 5.5 must not fall through to the Sonnet 5 row by prefix."""
        with metering_module() as um:
            assert model in um.MODEL_PRICING
            assert um._get_pricing(model) is um.MODEL_PRICING[model]

    def test_sonnet_55_does_not_resolve_to_sonnet_5(self):
        with metering_module() as um:
            assert um._get_pricing(SONNET_55) is not um.MODEL_PRICING['claude-sonnet-5']

    @pytest.mark.parametrize('variant,base', [
        ('claude-opus-5-5-20261101', OPUS_55),
        ('claude-opus-5-5-latest', OPUS_55),
        ('claude-sonnet-5-5-20261101', SONNET_55),
        ('claude-sonnet-5-5-latest', SONNET_55),
        ('claude-sonnet-5-5-x', SONNET_55),
    ])
    def test_snapshots_and_aliases_take_the_longest_prefix(self, variant, base):
        with metering_module() as um:
            assert um._get_pricing(variant) is um.MODEL_PRICING[base]

    @pytest.mark.parametrize('model', [OPUS_55, SONNET_55])
    def test_no_unknown_model_warning(self, model, caplog):
        with metering_module() as um, caplog.at_level('WARNING'):
            um._get_pricing(model)
        assert 'Unknown model for pricing' not in caplog.text

    def test_opus_55_cost_non_round_tokens(self):
        with metering_module() as um:
            cost = um.calculate_cost(OPUS_55, 123_457, 9_871, 54_321, 7_777)
        expected = (Decimal(123_457) * 4 + Decimal(9_871) * 20
                    + Decimal(54_321) * Decimal('0.20') + Decimal(7_777) * 5) / 1_000_000
        # = 0.493828 + 0.19742 + 0.0108642 + 0.038885
        assert cost == expected == Decimal('0.7409972')

    def test_sonnet_55_cost_non_round_tokens(self):
        with metering_module() as um:
            cost = um.calculate_cost(SONNET_55, 123_457, 9_871, 54_321, 7_777)
        expected = (Decimal(123_457) * 2 + Decimal(9_871) * 10
                    + Decimal(54_321) * Decimal('0.10') + Decimal(7_777) * Decimal('2.50')) / 1_000_000
        # = 0.246914 + 0.09871 + 0.0054321 + 0.0194425
        assert cost == expected == Decimal('0.3704986')

    def test_rollback_rows_unchanged(self):
        with metering_module() as um:
            p = um.MODEL_PRICING
        assert p['claude-fable-5-1'] == {
            'input': Decimal('10.00'), 'output': Decimal('50.00'),
            'cache_read': Decimal('0.25'), 'cache_creation': Decimal('12.50')}
        assert p['claude-sonnet-5'] == {
            'input': Decimal('2.00'), 'output': Decimal('10.00'),
            'cache_read': Decimal('0.20'), 'cache_creation': Decimal('2.50')}
        assert p['claude-opus-4-6']['input'] == Decimal('5.00')
        assert p['claude-sonnet-4-6']['input'] == Decimal('3.00')

    def test_served_snapshot_prices_at_the_55_row(self):
        """served_model() returns response.model verbatim; a dated Opus 5.5
        snapshot must still cost at $4/$20, not the $3/$15 default."""
        with patch.dict(sys.modules):
            sys.modules.pop('config', None)
            ar = _load('anthropic_request_us507', 'anthropic_request.py')
        served = ar.served_model(SimpleNamespace(model='claude-opus-5-5-20261101'), OPUS_55)
        with metering_module() as um:
            assert um._get_pricing(served) is um.MODEL_PRICING[OPUS_55]


# ---------------------------------------------------------------------------
# Config and container plumbing
# ---------------------------------------------------------------------------

_MODEL_VARS = ('ANTHROPIC_MODEL', 'ANTHROPIC_CHATBOT_MODEL', 'ANTHROPIC_CHATBOT_EFFORT')


class TestConfig:
    def test_defaults_with_env_unset(self):
        with config_module(unset=_MODEL_VARS) as cfg:
            assert cfg.Config.ANTHROPIC_MODEL == OPUS_55
            assert cfg.Config.ANTHROPIC_CHATBOT_MODEL == SONNET_55
            assert cfg.Config.ANTHROPIC_CHATBOT_EFFORT == 'low'
            assert cfg.ANTHROPIC_MODEL == OPUS_55

    def test_env_overrides_win(self):
        env = {'ANTHROPIC_MODEL': 'claude-fable-5-1',
               'ANTHROPIC_CHATBOT_MODEL': 'claude-sonnet-5',
               'ANTHROPIC_CHATBOT_EFFORT': 'medium'}
        with config_module(env=env) as cfg:
            assert cfg.Config.ANTHROPIC_MODEL == 'claude-fable-5-1'
            assert cfg.Config.ANTHROPIC_CHATBOT_MODEL == 'claude-sonnet-5'
            assert cfg.Config.ANTHROPIC_CHATBOT_EFFORT == 'medium'

    def test_invalid_chatbot_effort_is_normalized(self):
        with config_module(env={'ANTHROPIC_CHATBOT_EFFORT': 'turbo'}) as cfg:
            assert cfg.Config.ANTHROPIC_CHATBOT_EFFORT in cfg.ANTHROPIC_EFFORT_LEVELS

    @pytest.mark.parametrize('compose_file', ['docker-compose.yml', 'docker-compose.prod.yml'])
    def test_compose_defaults_are_the_new_ids(self, compose_file):
        """.env sets neither var, so the compose default is what runs."""
        content = (REPO_ROOT / compose_file).read_text()
        assert '${ANTHROPIC_MODEL:-claude-opus-5-5}' in content
        assert '${ANTHROPIC_CHATBOT_MODEL:-claude-sonnet-5-5}' in content
        assert '${ANTHROPIC_CHATBOT_EFFORT:-low}' in content

    def test_env_example_documents_new_defaults(self):
        content = (REPO_ROOT / '.env.example').read_text()
        assert 'ANTHROPIC_MODEL=claude-opus-5-5' in content
        assert 'ANTHROPIC_CHATBOT_MODEL=claude-sonnet-5-5' in content
        assert 'ANTHROPIC_CHATBOT_EFFORT=low' in content


# ---------------------------------------------------------------------------
# /api/chatbot, through the real route
# ---------------------------------------------------------------------------

def _block(kind, **kw):
    return SimpleNamespace(type=kind, **kw)


def _resp(content, stop_reason='end_turn', stop_details=None,
          usage=(1000, 200, 0, 0)):
    i, o, cr, cc = usage
    return SimpleNamespace(
        stop_reason=stop_reason, stop_details=stop_details, model=SONNET_55,
        content=list(content),
        usage=SimpleNamespace(input_tokens=i, output_tokens=o,
                              cache_read_input_tokens=cr,
                              cache_creation_input_tokens=cc),
    )


def _thinking(text=''):
    return _block('thinking', thinking=text, signature='sig')


def _tool_use(name='get_metric', tid='tu_1'):
    return _block('tool_use', name=name, id=tid, input={'metric': 'yield_curve_10y2y'})


LEAK = 'SECRET-REASONING-MUST-NOT-LEAK'


@pytest.fixture
def chatbot():
    """Drive /api/chatbot with a mocked Anthropic client and a logged-in user.

    Yields (post, client_mock, records) where records() returns the
    AIUsageRecord rows written by the request.
    """
    with patch.dict(sys.modules):
        sys.modules.pop('services', None)
        um = _load('services.usage_metering', 'services/usage_metering.py')
        sys.modules['services.usage_metering'] = um
        ai_service = _load('services.ai_service', 'services/ai_service.py')
        sys.modules['services.ai_service'] = ai_service

        client_mock = MagicMock()
        old = {k: app.config.get(k) for k in ('TESTING', 'WTF_CSRF_ENABLED')}
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with app.app_context():
            db.drop_all()
            db.create_all()
            user = User(username='us507', email='us507@example.com')
            user.set_password('TestPass123')
            db.session.add(user)
            db.session.commit()
            uid = user.id
            try:
                with patch.object(ai_service, 'get_system_ai_client',
                                  return_value=(client_mock, 'anthropic')), \
                     patch.object(dashboard, '_build_chatbot_enrichment_context', return_value=''), \
                     patch.object(dashboard, '_execute_tool', return_value='{"value": 0.42}'):
                    http = app.test_client()
                    with http.session_transaction() as sess:
                        sess['_user_id'] = str(uid)
                        sess['_fresh'] = True

                    def post(body=None):
                        return http.post('/api/chatbot', json=body or {
                            'message': 'What is the 10Y-2Y spread?',
                            'conversation': [], 'context': {'page': '/'}})

                    def records():
                        return AIUsageRecord.query.filter_by(user_id=uid).all()

                    yield post, client_mock, records
            finally:
                db.session.remove()
                db.drop_all()
                db.create_all()
                app.config.update(old)


class TestChatbotRequest:
    def test_sends_explicit_low_effort(self, chatbot):
        post, client, _ = chatbot
        client.messages.create.return_value = _resp([_thinking(), _block('text', text='Hi.')])
        assert post().status_code == 200
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs['output_config'] == {'effort': 'low'}

    def test_thinking_contract_unchanged(self, chatbot):
        post, client, _ = chatbot
        client.messages.create.return_value = _resp([_block('text', text='Hi.')])
        post()
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs['thinking'] == {'type': 'adaptive'}
        for banned in ('tool_choice', 'temperature', 'budget_tokens'):
            assert banned not in kwargs
        assert kwargs['model'] == SONNET_55

    def test_request_body_cannot_choose_model_or_effort(self, chatbot):
        post, client, _ = chatbot
        client.messages.create.return_value = _resp([_block('text', text='Hi.')])
        post({'message': 'hi', 'conversation': [], 'context': {'page': '/'},
              'model': 'claude-fable-5-1', 'effort': 'max',
              'output_config': {'effort': 'max'}})
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs['model'] == SONNET_55
        assert kwargs['output_config'] == {'effort': 'low'}


class TestChatbotAnswerExtraction:
    def test_only_final_text_reaches_the_user(self, chatbot):
        post, client, _ = chatbot
        client.messages.create.side_effect = [
            _resp([_thinking(LEAK), _block('text', text='Let me check.'), _tool_use()],
                  stop_reason='tool_use'),
            _resp([_thinking(LEAK), _block('text', text='The spread is 0.42%.')]),
        ]
        answer = post().get_json()['response']
        assert answer == 'The spread is 0.42%.'
        assert LEAK not in answer
        assert client.messages.create.call_count == 2

    def test_tool_turn_with_no_text_block(self, chatbot):
        post, client, _ = chatbot
        client.messages.create.side_effect = [
            _resp([_thinking('progress note'), _tool_use()], stop_reason='tool_use'),
            _resp([_thinking(), _block('text', text='It is 0.42%.')]),
        ]
        answer = post().get_json()['response']
        assert answer == 'It is 0.42%.'
        # The tool result went back with the assistant turn intact.
        second = client.messages.create.call_args_list[1].kwargs['messages']
        assert second[-1]['content'][0]['type'] == 'tool_result'

    def test_thinking_only_final_turn_falls_back_to_apology(self, chatbot):
        post, client, _ = chatbot
        client.messages.create.return_value = _resp([_thinking(LEAK)], stop_reason='max_tokens')
        res = post()
        assert res.status_code == 200
        answer = res.get_json()['response']
        assert 'apologize' in answer
        assert LEAK not in answer

    def test_refusal_handled_before_content(self, chatbot):
        post, client, _ = chatbot
        client.messages.create.return_value = _resp(
            [_block('text', text=LEAK)], stop_reason='refusal',
            stop_details=SimpleNamespace(type='refusal', category='cyber', explanation='x'))
        res = post()
        assert res.status_code == 200
        assert LEAK not in res.get_json()['response']


class TestChatbotMetering:
    def test_tool_session_metered_at_sonnet_55_rates(self, chatbot):
        post, client, records = chatbot
        client.messages.create.side_effect = [
            _resp([_thinking(), _tool_use()], stop_reason='tool_use',
                  usage=(123_457, 9_871, 54_321, 7_777)),
            _resp([_block('text', text='Done.')], usage=(1_000, 100, 0, 0)),
        ]
        assert post().status_code == 200
        rows = records()
        assert len(rows) == 1
        row = rows[0]
        assert row.model == SONNET_55
        assert row.interaction_type == 'chatbot'
        expected = (Decimal(124_457) * 2 + Decimal(9_971) * 10
                    + Decimal(54_321) * Decimal('0.10')
                    + Decimal(7_777) * Decimal('2.50')) / 1_000_000
        assert Decimal(str(row.estimated_cost)) == expected.quantize(
            Decimal(str(row.estimated_cost)))
