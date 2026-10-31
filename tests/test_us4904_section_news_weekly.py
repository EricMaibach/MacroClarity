"""
Tests for US-490.4: section pages and news page weekly framing + copy sweep.

Spec: docs/specs/weekly-week-in-review.md ("Section page briefing",
"Page-by-page changes → Section pages / News page", "Global copy sweep",
§1, §2, §8).

Covers:
- The five section templates (title, comment, collapsible hooks, compact
  range, week label, loading/empty copy, stale notice, footer) and their
  delegation to the shared loader in static/js/components/week-review.js
- The shared renderer's section mode (<h3> labels, inline watch items) and
  the compact range, run in node under several browser timezones
- briefing_week.format_week_label / is_stale (server-rendered news page)
- news_display: paragraph split, day grouping (ET, duplicates, missing and
  malformed timestamps), safe hrefs
- /news rendered for each state, including XSS and javascript: URLs
- The global copy sweep
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import pytz

REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'
TEMPLATES = SIGNALTRACKERS_DIR / 'templates'
WEEK_REVIEW_JS = SIGNALTRACKERS_DIR / 'static' / 'js' / 'components' / 'week-review.js'
WEEK_REVIEW_CSS = SIGNALTRACKERS_DIR / 'static' / 'css' / 'components' / 'week-review.css'
NEWS_CSS = SIGNALTRACKERS_DIR / 'static' / 'css' / 'news.css'

if str(SIGNALTRACKERS_DIR) not in sys.path:
    sys.path.insert(0, str(SIGNALTRACKERS_DIR))

import briefing_week  # noqa: E402
import news_display  # noqa: E402

ET = pytz.timezone('America/New_York')

# prefix → (title word, loader function name)
SECTIONS = {
    'equity': ('Equity', 'loadEquitySummary'),
    'crypto': ('Bitcoin', 'loadCryptoSummary'),
    'rates': ('Rates', 'loadRatesSummary'),
    'dollar': ('Currency', 'loadDollarSummary'),
    'credit': ('Credit', 'loadCreditSummary'),
}


def _section_block(html):
    start = html.index('<!-- Collapsible Section: AI Week in Review -->')
    return html[start:html.index('<!-- Collapsible Section: Market Statistics -->', start)]


# ─── Section templates ─────────────────────────────────────────────────

@pytest.mark.parametrize('prefix', SECTIONS)
class TestSectionTemplate:
    def test_title(self, prefix):
        block = _section_block((TEMPLATES / f'{prefix}.html').read_text())
        title = SECTIONS[prefix][0]
        assert (f'<h2 class="collapsible-section__title"><i class="bi bi-chat-square-text-fill"></i> '
                f'{title} Week in Review (AI)</h2>') in block

    def test_old_titles_and_comments_gone(self, prefix):
        html = (TEMPLATES / f'{prefix}.html').read_text()
        assert 'AI Credit Analysis' not in html
        assert not re.search(r'Daily \w+ Briefing', html)
        assert 'AI Daily Briefing' not in html
        assert '<!-- Collapsible Section: AI Week in Review -->' in html

    def test_collapsible_hooks_unchanged(self, prefix):
        block = _section_block((TEMPLATES / f'{prefix}.html').read_text())
        assert '<div class="collapsible-section" data-collapsed="true" data-section-id="ai-briefing">' in block
        assert '<button class="collapsible-section__header" aria-expanded="false">' in block
        assert '<div class="collapsible-section__content" hidden>' in block

    def test_compact_range_in_header(self, prefix):
        block = _section_block((TEMPLATES / f'{prefix}.html').read_text())
        header = block[block.index('<button'):block.index('</button>')]
        range_tag = f'<time class="collapsible-section__range" id="{prefix}-review-range" hidden></time>'
        assert range_tag in header
        # Between the title and the chevron
        assert header.index('</h2>') < header.index(range_tag) < header.index('collapsible-section__icon')

    def test_week_label_first_in_body(self, prefix):
        block = _section_block((TEMPLATES / f'{prefix}.html').read_text())
        body = block[block.index('collapsible-section__content'):]
        week = f'<time class="briefing-week-label section-review__week" id="{prefix}-review-week" hidden></time>'
        assert week in body
        assert body.index(week) < body.index(f'id="{prefix}-summary-content"')

    def test_loading_and_empty_copy(self, prefix):
        block = _section_block((TEMPLATES / f'{prefix}.html').read_text())
        assert "Loading this week's review..." in block
        assert "No review yet. The next one publishes after Friday's close." in block
        assert 'Reload Data' not in block
        assert 'daily briefing' not in block.lower()
        assert 'credit analysis' not in block.lower()

    def test_stale_notice_above_footer(self, prefix):
        block = _section_block((TEMPLATES / f'{prefix}.html').read_text())
        stale = (f'<div class="briefing-stale-notice" id="{prefix}-review-stale" role="status" hidden>'
                 '⚠ This review is from an earlier week. A new one is on the way.</div>')
        assert stale in block
        footer = f'<div id="{prefix}-summary-timestamp" class="section-review__footer"></div>'
        assert footer in block
        assert block.index(stale) < block.index(footer)

    def test_loader_delegates_to_shared(self, prefix):
        html = (TEMPLATES / f'{prefix}.html').read_text()
        fn = SECTIONS[prefix][1]
        assert (f"function {fn}() {{\n    return loadSectionReview('{prefix}', '/api/{prefix}-summary');\n}}") in html
        assert f'{fn}();' in html  # still called on load
        # The old per-page renderer is gone
        assert 'Generated: ' not in html
        assert 'toLocaleTimeString' not in html[html.index(f'function {fn}'):][:400]

    def test_shared_assets_loaded(self, prefix):
        html = (TEMPLATES / f'{prefix}.html').read_text()
        assert "filename='css/components/week-review.css'" in html
        assert "<script src=\"{{ url_for('static', filename='js/components/week-review.js') }}\"></script>" in html
        # Loaded before the page script that calls loadSectionReview
        assert html.index('js/components/week-review.js') < html.index('loadSectionReview(')


def test_homepage_loads_shared_assets():
    html = (TEMPLATES / 'index.html').read_text()
    assert "filename='css/components/week-review.css'" in html
    assert html.index('js/components/week-review.js') < html.index('Week in Review loader (US-490.3)')


# ─── Shared renderer, section mode ─────────────────────────────────────

@pytest.fixture(scope='module')
def shared_js():
    return WEEK_REVIEW_JS.read_text()


class TestSharedRenderer:
    def test_section_options(self, shared_js):
        assert "const SECTION_REVIEW_OPTIONS = { partTag: 'h3', inlineWatch: true };" in shared_js
        assert 'buildStructuredReview(data.structured, SECTION_REVIEW_OPTIONS)' in shared_js

    def test_inline_watch_form(self, shared_js):
        fn = shared_js[shared_js.index('function makeInlineWatchItem'):shared_js.index('function buildStructuredReview')]
        assert "makeEl('strong', 'briefing-watch__day-inline')" in fn
        assert "' — ' + item.reason" in fn
        # Full day name for screen readers, abbreviation hidden from them
        assert "'visually-hidden', WATCH_DAY_NAMES[item.day] || item.day" in fn
        assert 'innerHTML' not in fn
        assert 'addEventListener' not in fn

    def test_inline_watch_stacks_without_rail(self, shared_js):
        fn = shared_js[shared_js.index('function buildStructuredReview'):shared_js.index('function applyWeekLabel')]
        assert "if (inlineWatch) {\n            main.appendChild(part);" in fn

    def test_structured_fields_never_html(self, shared_js):
        builder = shared_js[shared_js.index('function makeEl'):shared_js.index('function renderSectionMarkdown')]
        assert 'innerHTML' not in builder
        assert 'contentEl.replaceChildren(structured)' in shared_js

    def test_markdown_fallback_kept(self, shared_js):
        loader = shared_js[shared_js.index('async function loadSectionReview'):]
        assert "data.format === 'structured' && data.structured" in loader
        assert 'renderSectionMarkdown(contentEl, data.summary)' in loader
        assert 'marked.parse(summary)' in shared_js

    def test_empty_on_error_or_missing_summary(self, shared_js):
        loader = shared_js[shared_js.index('async function loadSectionReview'):]
        assert 'if (!response.ok || !data.summary || data.error) {' in loader
        assert 'showEmpty();' in loader

    def test_freshness_and_stale(self, shared_js):
        loader = shared_js[shared_js.index('async function loadSectionReview'):]
        assert 'footerEl.textContent = formatFreshness(data);' in loader
        assert 'staleEl.hidden = !isBriefingStale(data);' in loader

    def test_labels_hidden_without_week_fields(self, shared_js):
        fn = shared_js[shared_js.index('function applyWeekLabel'):shared_js.index('/* ─── Section pages')]
        assert 'el.hidden = !text;' in fn
        assert "el.removeAttribute('datetime');" in fn


class TestSectionCss:
    def test_range_hidden_below_768(self):
        css = WEEK_REVIEW_CSS.read_text()
        base = re.search(r'\n\.collapsible-section__range \{(.*?)\}', css, re.DOTALL).group(1)
        assert 'display: none' in base
        tablet = css[css.index('@media (min-width: 768px)'):]
        block = re.search(r'\.collapsible-section__range:not\(\[hidden\]\) \{(.*?)\}', tablet, re.DOTALL).group(1)
        assert 'display: inline' in block
        assert 'white-space: nowrap' in block

    def test_week_label_style(self):
        css = WEEK_REVIEW_CSS.read_text()
        block = re.search(r'\n\.briefing-week-label \{(.*?)\}', css, re.DOTALL).group(1)
        assert 'font-size: 0.875rem' in block and 'font-weight: 500' in block and '#697386' in block

    def test_no_undefined_space_tokens(self):
        # --space-* tokens are defined per page; the shared file uses literals.
        assert 'var(--space-' not in WEEK_REVIEW_CSS.read_text()


# ─── Shared formatters in node (browser timezone must not matter) ──────

NODE = shutil.which('node')

_RANGE_CASES = {
    'same_month': {'week_start': '2026-10-26', 'week_end': '2026-10-30', 'through': '2026-10-30'},
    'two_months': {'week_start': '2026-10-29', 'week_end': '2026-11-02'},
    'two_years': {'week_start': '2025-12-29', 'week_end': '2026-01-02'},
    'wtd': {'week_start': '2026-11-02', 'week_end': '2026-11-06', 'through': '2026-11-04', 'week_to_date': True},
    'wtd_monday': {'week_start': '2026-11-02', 'week_end': '2026-11-06', 'through': '2026-11-02', 'week_to_date': True},
    'legacy': {'week_start': None, 'week_end': None},
}


@pytest.mark.skipif(NODE is None, reason='node not installed')
@pytest.mark.parametrize('tz', ['America/Los_Angeles', 'America/New_York', 'Asia/Tokyo', 'UTC'])
def test_compact_range(tz):
    js = WEEK_REVIEW_JS.read_text()
    script = js[js.index('const BRIEFING_STALE_MS'):js.index('function makeEl(')] + (
        '\nconst cases = ' + json.dumps(_RANGE_CASES) + ';\nconst out = {};\n'
        'for (const [k, d] of Object.entries(cases)) out[k] = formatCompactRange(d);\n'
        'console.log(JSON.stringify(out));\n')
    res = subprocess.run([NODE, '-e', script], capture_output=True, text=True,
                         env=dict(os.environ, TZ=tz), timeout=30)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {
        'same_month': 'Oct 26–30',
        'two_months': 'Oct 29 – Nov 2',
        'two_years': 'Dec 29 – Jan 2',
        'wtd': 'Nov 2–4',
        'wtd_monday': 'Nov 2',
        'legacy': None,
    }


# ─── Server-side week label and stale rule ─────────────────────────────

class TestServerWeekLabel:
    @pytest.mark.parametrize('fields,expected', [
        ({'week_start': '2026-10-26', 'week_end': '2026-10-30'}, 'Week of Oct 26–30, 2026'),
        ({'week_start': '2026-10-29', 'week_end': '2026-11-02'}, 'Week of Oct 29 – Nov 2, 2026'),
        ({'week_start': '2025-12-29', 'week_end': '2026-01-02'}, 'Week of Dec 29, 2025 – Jan 2, 2026'),
        ({'week_start': '2026-11-02', 'week_end': '2026-11-06', 'through': '2026-11-04', 'week_to_date': True},
         'Week to date · through Wed, Nov 4'),
        ({'week_start': None, 'week_end': None}, None),
        ({'week_start': 'garbage', 'week_end': '2026-10-30'}, None),
        (None, None),
    ])
    def test_label(self, fields, expected):
        assert briefing_week.format_week_label(fields) == expected

    def test_matches_display_fields_for_scheduled_record(self):
        fields = briefing_week.display_fields(
            {'week_start': '2026-10-26', 'week_end': '2026-10-30', 'through': '2026-10-30',
             'published_at': '2026-10-30T18:05:00-04:00', 'run_type': 'scheduled'},
            now=ET.localize(datetime(2026, 11, 4, 12)))
        assert briefing_week.format_week_label(fields) == 'Week of Oct 26–30, 2026'

    def test_stale_boundary(self):
        published = ET.localize(datetime(2026, 10, 30, 18, 5))
        fields = {'published_at': published.isoformat()}
        assert not briefing_week.is_stale(fields, now=published + timedelta(days=6))       # next Thursday
        assert not briefing_week.is_stale(fields, now=published + timedelta(days=8, minutes=-1))
        assert briefing_week.is_stale(fields, now=published + timedelta(days=8, minutes=1))
        assert not briefing_week.is_stale({'published_at': None})
        assert not briefing_week.is_stale({'published_at': 'not a date'})
        assert not briefing_week.is_stale(None)


# ─── news_display ──────────────────────────────────────────────────────

class TestSummaryParagraphs:
    @pytest.mark.parametrize('text,expected', [
        ('One paragraph only.', ['One paragraph only.']),
        ('A.\n\nB.\n\nC.\n\nD.', ['A.', 'B.', 'C.', 'D.']),
        ('A.\r\n\r\nB.', ['A.', 'B.']),
        ('A.\n\n\n\nB.\n \n\nC.', ['A.', 'B.', 'C.']),
        ('Line one\nline two', ['Line one\nline two']),
        ('', []),
        (None, []),
        ('\n\n\n', []),
    ])
    def test_split(self, text, expected):
        assert news_display.summary_paragraphs(text) == expected


def _art(headline, timestamp, url=None):
    return {'headline': headline, 'url': url or f'https://example.com/{headline}', 'source': 'example.com',
            'timestamp': timestamp, 'topic': 'macro'}


class TestGroupSourcesByDay:
    def test_days_newest_first_order_kept_within_day(self):
        arts = [
            _art('fri-1', '2026-10-30T14:00:00+00:00'),
            _art('thu-1', '2026-10-29T14:00:00+00:00'),
            _art('fri-2', '2026-10-30T13:00:00+00:00'),
            _art('mon-1', '2026-10-26T14:00:00+00:00'),
        ]
        groups = news_display.group_sources_by_day(arts)
        assert [g['label'] for g in groups] == ['Friday, Oct 30', 'Thursday, Oct 29', 'Monday, Oct 26']
        assert [a['headline'] for a in groups[0]['articles']] == ['fri-1', 'fri-2']
        # Days with no articles (Tue, Wed) get no group
        assert len(groups) == 3

    def test_missing_and_malformed_timestamps_go_last(self):
        arts = [
            _art('none', None),
            _art('bad', 'yesterday-ish'),
            _art('fri', '2026-10-30T14:00:00+00:00'),
            {'headline': 'no-ts-key', 'url': 'https://example.com/x'},
        ]
        groups = news_display.group_sources_by_day(arts)
        assert [g['label'] for g in groups] == ['Friday, Oct 30', 'Earlier this week']
        assert groups[-1]['date'] is None
        assert [a['headline'] for a in groups[-1]['articles']] == ['none', 'bad', 'no-ts-key']

    def test_utc_et_boundary(self):
        # Friday 20:30 EST is Saturday 01:30 UTC: it belongs under Friday.
        arts = [_art('late-fri', '2026-12-05T01:30:00+00:00')]
        groups = news_display.group_sources_by_day(arts)
        assert groups[0]['label'] == 'Friday, Dec 4'
        assert groups[0]['date'] == '2026-12-04'
        # Z suffix and naive (read as UTC) timestamps too
        assert news_display.group_sources_by_day([_art('z', '2026-12-05T01:30:00Z')])[0]['label'] == 'Friday, Dec 4'
        assert news_display.group_sources_by_day([_art('n', '2026-12-05T01:30:00')])[0]['label'] == 'Friday, Dec 4'

    def test_duplicate_url_kept_once_under_earliest_day(self):
        arts = [
            _art('story', '2026-10-30T14:00:00+00:00', url='https://example.com/same'),
            _art('other', '2026-10-30T15:00:00+00:00'),
            _art('story', '2026-10-28T14:00:00+00:00', url='https://example.com/same'),
            _art('story', None, url='https://example.com/same'),
        ]
        groups = news_display.group_sources_by_day(arts)
        cards = [(g['label'], a['headline']) for g in groups for a in g['articles']]
        assert cards == [('Friday, Oct 30', 'other'), ('Wednesday, Oct 28', 'story')]
        assert news_display.source_count(groups) == 2

    def test_duplicate_by_headline_when_no_url(self):
        arts = [{'headline': 'H', 'url': None, 'timestamp': '2026-10-30T14:00:00+00:00'},
                {'headline': 'H', 'url': None, 'timestamp': '2026-10-29T14:00:00+00:00'}]
        groups = news_display.group_sources_by_day(arts)
        assert news_display.source_count(groups) == 1
        assert groups[0]['label'] == 'Thursday, Oct 29'

    def test_empty_and_junk(self):
        assert news_display.group_sources_by_day(None) == []
        assert news_display.group_sources_by_day([]) == []
        assert news_display.group_sources_by_day(['not a dict', None]) == []

    @pytest.mark.parametrize('url,href', [
        ('https://example.com/a', 'https://example.com/a'),
        ('HTTP://example.com/a', 'HTTP://example.com/a'),
        ('javascript:alert(1)', None),
        ('  javascript:alert(1)', None),
        ('data:text/html,<script>', None),
        ('/relative', None),
        (None, None),
    ])
    def test_safe_href(self, url, href):
        assert news_display.safe_href(url) == href


# ─── /news rendered ────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def client():
    from dashboard import app
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _scheduled_review(published, articles, summary='Para one.\n\nPara two.'):
    week_start = (published - timedelta(days=4)).date().isoformat()
    return {
        'date': published.date().isoformat(), 'fetched_at': published.isoformat(),
        'articles': articles, 'summary': summary, 'topic_summaries': {},
        'week_start': week_start, 'week_end': published.date().isoformat(),
        'through': published.date().isoformat(), 'published_at': published.isoformat(),
        'run_type': 'scheduled',
    }


def _get_news(client, stored, now, has_review=True):
    with patch('news_pipeline.get_stored_news', return_value=stored), \
         patch('news_pipeline.has_published_review', return_value=has_review), \
         patch('briefing_week.now_et', return_value=now):
        resp = client.get('/news')
    assert resp.status_code == 200
    return resp.data.decode()


FRIDAY = ET.localize(datetime(2026, 10, 30, 18, 5))
ARTICLES = [
    _art('Fed holds', '2026-10-30T14:00:00+00:00'),
    _art('CPI cools', '2026-10-29T14:00:00+00:00'),
    _art('Fed holds', '2026-10-28T14:00:00+00:00', url='https://example.com/Fed holds'),
    _art('Late Friday', '2026-10-31T00:30:00+00:00'),
]


class TestNewsPage:
    def test_normal_week(self, client):
        html = _get_news(client, _scheduled_review(FRIDAY, ARTICLES), now=FRIDAY + timedelta(days=5))
        assert "This Week's Macro News" in html
        assert '<time class="news-header__date" datetime="2026-10-26/2026-10-30">Week of Oct 26–30, 2026</time>' in html
        assert re.search(r'</svg>\s*Week in Review\s*</h2>', html)
        assert html.count('<p>Para one.</p>') == 1 and '<p>Para two.</p>' in html
        # 3 cards after de-duplication; footer and SOURCES count agree
        assert html.count('class="news-source-card"') == 3
        assert 'Generated from 3 sources this week' in html
        assert 'Sources (3)' in html
        # Grouped by ET day, newest first; the 00:30 UTC Sat fetch is Friday ET
        days = re.findall(r'<h3 class="news-sources__day">([^<]+)</h3>', html)
        assert days == ['Friday, Oct 30', 'Thursday, Oct 29', 'Wednesday, Oct 28']
        assert 'news-stale-banner' not in html

    def test_mid_week_no_stale_banner(self, client):
        # Wednesday: get_stored_news returns last Friday's review (US-490.1 lookup)
        html = _get_news(client, _scheduled_review(FRIDAY, ARTICLES), now=FRIDAY + timedelta(days=5, hours=14))
        assert 'Week of Oct 26–30, 2026' in html
        assert 'news-stale-banner' not in html
        assert 'news-empty-state' not in html

    def test_stale_after_missed_friday(self, client):
        html = _get_news(client, _scheduled_review(FRIDAY, ARTICLES), now=FRIDAY + timedelta(days=9))
        assert ('<div class="news-stale-banner" role="status">' in html)
        assert "Showing the review for Week of Oct 26–30, 2026 — this week's review is on the way." in html

    def test_singular_source(self, client):
        html = _get_news(client, _scheduled_review(FRIDAY, ARTICLES[:1]), now=FRIDAY)
        assert 'Generated from 1 source this week' in html

    def test_empty_never_run(self, client):
        html = _get_news(client, None, now=FRIDAY, has_review=False)
        assert 'No news yet' in html
        assert "The first weekly news review hasn't been published yet.<br>" in html
        assert "It arrives after Friday's close." in html
        assert 'news-stale-banner' not in html

    def test_empty_no_prior_data(self, client):
        html = _get_news(client, None, now=FRIDAY, has_review=True)
        assert 'News unavailable' in html
        assert "We couldn't load this week's news and have no earlier review to show.<br>" in html
        assert 'The pipeline retries automatically.' in html

    def test_daily_record_without_summary_is_empty_state(self, client):
        stored = {'date': '2026-11-04', 'fetched_at': '2026-11-04T08:00:00-05:00',
                  'articles': ARTICLES, 'summary': None}
        html = _get_news(client, stored, now=FRIDAY, has_review=False)
        assert 'No news yet' in html
        assert 'news-summary-card' not in html

    def test_legacy_record_has_no_none_label(self, client):
        stored = {'date': '2026-10-27', 'fetched_at': 'garbage', 'articles': [], 'summary': 'Old.'}
        html = _get_news(client, stored, now=FRIDAY)
        assert 'None' not in re.sub(r'<script.*?</script>', '', html, flags=re.DOTALL)
        assert 'undefined' not in html.split('news-page')[1]

    def test_xss_rendered_as_text(self, client):
        payload = '<img src=x onerror=alert(1)>'
        arts = [{'headline': payload, 'url': 'https://example.com/a', 'source': payload,
                 'timestamp': '2026-10-30T14:00:00+00:00'}]
        html = _get_news(client, _scheduled_review(FRIDAY, arts, summary=payload + '\n\n' + payload), now=FRIDAY)
        assert payload not in html
        assert '&lt;img src=x onerror=alert(1)&gt;' in html

    def test_javascript_url_not_a_link(self, client):
        arts = [{'headline': 'Bad link', 'url': 'javascript:alert(1)', 'source': 'evil',
                 'timestamp': '2026-10-30T14:00:00+00:00'}]
        html = _get_news(client, _scheduled_review(FRIDAY, arts), now=FRIDAY)
        assert 'javascript:alert' not in html
        assert 'news-source-card news-source-card--no-link' in html
        assert 'Bad link' in html


class TestNewsTemplate:
    def test_heading_outline(self):
        html = (TEMPLATES / 'news.html').read_text()
        assert html.index('<h1') < html.index('<h2 class="news-sources__label">') < html.index('<h3 class="news-sources__day">')

    def test_day_heading_css(self):
        css = NEWS_CSS.read_text()
        block = re.search(r'\.news-sources__day \{(.*?)\}', css, re.DOTALL).group(1)
        assert 'text-transform: uppercase' in block
        assert 'font-weight: 600' in block
        assert 'var(--text-xs)' in block
        assert 'sticky' not in block


# ─── Copy sweep ────────────────────────────────────────────────────────

_SWEEP = re.compile(r"daily briefing|today's briefing|daily .* briefing", re.IGNORECASE)
# Email templates and the settings card are US-490.5.
_SWEEP_LATER = ('email/', 'settings_alerts.html')


def _visible(text):
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    return re.sub(r'\{#.*?#\}', '', text, flags=re.DOTALL)


def test_template_sweep_has_no_daily_briefing_copy():
    hits = []
    for path in TEMPLATES.rglob('*.html'):
        rel = path.relative_to(TEMPLATES).as_posix()
        if rel.startswith(_SWEEP_LATER[0]) or rel == _SWEEP_LATER[1]:
            continue
        for n, line in enumerate(_visible(path.read_text()).splitlines(), 1):
            if _SWEEP.search(line):
                hits.append(f'{rel}:{n}: {line.strip()}')
    assert hits == []


def test_base_meta_descriptions():
    html = (TEMPLATES / 'base.html').read_text()
    assert html.count('content="Weekly macro intelligence briefings for individual investors. Track credit,') == 2
    assert 'Daily macro intelligence' not in html
    assert 'Data updated daily' in html  # data cadence copy is unchanged


def test_reload_toast_is_data_only():
    html = (TEMPLATES / 'base.html').read_text()
    assert "showToast('Refresh Complete', 'Market data updated.', true);" in html
    assert 'AI briefings updated' not in html


def test_portfolio_copy():
    html = (TEMPLATES / 'portfolio.html').read_text()
    assert "and this week's review to evaluate your allocation." in html
    assert "today's briefings" not in html


def test_homepage_movers_unchanged():
    assert "TODAY'S MOVERS" in (TEMPLATES / 'index.html').read_text()


def test_chatbot_opening_for_review_is_weekly():
    js = (SIGNALTRACKERS_DIR / 'static' / 'js' / 'components' / 'ai-section-btn.js').read_text()
    entry = js[js.index("'briefing-section': {"):js.index('},', js.index("'briefing-section': {"))]
    assert "name: 'Week in Review'" in entry
    assert 'daily' not in entry.lower() and "today's" not in entry.lower()
