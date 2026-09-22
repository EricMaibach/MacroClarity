"""
Tests for US-16.1.2: migrate the Anthropic request surface to the current API.

These cover the parts that can be pinned without a real API call: the request
*shape* we send, the effort-validation path, the response-reading paths that
used to assume thinking was off, and the pricing rows the migration adds.

What they deliberately do not cover: whether the real API accepts the request.
That is the whole reason this story exists — a mocked call hid a real 400 — so
end-to-end verification against the live models is QA's, not these tests'.

Test isolation note: other modules in this suite leave MagicMocks in
sys.modules for 'config' and 'services', so everything here loads from a file
path inside a restored sys.modules snapshot.
"""

import importlib.util
import os
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'

sys.path.insert(0, str(SIGNALTRACKERS_DIR))

BRIEFING_MODEL = 'claude-fable-5-1'
CHATBOT_MODEL = 'claude-sonnet-5'
EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(
        name, SIGNALTRACKERS_DIR / relative_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@contextmanager
def request_module(env=None):
    """Load anthropic_request with a freshly-evaluated config underneath it.

    anthropic_request binds ANTHROPIC_EFFORT at import time, and config.py
    binds it from os.environ at *its* import time, so both have to be reloaded
    for an env override to take effect — the same chain a container start uses.
    """
    with patch.dict(os.environ, env or {}, clear=False), patch.dict(sys.modules):
        for cached in ('config', 'anthropic_request', 'services'):
            sys.modules.pop(cached, None)
        _load('config', 'config.py')
        yield _load('anthropic_request', 'anthropic_request.py')


@contextmanager
def metering_module():
    with patch.dict(sys.modules):
        for cached in ('config', 'services'):
            sys.modules.pop(cached, None)
        yield _load('usage_metering_under_test', 'services/usage_metering.py')


def _response(stop_reason='end_turn', content=(), model=None, stop_details=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        content=list(content),
    )


def _thinking_block():
    """What every response now leads with, empty under display='omitted'."""
    return SimpleNamespace(type='thinking', thinking='')


def _text_block(text):
    return SimpleNamespace(type='text', text=text)


# ---------------------------------------------------------------------------
# Request shape — the parameters that return 400 if we get them wrong
# ---------------------------------------------------------------------------

class TestRequestShape:
    def test_thinking_is_adaptive(self):
        """The only accepted on-mode. 'enabled' and 'disabled' both 400."""
        with request_module() as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert params['thinking'] == {'type': 'adaptive'}

    def test_no_budget_tokens_anywhere(self):
        """Regression guard: reintroducing budget_tokens is a 400 on every
        current model, and it is what this story exists to remove."""
        with request_module() as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert 'budget_tokens' not in repr(params)

    def test_effort_is_nested_in_output_config(self):
        """A top-level `effort` is not a request field — it must be nested."""
        with request_module() as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert 'effort' not in params
        assert set(params['output_config']) == {'effort'}

    def test_thinking_display_left_at_default(self):
        """'summarized' would bill output we never render."""
        with request_module() as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert 'display' not in params['thinking']

    def test_max_tokens_passed_through_unmodified(self):
        """No thinking budget is added on top of the caller's ceiling."""
        with request_module() as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 1234, 'sys', [])
        assert params['max_tokens'] == 1234

    def test_beta_header_pairs_with_the_scalar_fallback_form(self):
        """The header and the parameter form are coupled — mismatching them is
        itself a 400. -2026-07-01 goes with fallbacks="default"; the array form
        belongs to the older -2026-06-01 header."""
        with request_module() as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert params['betas'] == ['server-side-fallback-2026-07-01']
        assert params['fallbacks'] == 'default'

    def test_no_forced_tool_choice(self):
        """tool_choice any/tool return 400 on the briefing model."""
        with request_module() as ar:
            params = ar.build_request_params(
                BRIEFING_MODEL, 900, 'sys', [], tools=[{'name': 'search_web'}]
            )
        assert 'tool_choice' not in params

    def test_tools_omitted_when_none(self):
        with request_module() as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert 'tools' not in params

    def test_explicit_effort_overrides_config(self):
        """Short-output paths pin low effort so reasoning cannot eat the budget."""
        with request_module({'ANTHROPIC_EFFORT': 'max'}) as ar:
            params = ar.build_request_params(
                BRIEFING_MODEL, 900, 'sys', [], effort='low'
            )
        assert params['output_config'] == {'effort': 'low'}


# ---------------------------------------------------------------------------
# ANTHROPIC_EFFORT validation
# ---------------------------------------------------------------------------

class TestEffortValidation:
    @pytest.mark.parametrize('level', EFFORT_LEVELS)
    def test_every_valid_level_reaches_the_request(self, level):
        with request_module({'ANTHROPIC_EFFORT': level}) as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert params['output_config']['effort'] == level

    def test_xhigh_is_accepted(self):
        """New level, between high and max — it was not valid before this story."""
        with request_module({'ANTHROPIC_EFFORT': 'xhigh'}) as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert params['output_config']['effort'] == 'xhigh'

    @pytest.mark.parametrize('bad', ['turbo', '', '   ', '4096', 'enabled'])
    def test_invalid_value_never_reaches_the_api(self, bad):
        """Forwarding an unrecognised level would 400."""
        with request_module({'ANTHROPIC_EFFORT': bad}) as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert params['output_config']['effort'] in EFFORT_LEVELS
        assert params['output_config']['effort'] == 'medium'

    def test_invalid_value_warns(self, caplog):
        with patch.dict(sys.modules):
            for cached in ('config', 'anthropic_request'):
                sys.modules.pop(cached, None)
            with patch.dict(os.environ, {'ANTHROPIC_EFFORT': 'turbo'}, clear=False):
                with caplog.at_level('WARNING'):
                    _load('config', 'config.py')
        messages = [r.getMessage() for r in caplog.records]
        assert any('ANTHROPIC_EFFORT' in m and 'turbo' in m for m in messages), messages

    @pytest.mark.parametrize('raw,expected', [
        ('HIGH', 'high'), ('  Max ', 'max'), ('XHigh', 'xhigh'),
    ])
    def test_case_and_whitespace_normalised(self, raw, expected):
        with request_module({'ANTHROPIC_EFFORT': raw}) as ar:
            params = ar.build_request_params(BRIEFING_MODEL, 900, 'sys', [])
        assert params['output_config']['effort'] == expected


# ---------------------------------------------------------------------------
# Reading the response — the assumptions that break once thinking is always on
# ---------------------------------------------------------------------------

class TestResponseReading:
    def test_text_extracted_past_a_leading_thinking_block(self):
        """The content[0] trap: a ThinkingBlock has no .text. Under the default
        display='omitted' its text is empty, so a hand-built mock that puts the
        text block first hides this and only the real API fails."""
        with request_module() as ar:
            resp = _response(content=[_thinking_block(), _text_block(' answer ')])
            assert ar.extract_text(resp) == 'answer'

    def test_multiple_text_blocks_joined(self):
        with request_module() as ar:
            resp = _response(
                content=[_thinking_block(), _text_block('one'), _text_block('two')]
            )
            assert ar.extract_text(resp) == 'one two'

    def test_thinking_only_response_returns_none_not_an_error(self):
        """Reasoning consumed the whole budget — no visible text at all."""
        with request_module() as ar:
            resp = _response(stop_reason='max_tokens', content=[_thinking_block()])
            assert ar.extract_text(resp) is None

    def test_whitespace_only_text_is_treated_as_empty(self):
        with request_module() as ar:
            assert ar.extract_text(_response(content=[_text_block('   ')])) is None

    def test_refusal_reported_with_category_and_explanation(self):
        with request_module() as ar:
            resp = _response(
                stop_reason='refusal',
                stop_details=SimpleNamespace(category='cyber', explanation='no'),
            )
            reason = ar.refusal_reason(resp)
        assert 'cyber' in reason and 'no' in reason

    @pytest.mark.parametrize(
        'stop_reason', ['end_turn', 'max_tokens', 'tool_use', 'pause_turn'],
    )
    def test_stop_details_never_read_on_a_non_refusal(self, stop_reason):
        """stop_details is None for every stop reason except refusal, so an
        unguarded read is an AttributeError."""
        with request_module() as ar:
            resp = _response(stop_reason=stop_reason, stop_details=None)
            assert ar.refusal_reason(resp) is None

    def test_truncation_is_reported(self):
        with request_module() as ar:
            assert ar.warn_if_truncated(
                _response(stop_reason='max_tokens'), '[T]', 512) is True
            assert ar.warn_if_truncated(
                _response(stop_reason='end_turn'), '[T]', 512) is False

    def test_served_model_prefers_the_model_that_actually_ran(self):
        """A server-side fallback can answer on a different model; costing the
        turn at the requested model's rates would misattribute the spend."""
        with request_module() as ar:
            resp = _response(model='claude-opus-4-8', content=[_text_block('x')])
            assert ar.served_model(resp, BRIEFING_MODEL) == 'claude-opus-4-8'

    def test_served_model_falls_back_to_the_requested_model(self):
        with request_module() as ar:
            resp = _response(model=None, content=[_text_block('x')])
            assert ar.served_model(resp, BRIEFING_MODEL) == BRIEFING_MODEL


# ---------------------------------------------------------------------------
# Pricing rows added by this story
# ---------------------------------------------------------------------------

class TestPricing:
    EXPECTED = {
        BRIEFING_MODEL: {
            'input': Decimal('10.00'), 'output': Decimal('50.00'),
            'cache_read': Decimal('0.25'), 'cache_creation': Decimal('12.50'),
        },
        CHATBOT_MODEL: {
            'input': Decimal('2.00'), 'output': Decimal('10.00'),
            'cache_read': Decimal('0.20'), 'cache_creation': Decimal('2.50'),
        },
    }

    @pytest.mark.parametrize('model', [BRIEFING_MODEL, CHATBOT_MODEL])
    def test_new_defaults_have_their_own_row(self, model):
        with metering_module() as um:
            assert um._get_pricing(model) is um.MODEL_PRICING[model]
            assert um._get_pricing(model) is not um._DEFAULT_PRICING

    @pytest.mark.parametrize('model', [BRIEFING_MODEL, CHATBOT_MODEL])
    def test_published_figures(self, model):
        with metering_module() as um:
            assert um.MODEL_PRICING[model] == self.EXPECTED[model]

    def test_briefing_cache_read_is_not_ten_percent_of_input(self):
        """The one row where the usual 10% ratio does not hold. Deriving it
        would overstate cache-read cost by 4x on the model that reads the most
        cache, which is exactly what this story's cost measurement depends on."""
        with metering_module() as um:
            row = um.MODEL_PRICING[BRIEFING_MODEL]
        assert row['cache_read'] == Decimal('0.25')
        assert row['cache_read'] != row['input'] / 10

    def test_rollback_models_still_priced(self):
        """ANTHROPIC_MODEL=claude-opus-4-6 must not drop to default pricing,
        and historical usage records must keep costing correctly."""
        with metering_module() as um:
            for old in ('claude-opus-4-6', 'claude-sonnet-4-6'):
                assert um._get_pricing(old) is um.MODEL_PRICING[old]

    @pytest.mark.parametrize('variant', [
        'claude-fable-5-1-20260115', 'claude-fable-5-1-latest',
    ])
    def test_dated_snapshots_price_off_the_base_row(self, variant):
        """record_usage stores response.model verbatim, so a snapshot or alias
        has to resolve by prefix or it silently gets _DEFAULT_PRICING."""
        with metering_module() as um:
            assert um._get_pricing(variant) is um.MODEL_PRICING[BRIEFING_MODEL]

    def test_longest_prefix_wins_over_dict_order(self):
        """'claude-fable-5-1'.startswith('claude-fable-5') is True. If a
        shorter key is ever added, first-match-by-dict-order would hand the
        5.1 model the wrong row depending on insertion order alone."""
        with metering_module() as um:
            patched = dict(um.MODEL_PRICING)
            patched['claude-fable-5'] = dict(patched[BRIEFING_MODEL],
                                             input=Decimal('99.00'))
            with patch.object(um, 'MODEL_PRICING', patched):
                assert um._get_pricing('claude-fable-5-1-20260115') is \
                    patched[BRIEFING_MODEL]
                assert um._get_pricing('claude-fable-5-20260115') is \
                    patched['claude-fable-5']

    def test_cost_matches_a_hand_calculation(self):
        with metering_module() as um:
            cost = um.calculate_cost(
                BRIEFING_MODEL,
                input_tokens=100_000, output_tokens=20_000,
                cache_read_tokens=400_000, cache_creation_tokens=50_000,
            )
        # 0.1*10 + 0.02*50 + 0.4*0.25 + 0.05*12.50 = 1 + 1 + 0.1 + 0.625
        assert cost == Decimal('2.725')

    def test_unknown_model_still_falls_back_loudly(self):
        with metering_module() as um:
            assert um._get_pricing('not-a-real-model') is um._DEFAULT_PRICING
