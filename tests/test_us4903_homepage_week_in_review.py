"""
Tests for US-490.3: Homepage Week in Review card.

Covers:
- Section copy: title, aria-labels, template comment, loading/error copy
- Week label is a <time> element filled from server week fields
- Old ">24h → Briefing may be outdated" logic removed; 8-day stale notice
- Structured renderer builds DOM with textContent only (no innerHTML from fields)
- Markdown fallback path kept
- Mobile tap-to-explore tokenizes <li> and watch-item text separately
- Date formatting under several browser timezones (runs the page's own JS in node)
- Data blocks keep their daily wording
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'
INDEX_HTML = SIGNALTRACKERS_DIR / 'templates' / 'index.html'
MOBILE_JS = SIGNALTRACKERS_DIR / 'static' / 'js' / 'components' / 'ai-briefing-mobile.js'
SUMMARY_CSS = SIGNALTRACKERS_DIR / 'static' / 'css' / 'components' / 'conditions-summary.css'
DASHBOARD_CSS = SIGNALTRACKERS_DIR / 'static' / 'css' / 'dashboard.css'


@pytest.fixture(scope='module')
def index_html():
    return INDEX_HTML.read_text()


@pytest.fixture(scope='module')
def briefing_section(index_html):
    start = index_html.index('<section id="briefing-section"')
    return index_html[start:index_html.index('</section>', start)]


@pytest.fixture(scope='module')
def loader_js(index_html):
    start = index_html.index('Week in Review loader (US-490.3)')
    return index_html[start:index_html.index('Dashboard data', start)]


@pytest.fixture(scope='module')
def summary_css():
    return SUMMARY_CSS.read_text()


# ─── Section copy ──────────────────────────────────────────────────────

class TestSectionCopy:
    def test_aria_label(self, briefing_section):
        assert 'aria-label="Week in Review"' in briefing_section

    def test_title(self, briefing_section):
        assert 'WEEK IN REVIEW' in briefing_section
        assert 'DAILY BRIEFING' not in briefing_section

    def test_icon_unchanged(self, briefing_section):
        assert 'bi-newspaper' in briefing_section

    def test_ai_button_label(self, briefing_section):
        assert 'aria-label="Ask AI about this week\'s review"' in briefing_section
        assert 'Ask AI about Daily Briefing' not in briefing_section

    def test_ai_button_still_login_gated(self, briefing_section):
        assert re.search(r'{% if current_user.is_authenticated %}<button class="btn ai-section-btn"',
                         briefing_section)

    def test_template_comment(self, index_html):
        assert '§0: WEEK IN REVIEW — the weekly draw' in index_html
        assert 'the daily draw' not in index_html

    def test_attribution(self, briefing_section):
        assert "AI-generated from this week's market data" in briefing_section
        assert 'based on current market data' not in briefing_section

    def test_loading_copy(self, briefing_section):
        assert "Loading this week's review..." in briefing_section
        assert "today's briefing" not in briefing_section

    def test_extra_skeleton_line(self, briefing_section):
        loading = briefing_section[briefing_section.index('id="briefing-loading"'):
                                   briefing_section.index('id="briefing-content"')]
        assert loading.count('skeleton-line-full') == 2
        assert loading.count('skeleton-line-75') == 2

    def test_error_copy(self, briefing_section, loader_js):
        copy = "This week's review isn't ready yet. Check back shortly."
        assert copy in briefing_section
        assert 'BRIEFING_ERROR_COPY = "' + copy + '"' in loader_js
        assert 'being generated' not in briefing_section + loader_js

    def test_error_ignores_api_error_text(self, loader_js):
        # The 404 body says "No summary available"; the card shows spec copy.
        assert 'data.error ||' not in loader_js


# ─── Week label and freshness ──────────────────────────────────────────

class TestWeekLabel:
    def test_label_is_time_element(self, briefing_section):
        assert re.search(r'<time class="[^"]*briefing-week-label[^"]*" id="briefing-date" hidden>',
                         briefing_section)

    def test_datetime_built_from_server_fields(self, loader_js):
        assert "setAttribute('datetime', data.week_start + '/' + data.week_end)" in loader_js

    def test_label_never_from_browser_date(self, loader_js):
        # No new Date() / toLocaleDateString to build the label or footer dates.
        assert 'new Date()' not in loader_js
        assert 'toLocaleDateString' not in loader_js
        assert 'toLocaleTimeString' not in loader_js
        assert 'new Date(data.' not in loader_js

    def test_label_hidden_without_week_fields(self, loader_js):
        assert 'dateEl.hidden = !label' in loader_js


class TestFreshness:
    def test_old_outdated_warning_removed(self, index_html):
        assert 'Briefing may be outdated' not in index_html
        assert 'briefing-outdated-warning' not in index_html
        assert 'hoursDiff' not in index_html
        assert 'briefing-outdated-warning' not in DASHBOARD_CSS.read_text()

    def test_generated_timestamp_removed(self, index_html):
        assert 'Generated: ' not in index_html

    def test_stale_notice_markup(self, briefing_section):
        m = re.search(r'<div class="briefing-stale-notice" id="briefing-stale" role="status" hidden>(.*?)</div>',
                      briefing_section)
        assert m
        assert m.group(1) == '⚠ This review is from an earlier week. A new one is on the way.'
        # No Retry button inside the notice
        assert 'button' not in m.group(1)

    def test_stale_notice_sits_above_footer(self, briefing_section):
        assert briefing_section.index('id="briefing-stale"') < briefing_section.index('conditions-briefing__footer')

    def test_stale_threshold_is_eight_days(self, loader_js):
        assert 'BRIEFING_STALE_MS = 8 * 24 * 60 * 60 * 1000' in loader_js

    def test_stale_notice_styles(self, summary_css):
        block = re.search(r'\.briefing-stale-notice \{(.*?)\}', summary_css, re.DOTALL).group(1)
        assert '#FEF3C7' in block
        assert '#92400E' in block
        assert 'border-radius: 6px' in block
        assert 'padding: 8px 12px' in block


# ─── Structured rendering ──────────────────────────────────────────────

class TestStructuredRenderer:
    def test_part_labels_sentence_case_h4(self, loader_js):
        assert "makeEl('h4', 'briefing-part__label', label)" in loader_js
        for label in ('What changed', 'Why it matters', 'What to watch next week'):
            assert "makePart('" + label + "')" in loader_js

    def test_headline_is_paragraph(self, loader_js):
        assert "makeEl('p', 'briefing-headline', s.headline)" in loader_js

    def test_lists(self, loader_js):
        assert "makeEl('ul', 'briefing-changed')" in loader_js
        assert "makeEl('ol', 'briefing-watch')" in loader_js

    def test_watch_chip_has_full_day_name(self, loader_js):
        assert "makeEl('span', 'briefing-watch__day')" in loader_js
        assert "Wed: 'Wednesday'" in loader_js
        assert "'visually-hidden'" in loader_js

    def test_ongoing_chip(self, loader_js):
        assert "chip.setAttribute('aria-label', 'Ongoing')" in loader_js
        assert "'—'" in loader_js

    def test_watch_items_not_interactive(self, loader_js):
        builder = loader_js[loader_js.index('function makeWatchItem'):loader_js.index('function buildStructuredReview')]
        assert 'addEventListener' not in builder
        assert "'a'" not in builder and "'button'" not in builder

    def test_fields_never_parsed_as_html(self, loader_js):
        builder = loader_js[loader_js.index('function makeEl'):loader_js.index('function renderMarkdownBriefing')]
        assert 'innerHTML' not in builder
        assert 'textContent' in builder

    def test_rendered_inside_narrative(self, loader_js):
        assert 'narrativeEl.replaceChildren(structured)' in loader_js

    def test_markdown_fallback_kept(self, loader_js):
        assert "data.format === 'structured' && data.structured" in loader_js
        assert 'renderMarkdownBriefing(narrativeEl, data.summary)' in loader_js
        assert 'marked.parse(summary)' in loader_js

    def test_rail_only_when_both_columns_have_content(self, loader_js):
        assert "hasMain && rail ? ' briefing-review--with-rail' : ''" in loader_js


class TestLayoutCss:
    def test_headline_typography(self, summary_css):
        block = re.search(r'\.conditions-briefing__narrative \.briefing-headline \{(.*?)\}',
                          summary_css, re.DOTALL).group(1)
        assert 'font-family: var(--font-interface)' in block
        assert 'font-size: 1.125rem' in block
        assert 'font-weight: 600' in block

    def test_label_uppercase_via_css(self, summary_css):
        block = re.search(r'\.briefing-part__label \{(.*?)\}', summary_css, re.DOTALL).group(1)
        assert 'text-transform: uppercase' in block
        assert '#697386' in block

    def test_chip_colors(self, summary_css):
        block = re.search(r'\.briefing-watch__day \{(.*?)\}', summary_css, re.DOTALL).group(1)
        assert '#DBEAFE' in block and '#1E40AF' in block
        assert 'flex: 0 0 44px' in block
        assert 'min-height: 28px' in block

    def test_two_column_grid_at_1024(self, summary_css):
        desktop = summary_css[summary_css.index('@media (min-width: 1024px)'):]
        assert 'grid-template-columns: minmax(0, 62fr) minmax(0, 38fr)' in desktop
        assert 'border-left: 1px solid #E8ECEF' in desktop

    def test_label_shrinks_in_header_row(self, summary_css):
        tablet = summary_css[summary_css.index('@media (min-width: 768px)'):]
        block = re.search(r'\.conditions-briefing__header \.briefing-week-label \{(.*?)\}',
                          tablet, re.DOTALL).group(1)
        assert 'min-width: 0' in block


# ─── Mobile tap-to-explore ─────────────────────────────────────────────

class TestMobileTap:
    def test_tokenizes_list_items_and_watch_text(self):
        js = MOBILE_JS.read_text()
        assert "TEXT_BLOCK_SELECTOR = 'p, li, .briefing-watch__event, .briefing-watch__reason'" in js
        # Containers of other text blocks (the watch <li> with its chip) are skipped
        assert '!el.querySelector(TEXT_BLOCK_SELECTOR)' in js


# ─── Page render ───────────────────────────────────────────────────────

class TestRender:
    def test_homepage_renders_week_in_review(self):
        if str(SIGNALTRACKERS_DIR) not in sys.path:
            sys.path.insert(0, str(SIGNALTRACKERS_DIR))
        from unittest.mock import patch
        from dashboard import app
        with patch('dashboard.get_market_conditions', return_value=None), \
             patch('dashboard.get_conditions_history', return_value={}), \
             patch('dashboard.get_recession_probability', return_value=None), \
             app.test_client() as c:
            html = c.get('/').data.decode()
        assert 'WEEK IN REVIEW' in html
        assert 'id="briefing-narrative"' in html
        assert 'id="briefing-stale"' in html
        # Data blocks keep their daily wording
        assert "TODAY'S MOVERS" in html


# ─── Date formatting (the page's own JS, run in node) ──────────────────

NODE = shutil.which('node')

_CASES = [
    ('scheduled', {'week_start': '2026-10-26', 'week_end': '2026-10-30', 'through': '2026-10-30',
                   'published_at': '2026-10-30T17:45:00-04:00', 'next_review_at': '2026-11-06T18:00:00-05:00',
                   'run_type': 'scheduled', 'week_to_date': False}),
    ('months', {'week_start': '2026-06-29', 'week_end': '2026-07-03', 'through': '2026-07-03',
                'run_type': 'scheduled', 'week_to_date': False}),
    ('months_spec', {'week_start': '2026-10-29', 'week_end': '2026-11-02', 'run_type': 'scheduled'}),
    ('years', {'week_start': '2025-12-29', 'week_end': '2026-01-02', 'through': '2026-01-02',
               'run_type': 'scheduled', 'week_to_date': False}),
    ('wtd', {'week_start': '2026-11-02', 'week_end': '2026-11-06', 'through': '2026-11-04',
             'published_at': '2026-11-04T10:00:00-05:00', 'next_review_at': '2026-11-06T18:00:00-05:00',
             'run_type': 'manual', 'week_to_date': True}),
    ('legacy_empty', {'week_start': None, 'week_end': None, 'published_at': None, 'next_review_at': None}),
]


def _run_formatters(tz):
    js = INDEX_HTML.read_text()
    start = js.index('const BRIEFING_ERROR_COPY')
    end = js.index('function makeEl(')
    script = js[start:end] + '\nconst cases = ' + json.dumps(dict(_CASES)) + ''';
const out = {};
for (const [k, d] of Object.entries(cases)) {
    out[k] = { label: formatWeekLabel(d), fresh: formatFreshness(d) };
}
console.log(JSON.stringify(out));
'''
    env = dict(os.environ, TZ=tz)
    res = subprocess.run([NODE, '-e', script], capture_output=True, text=True, env=env, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.skipif(NODE is None, reason='node not installed')
@pytest.mark.parametrize('tz', ['America/Los_Angeles', 'America/New_York', 'Asia/Tokyo', 'UTC'])
class TestDateFormatting:
    def test_formats_identical_in_every_timezone(self, tz):
        out = _run_formatters(tz)
        assert out['scheduled']['label'] == 'Week of Oct 26–30, 2026'
        assert out['scheduled']['fresh'] == 'Published Fri, Oct 30 · Next review Fri, Nov 6'
        assert out['months']['label'] == 'Week of Jun 29 – Jul 3, 2026'
        assert out['months_spec']['label'] == 'Week of Oct 29 – Nov 2, 2026'
        assert out['years']['label'] == 'Week of Dec 29, 2025 – Jan 2, 2026'
        assert out['wtd']['label'] == 'Week to date · through Wed, Nov 4'
        assert out['wtd']['fresh'] == 'Updated Wed, Nov 4 · Next review Fri, Nov 6'
        assert out['legacy_empty']['label'] is None
        assert out['legacy_empty']['fresh'] == ''


@pytest.mark.skipif(NODE is None, reason='node not installed')
def test_stale_boundary():
    js = INDEX_HTML.read_text()
    script = js[js.index('const BRIEFING_ERROR_COPY'):js.index('function makeEl(')] + '''
const pub = Date.parse('2026-10-30T17:45:00-04:00');
const at = (ms) => { Date.now = () => ms; return isBriefingStale({ published_at: '2026-10-30T17:45:00-04:00' }); };
const DAY = 86400000, MIN = 60000;
console.log(JSON.stringify([at(pub + 6 * DAY), at(pub + 8 * DAY - MIN), at(pub + 8 * DAY + MIN),
                            isBriefingStale({ published_at: null })]));
'''
    res = subprocess.run([NODE, '-e', script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == [False, False, True, False]
