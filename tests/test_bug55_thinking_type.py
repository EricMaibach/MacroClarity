"""
Tests for Bug #55 / Bug #131 — the thinking.type flip-flop, and how it ended.

History, because it explains why this file exists at all:

* **Bug #55** (Feb 21, 2026) moved `thinking.type` from `"enabled"` to
  `"adaptive"` to clear a deprecation warning — but kept `budget_tokens`
  alongside it. `adaptive` does not accept `budget_tokens`, so every call
  400'd.
* **Bug #131** (Feb 24, 2026) diagnosed the type as the culprit and reverted to
  `"enabled"`. That restored working calls, so it looked correct, and this file
  was rewritten to assert `"enabled"` was present and `"adaptive"` was absent.
* **US-16.1.2** (Sep 22, 2026) established that #55 had the right idea and the
  wrong detail: `adaptive` was always the destination, and `budget_tokens` was
  the part to drop. On the current models (`claude-fable-5-1`,
  `claude-sonnet-5`) `{"type": "enabled", "budget_tokens": N}` and
  `{"type": "disabled"}` both return 400 — verified against the live API during
  QA verification, not inferred:

      "thinking.type.enabled" is not supported for this model. Use
      "thinking.type.adaptive" and "output_config.effort" to control
      thinking behavior.

So every assertion in the previous version of this file is now inverted. The
lasting value of #55/#131 is the regression guard: `budget_tokens` must never
come back, whatever `thinking.type` is set to.

Design note — these tests assert *behaviour*, not source text. The old versions
matched string literals in `ai_summary.py`, which is why they kept passing
through a 400-on-every-call outage: the source looked right. Here the request
is built through the real code path and the resulting payload is inspected, so
a test cannot pin a shape the code does not actually send.
"""

import pytest

from tests.thinking_contract_helpers import (
    EFFORT_LEVELS,
    build_sample_request,
    files_containing,
    request_module,
)


class TestThinkingTypeCorrect:
    """The thinking parameter must be adaptive, with no token budget."""

    def test_adaptive_is_the_thinking_type(self):
        """The request must send thinking.type='adaptive' — the only on-mode."""
        params = build_sample_request()
        assert params['thinking'] == {'type': 'adaptive'}, (
            "thinking must be exactly {'type': 'adaptive'}. 'enabled' and "
            "'disabled' both return 400 on the current models."
        )

    def test_no_enabled_type(self):
        """'enabled' must not be sent — it is the shape Bug #131 wrongly restored."""
        params = build_sample_request()
        assert params['thinking'].get('type') != 'enabled', (
            "thinking.type='enabled' is rejected with a 400 on claude-fable-5-1."
        )

    def test_budget_tokens_absent_from_request(self):
        """budget_tokens must not appear anywhere in the outgoing request."""
        params = build_sample_request()
        assert 'budget_tokens' not in params['thinking'], (
            "budget_tokens inside the thinking block returns a 400."
        )
        assert 'budget_tokens' not in params, (
            "budget_tokens must not be a top-level request field either."
        )

    def test_budget_tokens_absent_from_package_code(self):
        """Regression guard: budget_tokens must not reappear in any module.

        This is the assertion Bug #55 and Bug #131 were really about. Comments
        and docstrings explaining the removal are excluded, so only a genuine
        reintroduction fails this.
        """
        offenders = files_containing('budget_tokens')
        assert offenders == [], (
            f"budget_tokens was reintroduced in: {offenders}. It returns a 400 "
            "on every current model — use output_config.effort to control "
            "thinking depth instead."
        )

    def test_effort_budgets_dict_is_gone(self):
        """The effort -> token-budget mapping has no meaning under the new API."""
        offenders = files_containing('effort_budgets')
        assert offenders == [], (
            f"The effort_budgets dict still exists in: {offenders}. Effort is "
            "now a named level passed as output_config.effort, not a token count."
        )

    def test_effort_is_a_named_level_not_a_token_count(self):
        """output_config.effort must carry one of the five documented levels."""
        params = build_sample_request()
        effort = params['output_config']['effort']
        assert effort in EFFORT_LEVELS, (
            f"output_config.effort={effort!r} is not one of {EFFORT_LEVELS}. "
            "The API rejects anything else with a 400."
        )
        assert not isinstance(effort, int), (
            "effort is a named level, not a token budget — the Bug #55 confusion."
        )

    def test_thinking_is_configured_only_in_known_places(self):
        """Pin the set of modules that configure thinking.

        Bug #55's fix had to be applied by hand at every call site, which is
        how the flip-flop became possible. `anthropic_request.py` is now the
        shared builder; `dashboard.py` still sets `thinking` inline on the
        chatbot path, which does not go through that builder. Both are known
        and intentional as of US-16.1.2 — a *third* module appearing here means
        the request shape is drifting apart again and this test should fail
        until it is either routed through the builder or added deliberately.
        """
        configurers = files_containing('adaptive')
        assert configurers == [
            'signaltrackers/anthropic_request.py',
            'signaltrackers/dashboard.py',
        ], (
            f"Modules configuring thinking changed to {configurers}. Route new "
            "call sites through anthropic_request.build_request_params so a "
            "future API change stays a one-file edit."
        )


class TestThinkingTypeHistoricalRegressions:
    """Guards for the specific mistakes #55 and #131 each made."""

    def test_adaptive_and_budget_tokens_never_combined(self):
        """The Bug #55 defect exactly: adaptive + budget_tokens = 400 on every call."""
        params = build_sample_request()
        thinking = params['thinking']
        assert not (thinking.get('type') == 'adaptive' and 'budget_tokens' in thinking), (
            "adaptive with budget_tokens is the Bug #55 defect — a 400 on every "
            "AI summary call, silently swallowed by the old retry fallback."
        )

    def test_no_silent_retry_fallback(self):
        """The blanket retry that hid Bug #55 for three days must stay removed.

        `del api_params["thinking"]` in an `except` is what turned a 400 into a
        thinking-free briefing that looked successful.
        """
        offenders = files_containing('del api_params')
        assert offenders == [], (
            f"A delete-and-retry fallback reappeared in: {offenders}. It "
            "converts a malformed request into a silently degraded briefing."
        )

    @pytest.mark.parametrize('level', EFFORT_LEVELS)
    def test_every_documented_effort_level_is_accepted(self, level):
        """All five levels, including the newer 'xhigh', must pass validation."""
        with request_module({'ANTHROPIC_EFFORT': level}) as mod:
            assert mod.ANTHROPIC_EFFORT == level

    @pytest.mark.parametrize('bogus', ['turbo', '', '   ', '4096', 'enabled'])
    def test_invalid_effort_never_reaches_the_request(self, bogus):
        """An unrecognised level must be replaced, not forwarded.

        '4096' is in this list deliberately: it is what a leftover Bug #55-era
        token budget would look like if it survived into the new env var.
        """
        params = build_sample_request(env={'ANTHROPIC_EFFORT': bogus})
        assert params['output_config']['effort'] in EFFORT_LEVELS, (
            f"ANTHROPIC_EFFORT={bogus!r} reached the request unchanged; the API "
            "would reject it with a 400."
        )
