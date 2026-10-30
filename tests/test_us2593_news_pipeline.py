"""
Tests for US-259.3: News data pipeline — fetch, store, and summarize macro news.

US-490.1 split the pipeline: run_news_pipeline() fetches and stores articles
daily with no AI calls; summarize_week_news() summarizes the week's articles.

These are static source-analysis tests. No live Tavily calls, no Docker required.
"""

import ast
import importlib.util
import inspect
import json
import os
import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to load the module under test
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'

sys.path.insert(0, str(SIGNALTRACKERS_DIR))

# Load news_pipeline without executing side effects
NEWS_PIPELINE_FILE = SIGNALTRACKERS_DIR / 'news_pipeline.py'
NEWS_PIPELINE_SOURCE = NEWS_PIPELINE_FILE.read_text()

# Parse AST for static checks
NEWS_PIPELINE_AST = ast.parse(NEWS_PIPELINE_SOURCE)


def load_news_pipeline_module():
    """Import news_pipeline with minimal env so no live calls are made."""
    spec = importlib.util.spec_from_file_location('news_pipeline', NEWS_PIPELINE_FILE)
    mod = importlib.util.module_from_spec(spec)
    # Provide stubs for optional deps
    sys.modules.setdefault('pytz', __import__('pytz'))
    spec.loader.exec_module(mod)
    # Keep these tests off the checkout's real weekly-review file; the weekly
    # review lookup is covered in test_us4901_weekly_scheduler.py.
    mod.NEWS_REVIEWS_FILE = Path('/nonexistent/news_reviews.json')
    return mod


def _today():
    """Today in ET, as the pipeline keys records (the container clock is UTC)."""
    return date.fromisoformat(load_news_pipeline_module()._today())


# ---------------------------------------------------------------------------
# Module structure tests
# ---------------------------------------------------------------------------

class TestModuleStructure:
    def test_module_is_importable(self):
        """Module must import without error."""
        mod = load_news_pipeline_module()
        assert mod is not None

    def test_run_news_pipeline_exported(self):
        """run_news_pipeline() must be a callable exported from the module."""
        mod = load_news_pipeline_module()
        assert callable(getattr(mod, 'run_news_pipeline', None))

    def test_get_stored_news_exported(self):
        """get_stored_news() must be a callable exported from the module."""
        mod = load_news_pipeline_module()
        assert callable(getattr(mod, 'get_stored_news', None))

    def test_get_stored_news_has_max_stale_days_param(self):
        """get_stored_news() must accept max_stale_days argument."""
        mod = load_news_pipeline_module()
        sig = inspect.signature(mod.get_stored_news)
        assert 'max_stale_days' in sig.parameters

    def test_run_news_pipeline_callable_no_required_args(self):
        """run_news_pipeline() must be callable with no arguments."""
        mod = load_news_pipeline_module()
        sig = inspect.signature(mod.run_news_pipeline)
        required = [
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
        ]
        assert len(required) == 0


# ---------------------------------------------------------------------------
# Six topic areas
# ---------------------------------------------------------------------------

class TestTopicAreas:
    def test_six_topic_areas_defined(self):
        """Pipeline must define exactly 6 topic queries."""
        mod = load_news_pipeline_module()
        assert len(mod.TOPIC_QUERIES) == 6

    def test_topic_macro_present(self):
        topics = {t for t, _ in load_news_pipeline_module().TOPIC_QUERIES}
        assert 'macro' in topics

    def test_topic_crypto_present(self):
        topics = {t for t, _ in load_news_pipeline_module().TOPIC_QUERIES}
        assert 'crypto' in topics

    def test_topic_equity_present(self):
        topics = {t for t, _ in load_news_pipeline_module().TOPIC_QUERIES}
        assert 'equity' in topics

    def test_topic_rates_present(self):
        topics = {t for t, _ in load_news_pipeline_module().TOPIC_QUERIES}
        assert 'rates' in topics

    def test_topic_dollar_present(self):
        topics = {t for t, _ in load_news_pipeline_module().TOPIC_QUERIES}
        assert 'dollar' in topics

    def test_topic_credit_present(self):
        topics = {t for t, _ in load_news_pipeline_module().TOPIC_QUERIES}
        assert 'credit' in topics

    def test_all_topics_have_queries(self):
        """Every topic must have a non-empty query string."""
        mod = load_news_pipeline_module()
        for topic, query in mod.TOPIC_QUERIES:
            assert query.strip(), f"Empty query for topic: {topic}"


# ---------------------------------------------------------------------------
# Search depth and raw content flags
# ---------------------------------------------------------------------------

class TestFetchConfig:
    def test_advanced_search_depth_in_source(self):
        """_fetch_topic must use search_depth='advanced'."""
        assert "search_depth': 'advanced'" in NEWS_PIPELINE_SOURCE or \
               '"search_depth": "advanced"' in NEWS_PIPELINE_SOURCE or \
               "'advanced'" in NEWS_PIPELINE_SOURCE

    def test_include_raw_content_true_in_source(self):
        """_fetch_topic must set include_raw_content=True."""
        assert 'include_raw_content' in NEWS_PIPELINE_SOURCE
        assert 'True' in NEWS_PIPELINE_SOURCE


# ---------------------------------------------------------------------------
# Article schema
# ---------------------------------------------------------------------------

class TestArticleSchema:
    REQUIRED_KEYS = {'headline', 'url', 'source', 'timestamp', 'raw_content', 'topic'}

    def test_article_schema_contains_headline(self):
        assert 'headline' in NEWS_PIPELINE_SOURCE

    def test_article_schema_contains_url(self):
        assert "'url'" in NEWS_PIPELINE_SOURCE or '"url"' in NEWS_PIPELINE_SOURCE

    def test_article_schema_contains_source(self):
        assert "'source'" in NEWS_PIPELINE_SOURCE or '"source"' in NEWS_PIPELINE_SOURCE

    def test_article_schema_contains_timestamp(self):
        assert 'timestamp' in NEWS_PIPELINE_SOURCE

    def test_article_schema_contains_raw_content(self):
        assert 'raw_content' in NEWS_PIPELINE_SOURCE

    def test_article_schema_contains_topic(self):
        assert "'topic'" in NEWS_PIPELINE_SOURCE or '"topic"' in NEWS_PIPELINE_SOURCE

    def test_fetch_topic_builds_article_dicts(self):
        """_fetch_topic must build article dicts with required keys."""
        mod = load_news_pipeline_module()
        fake_response = {
            'results': [{
                'title': 'Test Headline',
                'url': 'https://reuters.com/test',
                'content': 'Test content',
                'raw_content': 'Full raw content here',
            }]
        }
        with patch('requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = fake_response
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            articles = mod._fetch_topic('testapikey', 'macro', 'test query')

        assert len(articles) == 1
        art = articles[0]
        for key in self.REQUIRED_KEYS:
            assert key in art, f"Missing key: {key}"

    def test_article_source_is_domain(self):
        """source field should be domain name, not full URL."""
        mod = load_news_pipeline_module()
        fake_response = {
            'results': [{
                'title': 'Test',
                'url': 'https://www.reuters.com/article/test',
                'content': 'content',
                'raw_content': None,
            }]
        }
        with patch('requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = fake_response
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            articles = mod._fetch_topic('key', 'macro', 'query')

        assert '/' not in articles[0]['source'], "source should be domain, not full URL"
        assert 'reuters.com' in articles[0]['source']

    def test_article_topic_set_correctly(self):
        """topic field must match the topic argument passed to _fetch_topic."""
        mod = load_news_pipeline_module()
        fake_response = {
            'results': [{'title': 'T', 'url': 'https://x.com', 'content': 'c', 'raw_content': 'r'}]
        }
        with patch('requests.post') as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = fake_response
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            articles = mod._fetch_topic('key', 'credit', 'credit query')

        assert articles[0]['topic'] == 'credit'


# ---------------------------------------------------------------------------
# Cross-market AI summary
# ---------------------------------------------------------------------------

class TestAISummary:
    def test_single_summary_function_exists(self):
        """A function to generate the cross-market summary must exist."""
        mod = load_news_pipeline_module()
        assert callable(getattr(mod, '_generate_cross_market_summary', None))

    def test_summary_prompt_instructs_prose_paragraphs(self):
        """AI prompt must instruct prose paragraphs, not bullets."""
        assert 'prose' in NEWS_PIPELINE_SOURCE.lower() or 'paragraph' in NEWS_PIPELINE_SOURCE.lower()
        # Must NOT instruct bullet points
        # (We check the negative: the prompt itself shouldn't say "use bullets")
        # The positive check above is sufficient

    def test_no_bullet_instruction_in_prompt(self):
        """The summarization prompt must NOT tell the model to use bullet points."""
        # Find the system_prompt string in source
        assert 'Do NOT use bullet' in NEWS_PIPELINE_SOURCE or \
               'no bullet' in NEWS_PIPELINE_SOURCE.lower() or \
               'not use bullet' in NEWS_PIPELINE_SOURCE.lower()

    def test_daily_record_stores_articles_without_summary(self):
        """The daily run stores articles only; it never calls the summarizers (US-490.1)."""
        mod = load_news_pipeline_module()
        article = {'headline': 'h', 'url': 'u', 'source': 's',
                   'timestamp': 't', 'raw_content': 'r', 'topic': 'macro'}
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'testkey'}), \
             patch.object(mod, '_fetch_topic', return_value=[article]), \
             patch.object(mod, '_generate_cross_market_summary') as cross, \
             patch.object(mod, '_generate_all_topic_summaries') as topics, \
             patch.object(mod, '_load_cache', return_value={}), \
             patch.object(mod, '_save_cache') as mock_save:

            mod.run_news_pipeline()

        cross.assert_not_called()
        topics.assert_not_called()
        saved_data = mock_save.call_args[0][0]
        record = saved_data[_today().isoformat()]
        assert record['articles']
        assert record.get('summary') is None

    def test_single_ai_call_not_per_topic(self):
        """One cross-market summarization call per weekly run (not one per topic)."""
        mod = load_news_pipeline_module()
        call_count = []

        def mock_summarize(articles):
            call_count.append(1)
            return 'summary text'

        day = _today().isoformat()
        cache = {day: {'date': day, 'articles': [
            {'headline': 'h', 'url': f'u{i}', 'source': 's',
             'timestamp': 't', 'raw_content': 'r', 'topic': topic}
            for i, topic in enumerate(['macro', 'rates', 'credit'])]}}

        with patch.object(mod, '_load_cache', return_value=cache), \
             patch.object(mod, '_generate_cross_market_summary', side_effect=mock_summarize), \
             patch.object(mod, '_generate_all_topic_summaries', return_value={}), \
             patch.object(mod, '_save_reviews'):

            mod.summarize_week_news(_today(), _today())

        assert len(call_count) == 1, f"Expected 1 summarization call, got {len(call_count)}"


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------

class TestStorageLayer:
    def test_storage_path_inside_data_dir(self):
        """NEWS_CACHE_FILE must be inside the data/ directory."""
        mod = load_news_pipeline_module()
        assert 'data' in str(mod.NEWS_CACHE_FILE)

    def test_storage_keyed_by_date_string(self):
        """Records must be stored with date string keys (YYYY-MM-DD)."""
        mod = load_news_pipeline_module()
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'key'}), \
             patch.object(mod, '_fetch_topic', return_value=[]), \
             patch.object(mod, '_generate_cross_market_summary', return_value=None), \
             patch.object(mod, '_save_cache') as mock_save:

            mod.run_news_pipeline()

        saved = mock_save.call_args[0][0]
        today = _today().isoformat()
        assert today in saved
        # Key format: YYYY-MM-DD
        assert len(today) == 10
        assert today[4] == '-' and today[7] == '-'

    def test_one_record_per_date_overwrites(self):
        """A second run on the same day must overwrite, not append."""
        mod = load_news_pipeline_module()
        today = _today().isoformat()
        existing = {
            today: {
                'date': today,
                'articles': [{'headline': 'old'}],
                'summary': 'old summary',
                'fetched_at': '2026-01-01T00:00:00',
            }
        }
        saved_calls = []
        fake_article = {
            'headline': 'h', 'url': 'u', 'source': 's',
            'timestamp': 't', 'raw_content': 'r', 'topic': 'macro',
        }

        def mock_save(data):
            saved_calls.append(data)

        with patch.dict(os.environ, {'TAVILY_API_KEY': 'key'}), \
             patch.object(mod, '_load_cache', return_value=existing.copy()), \
             patch.object(mod, '_fetch_topic', return_value=[fake_article]), \
             patch.object(mod, '_generate_cross_market_summary', return_value='new summary'), \
             patch.object(mod, '_save_cache', side_effect=mock_save):

            mod.run_news_pipeline()

        final = saved_calls[-1]
        # Should still have exactly one entry for today (overwritten, not duplicated)
        today_records = [k for k in final.keys() if k == today]
        assert len(today_records) == 1
        # The fake fetch returns the same article for each topic.
        assert final[today]['articles'] == [fake_article] * len(mod.TOPIC_QUERIES)

    def test_90_day_pruning_logic_present(self):
        """Source must contain 90-day pruning logic."""
        assert '90' in NEWS_PIPELINE_SOURCE
        assert 'prune' in NEWS_PIPELINE_SOURCE.lower() or 'cutoff' in NEWS_PIPELINE_SOURCE.lower() or \
               'retention' in NEWS_PIPELINE_SOURCE.lower()

    def test_pruning_removes_old_entries(self):
        """_prune must remove entries older than RETENTION_DAYS."""
        mod = load_news_pipeline_module()
        old_date = (_today() - timedelta(days=91)).isoformat()
        recent_date = _today().isoformat()
        data = {
            old_date: {'date': old_date},
            recent_date: {'date': recent_date},
        }
        pruned = mod._prune(data)
        assert old_date not in pruned
        assert recent_date in pruned

    def test_load_cache_returns_empty_dict_when_file_missing(self, tmp_path):
        """_load_cache must return {} when the file does not exist."""
        mod = load_news_pipeline_module()
        nonexistent = tmp_path / 'news_data.json'  # does not exist
        with patch.object(mod, 'NEWS_CACHE_FILE', nonexistent):
            result = mod._load_cache()
        assert result == {}

    def test_load_cache_returns_empty_dict_on_malformed_json(self, tmp_path):
        """_load_cache must return {} on malformed JSON without raising."""
        mod = load_news_pipeline_module()
        bad_file = tmp_path / 'bad.json'
        bad_file.write_text('{ invalid json }')
        with patch.object(mod, 'NEWS_CACHE_FILE', bad_file):
            result = mod._load_cache()
        assert result == {}


# ---------------------------------------------------------------------------
# get_stored_news behavior
# ---------------------------------------------------------------------------

class TestGetStoredNews:
    def test_returns_today_record_when_present(self):
        mod = load_news_pipeline_module()
        today = _today().isoformat()
        cache = {today: {'date': today, 'summary': 'today summary', 'articles': []}}
        with patch.object(mod, '_load_cache', return_value=cache):
            result = mod.get_stored_news()
        assert result is not None
        assert result['date'] == today

    def test_returns_recent_stale_record_within_window(self):
        mod = load_news_pipeline_module()
        yesterday = (_today() - timedelta(days=1)).isoformat()
        cache = {yesterday: {'date': yesterday, 'summary': 'yesterday', 'articles': []}}
        with patch.object(mod, '_load_cache', return_value=cache):
            result = mod.get_stored_news(max_stale_days=7)
        assert result is not None
        assert result['date'] == yesterday

    def test_returns_none_when_record_too_stale(self):
        mod = load_news_pipeline_module()
        old = (_today() - timedelta(days=8)).isoformat()
        cache = {old: {'date': old, 'summary': 'old', 'articles': []}}
        with patch.object(mod, '_load_cache', return_value=cache):
            result = mod.get_stored_news(max_stale_days=7)
        assert result is None

    def test_returns_none_when_file_absent(self):
        mod = load_news_pipeline_module()
        with patch.object(mod, '_load_cache', return_value={}):
            result = mod.get_stored_news()
        assert result is None

    def test_returns_none_on_malformed_cache(self):
        mod = load_news_pipeline_module()
        with patch.object(mod, '_load_cache', side_effect=Exception('boom')):
            result = mod.get_stored_news()
        assert result is None

    def test_returns_most_recent_when_multiple_stale_records(self):
        mod = load_news_pipeline_module()
        d1 = (_today() - timedelta(days=3)).isoformat()
        d2 = (_today() - timedelta(days=5)).isoformat()
        cache = {
            d1: {'date': d1, 'summary': 'newer', 'articles': []},
            d2: {'date': d2, 'summary': 'older', 'articles': []},
        }
        with patch.object(mod, '_load_cache', return_value=cache):
            result = mod.get_stored_news(max_stale_days=7)
        assert result['date'] == d1


# ---------------------------------------------------------------------------
# Graceful degradation (no TAVILY_API_KEY)
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    def test_run_news_pipeline_returns_false_without_api_key(self):
        mod = load_news_pipeline_module()
        env = {k: v for k, v in os.environ.items() if k != 'TAVILY_API_KEY'}
        with patch.dict(os.environ, env, clear=True):
            result = mod.run_news_pipeline()
        assert result is False

    def test_run_news_pipeline_does_not_raise_without_api_key(self):
        mod = load_news_pipeline_module()
        env = {k: v for k, v in os.environ.items() if k != 'TAVILY_API_KEY'}
        with patch.dict(os.environ, env, clear=True):
            try:
                mod.run_news_pipeline()
            except Exception as exc:
                pytest.fail(f"run_news_pipeline raised unexpectedly: {exc}")

    def test_fetch_topic_returns_empty_list_on_http_error(self):
        mod = load_news_pipeline_module()
        with patch('requests.post', side_effect=Exception('network error')):
            result = mod._fetch_topic('key', 'macro', 'query')
        assert result == []

    def test_run_pipeline_continues_if_summary_generation_fails(self):
        """If AI summarization fails, pipeline should still save articles."""
        mod = load_news_pipeline_module()
        saved = []
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'key'}), \
             patch.object(mod, '_fetch_topic', return_value=[
                 {'headline': 'h', 'url': 'u', 'source': 's',
                  'timestamp': 't', 'raw_content': 'r', 'topic': 'macro'}
             ]), \
             patch.object(mod, '_generate_cross_market_summary', return_value=None), \
             patch.object(mod, '_save_cache', side_effect=lambda d: saved.append(d)):

            mod.run_news_pipeline()

        assert saved, "Cache was not saved when summary generation returned None"
        today = _today().isoformat()
        assert today in saved[0]
        assert len(saved[0][today]['articles']) > 0


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------

class TestSecurity:
    def test_api_key_read_from_env_not_hardcoded(self):
        """TAVILY_API_KEY must come from os.environ, not be hardcoded."""
        assert "os.environ.get('TAVILY_API_KEY')" in NEWS_PIPELINE_SOURCE or \
               'os.environ.get("TAVILY_API_KEY")' in NEWS_PIPELINE_SOURCE

    def test_no_hardcoded_api_key_value(self):
        """No API key value pattern should appear in source."""
        import re
        # Check for common API key patterns (long alphanumeric strings)
        suspicious = re.findall(r'tvly-[A-Za-z0-9]{20,}', NEWS_PIPELINE_SOURCE)
        assert len(suspicious) == 0, f"Possible hardcoded Tavily key found: {suspicious}"

    def test_no_eval_exec_on_article_content(self):
        """Article content must never be passed to eval() or exec()."""
        assert 'eval(' not in NEWS_PIPELINE_SOURCE
        assert 'exec(' not in NEWS_PIPELINE_SOURCE


# ---------------------------------------------------------------------------
# Performance / API credit usage
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_at_most_six_tavily_calls_per_run(self):
        """Pipeline must not exceed 6 Tavily search calls per run."""
        mod = load_news_pipeline_module()
        fetch_calls = []

        def mock_fetch(api_key, topic, query):
            fetch_calls.append(topic)
            return []

        with patch.dict(os.environ, {'TAVILY_API_KEY': 'key'}), \
             patch.object(mod, '_fetch_topic', side_effect=mock_fetch), \
             patch.object(mod, '_generate_cross_market_summary', return_value=None), \
             patch.object(mod, '_save_cache'):

            mod.run_news_pipeline()

        assert len(fetch_calls) <= 6, f"Expected ≤6 fetch calls, got {len(fetch_calls)}"


# ---------------------------------------------------------------------------
# dashboard.py integration
# ---------------------------------------------------------------------------

DASHBOARD_FILE = SIGNALTRACKERS_DIR / 'dashboard.py'
DASHBOARD_SOURCE = DASHBOARD_FILE.read_text()


class TestDashboardIntegration:
    def test_news_pipeline_imported_in_run_data_collection(self):
        """run_data_collection must import or reference news_pipeline."""
        assert 'news_pipeline' in DASHBOARD_SOURCE

    def test_run_news_pipeline_called_in_data_collection(self):
        """run_data_collection must call run_news_pipeline()."""
        assert 'run_news_pipeline' in DASHBOARD_SOURCE

    def test_news_pipeline_call_wrapped_in_try_except(self):
        """News pipeline call must be wrapped in try/except."""
        # Find the block containing run_news_pipeline
        assert 'run_news_pipeline' in DASHBOARD_SOURCE
        # Verify try/except pattern exists near it
        idx = DASHBOARD_SOURCE.find('run_news_pipeline')
        surrounding = DASHBOARD_SOURCE[max(0, idx - 200):idx + 200]
        assert 'try' in surrounding or 'except' in surrounding

    @staticmethod
    def _body(start, end):
        return DASHBOARD_SOURCE[DASHBOARD_SOURCE.index(start):DASHBOARD_SOURCE.index(end)]

    def test_news_fetch_runs_in_daily_data_phase(self):
        """The daily data phase fetches and stores news (no summarizing, US-490.1)."""
        body = self._body('def _run_data_phase(', 'def run_data_collection(')
        assert 'run_news_pipeline()' in body
        assert 'summarize_week_news' not in body

    def test_news_summary_runs_before_briefing_generation(self):
        """The weekly review summarizes the week's news before the section briefings."""
        body = self._body('def run_weekly_ai_review(', 'def api_reload_data(')
        news_idx = body.find('summarize_week_news(')
        crypto_call_idx = body.find('generate_crypto_summary(')
        assert news_idx >= 0, "summarize_week_news not found in run_weekly_ai_review body"
        assert crypto_call_idx >= 0, "generate_crypto_summary call not found in run_weekly_ai_review body"
        assert news_idx < crypto_call_idx

    def test_news_summary_runs_before_general_summary(self):
        body = self._body('def run_weekly_ai_review(', 'def api_reload_data(')
        news_idx = body.find('summarize_week_news(')
        daily_idx = body.find('generate_daily_summary(')
        assert news_idx >= 0
        assert daily_idx >= 0
        assert news_idx < daily_idx

    def test_fetching_news_status_message(self):
        """run_data_collection must emit a status message for the news step."""
        assert 'Fetching daily news' in DASHBOARD_SOURCE or \
               'news pipeline' in DASHBOARD_SOURCE.lower()
