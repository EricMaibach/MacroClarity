"""
Static verification tests for Bug #257: Credit briefing excluded from daily synthesis
and portfolio context.

These tests verify that get_latest_credit_summary() is wired into both
generate_weekly_summary() (ai_summary.py) and generate_portfolio_market_context()
(dashboard.py), following the identical pattern used for crypto/equity/rates/dollar.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNALTRACKERS_DIR = os.path.join(REPO_ROOT, 'signaltrackers')
sys.path.insert(0, SIGNALTRACKERS_DIR)


def read_source(filename):
    path = os.path.join(SIGNALTRACKERS_DIR, filename)
    with open(path, 'r') as f:
        return f.read()


# ---------------------------------------------------------------------------
# TestCreditStepInDailySummary — ai_summary.py generate_weekly_summary()
# ---------------------------------------------------------------------------


class TestCreditStepInDailySummary(unittest.TestCase):
    """get_latest_credit_summary() must be called inside generate_weekly_summary()."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def _get_daily_summary_section(self):
        """Extract the generate_daily_summary function body for scoped assertions."""
        start = self.src.find('def generate_weekly_summary(')
        self.assertGreater(start, 0, 'generate_weekly_summary() not found in ai_summary.py')
        # Take a generous window — function is ~200 lines
        return self.src[start:start + 6000]

    def test_get_latest_credit_summary_called(self):
        section = self._get_daily_summary_section()
        self.assertIn('get_latest_credit_summary()', section,
                      'get_latest_credit_summary() must be called inside generate_weekly_summary()')

    def test_credit_summary_date_guard(self):
        section = self._get_daily_summary_section()
        # US-490.2: every section review, credit included, passes the same-week guard
        self.assertIn('get_latest_credit_summary()', section)
        self.assertIn('if _same_week(record, week_start):', section,
                      'section reviews must be guarded to the week under review')

    def test_credit_briefing_label_appended(self):
        section = self._get_daily_summary_section()
        self.assertIn('("Credit Markets"', section,
                      '"("Credit Markets"" label must be appended to briefings_found')

    def test_credit_block_after_dollar_block(self):
        section = self._get_daily_summary_section()
        dollar_pos = section.find('get_latest_dollar_summary()')
        credit_pos = section.find('get_latest_credit_summary()')
        self.assertGreater(dollar_pos, 0, 'dollar_summary block not found in generate_weekly_summary()')
        self.assertGreater(credit_pos, 0, 'credit_summary block not found in generate_weekly_summary()')
        self.assertGreater(credit_pos, dollar_pos,
                           'Credit block must appear AFTER dollar block in generate_weekly_summary()')

    def test_existing_crypto_branch_intact(self):
        section = self._get_daily_summary_section()
        self.assertIn('get_latest_crypto_summary()', section)
        self.assertIn('("Crypto/Bitcoin"', section)

    def test_existing_equity_branch_intact(self):
        section = self._get_daily_summary_section()
        self.assertIn('get_latest_equity_summary()', section)
        self.assertIn('("Equity Markets"', section)

    def test_existing_rates_branch_intact(self):
        section = self._get_daily_summary_section()
        self.assertIn('get_latest_rates_summary()', section)
        self.assertIn('("Rates & Yield Curve"', section)

    def test_existing_dollar_branch_intact(self):
        section = self._get_daily_summary_section()
        self.assertIn('get_latest_dollar_summary()', section)
        self.assertIn('("Dollar & Currency"', section)


# ---------------------------------------------------------------------------
# TestCreditStepInPortfolioContext — dashboard.py generate_portfolio_market_context()
# ---------------------------------------------------------------------------


class TestCreditStepInPortfolioContext(unittest.TestCase):
    """get_latest_credit_summary() must be imported and called in generate_portfolio_market_context()."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def _get_portfolio_context_section(self):
        """Extract generate_portfolio_market_context function body for scoped assertions."""
        start = self.src.find('def generate_portfolio_market_context(')
        self.assertGreater(start, 0, 'generate_portfolio_market_context() not found in dashboard.py')
        return self.src[start:start + 4000]

    def test_get_latest_credit_summary_imported(self):
        section = self._get_portfolio_context_section()
        self.assertIn('get_latest_credit_summary', section,
                      'get_latest_credit_summary must appear in the from ai_summary import block')

    def test_get_latest_credit_summary_called(self):
        section = self._get_portfolio_context_section()
        self.assertIn('get_latest_credit_summary()', section,
                      'get_latest_credit_summary() must be called inside generate_portfolio_market_context()')

    def test_credit_if_guard_present(self):
        section = self._get_portfolio_context_section()
        # Find credit call and check surrounding code for if credit: guard
        credit_pos = section.find('get_latest_credit_summary()')
        self.assertGreater(credit_pos, 0)
        ctx = section[credit_pos:credit_pos + 300]
        self.assertIn('if credit', ctx,
                      'if credit: guard must follow get_latest_credit_summary() call')

    def test_credit_briefing_appended_to_briefings(self):
        section = self._get_portfolio_context_section()
        self.assertIn('Credit Briefing', section,
                      '"Credit Briefing" entry must be appended to briefings list')

    def test_credit_block_after_dollar_block(self):
        section = self._get_portfolio_context_section()
        dollar_pos = section.find('get_latest_dollar_summary()')
        credit_pos = section.find('get_latest_credit_summary()')
        self.assertGreater(dollar_pos, 0, 'get_latest_dollar_summary() call not found')
        self.assertGreater(credit_pos, 0, 'get_latest_credit_summary() call not found')
        self.assertGreater(credit_pos, dollar_pos,
                           'Credit block must appear AFTER dollar block in generate_portfolio_market_context()')

    def test_existing_crypto_branch_intact(self):
        section = self._get_portfolio_context_section()
        self.assertIn('get_latest_crypto_summary()', section)
        self.assertIn('Crypto Briefing', section)

    def test_existing_equity_branch_intact(self):
        section = self._get_portfolio_context_section()
        self.assertIn('get_latest_equity_summary()', section)
        self.assertIn('Equity Briefing', section)

    def test_existing_rates_branch_intact(self):
        section = self._get_portfolio_context_section()
        self.assertIn('get_latest_rates_summary()', section)
        self.assertIn('Rates Briefing', section)

    def test_existing_dollar_branch_intact(self):
        section = self._get_portfolio_context_section()
        self.assertIn('get_latest_dollar_summary()', section)
        self.assertIn('Dollar Briefing', section)

    def test_existing_general_branch_intact(self):
        section = self._get_portfolio_context_section()
        self.assertIn('get_latest_summary()', section)
        self.assertIn('General Market Briefing', section)


# ---------------------------------------------------------------------------
# TestExistingStepsUnmodified — regression: no logic changes elsewhere
# ---------------------------------------------------------------------------


class TestExistingStepsUnmodified(unittest.TestCase):
    """Verify no logic was accidentally changed in either file."""

    @classmethod
    def setUpClass(cls):
        cls.ai_src = read_source('ai_summary.py')
        cls.dash_src = read_source('dashboard.py')

    def test_get_latest_credit_summary_definition_exists(self):
        self.assertIn('def get_latest_credit_summary(', self.ai_src,
                      'get_latest_credit_summary() definition must not have been removed')

    def test_generate_credit_summary_definition_exists(self):
        self.assertIn('def generate_credit_summary(', self.ai_src,
                      'generate_credit_summary() definition must not have been removed')

    def test_generate_credit_market_summary_definition_exists(self):
        self.assertIn('def generate_credit_market_summary(', self.dash_src,
                      'generate_credit_market_summary() definition must not have been removed')

    def test_ai_summary_dollar_branch_unmodified(self):
        # Dollar block is immediately before credit; ensure it was not accidentally deleted
        self.assertIn('get_latest_dollar_summary()', self.ai_src)
        self.assertIn('("Dollar & Currency"', self.ai_src)

    def test_dashboard_dollar_branch_unmodified(self):
        self.assertIn('get_latest_dollar_summary()', self.dash_src)
        self.assertIn('Dollar Briefing', self.dash_src)

    def test_no_logic_change_to_generate_daily_summary_structure(self):
        # briefings_found list and specific_briefings_context must still exist
        self.assertIn('briefings_found', self.ai_src)
        self.assertIn('specific_briefings_context', self.ai_src)

    def test_no_logic_change_to_portfolio_context_structure(self):
        # briefings list and context_parts must still exist
        self.assertIn('context_parts', self.dash_src)
        # generate_portfolio_market_context must still have briefings list
        start = self.dash_src.find('def generate_portfolio_market_context(')
        section = self.dash_src[start:start + 4000]
        self.assertIn('briefings = []', section)
        self.assertIn('context_parts.append', section)


# ---------------------------------------------------------------------------
# TestEdgeCases — None guard coverage
# ---------------------------------------------------------------------------


class TestEdgeCases(unittest.TestCase):
    """Verify None-safe guards exist so missing credit summary does not crash."""

    @classmethod
    def setUpClass(cls):
        cls.ai_src = read_source('ai_summary.py')
        cls.dash_src = read_source('dashboard.py')

    def test_ai_summary_credit_guard_is_none_safe(self):
        # US-490.2: _same_week() returns False for a missing (None) review
        start = self.ai_src.find('def _same_week(')
        self.assertIn('if not record:\n        return False', self.ai_src[start:start + 400],
                      'a missing section review must be None-checked before use')

    def test_portfolio_context_credit_guard_is_none_safe(self):
        start = self.dash_src.find('def generate_portfolio_market_context(')
        section = self.dash_src[start:start + 4000]
        # Pattern: `if credit:` — truthy check handles None return
        self.assertIn('if credit:', section,
                      'if credit: guard must be present to handle None return safely')


if __name__ == '__main__':
    unittest.main()
