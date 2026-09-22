"""
Tests for US-325.1: AI briefing — conditions context, 90d history, 14d briefing history.

Verifies that:
- ai_summary.py uses market conditions (not old regime labels) in prompts
- Conditions context includes all 4 dimensions with scores and states
- 90-day conditions history is built from market_conditions_history.json
- Briefing history expanded from 3 to 14 days
- Prompt uses quadrant-led three-paragraph narrative structure
- Old regime labels are not referenced in the AI prompt
- Rule-based fallback generates coherent briefings from dimension states
"""

import json
import os
import sys
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNALTRACKERS_DIR = os.path.join(REPO_ROOT, 'signaltrackers')
sys.path.insert(0, SIGNALTRACKERS_DIR)


def read_source(filename):
    path = os.path.join(SIGNALTRACKERS_DIR, filename)
    with open(path, 'r') as f:
        return f.read()


# ---------------------------------------------------------------------------
# Static / structural tests — ai_summary.py
# ---------------------------------------------------------------------------


class TestConditionsImports(unittest.TestCase):
    """ai_summary.py must import from market_conditions, not regime_detection."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_get_market_conditions_imported(self):
        self.assertIn('get_market_conditions', self.src)

    def test_get_conditions_history_imported(self):
        self.assertIn('get_conditions_history', self.src)

    def test_old_regime_import_removed(self):
        self.assertNotIn('from regime_detection import get_macro_regime', self.src)

    def test_get_macro_regime_not_called(self):
        self.assertNotIn('get_macro_regime()', self.src)


class TestConditionsContextInPrompt(unittest.TestCase):
    """Conditions context must be present in the AI prompt."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_conditions_context_heading(self):
        self.assertIn('CURRENT MARKET CONDITIONS', self.src)

    def test_conditions_history_heading(self):
        self.assertIn('CONDITIONS HISTORY', self.src)

    def test_quadrant_in_context(self):
        self.assertIn('Quadrant:', self.src)

    def test_liquidity_in_context(self):
        self.assertIn('Liquidity:', self.src)

    def test_risk_in_context(self):
        self.assertIn('Risk:', self.src)

    def test_policy_in_context(self):
        self.assertIn('Policy:', self.src)


class TestThreeParagraphNarrative(unittest.TestCase):
    """System prompt must enforce three-paragraph quadrant-led structure."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_three_paragraphs_instruction(self):
        self.assertIn('3 paragraphs', self.src)

    def test_whats_happening_paragraph(self):
        self.assertIn("WHAT'S HAPPENING", self.src)

    def test_whats_changing_paragraph(self):
        self.assertIn("WHAT'S CHANGING", self.src)

    def test_what_to_do_paragraph(self):
        self.assertIn("WHAT TO DO", self.src)

    def test_quadrant_naming_instruction(self):
        self.assertIn('naming the current macro quadrant', self.src)


class TestOldRegimeRemoved(unittest.TestCase):
    """Old regime references must not appear in AI prompt construction."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_no_regime_prefix_briefing(self):
        self.assertNotIn('regime_prefix_briefing', self.src)

    def test_no_regime_state_variable(self):
        self.assertNotIn('regime_state', self.src)

    def test_four_quadrants_listed(self):
        self.assertIn('Goldilocks, Reflation, Stagflation, Deflation Risk', self.src)


class TestExpandedBriefingHistory(unittest.TestCase):
    """Briefing history must be expanded from 3 to 14 days."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_14_day_history(self):
        self.assertIn('get_recent_summaries(days=14)', self.src)

    def test_daily_summary_uses_14_day_history(self):
        # The generate_daily_summary function must call with days=14
        idx = self.src.find('def generate_daily_summary')
        block = self.src[idx:idx + 3000]
        self.assertIn('get_recent_summaries(days=14)', block)

    def test_earlier_predictions_reference(self):
        self.assertIn('earlier predictions', self.src)


class TestMarketSpecificBriefingHistory(unittest.TestCase):
    """All 5 market-specific briefings must use 14-day history."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_crypto_summary_uses_14_day_history(self):
        idx = self.src.find('def generate_crypto_summary')
        block = self.src[idx:idx + 3000]
        self.assertIn('get_recent_crypto_summaries(days=14)', block)

    def test_equity_summary_uses_14_day_history(self):
        idx = self.src.find('def generate_equity_summary')
        block = self.src[idx:idx + 3000]
        self.assertIn('get_recent_equity_summaries(days=14)', block)

    def test_rates_summary_uses_14_day_history(self):
        idx = self.src.find('def generate_rates_summary')
        block = self.src[idx:idx + 3000]
        self.assertIn('get_recent_rates_summaries(days=14)', block)

    def test_dollar_summary_uses_14_day_history(self):
        idx = self.src.find('def generate_dollar_summary')
        block = self.src[idx:idx + 3000]
        self.assertIn('get_recent_dollar_summaries(days=14)', block)

    def test_credit_summary_uses_14_day_history(self):
        idx = self.src.find('def generate_credit_summary')
        block = self.src[idx:idx + 3000]
        self.assertIn('get_recent_credit_summaries(days=14)', block)


class TestOldRegimeRemovedFromPrompts(unittest.TestCase):
    """Old MACRO REGIME sections must be removed from AI prompt builders."""

    @classmethod
    def setUpClass(cls):
        cls.dashboard_src = read_source('dashboard.py')
        cls.ai_src = read_source('ai_summary.py')

    def test_no_macro_regime_heading_in_dashboard_prompts(self):
        """'## MACRO REGIME' heading must not appear in prompt builders."""
        self.assertNotIn('"## MACRO REGIME"', self.dashboard_src)
        self.assertNotIn("'## MACRO REGIME'", self.dashboard_src)

    def test_no_old_regime_labels_in_credit_prompt(self):
        """Credit prompt must not reference bull/bear/recession watch labels."""
        idx = self.ai_src.find('def generate_credit_summary')
        block = self.ai_src[idx:idx + 5000]
        self.assertNotIn('bull/bear/recession watch', block.lower())

    def test_credit_prompt_uses_conditions_quadrant(self):
        """Credit prompt should reference conditions quadrant, not old regime."""
        idx = self.ai_src.find('def generate_credit_summary')
        block = self.ai_src[idx:idx + 5000]
        self.assertIn('conditions quadrant', block)


class TestMaxTokensIncreased(unittest.TestCase):
    """max_tokens should be increased for 3-paragraph output."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_max_tokens_sufficient(self):
        # Find the call_ai_with_tools call in generate_daily_summary context.
        # Asserted as a floor, not an exact value: US-16.1.2 made max_tokens a
        # combined reasoning + output ceiling (thinking is always on and is
        # generated against the same budget), so the number legitimately moves
        # upward. What must never happen is it dropping below what three
        # paragraphs need.
        idx = self.src.find('def generate_daily_summary')
        end_idx = self.src.find('\ndef ', idx + 1)
        block = self.src[idx:end_idx]
        match = re.search(r'max_tokens=(\d+)', block)
        self.assertIsNotNone(match, 'no max_tokens= in generate_daily_summary')
        self.assertGreaterEqual(int(match.group(1)), 800)


# ---------------------------------------------------------------------------
# Functional tests — helper functions
# ---------------------------------------------------------------------------


class TestBuildConditionsContext(unittest.TestCase):
    """_build_conditions_context produces correct output."""

    def _call(self, conditions):
        from ai_summary import _build_conditions_context
        return _build_conditions_context(conditions)

    def test_none_returns_empty(self):
        self.assertEqual(self._call(None), "")

    def test_empty_dict_returns_empty(self):
        result = self._call({})
        self.assertEqual(result, "")

    def test_full_conditions(self):
        conditions = {
            'quadrant': 'Goldilocks',
            'dimensions': {
                'quadrant': {'state': 'Goldilocks', 'growth_composite': 0.6, 'inflation_composite': -0.4},
                'liquidity': {'state': 'Expanding', 'score': 0.82},
                'risk': {'state': 'Calm', 'score': 1, 'vix_level': 14.8},
                'policy': {'stance': 'Neutral', 'direction': 'Easing', 'taylor_gap': -0.3},
            },
            'asset_expectations': [
                {'asset': 'S&P 500', 'direction': 'positive', 'magnitude': 'strong'},
            ],
        }
        result = self._call(conditions)
        self.assertIn('Goldilocks', result)
        self.assertIn('Expanding', result)
        self.assertIn('score: 0.82', result)
        self.assertIn('Calm', result)
        self.assertIn('VIX: 14.8', result)
        self.assertIn('Neutral', result)
        self.assertIn('Easing', result)
        self.assertIn('Taylor gap: -0.30', result)
        self.assertIn('Growth composite: +0.60', result)
        self.assertIn('Inflation composite: -0.40', result)
        self.assertIn('S&P 500: positive (strong)', result)


class TestBuildConditionsHistoryContext(unittest.TestCase):
    """_build_conditions_history_context produces correct output."""

    def _call(self, history, days=90):
        from ai_summary import _build_conditions_history_context
        return _build_conditions_history_context(history, days=days)

    def test_empty_history(self):
        self.assertEqual(self._call({}), "")

    def test_none_history(self):
        self.assertEqual(self._call(None), "")

    def test_single_entry(self):
        history = {
            '2026-03-18': {
                'quadrant': 'Goldilocks',
                'dimensions': {
                    'liquidity': {'state': 'Expanding', 'score': 0.82},
                    'risk': {'state': 'Calm'},
                    'policy': {'stance': 'Neutral', 'direction': 'Easing'},
                },
            }
        }
        result = self._call(history, days=90)
        self.assertIn('CONDITIONS HISTORY', result)
        self.assertIn('2026-03-18', result)
        self.assertIn('Goldilocks', result)

    def test_transition_detection(self):
        history = {
            '2026-01-15': {
                'quadrant': 'Reflation',
                'dimensions': {
                    'liquidity': {'state': 'Neutral', 'score': 0.1},
                    'risk': {'state': 'Normal'},
                    'policy': {'stance': 'Neutral', 'direction': 'Paused'},
                },
            },
            '2026-02-15': {
                'quadrant': 'Goldilocks',
                'dimensions': {
                    'liquidity': {'state': 'Expanding', 'score': 0.5},
                    'risk': {'state': 'Calm'},
                    'policy': {'stance': 'Neutral', 'direction': 'Easing'},
                },
            },
        }
        result = self._call(history, days=90)
        self.assertIn('Transitions:', result)
        self.assertIn('Reflation', result)

    def test_growth_inflation_scores_included(self):
        history = {
            '2026-03-10': {
                'quadrant': 'Goldilocks',
                'growth_score': 0.309,
                'inflation_score': -0.552,
                'dimensions': {
                    'quadrant': {'state': 'Goldilocks'},
                    'liquidity': {'state': 'Expanding', 'score': 0.7},
                    'risk': {'state': 'Calm'},
                    'policy': {'stance': 'Neutral', 'direction': 'Easing'},
                },
            },
        }
        result = self._call(history, days=90)
        self.assertIn('growth=+0.31', result)
        self.assertIn('inflation=-0.55', result)

    def test_streak_counting(self):
        history = {
            '2026-01-15': {
                'quadrant': 'Goldilocks',
                'dimensions': {
                    'liquidity': {'state': 'Expanding', 'score': 0.5},
                    'risk': {'state': 'Calm'},
                    'policy': {'stance': 'Neutral', 'direction': 'Easing'},
                },
            },
            '2026-02-15': {
                'quadrant': 'Goldilocks',
                'dimensions': {
                    'liquidity': {'state': 'Expanding', 'score': 0.6},
                    'risk': {'state': 'Calm'},
                    'policy': {'stance': 'Neutral', 'direction': 'Easing'},
                },
            },
            '2026-03-15': {
                'quadrant': 'Goldilocks',
                'dimensions': {
                    'liquidity': {'state': 'Expanding', 'score': 0.82},
                    'risk': {'state': 'Calm'},
                    'policy': {'stance': 'Neutral', 'direction': 'Easing'},
                },
            },
        }
        result = self._call(history, days=90)
        self.assertIn('Goldilocks for 3 consecutive snapshots', result)


class TestFallbackBriefing(unittest.TestCase):
    """_generate_fallback_briefing produces coherent output for all quadrants."""

    def _call(self, conditions):
        from ai_summary import _generate_fallback_briefing
        return _generate_fallback_briefing(conditions)

    def test_none_conditions(self):
        result = self._call(None)
        self.assertIn('unavailable', result)

    def test_goldilocks_expanding(self):
        conditions = {
            'quadrant': 'Goldilocks',
            'dimensions': {
                'liquidity': {'state': 'Expanding'},
                'risk': {'state': 'Calm'},
                'policy': {'stance': 'Neutral', 'direction': 'Easing'},
            },
        }
        result = self._call(conditions)
        self.assertIn('Goldilocks', result)
        self.assertIn('Expanding', result)
        self.assertIn('tailwind', result.lower())

    def test_stagflation_contracting(self):
        conditions = {
            'quadrant': 'Stagflation',
            'dimensions': {
                'liquidity': {'state': 'Contracting'},
                'risk': {'state': 'Stressed'},
                'policy': {'stance': 'Restrictive', 'direction': 'Tightening'},
            },
        }
        result = self._call(conditions)
        self.assertIn('Stagflation', result)
        self.assertIn('Contracting', result)

    def test_deflation_risk_neutral(self):
        conditions = {
            'quadrant': 'Deflation Risk',
            'dimensions': {
                'liquidity': {'state': 'Neutral'},
                'risk': {'state': 'Elevated'},
                'policy': {'stance': 'Accommodative', 'direction': 'Easing'},
            },
        }
        result = self._call(conditions)
        self.assertIn('Deflation Risk', result)

    def test_reflation_expanding(self):
        conditions = {
            'quadrant': 'Reflation',
            'dimensions': {
                'liquidity': {'state': 'Expanding'},
                'risk': {'state': 'Normal'},
                'policy': {'stance': 'Neutral', 'direction': 'Paused'},
            },
        }
        result = self._call(conditions)
        self.assertIn('Reflation', result)

    def test_no_old_regime_labels(self):
        """Fallback must not mention Bull, Bear, Neutral (as regime), or Recession Watch."""
        for quadrant in ['Goldilocks', 'Reflation', 'Stagflation', 'Deflation Risk']:
            conditions = {
                'quadrant': quadrant,
                'dimensions': {
                    'liquidity': {'state': 'Neutral'},
                    'risk': {'state': 'Normal'},
                    'policy': {'stance': 'Neutral', 'direction': 'Paused'},
                },
            }
            result = self._call(conditions)
            self.assertNotIn('Bull regime', result)
            self.assertNotIn('Bear regime', result)
            self.assertNotIn('Recession Watch', result)


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------


class TestSecurityConstraints(unittest.TestCase):
    """Conditions data must be handled safely in prompt construction."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_no_eval_in_conditions_context(self):
        self.assertNotIn('eval(', self.src)

    def test_conditions_data_not_rendered_with_safe(self):
        self.assertNotIn('| safe', self.src)

    def test_history_data_typed(self):
        """History data should use .get() for safe access."""
        # Check that the history builder uses .get() not direct key access
        idx = self.src.find('def _build_conditions_history_context')
        block = self.src[idx:idx + 2000]
        self.assertIn('.get(', block)


if __name__ == '__main__':
    unittest.main()
