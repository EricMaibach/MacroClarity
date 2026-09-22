"""
Tests for Bug #131 — rewritten for the current API contract (US-16.1.2).

**What this file used to assert, and why it is inverted.** Bug #131 concluded
that `thinking.type="adaptive"` was the cause of a 400-on-every-call outage and
reverted to `"enabled"` with `budget_tokens`. That diagnosis was wrong in its
detail: the 400 came from pairing `adaptive` *with* `budget_tokens`, which
Bug #55 had left in place. `adaptive` was always the right destination.

On `claude-fable-5-1` / `claude-sonnet-5` the old contract is now rejected
outright. Verified against the live API during QA verification of US-16.1.2:

    thinking={"type": "enabled", "budget_tokens": 4096}  -> 400
        '"thinking.type.enabled" is not supported for this model.'
    thinking={"type": "disabled"}                        -> 400
    output_config={"effort": "turbo"}                    -> 400
        "output_config.effort: Input should be 'low', 'medium', 'high',
         'xhigh' or 'max'"

So every AC in the original file (`"enabled"` present, `adaptive` absent,
`budget_tokens` present, `effort_budgets` in 1..32768, retry fallback present)
now describes a broken request.

**The deeper lesson, which shaped this rewrite.** The old tests read
`ai_summary.py` as text and matched string literals. That is why they stayed
green through the Bug #55 outage and went red on the fix: they pinned the
*form* of the source rather than the behaviour of the request. Worse, they
asserted `del api_params["thinking"]` must be present — locking in the silent
retry that hid the outage in the first place.

These tests build the request through the real code path and assert on the
resulting payload, and they cover the response-reading paths the old file never
touched — which is where the genuinely dangerous bug lived: with thinking
always on, `response.content[0]` is a `thinking` block with no `.text`
attribute, so the old `content[0].text` read raised `AttributeError` into a
bare `except` and produced a silently empty briefing. Confirmed against the
live API at every effort level.
"""

import pytest

from tests.thinking_contract_helpers import (
    EFFORT_LEVELS,
    build_sample_request,
    files_containing,
    request_module,
)


class _Block:
    """Minimal stand-in for a response content block."""

    def __init__(self, type_, text=None):
        self.type = type_
        if text is not None:
            self.text = text


class _ThinkingBlock:
    """A thinking block has no `.text` attribute at all — that is the trap.

    Under the default `display: "omitted"` its text is empty, so a mock that
    gives it an empty string still hides the failure. Only the missing
    attribute reproduces what the real API returns.
    """

    type = 'thinking'


class _Response:
    def __init__(self, content=None, stop_reason='end_turn',
                 stop_details=None, model='claude-fable-5-1'):
        self.content = content or []
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.model = model


class TestRequestSurface:
    """The request we actually send must match the current API contract."""

    def test_thinking_is_adaptive(self):
        params = build_sample_request()
        assert params['thinking'] == {'type': 'adaptive'}

    def test_thinking_type_disabled_is_never_sent(self):
        """'disabled' is a 400 on claude-fable-5-1, same as 'enabled'."""
        params = build_sample_request()
        assert params['thinking'].get('type') != 'disabled'

    def test_thinking_display_left_at_default(self):
        """We never surface reasoning, so 'summarized' would be billed and discarded."""
        params = build_sample_request()
        assert 'display' not in params['thinking'], (
            "display should stay at its default ('omitted'); 'summarized' adds "
            "output tokens the product never shows."
        )

    def test_effort_is_nested_inside_output_config(self):
        """effort is a field of output_config, never a top-level request field."""
        params = build_sample_request()
        assert params['output_config']['effort'] in EFFORT_LEVELS
        assert 'effort' not in params, (
            "effort must be nested in output_config; a top-level 'effort' is "
            "not a recognised request field."
        )

    def test_max_tokens_has_no_thinking_budget_added(self):
        """max_tokens must be passed through, not summed with a budget."""
        params = build_sample_request(max_tokens=16000)
        assert params['max_tokens'] == 16000, (
            "max_tokens was altered. The old code sent max_tokens + "
            "thinking_budget; thinking now shares the single ceiling."
        )

    def test_refusal_fallback_header_and_param_are_paired(self):
        """The -2026-07-01 header goes with the scalar form fallbacks='default'."""
        params = build_sample_request()
        assert params['betas'] == ['server-side-fallback-2026-07-01']
        assert params['fallbacks'] == 'default', (
            "The scalar 'default' form is what the -2026-07-01 header selects; "
            "the array form belongs to the older -2026-06-01 header."
        )

    def test_tool_choice_is_never_forced(self):
        """'any'/'tool' return a 400 on claude-fable-5-1; omitting it means auto."""
        params = build_sample_request(
            tools=[{'name': 't', 'description': 'd', 'input_schema': {}}]
        )
        assert params.get('tool_choice') in (None, 'auto', {'type': 'auto'})

    def test_tools_omitted_when_none_supplied(self):
        params = build_sample_request()
        assert 'tools' not in params


class TestEffortValidation:
    """ANTHROPIC_EFFORT is a named level; an invalid one must never be forwarded."""

    @pytest.mark.parametrize('level', EFFORT_LEVELS)
    def test_each_level_survives_to_the_request(self, level):
        params = build_sample_request(env={'ANTHROPIC_EFFORT': level})
        assert params['output_config']['effort'] == level

    def test_xhigh_is_accepted(self):
        """'xhigh' is newer than the original four and is easy to omit."""
        params = build_sample_request(env={'ANTHROPIC_EFFORT': 'xhigh'})
        assert params['output_config']['effort'] == 'xhigh'

    @pytest.mark.parametrize('bogus', ['turbo', '', '   ', 'None', '10000'])
    def test_invalid_values_fall_back(self, bogus):
        params = build_sample_request(env={'ANTHROPIC_EFFORT': bogus})
        assert params['output_config']['effort'] in EFFORT_LEVELS

    def test_invalid_value_logs_a_warning(self, caplog):
        with caplog.at_level('WARNING'):
            with request_module({'ANTHROPIC_EFFORT': 'turbo'}):
                pass
        warnings = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
        assert any('ANTHROPIC_EFFORT' in msg and 'turbo' in msg for msg in warnings), (
            "An unrecognised effort level must warn naming the offending value, "
            f"not fail silently. Warnings seen: {warnings}"
        )

    @pytest.mark.parametrize('cased', ['HIGH', 'High', ' high '])
    def test_case_and_whitespace_normalised(self, cased):
        params = build_sample_request(env={'ANTHROPIC_EFFORT': cased})
        assert params['output_config']['effort'] == 'high'

    def test_per_call_effort_override(self):
        """Short-output paths pin a low effort regardless of the configured level."""
        params = build_sample_request(env={'ANTHROPIC_EFFORT': 'max'}, effort='low')
        assert params['output_config']['effort'] == 'low'


class TestResponseReading:
    """With thinking always on, the first content block is not text."""

    def test_extract_text_skips_a_leading_thinking_block(self):
        with request_module() as mod:
            response = _Response(content=[_ThinkingBlock(), _Block('text', 'Briefing.')])
            assert mod.extract_text(response) == 'Briefing.'

    def test_old_content_zero_access_would_have_raised(self):
        """Pin the exact failure the old code hit, so nobody reinstates it."""
        response = _Response(content=[_ThinkingBlock(), _Block('text', 'Briefing.')])
        with pytest.raises(AttributeError):
            _ = response.content[0].text

    def test_extract_text_joins_multiple_text_blocks(self):
        with request_module() as mod:
            response = _Response(content=[
                _ThinkingBlock(), _Block('text', 'One.'), _Block('text', 'Two.'),
            ])
            assert mod.extract_text(response) == 'One. Two.'

    def test_extract_text_returns_none_when_only_thinking(self):
        """A budget consumed entirely by reasoning must read as no content."""
        with request_module() as mod:
            assert mod.extract_text(_Response(content=[_ThinkingBlock()])) is None

    def test_extract_text_returns_none_for_whitespace_only(self):
        with request_module() as mod:
            response = _Response(content=[_Block('text', '   ')])
            assert mod.extract_text(response) is None

    def test_no_content_zero_indexing_in_package(self):
        offenders = files_containing('content[0]')
        assert offenders == [], (
            f"content[0] indexing reappeared in: {offenders}. The first block "
            "is a thinking block whenever the model reasons."
        )


class TestRefusalHandling:
    """A refusal is not a briefing, and stop_details is null unless it is one."""

    def test_refusal_reason_none_for_normal_stop(self):
        with request_module() as mod:
            assert mod.refusal_reason(_Response(stop_reason='end_turn')) is None

    def test_stop_details_not_read_when_absent(self):
        """stop_details is None for every non-refusal stop — an unguarded read raises."""
        with request_module() as mod:
            for reason in ('end_turn', 'max_tokens', 'tool_use', 'pause_turn'):
                assert mod.refusal_reason(
                    _Response(stop_reason=reason, stop_details=None)
                ) is None

    def test_refusal_reports_category_and_explanation(self):
        with request_module() as mod:
            details = type('D', (), {'category': 'cyber', 'explanation': 'declined'})()
            reason = mod.refusal_reason(
                _Response(stop_reason='refusal', stop_details=details)
            )
            assert reason is not None
            assert 'cyber' in reason and 'declined' in reason

    def test_refusal_with_null_details_still_detected(self):
        """The chain can refuse without populated details — still not a briefing."""
        with request_module() as mod:
            assert mod.refusal_reason(
                _Response(stop_reason='refusal', stop_details=None)
            ) is not None


class TestTruncationAndFallbackAttribution:

    def test_max_tokens_stop_is_flagged(self):
        with request_module() as mod:
            assert mod.warn_if_truncated(
                _Response(stop_reason='max_tokens'), '[t]', 4000) is True

    def test_normal_stop_not_flagged(self):
        with request_module() as mod:
            assert mod.warn_if_truncated(
                _Response(stop_reason='end_turn'), '[t]', 4000) is False

    def test_cost_attributed_to_the_model_that_actually_ran(self):
        """A server-side fallback can serve the turn on a different model."""
        with request_module() as mod:
            served = mod.served_model(
                _Response(model='claude-opus-4-8'), 'claude-fable-5-1')
            assert served == 'claude-opus-4-8', (
                "Costing a fallback-served turn at the requested model's rates "
                "attributes spend to a model that never ran."
            )

    def test_requested_model_used_when_response_has_none(self):
        with request_module() as mod:
            assert mod.served_model(
                _Response(model=None), 'claude-fable-5-1') == 'claude-fable-5-1'


class TestOldContractCannotReturn:
    """Regression guards for the specific shapes that now 400."""

    def test_no_budget_tokens_anywhere(self):
        assert files_containing('budget_tokens') == []

    def test_no_effort_budgets_dict(self):
        assert files_containing('effort_budgets') == []

    def test_no_enabled_thinking_type(self):
        assert files_containing('"type": "enabled"') == []
        assert files_containing("'type': 'enabled'") == []

    def test_no_silent_retry_without_thinking(self):
        """The `del api_params["thinking"]` retry is what hid Bug #55 for days."""
        assert files_containing('del api_params') == []
