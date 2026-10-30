"""
Static verification tests for US-169.3: Credit Page — AI Briefing Integration.

Tests verify:
- generate_credit_summary() function exists in ai_summary.py with correct contract
- /api/credit-summary GET and /api/credit-summary/generate POST endpoints exist
- Credit step is present in run_weekly_ai_review() after Dollar step, before Daily Summary
- generate_market_summary() already contains credit spread data
- credit.html includes an AJAX-fetched AI analysis collapsible section
"""

import os
import sys
import ast
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNALTRACKERS_DIR = os.path.join(REPO_ROOT, 'signaltrackers')
TEMPLATES_DIR = os.path.join(SIGNALTRACKERS_DIR, 'templates')
sys.path.insert(0, SIGNALTRACKERS_DIR)


def read_source(filename):
    path = os.path.join(SIGNALTRACKERS_DIR, filename)
    with open(path, 'r') as f:
        return f.read()


def read_template(filename):
    path = os.path.join(TEMPLATES_DIR, filename)
    with open(path, 'r') as f:
        return f.read()


# ---------------------------------------------------------------------------
# ai_summary.py — generate_credit_summary function
# ---------------------------------------------------------------------------


class TestCreditSummaryFunctionExists(unittest.TestCase):
    """generate_credit_summary() must exist in ai_summary.py."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_function_defined(self):
        self.assertIn('def generate_credit_summary(', self.src,
                      'generate_credit_summary() function not found in ai_summary.py')

    def test_function_accepts_string_arg(self):
        """Function signature must accept a data summary argument."""
        self.assertIn('def generate_credit_summary(credit_data_summary)', self.src,
                      'generate_credit_summary() must accept credit_data_summary parameter')

    def test_returns_success_key(self):
        idx = self.src.find('def generate_credit_summary(')
        ctx = self.src[idx:idx + 5000]
        self.assertIn("'success'", ctx,
                      "generate_credit_summary() must return dict with 'success' key")

    def test_returns_summary_key(self):
        idx = self.src.find('def generate_credit_summary(')
        ctx = self.src[idx:idx + 5000]
        self.assertIn("'summary'", ctx,
                      "generate_credit_summary() must return dict with 'summary' key")

    def test_returns_error_key(self):
        idx = self.src.find('def generate_credit_summary(')
        ctx = self.src[idx:idx + 5000]
        self.assertIn("'error'", ctx,
                      "generate_credit_summary() must return dict with 'error' key")

    def test_calls_get_ai_client(self):
        idx = self.src.find('def generate_credit_summary(')
        ctx = self.src[idx:idx + 5000]
        self.assertIn('get_ai_client()', ctx,
                      'generate_credit_summary() must call get_ai_client()')

    def test_handles_client_unavailable(self):
        """When client is None, returns success=False gracefully."""
        idx = self.src.find('def generate_credit_summary(')
        ctx = self.src[idx:idx + 500]
        self.assertIn('if client is None', ctx,
                      'generate_credit_summary() must handle missing AI client gracefully')

    def test_has_exception_handler(self):
        idx = self.src.find('def generate_credit_summary(')
        ctx = self.src[idx:idx + 5000]
        self.assertIn('except Exception', ctx,
                      'generate_credit_summary() must have a top-level exception handler')

    def test_calls_save_credit_summary(self):
        idx = self.src.find('def generate_credit_summary(')
        ctx = self.src[idx:idx + 5000]
        self.assertIn('save_credit_summary(', ctx,
                      'generate_credit_summary() must call save_credit_summary() on success')


# ---------------------------------------------------------------------------
# ai_summary.py — storage helpers
# ---------------------------------------------------------------------------


class TestCreditStorageHelpers(unittest.TestCase):
    """Credit storage helpers must exist in ai_summary.py."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('ai_summary.py')

    def test_credit_summaries_file_constant(self):
        self.assertIn('CREDIT_SUMMARIES_FILE', self.src,
                      'CREDIT_SUMMARIES_FILE constant missing from ai_summary.py')

    def test_load_credit_summaries_exists(self):
        self.assertIn('def load_credit_summaries()', self.src)

    def test_save_credit_summary_exists(self):
        self.assertIn('def save_credit_summary(', self.src)

    def test_get_latest_credit_summary_exists(self):
        self.assertIn('def get_latest_credit_summary()', self.src)

    def test_get_credit_summary_for_display_exists(self):
        self.assertIn('def get_credit_summary_for_display()', self.src)

    def test_credit_summaries_json_filename(self):
        self.assertIn('credit_summaries.json', self.src,
                      'credit_summaries.json storage file reference missing')


# ---------------------------------------------------------------------------
# ai_summary.py — importability
# ---------------------------------------------------------------------------


class TestCreditSummaryImportable(unittest.TestCase):
    """generate_credit_summary must be importable without AI credentials."""

    def test_importable(self):
        try:
            from ai_summary import generate_credit_summary
        except ImportError as e:
            self.fail(f'generate_credit_summary not importable: {e}')

    def test_get_credit_summary_for_display_importable(self):
        try:
            from ai_summary import get_credit_summary_for_display
        except ImportError as e:
            self.fail(f'get_credit_summary_for_display not importable: {e}')


# ---------------------------------------------------------------------------
# dashboard.py — import of credit functions
# ---------------------------------------------------------------------------


class TestDashboardImportsCreditFunctions(unittest.TestCase):
    """dashboard.py must import credit summary functions from ai_summary."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_imports_generate_credit_summary(self):
        self.assertIn('generate_credit_summary', self.src,
                      'generate_credit_summary must be imported in dashboard.py')

    def test_imports_get_credit_summary_for_display(self):
        self.assertIn('get_credit_summary_for_display', self.src,
                      'get_credit_summary_for_display must be imported in dashboard.py')


# ---------------------------------------------------------------------------
# dashboard.py — API endpoints
# ---------------------------------------------------------------------------


class TestCreditSummaryEndpoints(unittest.TestCase):
    """Credit summary API endpoints must be defined in dashboard.py."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_get_endpoint_route_defined(self):
        self.assertIn("@app.route('/api/credit-summary')", self.src,
                      "GET /api/credit-summary route missing from dashboard.py")

    def test_get_endpoint_view_function(self):
        self.assertIn('def api_credit_summary()', self.src,
                      'api_credit_summary() view function missing')

    def test_get_endpoint_calls_get_credit_summary_for_display(self):
        idx = self.src.find('def api_credit_summary()')
        ctx = self.src[idx:idx + 400]
        self.assertIn('get_credit_summary_for_display()', ctx,
                      'api_credit_summary() must call get_credit_summary_for_display()')

    def test_get_endpoint_returns_404_when_no_summary(self):
        idx = self.src.find('def api_credit_summary()')
        ctx = self.src[idx:idx + 400]
        self.assertIn('404', ctx,
                      'GET /api/credit-summary must return 404 when no summary available')

    def test_post_endpoint_route_defined(self):
        self.assertIn("@app.route('/api/credit-summary/generate', methods=['POST'])", self.src,
                      "POST /api/credit-summary/generate route missing")

    def test_post_endpoint_view_function(self):
        self.assertIn('def api_generate_credit_summary()', self.src,
                      'api_generate_credit_summary() view function missing')

    def test_post_endpoint_calls_generate_credit_market_summary(self):
        idx = self.src.find('def api_generate_credit_summary()')
        ctx = self.src[idx:idx + 400]
        self.assertIn('generate_credit_market_summary()', ctx,
                      'POST endpoint must call generate_credit_market_summary()')

    def test_post_endpoint_calls_generate_credit_summary(self):
        idx = self.src.find('def api_generate_credit_summary()')
        ctx = self.src[idx:idx + 400]
        self.assertIn('generate_credit_summary(', ctx,
                      'POST endpoint must call generate_credit_summary()')

    def test_post_endpoint_returns_success_on_success(self):
        idx = self.src.find('def api_generate_credit_summary()')
        ctx = self.src[idx:idx + 600]
        self.assertIn("'status': 'success'", ctx,
                      "POST endpoint must return {'status': 'success'} on success")

    def test_post_endpoint_returns_error_on_failure(self):
        idx = self.src.find('def api_generate_credit_summary()')
        ctx = self.src[idx:idx + 600]
        self.assertIn("'status': 'error'", ctx,
                      "POST endpoint must return {'status': 'error'} on failure")

    def test_post_endpoint_returns_500_on_failure(self):
        idx = self.src.find('def api_generate_credit_summary()')
        ctx = self.src[idx:idx + 600]
        self.assertIn('500', ctx,
                      'POST endpoint must return HTTP 500 on failure')

    def test_post_endpoint_has_exception_handler(self):
        idx = self.src.find('def api_generate_credit_summary()')
        ctx = self.src[idx:idx + 600]
        self.assertIn('except Exception', ctx,
                      'POST endpoint must have top-level exception handler')

    def test_post_method_not_get(self):
        """Endpoint must be POST-only (not triggerable by browser navigation)."""
        route_line = "@app.route('/api/credit-summary/generate', methods=['POST'])"
        self.assertIn(route_line, self.src,
                      'credit-summary/generate must be POST-only')


# ---------------------------------------------------------------------------
# dashboard.py — generate_credit_market_summary function
# ---------------------------------------------------------------------------


class TestGenerateCreditMarketSummary(unittest.TestCase):
    """generate_credit_market_summary() must exist in dashboard.py."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_function_defined(self):
        self.assertIn('def generate_credit_market_summary()', self.src,
                      'generate_credit_market_summary() missing from dashboard.py')

    def test_loads_high_yield_csv(self):
        idx = self.src.find('def generate_credit_market_summary()')
        ctx = self.src[idx:idx + 3000]
        self.assertIn('high_yield_spread.csv', ctx,
                      'generate_credit_market_summary() must load high_yield_spread.csv')

    def test_loads_investment_grade_csv(self):
        idx = self.src.find('def generate_credit_market_summary()')
        ctx = self.src[idx:idx + 3000]
        self.assertIn('investment_grade_spread.csv', ctx,
                      'generate_credit_market_summary() must load investment_grade_spread.csv')

    def test_loads_ccc_spread_csv(self):
        idx = self.src.find('def generate_credit_market_summary()')
        ctx = self.src[idx:idx + 3000]
        self.assertIn('ccc_spread.csv', ctx,
                      'generate_credit_market_summary() must load ccc_spread.csv')

    def test_has_exception_handler(self):
        idx = self.src.find('def generate_credit_market_summary()')
        ctx = self.src[idx:idx + 10000]
        self.assertIn('except Exception', ctx)

    def test_returns_string(self):
        """Function must return a string (joined summary_parts)."""
        idx = self.src.find('def generate_credit_market_summary()')
        ctx = self.src[idx:idx + 10000]
        self.assertIn("return \"\\n\".join(summary_parts)", ctx,
                      'generate_credit_market_summary() must return joined string')


# ---------------------------------------------------------------------------
# dashboard.py — credit step in run_weekly_ai_review()
# ---------------------------------------------------------------------------


class TestCreditStepInPipeline(unittest.TestCase):
    """Credit briefing step must exist in run_weekly_ai_review()."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_credit_step_status_message_present(self):
        self.assertIn("'Generating Credit AI summary...'", self.src,
                      "reload_status credit step status message missing")

    def test_credit_step_print_log_present(self):
        self.assertIn('Generating Credit AI summary...', self.src,
                      'Credit step print log message missing')

    def test_generate_credit_market_summary_called_in_pipeline(self):
        # Must appear multiple times: pipeline + API route
        count = self.src.count('generate_credit_market_summary()')
        self.assertGreaterEqual(count, 2,
                                'generate_credit_market_summary() should appear in both pipeline and API')

    def test_generate_credit_summary_called_in_pipeline(self):
        count = self.src.count('generate_credit_summary(')
        self.assertGreaterEqual(count, 2,
                                'generate_credit_summary() should appear in pipeline and API')

    def test_credit_error_log_non_fatal(self):
        self.assertIn('Credit AI summary error (non-fatal)', self.src,
                      'Non-fatal error log message missing for credit step')


class TestCreditStepOrdering(unittest.TestCase):
    """Credit step must appear after Dollar step and before Daily Summary step."""

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

    def test_credit_step_after_dollar_step(self):
        dollar_pos = self._find_pos('Generating Dollar AI summary...')
        credit_pos = self._find_pos('Generating Credit AI summary...')
        self.assertGreater(credit_pos, dollar_pos,
                           'Credit step must appear AFTER Dollar step in run_weekly_ai_review()')

    def test_credit_step_before_daily_summary(self):
        credit_pos = self._find_pos('Generating Credit AI summary...')
        daily_pos = self._find_pos('Generating AI daily summary...')
        self.assertGreater(daily_pos, credit_pos,
                           'Credit step must appear BEFORE Daily Summary step')

    def test_credit_step_after_rates_step(self):
        rates_pos = self._find_pos('Generating Rates AI summary...')
        credit_pos = self._find_pos('Generating Credit AI summary...')
        self.assertGreater(credit_pos, rates_pos,
                           'Credit step must appear AFTER Rates step')


class TestCreditStepNonFatal(unittest.TestCase):
    """Credit step must be wrapped in try/except so failures do not block daily summary."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_credit_step_has_try_except(self):
        credit_pos = self.src.find('Generating Credit AI summary...')
        self.assertGreater(credit_pos, 0)
        ctx = self.src[max(0, credit_pos - 100):credit_pos + 600]
        self.assertIn('try:', ctx, 'Credit step must be inside a try block')
        self.assertIn('except', ctx, 'Credit step must be wrapped in try/except')

    def test_daily_summary_step_follows_credit_except(self):
        credit_except_pos = self.src.find('Credit AI summary error (non-fatal)')
        daily_status_pos = self.src.find('Generating AI daily summary...')
        self.assertGreater(daily_status_pos, credit_except_pos,
                           'Daily summary step must follow the credit except block')


# ---------------------------------------------------------------------------
# dashboard.py — generate_market_summary() already has credit spreads
# ---------------------------------------------------------------------------


class TestMarketSummaryHasCreditData(unittest.TestCase):
    """generate_market_summary() must include credit spread context."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def _get_market_summary_ctx(self):
        idx = self.src.find('def generate_market_summary()')
        self.assertGreater(idx, 0, 'generate_market_summary() not found')
        return self.src[idx:idx + 4000]

    def test_high_yield_csv_loaded(self):
        ctx = self._get_market_summary_ctx()
        self.assertIn('high_yield_spread.csv', ctx,
                      'generate_market_summary() must load high_yield_spread.csv')

    def test_investment_grade_csv_loaded(self):
        ctx = self._get_market_summary_ctx()
        self.assertIn('investment_grade_spread.csv', ctx,
                      'generate_market_summary() must load investment_grade_spread.csv')

    def test_credit_section_header_present(self):
        ctx = self._get_market_summary_ctx()
        self.assertIn('CREDIT SPREADS', ctx,
                      'generate_market_summary() must have a ## CREDIT SPREADS section')

    def test_uses_load_csv_data(self):
        ctx = self._get_market_summary_ctx()
        self.assertIn("load_csv_data('high_yield_spread.csv')", ctx,
                      'generate_market_summary() must use load_csv_data() for credit CSVs')


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------


class TestSecurityRequirements(unittest.TestCase):
    """Security constraints for credit AI endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_generate_endpoint_is_post_only(self):
        self.assertIn("methods=['POST']", self.src)
        self.assertNotIn("@app.route('/api/credit-summary/generate')\n", self.src,
                         'credit-summary/generate must not be a GET route (no methods= means GET-only)')

    def test_no_user_input_in_credit_prompt(self):
        idx = self.src.find('def generate_credit_market_summary()')
        ctx = self.src[idx:idx + 3000]
        self.assertNotIn('request.', ctx,
                         'generate_credit_market_summary() must not use request data (user input)')


# ---------------------------------------------------------------------------
# Template — credit.html AI section
# ---------------------------------------------------------------------------


class TestCreditTemplateAISection(unittest.TestCase):
    """credit.html must include an AI analysis collapsible section."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_template('credit.html')

    def test_ai_analysis_section_present(self):
        self.assertIn('AI Credit Analysis', self.src,
                      'credit.html must have an "AI Credit Analysis" section heading')

    def test_collapsible_section_for_ai(self):
        self.assertIn('data-section-id="ai-briefing"', self.src,
                      'AI briefing collapsible section must have data-section-id="ai-briefing"')

    def test_credit_summary_content_div(self):
        self.assertIn('id="credit-summary-content"', self.src,
                      'credit-summary-content div missing from credit.html')

    def test_credit_summary_error_div(self):
        self.assertIn('id="credit-summary-error"', self.src,
                      'credit-summary-error div missing from credit.html')

    def test_credit_summary_timestamp_div(self):
        self.assertIn('id="credit-summary-timestamp"', self.src,
                      'credit-summary-timestamp div missing from credit.html')

    def test_ajax_fetch_targets_credit_summary_endpoint(self):
        self.assertIn('/api/credit-summary', self.src,
                      'credit.html must AJAX-fetch from /api/credit-summary')

    def test_load_credit_summary_function(self):
        self.assertIn('loadCreditSummary()', self.src,
                      'loadCreditSummary() JS function must be present and called')

    def test_load_credit_summary_called_on_domcontentloaded(self):
        idx = self.src.find('DOMContentLoaded')
        self.assertGreater(idx, 0, 'DOMContentLoaded handler missing')
        ctx = self.src[idx:idx + 300]
        self.assertIn('loadCreditSummary()', ctx,
                      'loadCreditSummary() must be called in DOMContentLoaded handler')

    def test_ai_section_uses_collapsible_class(self):
        # Find the AI briefing section and check it uses collapsible-section
        idx = self.src.find('data-section-id="ai-briefing"')
        self.assertGreater(idx, 0)
        ctx = self.src[max(0, idx - 200):idx + 50]
        self.assertIn('collapsible-section', ctx,
                      'AI briefing section must use collapsible-section CSS class')

    def test_ai_section_collapsed_by_default(self):
        idx = self.src.find('data-section-id="ai-briefing"')
        ctx = self.src[max(0, idx - 200):idx + 50]
        self.assertIn('data-collapsed="true"', ctx,
                      'AI briefing section must be collapsed by default')


# ---------------------------------------------------------------------------
# Regression — existing pipeline steps unmodified
# ---------------------------------------------------------------------------


class TestExistingPipelineStepsUnmodified(unittest.TestCase):
    """Existing crypto, equity, rates, dollar briefing steps must still be present."""

    @classmethod
    def setUpClass(cls):
        cls.src = read_source('dashboard.py')

    def test_crypto_step_present(self):
        self.assertIn('Generating Crypto AI summary...', self.src)
        self.assertIn('generate_crypto_market_summary()', self.src)

    def test_equity_step_present(self):
        self.assertIn('Generating Equity Markets AI summary...', self.src)
        self.assertIn('generate_equity_market_summary()', self.src)

    def test_rates_step_present(self):
        self.assertIn('Generating Rates AI summary...', self.src)
        self.assertIn('generate_rates_market_summary()', self.src)

    def test_dollar_step_present(self):
        self.assertIn('Generating Dollar AI summary...', self.src)
        self.assertIn('generate_dollar_market_summary()', self.src)

    def test_daily_summary_step_present(self):
        self.assertIn('Generating AI daily summary...', self.src)
        self.assertIn('generate_daily_summary(', self.src)


if __name__ == '__main__':
    unittest.main()
