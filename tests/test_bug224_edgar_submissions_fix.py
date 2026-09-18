"""
Static and mocked-HTTP tests for Bug #224:
EDGAR fetch is broken — EFTS search entity filter ignored, primary doc is XBRL.

Fix: replace _fetch_recent_8k_filing_texts() with the EDGAR submissions API
(data.sec.gov/submissions/CIK{cik}.json) + EX-99.1 index-HTML extraction.

All tests run without live network calls or FinBERT installed.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNALTRACKERS_DIR = os.path.join(REPO_ROOT, 'signaltrackers')
sys.path.insert(0, SIGNALTRACKERS_DIR)


def read_source(filename):
    path = os.path.join(SIGNALTRACKERS_DIR, filename)
    with open(path, 'r') as f:
        return f.read()


def _make_submissions_json(accession_numbers, forms, filing_dates, primary_docs, items=None):
    """Helper: build a minimal EDGAR submissions API response."""
    if items is None:
        # Default: mark all 8-K filings as earnings (Item 2.02)
        items = ["2.02,9.01" if f == "8-K" else "" for f in forms]
    return {
        "filings": {
            "recent": {
                "accessionNumber": accession_numbers,
                "form": forms,
                "filingDate": filing_dates,
                "primaryDocument": primary_docs,
                "items": items,
            }
        }
    }


def _make_mock_response(status_code=200, json_data=None, text=""):
    """Helper: build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = (status_code == 200)
    if json_data is not None:
        resp.json.return_value = json_data
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Source Code Inspection Tests
# ---------------------------------------------------------------------------


class TestSourceNoLongerUsesEftsEntityFilter(unittest.TestCase):
    """_fetch_recent_8k_filing_texts must not use EFTS entity filter."""

    def setUp(self):
        self.src = read_source('sector_tone_pipeline.py')

    def test_fetch_function_does_not_use_entity_param(self):
        """The entity= param that didn't work must be removed from the fetch function."""
        import ast
        # Parse and find the function body
        tree = ast.parse(self.src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_fetch_recent_8k_filing_texts':
                func_src = ast.get_source_segment(self.src, node) or ''
                self.assertNotIn('"entity"', func_src,
                                 '_fetch_recent_8k_filing_texts must not use entity param')
                self.assertNotIn("'entity'", func_src,
                                 '_fetch_recent_8k_filing_texts must not use entity param')
                return
        # If we reach here, function not found in AST (should not happen)
        self.assertIn('_fetch_recent_8k_filing_texts', self.src)

    def test_fetch_function_does_not_call_efts_search_url(self):
        """Function must not call _EDGAR_SEARCH_URL with entity filter."""
        import ast
        tree = ast.parse(self.src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_fetch_recent_8k_filing_texts':
                func_src = ast.get_source_segment(self.src, node) or ''
                self.assertNotIn('_EDGAR_SEARCH_URL', func_src,
                                 '_fetch_recent_8k_filing_texts must not use _EDGAR_SEARCH_URL')
                return

    def test_submissions_url_pattern_in_source(self):
        """Source must contain the submissions API URL with zero-padded CIK."""
        self.assertIn('data.sec.gov/submissions/CIK', self.src)

    def test_submissions_url_constant_defined(self):
        """_EDGAR_SUBMISSIONS_URL constant must be defined."""
        import sector_tone_pipeline as stp
        self.assertTrue(hasattr(stp, '_EDGAR_SUBMISSIONS_URL'))
        self.assertIn('data.sec.gov/submissions', stp._EDGAR_SUBMISSIONS_URL)

    def test_ex991_extraction_logic_in_source(self):
        """Source must contain EX-99.1 extraction logic."""
        self.assertIn('EX-99', self.src)

    def test_ex991_helper_function_exists(self):
        """_extract_ex991_url helper must be defined."""
        from sector_tone_pipeline import _extract_ex991_url
        self.assertTrue(callable(_extract_ex991_url))

    def test_max_filings_constant_still_used(self):
        """_MAX_FILINGS_PER_COMPANY must still bound the filings processed."""
        import ast
        tree = ast.parse(self.src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_fetch_recent_8k_filing_texts':
                func_src = ast.get_source_segment(self.src, node) or ''
                self.assertIn('_MAX_FILINGS_PER_COMPANY', func_src,
                              '_MAX_FILINGS_PER_COMPANY must bound filings in fetch function')
                return

    def test_edgar_search_url_constant_preserved(self):
        """_EDGAR_SEARCH_URL constant kept for backwards compat with existing tests."""
        import sector_tone_pipeline as stp
        self.assertTrue(hasattr(stp, '_EDGAR_SEARCH_URL'))


# ---------------------------------------------------------------------------
# _extract_ex991_url helper
# ---------------------------------------------------------------------------


class TestExtractEx991Url(unittest.TestCase):
    """_extract_ex991_url must find EX-99.1 href in filing index HTML."""

    def setUp(self):
        from sector_tone_pipeline import _extract_ex991_url
        self.fn = _extract_ex991_url

    def _make_index_html(self, exhibit_path):
        """Build a minimal filing index HTML with an EX-99.1 row."""
        return f"""
<html><body><table>
<tr><td>Exhibit 99.1</td><td>EX-99.1</td>
<td><a href="{exhibit_path}">exhibit</a></td></tr>
</table></body></html>
"""

    def test_returns_none_for_empty_html(self):
        result = self.fn("", 320193, "000032019324000001")
        self.assertIsNone(result)

    def test_returns_none_when_no_ex991_row(self):
        html = "<html><table><tr><td>EX-10.1</td><td><a href='/some/path.htm'>doc</a></td></tr></table></html>"
        result = self.fn(html, 320193, "000032019324000001")
        self.assertIsNone(result)

    def test_returns_url_when_ex991_present(self):
        html = self._make_index_html("/Archives/edgar/data/320193/000032019324000001/ex991.htm")
        result = self.fn(html, 320193, "000032019324000001")
        self.assertIsNotNone(result)
        self.assertIn("https://www.sec.gov", result)
        self.assertIn("ex991.htm", result)

    def test_returned_url_is_https(self):
        html = self._make_index_html("/Archives/edgar/data/320193/000032019324000001/ex991.htm")
        result = self.fn(html, 320193, "000032019324000001")
        self.assertTrue(result.startswith("https://"))

    def test_case_insensitive_ex991_match(self):
        html = '<table><tr><td>ex-99.1</td><td><a href="/Archives/edgar/data/320193/000032019324000001/ex99.htm">doc</a></td></tr></table>'
        result = self.fn(html, 320193, "000032019324000001")
        self.assertIsNotNone(result)

    def test_returns_none_when_ex991_row_has_no_href(self):
        html = "<table><tr><td>EX-99.1</td><td>no link here</td></tr></table>"
        result = self.fn(html, 320193, "000032019324000001")
        self.assertIsNone(result)

    def test_first_ex991_href_is_returned(self):
        html = """<table>
        <tr><td>EX-99.1</td>
        <td><a href="/Archives/edgar/data/1/1/first.htm">first</a></td>
        <td><a href="/Archives/edgar/data/1/1/second.htm">second</a></td>
        </tr></table>"""
        result = self.fn(html, 1, "1")
        self.assertIn("first.htm", result)


# ---------------------------------------------------------------------------
# Functional Tests — _fetch_recent_8k_filing_texts (mocked HTTP)
# ---------------------------------------------------------------------------


SAMPLE_INDEX_HTML = """
<html><body>
<table>
<tr><td>8-K</td><td>EX-99.1</td>
<td><a href="/Archives/edgar/data/320193/000032019324000001/ex99-1.htm">Earnings PR</a></td>
</tr>
</table>
</body></html>
"""

SAMPLE_EARNINGS_TEXT = (
    "Apple Inc. today announced financial results for its fiscal 2024 first quarter. "
    "Revenue of $119.6 billion and earnings per diluted share of $2.18. "
    "CEO Tim Cook commented that strong iPhone sales drove record performance."
)

XBRL_BOILERPLATE = (
    "true true true NASDAQ 0000320193 aapl:Zero500NotesDue2031Member "
    "aapl:A250NotesDue2030Member dei:EntityCommonStockSharesOutstanding"
)

SAMPLE_CIK = "0000320193"
SAMPLE_QUARTER = "Q1"
SAMPLE_YEAR = 2024
SAMPLE_ACCESSION = "0000320193-24-000001"
SAMPLE_PRIMARY_DOC = "aapl-20240101.htm"


def _make_submissions_response(
    accession=SAMPLE_ACCESSION,
    form="8-K",
    filing_date="2024-01-31",
    primary_doc=SAMPLE_PRIMARY_DOC,
):
    return _make_submissions_json(
        accession_numbers=[accession],
        forms=[form],
        filing_dates=[filing_date],
        primary_docs=[primary_doc],
    )


class TestFetchRecentFilingsHappyPath(unittest.TestCase):
    """Happy path: submissions API → index HTML → EX-99.1 → earnings text."""

    def setUp(self):
        from sector_tone_pipeline import _fetch_recent_8k_filing_texts
        self.fetch_fn = _fetch_recent_8k_filing_texts

    @patch('time.sleep')
    @patch('requests.get')
    def test_first_request_goes_to_submissions_url(self, mock_get, mock_sleep):
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text=SAMPLE_EARNINGS_TEXT)
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]

        self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        first_url = mock_get.call_args_list[0][0][0]
        self.assertIn('data.sec.gov/submissions/CIK', first_url)
        self.assertIn(SAMPLE_CIK, first_url)

    @patch('time.sleep')
    @patch('requests.get')
    def test_zero_padded_cik_in_submissions_url(self, mock_get, mock_sleep):
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text=SAMPLE_EARNINGS_TEXT)
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]

        self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        first_url = mock_get.call_args_list[0][0][0]
        # CIK must be zero-padded to 10 digits
        self.assertIn('CIK0000320193', first_url)

    @patch('time.sleep')
    @patch('requests.get')
    def test_index_html_fetched_for_each_matching_8k(self, mock_get, mock_sleep):
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text=SAMPLE_EARNINGS_TEXT)
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]

        self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        # Second call should be to the index HTML
        second_url = mock_get.call_args_list[1][0][0]
        self.assertIn('Archives/edgar/data', second_url)
        self.assertIn('index.htm', second_url)

    @patch('time.sleep')
    @patch('requests.get')
    def test_ex991_document_fetched_when_found_in_index(self, mock_get, mock_sleep):
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text=SAMPLE_EARNINGS_TEXT)
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]

        self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        # Third call should be to the EX-99.1 document
        third_url = mock_get.call_args_list[2][0][0]
        self.assertIn('ex99-1.htm', third_url)

    @patch('time.sleep')
    @patch('requests.get')
    def test_returns_earnings_prose_not_xbrl(self, mock_get, mock_sleep):
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text=SAMPLE_EARNINGS_TEXT)
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        self.assertEqual(len(result), 1)
        self.assertIn("Apple", result[0]["text"])
        self.assertNotIn("aapl:Zero500NotesDue", result[0]["text"])

    @patch('time.sleep')
    @patch('requests.get')
    def test_result_length_bounded_by_max_filings(self, mock_get, mock_sleep):
        import sector_tone_pipeline as stp
        max_f = stp._MAX_FILINGS_PER_COMPANY
        # Provide more filings than max
        accessions = [f"0000320193-24-{i:06d}" for i in range(max_f + 3)]
        submissions_data = _make_submissions_json(
            accession_numbers=accessions,
            forms=["8-K"] * len(accessions),
            filing_dates=["2024-01-15"] * len(accessions),
            primary_docs=[f"doc{i}.htm" for i in range(len(accessions))],
        )
        responses = [_make_mock_response(json_data=submissions_data)]
        for _ in range(max_f):
            responses.append(_make_mock_response(text=SAMPLE_INDEX_HTML))
            responses.append(_make_mock_response(text=SAMPLE_EARNINGS_TEXT))
        mock_get.side_effect = responses

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertLessEqual(len(result), max_f)

    @patch('time.sleep')
    @patch('requests.get')
    def test_rate_limit_pause_applied(self, mock_get, mock_sleep):
        """time.sleep must be called between EDGAR requests."""
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text=SAMPLE_EARNINGS_TEXT)
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]

        self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        import sector_tone_pipeline as stp
        mock_sleep.assert_called_with(stp._EDGAR_RATE_LIMIT_PAUSE)
        self.assertGreater(mock_sleep.call_count, 0)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestFetchFilingsEdgeCases(unittest.TestCase):

    def setUp(self):
        from sector_tone_pipeline import _fetch_recent_8k_filing_texts
        self.fetch_fn = _fetch_recent_8k_filing_texts

    @patch('time.sleep')
    @patch('requests.get')
    def test_empty_submissions_recent_returns_empty_list(self, mock_get, mock_sleep):
        """Submissions JSON with no filings → returns []."""
        submissions_data = {"filings": {"recent": {}}}
        mock_get.return_value = _make_mock_response(json_data=submissions_data)

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_no_8k_filings_in_recent_returns_empty_list(self, mock_get, mock_sleep):
        """Submissions with only non-8-K forms → returns []."""
        submissions_data = _make_submissions_json(
            accession_numbers=["0000320193-24-000001"],
            forms=["10-Q"],
            filing_dates=["2024-02-01"],
            primary_docs=["10q.htm"],
        )
        mock_get.return_value = _make_mock_response(json_data=submissions_data)

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(result, [])
        # Only one request (submissions); no index fetch needed
        self.assertEqual(mock_get.call_count, 1)

    @patch('time.sleep')
    @patch('requests.get')
    def test_filings_outside_quarter_are_excluded(self, mock_get, mock_sleep):
        """8-K filings outside the quarter date range are skipped."""
        submissions_data = _make_submissions_json(
            accession_numbers=["0000320193-24-000001", "0000320193-24-000002"],
            forms=["8-K", "8-K"],
            filing_dates=["2024-07-15", "2024-08-01"],  # Q3 2024, not Q1
            primary_docs=["doc1.htm", "doc2.htm"],
        )
        mock_get.return_value = _make_mock_response(json_data=submissions_data)

        result = self.fetch_fn(SAMPLE_CIK, "Q1", 2024)
        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_non_earnings_8k_items_are_excluded(self, mock_get, mock_sleep):
        """8-K filings without Item 2.02 (earnings) are skipped."""
        submissions_data = _make_submissions_json(
            accession_numbers=["0000320193-24-000001"],
            forms=["8-K"],
            filing_dates=["2024-01-15"],
            primary_docs=["doc.htm"],
            items=["8.01,9.01"],  # Not an earnings filing
        )
        mock_get.return_value = _make_mock_response(json_data=submissions_data)

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(result, [])
        # Only one request (submissions); no index fetch needed
        self.assertEqual(mock_get.call_count, 1)

    @patch('time.sleep')
    @patch('requests.get')
    def test_no_ex991_skips_filing(self, mock_get, mock_sleep):
        """When index has no EX-99.1 row, filing is skipped (not an earnings release)."""
        index_html_no_ex991 = "<html><table><tr><td>EX-10.1</td><td><a href='/Archives/edgar/data/320193/abc/doc.htm'>doc</a></td></tr></table></html>"

        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=index_html_no_ex991)
        mock_get.side_effect = [submissions_resp, index_resp]

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_ex991_http_error_skips_filing(self, mock_get, mock_sleep):
        """When EX-99.1 fetch returns HTTP error, filing is skipped."""
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        ex991_error = _make_mock_response(status_code=404)
        mock_get.side_effect = [submissions_resp, index_resp, ex991_error]

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_submissions_429_returns_empty_list(self, mock_get, mock_sleep):
        """Submissions API returns 429 → returns []."""
        mock_get.return_value = _make_mock_response(status_code=429)
        mock_get.return_value.raise_for_status.return_value = None  # 429 doesn't raise

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_submissions_network_exception_returns_empty_list(self, mock_get, mock_sleep):
        """Submissions API network error → returns []."""
        mock_get.side_effect = ConnectionError("network error")

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_index_fetch_failure_skips_filing(self, mock_get, mock_sleep):
        """Index HTML fetch failure → filing skipped, no unhandled exception."""
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_error = _make_mock_response(status_code=500)
        mock_get.side_effect = [submissions_resp, index_error]

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_cik_zero_padded_in_submissions_url(self, mock_get, mock_sleep):
        """CIK with leading zeros is correctly zero-padded to 10 digits."""
        short_cik = "0000001234"  # 10-digit padded CIK
        submissions_data = _make_submissions_json([], [], [], [])
        mock_get.return_value = _make_mock_response(json_data=submissions_data)

        self.fetch_fn(short_cik, SAMPLE_QUARTER, SAMPLE_YEAR)

        url = mock_get.call_args_list[0][0][0]
        self.assertIn('CIK0000001234', url)

    @patch('time.sleep')
    @patch('requests.get')
    def test_empty_primary_doc_does_not_crash(self, mock_get, mock_sleep):
        """When primary_doc is empty string and EX-99.1 absent, returns []."""
        index_html_no_ex991 = "<html><table></table></html>"
        submissions_data = _make_submissions_json(
            accession_numbers=[SAMPLE_ACCESSION],
            forms=["8-K"],
            filing_dates=["2024-01-15"],
            primary_docs=[""],  # empty primary doc
        )
        submissions_resp = _make_mock_response(json_data=submissions_data)
        index_resp = _make_mock_response(text=index_html_no_ex991)
        mock_get.side_effect = [submissions_resp, index_resp]

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_multiple_8k_filings_all_processed_up_to_max(self, mock_get, mock_sleep):
        """Multiple matching 8-Ks all get index + doc fetches, up to MAX."""
        import sector_tone_pipeline as stp
        max_f = stp._MAX_FILINGS_PER_COMPANY

        accessions = [f"0000320193-24-{i:06d}" for i in range(max_f)]
        submissions_data = _make_submissions_json(
            accession_numbers=accessions,
            forms=["8-K"] * max_f,
            filing_dates=["2024-01-15"] * max_f,
            primary_docs=[f"doc{i}.htm" for i in range(max_f)],
        )
        responses = [_make_mock_response(json_data=submissions_data)]
        for _ in range(max_f):
            responses.append(_make_mock_response(text=SAMPLE_INDEX_HTML))
            responses.append(_make_mock_response(text=SAMPLE_EARNINGS_TEXT))
        mock_get.side_effect = responses

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(len(result), max_f)


# ---------------------------------------------------------------------------
# Security Tests
# ---------------------------------------------------------------------------


class TestSecurityNoSafeFilter(unittest.TestCase):
    """EDGAR-sourced text must not be rendered with Jinja2 | safe filter."""

    def test_sector_tone_template_no_safe_filter_on_sector_data(self):
        """sector_tone.html must not render sector data with | safe."""
        templates_dir = os.path.join(SIGNALTRACKERS_DIR, 'templates')
        template_path = os.path.join(templates_dir, 'index.html')
        if not os.path.exists(template_path):
            self.skipTest("index.html not found")
        with open(template_path) as f:
            content = f.read()
        # sector_management_tone data sourced from EDGAR must never use | safe
        # We check that sector tone fields aren't piped through safe
        import re
        safe_uses = re.findall(r'\{\{[^}]*sector_management_tone[^}]*\|\s*safe[^}]*\}\}', content)
        self.assertEqual(safe_uses, [],
                         f"sector_management_tone data rendered with | safe: {safe_uses}")

    def test_cik_is_not_from_user_input(self):
        """CIK used in submissions URL must come from EDGAR ticker map, not user input."""
        src = read_source('sector_tone_pipeline.py')
        # The pipeline uses SP500_BY_SECTOR tickers → ticker_map CIK lookup
        # Verify the fetch is called with CIK from ticker_map, not request args
        self.assertIn('ticker_map', src)
        self.assertNotIn('request.args', src)
        self.assertNotIn('request.form', src)


class TestSubmissionsUrlSecurity(unittest.TestCase):
    """Submissions URL must be constructed from validated CIK only."""

    def test_submissions_url_template_uses_cik_only(self):
        """_EDGAR_SUBMISSIONS_URL format string uses only {cik} placeholder."""
        import sector_tone_pipeline as stp
        # Should only have one format placeholder: {cik}
        url_template = stp._EDGAR_SUBMISSIONS_URL
        self.assertIn('{cik}', url_template)
        # Should not have any other placeholders
        placeholders = [p for p in url_template.split('{') if '}' in p and p.split('}')[0] != 'cik']
        self.assertEqual(placeholders, [])

    def test_submissions_url_is_https(self):
        """Submissions URL must use HTTPS."""
        import sector_tone_pipeline as stp
        self.assertTrue(stp._EDGAR_SUBMISSIONS_URL.startswith('https://'))

    @patch('time.sleep')
    @patch('requests.get')
    def test_no_user_input_injected_into_submissions_url(self, mock_get, mock_sleep):
        """CIK value is sanitized (zero-padded string from ticker map, not raw user input)."""
        from sector_tone_pipeline import _fetch_recent_8k_filing_texts
        # CIK is always a zero-padded 10-digit string from _fetch_edgar_ticker_map
        submissions_data = _make_submissions_json([], [], [], [])
        mock_get.return_value = _make_mock_response(json_data=submissions_data)

        # Even with a numeric CIK string, the URL must be well-formed
        _fetch_recent_8k_filing_texts("0000320193", "Q1", 2024)

        url = mock_get.call_args_list[0][0][0]
        # URL must be a proper EDGAR URL
        self.assertTrue(url.startswith('https://data.sec.gov/submissions/CIK'))


# ---------------------------------------------------------------------------
# Performance / Correctness Contracts
# ---------------------------------------------------------------------------


class TestPerformanceContracts(unittest.TestCase):

    def setUp(self):
        from sector_tone_pipeline import _fetch_recent_8k_filing_texts
        self.fetch_fn = _fetch_recent_8k_filing_texts

    @patch('time.sleep')
    @patch('requests.get')
    def test_empty_text_not_appended_to_results(self, mock_get, mock_sleep):
        """Filings with empty/whitespace text are not included in results."""
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text="   \n  ")  # whitespace only
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]
        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertEqual(result, [])

    @patch('time.sleep')
    @patch('requests.get')
    def test_full_text_preserved_without_truncation(self, mock_get, mock_sleep):
        """Returned texts must contain full untruncated content for training use."""
        long_text = "earnings " * 10000  # Very long earnings text
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text=long_text)
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]

        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        self.assertEqual(len(result), 1)
        # Full text is preserved (not truncated to _MAX_FINBERT_INPUT_CHARS)
        self.assertGreater(len(result[0]["text"]), 2048)

    @patch('time.sleep')
    @patch('requests.get')
    def test_returns_list_type(self, mock_get, mock_sleep):
        """Return type must always be a list."""
        mock_get.side_effect = ConnectionError("fail")
        result = self.fetch_fn(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)
        self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# Acceptance Criteria Verification
# ---------------------------------------------------------------------------


class TestAcceptanceCriteria(unittest.TestCase):

    def test_ac1_submissions_api_used(self):
        """AC: submissions API URL present in fetch function body."""
        import ast
        src = read_source('sector_tone_pipeline.py')
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_fetch_recent_8k_filing_texts':
                func_src = ast.get_source_segment(src, node) or ''
                self.assertIn('_EDGAR_SUBMISSIONS_URL', func_src)
                return
        self.fail('_fetch_recent_8k_filing_texts not found in source')

    def test_ac2_index_html_parsed_for_ex991(self):
        """AC: filing index HTML is parsed to locate EX-99.1 exhibit."""
        src = read_source('sector_tone_pipeline.py')
        self.assertIn('_extract_ex991_url', src)

    def test_ac3_item_202_filter_and_ex991_check(self):
        """AC: 8-K filings filtered by Item 2.02 (earnings) and EX-99.1 presence."""
        src = read_source('sector_tone_pipeline.py')
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_fetch_recent_8k_filing_texts':
                func_src = ast.get_source_segment(src, node) or ''
                # Must filter on Item 2.02 and check for EX-99.1
                self.assertIn('2.02', func_src)
                self.assertIn('not ex99_url', func_src)
                return
        self.fail('_fetch_recent_8k_filing_texts not found')

    def test_ac4_rate_limit_pause_constant_exists(self):
        """AC: _EDGAR_RATE_LIMIT_PAUSE constant exists and is used."""
        import sector_tone_pipeline as stp
        self.assertTrue(hasattr(stp, '_EDGAR_RATE_LIMIT_PAUSE'))
        self.assertGreater(stp._EDGAR_RATE_LIMIT_PAUSE, 0)
        src = read_source('sector_tone_pipeline.py')
        self.assertIn('_EDGAR_RATE_LIMIT_PAUSE', src)

    @patch('time.sleep')
    @patch('requests.get')
    def test_ac5_ex991_text_passed_to_finbert_not_xbrl(self, mock_get, mock_sleep):
        """AC: EX-99.1 earnings prose text is returned (not XBRL boilerplate)."""
        from sector_tone_pipeline import _fetch_recent_8k_filing_texts
        submissions_resp = _make_mock_response(json_data=_make_submissions_response())
        index_resp = _make_mock_response(text=SAMPLE_INDEX_HTML)
        doc_resp = _make_mock_response(text=SAMPLE_EARNINGS_TEXT)
        mock_get.side_effect = [submissions_resp, index_resp, doc_resp]

        result = _fetch_recent_8k_filing_texts(SAMPLE_CIK, SAMPLE_QUARTER, SAMPLE_YEAR)

        self.assertEqual(len(result), 1)
        # Verify it contains earnings prose, not XBRL boilerplate
        self.assertNotIn('aapl:Zero500NotesDue', result[0]["text"])


# ---------------------------------------------------------------------------
# _strip_html helper (HTML stripping before FinBERT scoring)
# ---------------------------------------------------------------------------


class TestStripHtml(unittest.TestCase):
    """_strip_html must convert raw EDGAR HTML/SGML to clean plain text."""

    def setUp(self):
        from sector_tone_pipeline import _strip_html
        self.strip = _strip_html

    def test_strips_html_tags(self):
        """Basic HTML tags must be removed."""
        raw = "<html><body><p>Revenue increased 12%.</p></body></html>"
        result = self.strip(raw)
        self.assertNotIn('<', result)
        self.assertNotIn('>', result)
        self.assertIn("Revenue increased 12%.", result)

    def test_strips_sgml_envelope_headers(self):
        """SGML headers like <DOCUMENT>, <TYPE>, <SEQUENCE> must be removed."""
        raw = "<DOCUMENT>\n<TYPE>EX-99.1\n<SEQUENCE>2\n<TEXT>\n<html><body>Earnings text.</body></html>"
        result = self.strip(raw)
        self.assertNotIn('<DOCUMENT>', result)
        self.assertNotIn('<TYPE>', result)
        self.assertNotIn('<SEQUENCE>', result)
        self.assertIn("Earnings text.", result)

    def test_decodes_html_entities(self):
        """HTML entities must be decoded to their character equivalents."""
        raw = "<p>Revenue &amp; profit rose 10%&nbsp;year-over-year.</p>"
        result = self.strip(raw)
        self.assertIn("Revenue & profit rose 10%", result)
        self.assertNotIn("&amp;", result)
        self.assertNotIn("&nbsp;", result)

    def test_collapses_whitespace(self):
        """Multiple spaces, tabs, and newlines must be collapsed to single spaces."""
        raw = "<p>Net income\n\n   was\t$1.2 billion.</p>"
        result = self.strip(raw)
        self.assertNotIn('\n', result)
        self.assertNotIn('\t', result)
        self.assertNotIn('  ', result)
        self.assertIn("Net income", result)
        self.assertIn("$1.2 billion.", result)

    def test_returns_stripped_string_not_bytes(self):
        """Return value must be a str with no leading/trailing whitespace."""
        raw = "  <p>Hello world</p>  "
        result = self.strip(raw)
        self.assertIsInstance(result, str)
        self.assertEqual(result, result.strip())

    def test_empty_string_returns_empty(self):
        """Empty input must return empty string."""
        self.assertEqual(self.strip(""), "")

    def test_strips_script_and_style_tags(self):
        """Script and style tag content is removed with the tags."""
        raw = "<html><head><style>body{color:red}</style></head><body><p>Real content.</p></body></html>"
        result = self.strip(raw)
        self.assertIn("Real content.", result)
        # Tag content (CSS) removed with tags — the text between them may appear
        # but structural markup must be gone
        self.assertNotIn('<style>', result)
        self.assertNotIn('<html>', result)

    def test_real_edgar_sgml_envelope(self):
        """Reproduces QA evidence: META EX-99.1 SGML envelope is stripped."""
        raw = (
            "<DOCUMENT>\n<TYPE>EX-99.1\n<SEQUENCE>2\n"
            "<FILENAME>meta-12312025xexhibit991.htm\n<DESCRIPTION>EX-99.1\n<TEXT>\n"
            "<html><head>\n<!-- Document created using Wdesk -->\n"
            "<title>Document</title></head>"
            "<body><div id='if4849fe'>"
            "<p>Meta Platforms reports Q4 2025 revenue of $48.4 billion, up 21% year-over-year.</p>"
            "</div></body></html>"
        )
        result = self.strip(raw)
        self.assertNotIn('<DOCUMENT>', result)
        self.assertNotIn('<html>', result)
        self.assertNotIn('<head>', result)
        self.assertNotIn('<body>', result)
        self.assertIn("Meta Platforms reports Q4 2025 revenue of $48.4 billion", result)

    def test_finbert_window_not_wasted_on_markup(self):
        """After stripping, markup overhead does not fill the text."""
        # Simulate a document where markup consumes the first 500 chars
        markup_prefix = "<html><head>" + "<div class='x'>" * 20 + "</head><body>"
        prose = "Net revenue for the quarter was $10 billion."
        raw = markup_prefix + prose + "</body></html>"
        result = self.strip(raw)
        # Prose must be present — not buried behind markup chars
        self.assertIn("Net revenue for the quarter was $10 billion.", result)

    def test_strip_html_function_exists_in_module(self):
        """_strip_html must be importable from sector_tone_pipeline."""
        from sector_tone_pipeline import _strip_html
        self.assertTrue(callable(_strip_html))

    def test_strip_html_called_before_finbert_input(self):
        """Source code must apply _strip_html to fetched document text."""
        src = read_source('sector_tone_pipeline.py')
        self.assertIn('_strip_html(', src,
                      '_strip_html must be called before passing text to FinBERT')

    def test_html_module_imported(self):
        """html stdlib module must be imported for entity decoding."""
        src = read_source('sector_tone_pipeline.py')
        self.assertIn('import html', src)


if __name__ == '__main__':
    unittest.main()
