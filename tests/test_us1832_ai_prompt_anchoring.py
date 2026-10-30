"""
Tests for US-183.2: Homepage Narrative Cohesion — AI Prompt Anchoring (§1 + §2)

Verifies that:
1. generate_market_conditions_synthesis() (dashboard.py §1) injects a regime-anchoring
   prefix into its user_prompt when macro_regime is available and not 'unknown'.
2. generate_weekly_summary() (ai_summary.py §2) injects a regime-anchoring prefix into
   its user_prompt when macro_regime is available and not 'unknown'.
3. Both functions fall back gracefully (no prefix, no crash) when macro_regime is None
   or state is 'unknown' / 'Unknown'.
4. No HTML template containers are modified — regime reference is in generated text only.

Tests are primarily static (source-code analysis) supplemented by functional tests
with mocking for generate_weekly_summary() which can be tested without Flask.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNALTRACKERS_DIR = os.path.join(REPO_ROOT, 'signaltrackers')
sys.path.insert(0, SIGNALTRACKERS_DIR)


def read_source(filename):
    path = os.path.join(SIGNALTRACKERS_DIR, filename)
    with open(path, 'r') as f:
        return f.read()


# =============================================================================
# Static tests — source code structure
# =============================================================================

class TestStaticAiSummaryConditionsContext(unittest.TestCase):
    """generate_weekly_summary() must use market conditions context (US-325.1)."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_get_market_conditions_imported(self):
        self.assertIn('get_market_conditions', self.src,
                      'get_market_conditions not imported in ai_summary.py')

    def test_get_conditions_history_imported(self):
        self.assertIn('get_conditions_history', self.src,
                      'get_conditions_history not imported in ai_summary.py')

    def test_import_has_fallback(self):
        self.assertIn('except ImportError', self.src,
                      'ImportError fallback not found in ai_summary.py')

    def test_conditions_context_built(self):
        self.assertIn('_build_conditions_context', self.src,
                      'conditions context builder not found in ai_summary.py')

    def test_conditions_history_built(self):
        self.assertIn('_build_conditions_history_context', self.src,
                      'conditions history builder not found in ai_summary.py')

    def test_conditions_context_in_prompt(self):
        self.assertIn('{conditions_context}', self.src,
                      'conditions_context not interpolated in user_prompt in ai_summary.py')

    def test_conditions_history_in_prompt(self):
        self.assertIn('{conditions_history}', self.src,
                      'conditions_history not interpolated in user_prompt in ai_summary.py')

    def test_quadrant_naming_instruction(self):
        self.assertIn('Use only the four quadrants', self.src,
                      'Quadrant instruction not found in ai_summary.py')

    def test_week_in_review_structure(self):
        # US-490.2: the three daily paragraphs became the week-in-review contract
        self.assertIn('weekly_review.output_format_instructions(weekly_review.GENERAL)', self.src,
                      'Week-in-review output contract not found in ai_summary.py')

    def test_old_regime_prefix_removed(self):
        self.assertNotIn('regime_prefix_briefing', self.src,
                         'Old regime_prefix_briefing should be removed from ai_summary.py')


class TestNoTemplateChanges(unittest.TestCase):
    """HTML containers must NOT be modified — regime reference in generated text only."""

    @classmethod
    def setUpClass(cls):
        cls.index_src = read_source('templates/index.html')

    def test_briefing_narrative_container_unchanged(self):
        # The div#briefing-narrative must still exist
        self.assertIn('id="briefing-narrative"', self.index_src,
                      '#briefing-narrative container missing from index.html')


# =============================================================================
# Functional tests — generate_weekly_summary() with mocking
# =============================================================================

class TestGenerateDailySummaryConditionsContext(unittest.TestCase):
    """
    Functional tests for conditions context injection in generate_weekly_summary().
    Mocks get_market_conditions() and call_ai_with_tools() to verify prompt construction.
    """

    MOCK_CONDITIONS = {
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

    MOCK_HISTORY = {
        '2026-02-18': {
            'quadrant': 'Reflation',
            'dimensions': {
                'liquidity': {'state': 'Neutral', 'score': 0.1},
                'risk': {'state': 'Normal'},
                'policy': {'stance': 'Neutral', 'direction': 'Paused'},
            },
        },
        '2026-03-18': {
            'quadrant': 'Goldilocks',
            'dimensions': {
                'liquidity': {'state': 'Expanding', 'score': 0.82},
                'risk': {'state': 'Calm'},
                'policy': {'stance': 'Neutral', 'direction': 'Easing'},
            },
        },
    }

    def _call_with_mocked_conditions(self, conditions, history=None):
        """Helper: call generate_weekly_summary() with mocked conditions and capture prompt."""
        import ai_summary as ai_mod

        captured = {}

        def mock_call_ai(client, system_prompt, user_prompt, **kwargs):
            captured['user_prompt'] = user_prompt
            captured['system_prompt'] = system_prompt
            return {'success': True, 'content': 'Mock briefing text.'}

        with patch.object(ai_mod, 'get_market_conditions', return_value=conditions), \
             patch.object(ai_mod, 'get_conditions_history', return_value=history or {}), \
             patch.object(ai_mod, 'call_ai_with_tools', side_effect=mock_call_ai), \
             patch.object(ai_mod, 'get_ai_client', return_value=(MagicMock(), 'openai')), \
             patch.object(ai_mod, 'get_recent_summaries', return_value=[]), \
             patch.object(ai_mod, '_get_stored_news_context', return_value=None), \
             patch.object(ai_mod, 'get_latest_crypto_summary', return_value=None), \
             patch.object(ai_mod, 'get_latest_equity_summary', return_value=None), \
             patch.object(ai_mod, 'get_latest_rates_summary', return_value=None), \
             patch.object(ai_mod, 'get_latest_dollar_summary', return_value=None), \
             patch.object(ai_mod, 'get_latest_credit_summary', return_value=None), \
             patch.object(ai_mod, 'save_summary', return_value=None):
            result = ai_mod.generate_weekly_summary('## MARKET DATA\nTest data.', [])

        return result, captured

    def test_conditions_context_in_prompt(self):
        result, captured = self._call_with_mocked_conditions(self.MOCK_CONDITIONS)
        self.assertTrue(result['success'])
        self.assertIn('CURRENT MARKET CONDITIONS', captured['user_prompt'])
        self.assertIn('Goldilocks', captured['user_prompt'])

    def test_conditions_dimensions_in_prompt(self):
        result, captured = self._call_with_mocked_conditions(self.MOCK_CONDITIONS)
        prompt = captured['user_prompt']
        self.assertIn('Expanding', prompt)
        self.assertIn('Calm', prompt)
        self.assertIn('Easing', prompt)

    def test_conditions_history_in_prompt(self):
        result, captured = self._call_with_mocked_conditions(self.MOCK_CONDITIONS, self.MOCK_HISTORY)
        self.assertIn('CONDITIONS HISTORY', captured['user_prompt'])

    def test_none_conditions_graceful(self):
        result, captured = self._call_with_mocked_conditions(None)
        self.assertTrue(result['success'])
        # Should not crash, just have empty conditions context
        self.assertNotIn('CURRENT MARKET CONDITIONS', captured['user_prompt'])

    def test_no_old_regime_prefix_in_prompt(self):
        result, captured = self._call_with_mocked_conditions(self.MOCK_CONDITIONS)
        self.assertNotIn('Open your briefing by naming the current macro regime',
                         captured['user_prompt'])

    def test_week_in_review_instruction(self):
        result, captured = self._call_with_mocked_conditions(self.MOCK_CONDITIONS)
        self.assertIn('## What to watch next week', captured['system_prompt'])

    def test_market_data_preserved(self):
        result, captured = self._call_with_mocked_conditions(self.MOCK_CONDITIONS)
        self.assertIn('## MARKET DATA', captured['user_prompt'])

    def test_week_dates_in_prompt(self):
        result, captured = self._call_with_mocked_conditions(self.MOCK_CONDITIONS)
        self.assertIn('The week under review runs', captured['user_prompt'])



if __name__ == '__main__':
    unittest.main()
