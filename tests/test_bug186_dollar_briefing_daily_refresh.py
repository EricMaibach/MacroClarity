"""
Static verification tests for Bug #186: Dollar briefing missing from automated daily refresh.

These tests verify that run_weekly_ai_review() in dashboard.py generates the Dollar &
Currency AI briefing in the correct position (after rates, before daily summary) and
that the consumer code in ai_summary.py and dashboard.py remains intact.
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
# Structural tests — dollar step present in run_weekly_ai_review()
# ---------------------------------------------------------------------------


class TestDollarStepPresent(unittest.TestCase):
    """Dollar briefing step must exist inside run_weekly_ai_review()."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_generate_dollar_market_summary_called(self):
        self.assertIn('generate_dollar_market_summary()', self.src,
                      'generate_dollar_market_summary() call missing from dashboard.py')

    def test_generate_dollar_summary_called_in_pipeline(self):
        # Must appear at least twice: once in pipeline, once in api_generate_dollar_summary
        count = self.src.count('generate_dollar_summary(')
        self.assertGreaterEqual(count, 2,
                                'generate_dollar_summary() should appear in both pipeline and API route')

    def test_dollar_step_status_message_present(self):
        self.assertIn("'Generating Dollar AI summary...'", self.src,
                      "reload_status['status'] = 'Generating Dollar AI summary...' missing")

    def test_dollar_step_print_log_present(self):
        self.assertIn('Generating Dollar & Currency AI summary', self.src,
                      'Dollar step print log message missing from dashboard.py')


# ---------------------------------------------------------------------------
# Ordering tests — dollar must run after rates, before daily summary
# ---------------------------------------------------------------------------


class TestDollarStepOrdering(unittest.TestCase):
    """Dollar briefing step must run after rates step and before daily summary step."""

    @classmethod
    def setUpClass(cls):
        # Scheduled briefings run in the weekly review since US-490.1.
        src = read_source('dashboard.py')
        start = src.index('def run_weekly_ai_review(')
        cls.src = src[start:src.index('\ndef ', start + 1)]

    def _find_pos(self, needle):
        pos = self.src.find(needle)
        self.assertGreater(pos, 0, f'Expected to find: {needle!r}')
        return pos

    def test_dollar_step_after_rates_step(self):
        rates_pos = self._find_pos('Generating Rates AI summary...')
        dollar_pos = self._find_pos('Generating Dollar AI summary...')
        self.assertGreater(dollar_pos, rates_pos,
                           'Dollar step must appear AFTER Rates step in run_weekly_ai_review()')

    def test_dollar_step_before_daily_summary_step(self):
        dollar_pos = self._find_pos('Generating Dollar AI summary...')
        daily_pos = self._find_pos('Generating the week in review...')
        self.assertGreater(daily_pos, dollar_pos,
                           'Dollar step must appear BEFORE Daily Summary step in run_weekly_ai_review()')

    def test_dollar_step_after_equity_step(self):
        equity_pos = self._find_pos('Generating Equity Markets AI summary...')
        dollar_pos = self._find_pos('Generating Dollar AI summary...')
        self.assertGreater(dollar_pos, equity_pos,
                           'Dollar step must appear AFTER Equity step')

    def test_dollar_step_after_crypto_step(self):
        crypto_pos = self._find_pos('Generating Crypto AI summary...')
        dollar_pos = self._find_pos('Generating Dollar AI summary...')
        self.assertGreater(dollar_pos, crypto_pos,
                           'Dollar step must appear AFTER Crypto step')


# ---------------------------------------------------------------------------
# Non-fatal pattern — dollar step must be wrapped in try/except
# ---------------------------------------------------------------------------


class TestDollarStepNonFatal(unittest.TestCase):
    """Dollar step must be wrapped in try/except so failures do not block daily summary."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_dollar_step_has_try_except(self):
        # Locate the dollar step and inspect surrounding ~300 chars for try/except
        dollar_pos = self.src.find('Generating Dollar AI summary...')
        self.assertGreater(dollar_pos, 0)
        ctx = self.src[max(0, dollar_pos - 100):dollar_pos + 600]
        self.assertIn('try:', ctx,
                      'Dollar step must be inside a try block (non-fatal error handling)')
        self.assertIn('except', ctx,
                      'Dollar step must be wrapped in try/except')

    def test_dollar_error_log_message_present(self):
        self.assertIn('Dollar AI summary error (non-fatal)', self.src,
                      'Non-fatal error log message missing for dollar step')

    def test_daily_summary_step_follows_dollar_except(self):
        # After the except clause for the dollar step, the daily summary step must follow
        dollar_except_pos = self.src.find('Dollar AI summary error (non-fatal)')
        daily_status_pos = self.src.find('Generating the week in review...')
        self.assertGreater(daily_status_pos, dollar_except_pos,
                           'Daily summary step must follow the dollar except block')


# ---------------------------------------------------------------------------
# Regression tests — existing steps must be unmodified
# ---------------------------------------------------------------------------


class TestExistingStepsUnmodified(unittest.TestCase):
    """Existing crypto, equity, rates briefing steps must be unmodified."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_crypto_step_present(self):
        self.assertIn('Generating Crypto AI summary...', self.src)
        self.assertIn('generate_crypto_market_summary()', self.src)
        self.assertIn('generate_crypto_summary(', self.src)

    def test_equity_step_present(self):
        self.assertIn('Generating Equity Markets AI summary...', self.src)
        self.assertIn('generate_equity_market_summary()', self.src)
        self.assertIn('generate_equity_summary(', self.src)

    def test_rates_step_present(self):
        self.assertIn('Generating Rates AI summary...', self.src)
        self.assertIn('generate_rates_market_summary()', self.src)
        self.assertIn('generate_rates_summary(', self.src)

    def test_daily_summary_step_present(self):
        self.assertIn('Generating the week in review...', self.src)
        self.assertIn('generate_weekly_summary(', self.src)

    def test_crypto_error_log_unchanged(self):
        self.assertIn('Crypto AI summary error (non-fatal)', self.src)

    def test_equity_error_log_unchanged(self):
        self.assertIn('Equity AI summary error (non-fatal)', self.src)

    def test_rates_error_log_unchanged(self):
        self.assertIn('Rates AI summary error (non-fatal)', self.src)


# ---------------------------------------------------------------------------
# Consumer code — ai_summary.py dollar branch must be intact
# ---------------------------------------------------------------------------


class TestDailySummaryDollarBranch(unittest.TestCase):
    """generate_weekly_summary() dollar branch must be intact in ai_summary.py."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_get_latest_dollar_summary_called(self):
        self.assertIn('get_latest_dollar_summary()', self.src,
                      'get_latest_dollar_summary() call missing from ai_summary.py')

    def test_dollar_date_guard_present(self):
        # US-490.2: section reviews are fed in only when they are from the same week
        self.assertIn('if _same_week(record, week_start):', self.src,
                      'Same-week guard on section reviews missing in ai_summary.py')

    def test_dollar_briefing_context_label(self):
        self.assertIn('("Dollar & Currency", get_latest_dollar_summary())', self.src,
                      'Dollar & Currency review label missing from ai_summary.py')

    def test_crypto_equity_rates_branches_intact(self):
        self.assertIn('("Crypto/Bitcoin"', self.src)
        self.assertIn('("Equity Markets"', self.src)
        self.assertIn('("Rates & Yield Curve"', self.src)


# ---------------------------------------------------------------------------
# Consumer code — generate_portfolio_market_context() dollar guard intact
# ---------------------------------------------------------------------------


class TestPortfolioContextDollarGuard(unittest.TestCase):
    """generate_portfolio_market_context() must retain its dollar guard."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_portfolio_context_calls_get_latest_dollar_summary(self):
        self.assertIn('get_latest_dollar_summary()', self.src,
                      'get_latest_dollar_summary() call missing from dashboard.py (portfolio context)')

    def test_portfolio_context_dollar_guard_present(self):
        # Check that the if dollar: guard exists near the get_latest_dollar_summary call
        pos = self.src.find('get_latest_dollar_summary()')
        self.assertGreater(pos, 0)
        # Look for 'if dollar:' within 300 chars after the call
        ctx = self.src[pos:pos + 300]
        self.assertIn('if dollar:', ctx,
                      "'if dollar:' guard must follow get_latest_dollar_summary() call")


# ---------------------------------------------------------------------------
# Import verification — generate_dollar_summary imported at top-level
# ---------------------------------------------------------------------------


class TestDollarSummaryImport(unittest.TestCase):
    """generate_dollar_summary must be imported from ai_summary at the top level."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_generate_dollar_summary_imported(self):
        # Check that generate_dollar_summary appears in the top-level import block
        import_block_end = self.src.find('\n\n\n')
        import_block = self.src[:import_block_end] if import_block_end > 0 else self.src[:3000]
        self.assertIn('generate_dollar_summary', import_block,
                      'generate_dollar_summary must be imported from ai_summary at top level')

    def test_generate_dollar_market_summary_defined(self):
        self.assertIn('def generate_dollar_market_summary()', self.src,
                      'generate_dollar_market_summary() function definition missing from dashboard.py')


if __name__ == '__main__':
    unittest.main()
