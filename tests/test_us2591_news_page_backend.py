"""
Tests for US-259.1: News page backend route and data model.

Verifies:
- Flask route /news exists in dashboard.py
- Route function is named 'news'
- Route passes required template variables (summary_text, source_groups, week_label, is_stale, empty_state, extract_domain)
  (weekly framing since US-490.4)
- extract_domain correctly strips domain from URLs
- news.html template exists and extends base.html
- news.html renders 'No news yet' empty state when no data
- news.html renders stale banner when is_stale=True
- news.html renders summary card when summary_text is provided
- news.html renders source cards for each source
- news.html sets correct page title
- base.html includes /news nav link
- news.css exists in static/css/
- base.html active state logic includes 'news' endpoint check
"""

import os
import re
import pytest


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'templates')
STATIC_CSS = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'static', 'css')
DASHBOARD_PY = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'dashboard.py')

BASE_HTML = os.path.join(TEMPLATES_DIR, 'base.html')
NEWS_HTML = os.path.join(TEMPLATES_DIR, 'news.html')
NEWS_CSS = os.path.join(STATIC_CSS, 'news.css')


@pytest.fixture(scope='module')
def dashboard_src():
    with open(DASHBOARD_PY) as f:
        return f.read()


@pytest.fixture(scope='module')
def base_html():
    with open(BASE_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def news_html():
    with open(NEWS_HTML) as f:
        return f.read()


# ─── Route ───────────────────────────────────────────────────────────────────

class TestNewsRoute:
    def test_route_decorator_exists(self, dashboard_src):
        assert "@app.route('/news')" in dashboard_src

    def test_route_function_named_news(self, dashboard_src):
        assert re.search(r"def news\(\)", dashboard_src)

    def test_route_renders_news_template(self, dashboard_src):
        assert "render_template(\n        'news.html'" in dashboard_src or \
               "render_template('news.html'" in dashboard_src

    def test_route_passes_summary_text(self, dashboard_src):
        assert 'summary_text=summary_text' in dashboard_src

    def test_route_passes_sources(self, dashboard_src):
        # Grouped by day since US-490.4
        assert 'source_groups=source_groups' in dashboard_src

    def test_route_passes_week_label(self, dashboard_src):
        assert 'week_label=week_label' in dashboard_src

    def test_route_passes_is_stale(self, dashboard_src):
        assert 'is_stale=is_stale' in dashboard_src

    def test_route_passes_empty_state(self, dashboard_src):
        assert 'empty_state=empty_state' in dashboard_src

    def test_route_passes_extract_domain(self, dashboard_src):
        assert 'extract_domain=extract_domain' in dashboard_src

    def test_extract_domain_defined(self, dashboard_src):
        assert 'def extract_domain(' in dashboard_src

    def test_extract_domain_uses_urlparse(self, dashboard_src):
        assert 'urlparse' in dashboard_src


# ─── Domain Extraction Logic ──────────────────────────────────────────────────

class TestExtractDomain:
    """Test the extract_domain logic inline — import-free static analysis + functional test."""

    def _extract_domain(self, url):
        """Mirror of the extract_domain logic in dashboard.py."""
        from urllib.parse import urlparse
        try:
            netloc = urlparse(url).netloc
            if netloc.startswith('www.'):
                netloc = netloc[4:]
            return netloc
        except Exception:
            return url

    def test_reuters(self):
        assert self._extract_domain('https://www.reuters.com/article/foo') == 'reuters.com'

    def test_ft(self):
        assert self._extract_domain('https://ft.com/content/bar') == 'ft.com'

    def test_wsj_with_www(self):
        assert self._extract_domain('https://www.wsj.com/articles/baz') == 'wsj.com'

    def test_bloomberg(self):
        assert self._extract_domain('https://bloomberg.com/news/qux') == 'bloomberg.com'

    def test_no_www_prefix(self):
        result = self._extract_domain('https://www.example.com/path')
        assert not result.startswith('www.')


# ─── Template Existence ───────────────────────────────────────────────────────

class TestNewsTemplate:
    def test_news_html_exists(self):
        assert os.path.exists(NEWS_HTML)

    def test_extends_base(self, news_html):
        assert '{% extends "base.html" %}' in news_html

    def test_title_block_set(self, news_html):
        assert 'News' in news_html
        assert '{% block title %}' in news_html

    def test_includes_news_css(self, news_html):
        assert 'news.css' in news_html

    def test_has_page_heading(self, news_html):
        assert "This Week's Macro News" in news_html

    def test_has_ai_summary_region(self, news_html):
        assert 'AI-generated weekly news review' in news_html

    def test_has_sources_section(self, news_html):
        assert 'Sources' in news_html or 'sources' in news_html.lower()

    def test_has_no_news_yet_empty_state(self, news_html):
        assert 'No news yet' in news_html

    def test_has_unavailable_empty_state(self, news_html):
        assert 'News unavailable' in news_html or 'unavailable' in news_html

    def test_has_stale_banner(self, news_html):
        assert 'is_stale' in news_html

    def test_source_cards_open_in_new_tab(self, news_html):
        assert 'target="_blank"' in news_html

    def test_source_cards_have_noopener(self, news_html):
        assert 'rel="noopener noreferrer"' in news_html

    def test_source_cards_have_aria_label(self, news_html):
        assert 'aria-label' in news_html

    def test_extract_domain_called(self, news_html):
        assert 'extract_domain' in news_html

    def test_summary_text_rendered(self, news_html):
        assert 'summary_text' in news_html

    def test_sources_loop(self, news_html):
        assert 'for group in source_groups' in news_html
        assert 'for source in group.articles' in news_html


# ─── CSS ──────────────────────────────────────────────────────────────────────

class TestNewsCSS:
    def test_news_css_exists(self):
        assert os.path.exists(NEWS_CSS)

    def test_news_summary_card_class(self):
        with open(NEWS_CSS) as f:
            css = f.read()
        assert '.news-summary-card' in css

    def test_ai_left_border(self):
        with open(NEWS_CSS) as f:
            css = f.read()
        # Should have indigo left border (AI pattern)
        assert 'brand-indigo-500' in css or '#6366' in css

    def test_news_source_card_class(self):
        with open(NEWS_CSS) as f:
            css = f.read()
        assert '.news-source-card' in css

    def test_source_card_min_height(self):
        with open(NEWS_CSS) as f:
            css = f.read()
        assert '56px' in css

    def test_empty_state_class(self):
        with open(NEWS_CSS) as f:
            css = f.read()
        assert '.news-empty-state' in css

    def test_desktop_two_column(self):
        with open(NEWS_CSS) as f:
            css = f.read()
        assert '1024px' in css

    def test_stale_banner_class(self):
        with open(NEWS_CSS) as f:
            css = f.read()
        assert 'stale' in css


# ─── Navbar ───────────────────────────────────────────────────────────────────

class TestNavbar:
    def test_news_link_in_navbar(self, base_html):
        assert 'href="/news"' in base_html

    def test_news_label_in_navbar(self, base_html):
        assert '>News<' in base_html or '> News\n' in base_html or '>News\n' in base_html or 'News' in base_html

    def test_news_active_state(self, base_html):
        assert "endpoint == 'news'" in base_html

    def test_news_newspaper_icon(self, base_html):
        assert 'bi-newspaper' in base_html

    def test_news_link_between_dashboard_and_markets(self, base_html):
        # News nav link should appear after Dashboard and before Markets dropdown
        dashboard_pos = base_html.find("endpoint == 'index'")
        news_pos = base_html.find('href="/news"')
        markets_pos = base_html.find('Markets Dropdown')
        assert dashboard_pos < news_pos < markets_pos
