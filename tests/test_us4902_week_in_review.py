"""
US-490.2: Week-in-review prompts and structured briefing output.

Covers the content-contract parser (valid, missing parts, malformed,
truncated, day normalisation, limits, unicode/markup), the holiday-correct
full-week inputs, the prompts built through the real generator code paths
(weekly framing, no "today"/"daily", audience rules, sections fed into the
general review), storage of structured parts next to the raw markdown, the
display APIs for new and pre-change records, and the weekly news prompts.
"""

import json
import re
import shutil
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SIGNALTRACKERS_DIR = Path(__file__).parent.parent / 'signaltrackers'
sys.path.insert(0, str(SIGNALTRACKERS_DIR))

import pandas as pd
import pytest

import ai_summary
import briefing_week
import news_pipeline
import weekly_review
from weekly_review import GENERAL, SECTION, parse_review

ET = briefing_week.ET
BANNED = re.compile(r'\b(today|daily)\b', re.IGNORECASE)


def et(*args):
    return ET.localize(datetime(*args))


VALID_GENERAL = """Inflation cooled more than expected while long yields fell, easing pressure on rate-sensitive assets.

## What changed
- CPI: rose 0.1% in September, below the 0.3% forecast.
- 10-year yield: fell 18 bp to 4.02% over the week.
- High-yield spreads: were flat at 3.10%.

## Why it matters
Two soft inflation prints in a row make a December cut more likely.

That eases the squeeze on rate-sensitive stocks.

## What to watch next week
- Wed | FOMC decision | First meeting since the soft CPI print
- Fri | October payrolls | Tests the cooling-labor story
"""

VALID_SECTION = """## What changed
- 10-year yield: fell 18 bp to 4.02%.
- 2s10s: steepened to +31 bp.

## Why it matters
Falling long yields after a soft CPI ease financial conditions.

## What to watch next week
- Wed | FOMC decision | Sets the path for front-end yields
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TestParserValid:
    def test_general_all_parts(self):
        s, warnings = parse_review(VALID_GENERAL, GENERAL)
        assert s['headline'].startswith('Inflation cooled')
        assert len(s['changed']) == 3
        assert s['changed'][1] == '10-year yield: fell 18 bp to 4.02% over the week.'
        assert s['why'] == ('Two soft inflation prints in a row make a December cut more likely.'
                            '\n\nThat eases the squeeze on rate-sensitive stocks.')
        assert s['watch'] == [
            {'day': 'Wed', 'event': 'FOMC decision', 'reason': 'First meeting since the soft CPI print'},
            {'day': 'Fri', 'event': 'October payrolls', 'reason': 'Tests the cooling-labor story'},
        ]
        assert warnings == []

    def test_section_has_no_headline(self):
        s, warnings = parse_review(VALID_SECTION, SECTION)
        assert 'headline' not in s
        assert len(s['changed']) == 2 and len(s['watch']) == 1
        assert warnings == []

    def test_heading_variants(self):
        text = VALID_GENERAL.replace('## What changed', '**What changed:**') \
                            .replace('## Why it matters', '### Why It Matters') \
                            .replace('## What to watch next week', '## What to watch')
        s, _ = parse_review(text, GENERAL)
        assert s is not None and len(s['watch']) == 2

    def test_wrapped_bullet_continuation(self):
        text = VALID_SECTION.replace('- 2s10s: steepened to +31 bp.', '- 2s10s: steepened\n  to +31 bp.')
        s, _ = parse_review(text, SECTION)
        assert s['changed'][1] == '2s10s: steepened to +31 bp.'

    def test_bold_markers_stripped(self):
        text = VALID_SECTION.replace('- Wed | FOMC decision', '- **Wed** | **FOMC decision**')
        s, _ = parse_review(text, SECTION)
        assert s['watch'][0]['day'] == 'Wed' and s['watch'][0]['event'] == 'FOMC decision'

    def test_loose_watch_form(self):
        text = VALID_SECTION.replace('- Wed | FOMC decision | Sets the path for front-end yields',
                                     '- Wednesday: FOMC decision — sets the path for front-end yields')
        s, _ = parse_review(text, SECTION)
        assert s['watch'] == [{'day': 'Wed', 'event': 'FOMC decision',
                               'reason': 'sets the path for front-end yields'}]

    def test_bold_headline_kept(self):
        first = VALID_GENERAL.split('\n')[0]
        s, _ = parse_review(VALID_GENERAL.replace(first, f'**{first}**'), GENERAL)
        assert s['headline'] == first

    def test_stray_title_above_headline(self):
        s, warnings = parse_review('# Week in Review\n\n' + VALID_GENERAL, GENERAL)
        assert s['headline'].startswith('Inflation cooled')
        assert any('title' in w for w in warnings)

    def test_unknown_heading_ignored(self):
        text = VALID_SECTION + '\n## Bonus thoughts\nSomething extra.\n'
        s, warnings = parse_review(text, SECTION)
        assert s is not None
        assert all('Something extra' not in w for w in s['why'].split('\n'))
        assert any('unknown heading' in w for w in warnings)


class TestParserFallback:
    """Anything that cannot be split into every required part returns None,
    so the caller serves the raw markdown exactly as before."""

    @pytest.mark.parametrize('text', [
        'Markets were mixed this week. Yields fell and stocks rose.\n\nMore prose here.',
        '{"headline": "Inflation cooled", "changed": ["CPI rose 0.1%"',
        '## Summary\nStuff\n## Outlook\nMore stuff',
        '',
        None,
    ])
    def test_malformed(self, text):
        s, warnings = parse_review(text, GENERAL)
        assert s is None and warnings

    def test_missing_watch_list(self):
        text = VALID_GENERAL.split('## What to watch next week')[0]
        s, warnings = parse_review(text, GENERAL)
        assert s is None and any('What to watch' in w for w in warnings)

    def test_missing_headline_general(self):
        text = VALID_GENERAL.split('\n', 2)[2]
        s, warnings = parse_review(text, GENERAL)
        assert s is None and 'missing headline' in warnings

    def test_missing_why(self):
        text = re.sub(r'## Why it matters.*?(?=## What to watch)', '', VALID_SECTION, flags=re.S)
        assert parse_review(text, SECTION)[0] is None

    def test_truncated_never_structured(self):
        s, warnings = parse_review(VALID_GENERAL, GENERAL, truncated=True)
        assert s is None and 'truncated' in warnings[0]

    def test_cut_off_mid_watch_list(self):
        text = VALID_GENERAL.rsplit('- Wed', 1)[0] + '- Wed | FOMC'
        assert parse_review(text, GENERAL)[0] is None


class TestWatchDays:
    @pytest.mark.parametrize('raw,expected', [
        ('Mon', 'Mon'), ('Thursday', 'Thu'), ('THU', 'Thu'), ('Thurs', 'Thu'),
        ('tues', 'Tue'), ('Fri.', 'Fri'), ('Ongoing', 'Ongoing'), ('All week', 'Ongoing'),
        ('**Wed**', 'Wed'),
    ])
    def test_normalised(self, raw, expected):
        assert weekly_review.normalise_day(raw) == expected

    @pytest.mark.parametrize('raw', ['11/5', 'TBD', 'Sat', 'Saturday', 'Sunday', 'Th', 'Nov 5', ''])
    def test_rejected(self, raw):
        assert weekly_review.normalise_day(raw) is None

    def test_invalid_days_dropped_not_passed_through(self):
        text = VALID_SECTION.replace(
            '- Wed | FOMC decision | Sets the path for front-end yields',
            '- 11/5 | Something | reason\n- TBD | Other | reason\n- Thursday | Claims | Labor check')
        s, warnings = parse_review(text, SECTION)
        assert [w['day'] for w in s['watch']] == ['Thu']
        assert sum('dropped watch item' in w for w in warnings) == 2
        assert all(w['day'] in weekly_review.WATCH_DAYS for w in s['watch'])

    def test_all_days_invalid_falls_back(self):
        text = VALID_SECTION.replace('- Wed |', '- Saturday |')
        assert parse_review(text, SECTION)[0] is None


class TestLimits:
    """Over-limit output is trimmed or accepted with a warning, never dropped
    into the markdown fallback."""

    def test_too_many_bullets_trimmed(self):
        extra = ''.join(f'- Item {i}: moved.\n' for i in range(4))
        text = VALID_GENERAL.replace('- High-yield spreads: were flat at 3.10%.\n',
                                     '- High-yield spreads: were flat at 3.10%.\n' + extra)
        s, warnings = parse_review(text, GENERAL)
        assert len(s['changed']) == 5
        assert any('trimmed to 5' in w for w in warnings)

    def test_too_many_section_watch_items_trimmed(self):
        items = ''.join(f'- {d} | Event {d} | Reason\n' for d in ('Mon', 'Tue', 'Wed', 'Thu', 'Fri'))
        text = VALID_SECTION.split('## What to watch next week')[0] + '## What to watch next week\n' + items
        s, warnings = parse_review(text, SECTION)
        assert [w['day'] for w in s['watch']] == ['Mon', 'Tue', 'Wed']

    def test_long_why_and_headline_accepted(self):
        long_headline = ' '.join(['word'] * 40) + '.'
        long_why = ' '.join(['word'] * 150)
        text = VALID_GENERAL.replace(VALID_GENERAL.split('\n')[0], long_headline) \
                            .replace('That eases the squeeze on rate-sensitive stocks.', long_why)
        s, warnings = parse_review(text, GENERAL)
        assert s is not None
        assert any('headline is 40 words' in w for w in warnings)
        assert any('max 120' in w for w in warnings)

    def test_too_few_bullets_warns(self):
        text = VALID_GENERAL.replace('- High-yield spreads: were flat at 3.10%.\n', '')
        s, warnings = parse_review(text, GENERAL)
        assert s is not None and any('min 3' in w for w in warnings)

    def test_two_sentence_headline_warns(self):
        text = 'Inflation cooled. Yields fell.\n\n' + VALID_GENERAL.split('\n', 2)[2]
        s, warnings = parse_review(text, GENERAL)
        assert s is not None and 'headline is more than one sentence' in warnings

    def test_banned_words_flagged(self):
        text = VALID_SECTION.replace('Falling long yields', 'Today, falling long yields')
        s, warnings = parse_review(text, SECTION)
        assert s is not None and any('banned words: today' in w for w in warnings)

    def test_todays_in_title_also_flagged(self):
        # Documented choice: \b matches "Today's" too, quoted titles included.
        # It is only a logged warning, so over-flagging costs nothing.
        assert weekly_review.BANNED_WORDS_RE.search("the \"Today's Markets\" column")


class TestUnicodeAndMarkup:
    def test_text_survives_as_data(self):
        text = VALID_SECTION.replace(
            '- 2s10s: steepened to +31 bp.',
            '- Spreads — HY & IG: 5% wider, $2bn outflows, <script>alert(1)</script> > 3 < 4')
        s, _ = parse_review(text, SECTION)
        assert s['changed'][1] == ('Spreads — HY & IG: 5% wider, $2bn outflows, '
                                   '<script>alert(1)</script> > 3 < 4')


# ---------------------------------------------------------------------------
# Full-week inputs
# ---------------------------------------------------------------------------

def _write_csv(path, col, rows):
    pd.DataFrame(rows, columns=['date', col]).to_csv(path, index=False)


class TestWeeklyChange:
    def test_holiday_friday_week(self, tmp_path):
        # Fri Jul 3, 2026 is the Independence Day holiday: week ends Thu Jul 2.
        _write_csv(tmp_path / 'treasury_10y.csv', 'treasury_10y', [
            ('2026-06-25', 4.10), ('2026-06-26', 4.20),
            ('2026-06-29', 4.15), ('2026-07-01', 4.05), ('2026-07-02', 4.00),
        ])
        df = weekly_review._load_series('treasury_10y', tmp_path)
        ch = weekly_review.weekly_change(df, date(2026, 6, 29), date(2026, 7, 3))
        assert ch['start_date'] == date(2026, 6, 26) and ch['end_date'] == date(2026, 7, 2)
        block = weekly_review.week_in_numbers('rates', date(2026, 6, 29), date(2026, 7, 3), tmp_path)
        assert '4.20% -> 4.00% (-20 bp)' in block

    def test_week_after_good_friday(self, tmp_path):
        # Good Friday Apr 3, 2026: the next week is measured from Thu Apr 2.
        _write_csv(tmp_path / 'sp500_price.csv', 'sp500_price', [
            ('2026-04-01', 100.0), ('2026-04-02', 110.0), ('2026-04-10', 121.0),
        ])
        df = weekly_review._load_series('sp500_price', tmp_path)
        ch = weekly_review.weekly_change(df, date(2026, 4, 6), date(2026, 4, 10))
        assert ch['start_date'] == date(2026, 4, 2)
        assert '(+10.0%)' in weekly_review.week_in_numbers('equity', date(2026, 4, 6), date(2026, 4, 10), tmp_path)

    def test_weekend_trading_uses_previous_friday(self, tmp_path):
        _write_csv(tmp_path / 'bitcoin_price.csv', 'bitcoin_price', [
            ('2026-10-23', 100.0), ('2026-10-24', 90.0), ('2026-10-25', 95.0), ('2026-10-30', 110.0),
        ])
        df = weekly_review._load_series('bitcoin_price', tmp_path)
        ch = weekly_review.weekly_change(df, date(2026, 10, 26), date(2026, 10, 30))
        assert ch['start'] == 100.0 and ch['end'] == 110.0

    def test_no_data_inside_week_is_omitted(self, tmp_path):
        _write_csv(tmp_path / 'treasury_10y.csv', 'treasury_10y', [('2026-10-20', 4.0), ('2026-10-23', 4.1)])
        assert weekly_review.week_in_numbers('rates', date(2026, 10, 26), date(2026, 10, 30), tmp_path) == ''

    def test_spot_check_real_csvs(self):
        """Friday close minus previous Friday close, computed independently from the CSVs."""
        data_dir = SIGNALTRACKERS_DIR / 'data'
        if not (data_dir / 'treasury_10y.csv').exists():
            pytest.skip('data/ not present in this checkout')
        def last_friday_pair(stem):
            df = pd.read_csv(data_dir / f'{stem}.csv', parse_dates=['date'])
            fridays = df[df['date'].dt.weekday == 4]
            prev, last = fridays.iloc[-2], fridays.iloc[-1]
            return prev, last, df.columns[1]
        prev, last, _ = last_friday_pair('treasury_10y')
        week_start = (last['date'] - timedelta(days=4)).date()
        through = last['date'].date()
        if (last['date'] - prev['date']).days != 7:
            pytest.skip('latest Friday pair spans a holiday')
        block = weekly_review.week_in_numbers('general', week_start, through, data_dir)
        for stem, label, kind in [('treasury_10y', '10-year Treasury yield', 'bp'),
                                  ('sp500_price', 'S&P 500 (SPY)', 'pct'),
                                  ('high_yield_spread', 'High-yield spread', 'bp')]:
            p, l, col = last_friday_pair(stem)
            if l['date'].date() != through:
                continue
            if kind == 'bp':
                expected = f"{(l[col] - p[col]) * 100:+.0f} bp"
            else:
                expected = f"{(l[col] / p[col] - 1) * 100:+.1f}%"
            line = next(x for x in block.splitlines() if x.startswith(f'- {label}:'))
            assert expected in line, (line, expected)


class TestMacroReadings:
    def test_claims_released_this_week(self, tmp_path):
        # Week ending Sat Oct 24 is published Thu Oct 29.
        _write_csv(tmp_path / 'initial_claims.csv', 'initial_claims',
                   [('2026-10-17', 220000), ('2026-10-24', 210000)])
        block = weekly_review.macro_readings(date(2026, 10, 26), date(2026, 10, 30), tmp_path)
        assert 'Initial jobless claims (week ending Oct 24): 210,000, prior 220,000 [released this week]' in block

    def test_monthly_series_never_claimed_as_this_week(self, tmp_path):
        _write_csv(tmp_path / 'cpi.csv', 'cpi', [('2026-08-01', 100.0), ('2026-09-01', 100.1)])
        block = weekly_review.macro_readings(date(2026, 10, 26), date(2026, 10, 30), tmp_path)
        assert 'CPI (index) (Sep 2026)' in block
        assert 'release date not in our data' in block
        assert 'Never present an earlier release' in block


# ---------------------------------------------------------------------------
# Prompts through the real generator code paths
# ---------------------------------------------------------------------------

def _seed_week_data(data_dir):
    rows = [('2026-10-23', 4.20), ('2026-10-26', 4.15), ('2026-10-30', 4.02)]
    for stem in ('treasury_10y', 'high_yield_spread'):
        _write_csv(data_dir / f'{stem}.csv', stem, rows)
    _write_csv(data_dir / 'bitcoin_price.csv', 'bitcoin_price',
               [('2026-10-23', 100.0), ('2026-10-30', 110.0)])


@pytest.fixture
def week_run(tmp_path):
    """A scheduled run for the week of Oct 26-30, 2026, with isolated storage."""
    _seed_week_data(tmp_path)
    files = {
        name: tmp_path / f'{name}.json'
        for name in ('SUMMARIES_FILE', 'CRYPTO_SUMMARIES_FILE', 'EQUITY_SUMMARIES_FILE',
                     'RATES_SUMMARIES_FILE', 'DOLLAR_SUMMARIES_FILE', 'CREDIT_SUMMARIES_FILE')
    }
    patches = [patch.object(ai_summary, name, path) for name, path in files.items()]
    patches += [
        patch.object(ai_summary, 'DATA_DIR', tmp_path),
        patch.object(weekly_review, 'DATA_DIR', tmp_path),
        patch.object(briefing_week, 'now_et', return_value=et(2026, 10, 30, 18, 10)),
        patch.object(ai_summary, 'get_ai_client', return_value=(MagicMock(), 'anthropic')),
        patch.object(ai_summary, 'get_market_conditions', return_value=None),
        patch.object(ai_summary, 'get_conditions_history', return_value={}),
        patch.object(ai_summary, '_get_stored_news_context', return_value=None),
        patch.object(ai_summary, 'is_tavily_configured', return_value=True),
    ]
    for p in patches:
        p.start()
    with briefing_week.scheduled_run(date(2026, 10, 26), date(2026, 10, 30)):
        yield tmp_path
    for p in patches:
        p.stop()


def _capture(content, truncated=False):
    calls = []

    def fake(client, system_prompt, user_prompt, **kwargs):
        calls.append({'system': system_prompt, 'user': user_prompt, **kwargs})
        return {'success': True, 'content': content, 'error': None,
                'usage': {}, 'model': 'm', 'truncated': truncated}
    return calls, fake


class TestGeneralPrompt:
    def _run(self, content=VALID_GENERAL, truncated=False, movers=None):
        calls, fake = _capture(content, truncated)
        with patch.object(ai_summary, 'call_ai_with_tools', side_effect=fake):
            result = ai_summary.generate_weekly_summary('## MARKET DATA\nSPY 600.', movers or [])
        return result, calls[0]

    def test_weekly_framing_and_contract(self, week_run):
        _, call = self._run()
        assert 'Week in Review' in call['system']
        assert '## What to watch next week' in call['system']
        assert 'at most 30 words' in call['system']
        assert 'financially literate non-professional' in call['system']
        assert 'runs Mon Oct 26, 2026 to Fri Oct 30, 2026' in call['user']

    def test_no_today_or_daily_in_prompt(self, week_run):
        movers = [{'name': 'HY Spread', 'change': 12.0, 'unit': 'bp', 'z_score': 2.1}]
        _, call = self._run(movers=movers)
        assert not BANNED.search(call['system']), BANNED.search(call['system'])
        assert not BANNED.search(call['user']), BANNED.search(call['user'])

    def test_full_week_inputs(self, week_run):
        _, call = self._run()
        assert '## WEEK IN NUMBERS' in call['user']
        assert '10-year Treasury yield: 4.20% -> 4.02% (-18 bp)' in call['user']
        assert '[Fri Oct 23 close vs Fri Oct 30 close]' in call['user']
        # Next week's calendar comes from web search, with no guessing
        assert 'Next week runs Mon Nov 2, 2026 to Fri Nov 6, 2026' in call['user']
        assert 'Never assign a day from memory' in call['user']
        assert f'at most {weekly_review.MAX_SEARCHES} web searches' in call['user']

    def test_no_search_tool_no_specific_days(self, week_run):
        with patch.object(ai_summary, 'is_tavily_configured', return_value=False):
            _, call = self._run()
        assert 'do not name a specific day' in call['user']

    def test_review_effort(self, week_run):
        _, call = self._run()
        assert call['effort'] == weekly_review.REVIEW_EFFORT
        assert call['max_tokens'] <= 21333  # SDK non-streaming ceiling

    def test_sections_from_this_week_fed_in(self, week_run):
        ai_summary.save_rates_summary('2026-10-30', 'RATES THIS WEEK', structured=None)
        # A crypto review from the previous week must not be fed in
        crypto = {'summaries': [{'date': '2026-10-23', 'summary': 'CRYPTO LAST WEEK',
                                 'week_start': '2026-10-19', 'week_end': '2026-10-23'}]}
        Path(ai_summary.CRYPTO_SUMMARIES_FILE).write_text(json.dumps(crypto))
        _, call = self._run()
        assert "THIS WEEK'S SECTION REVIEWS" in call['user']
        assert 'RATES THIS WEEK' in call['user']
        assert 'CRYPTO LAST WEEK' not in call['user']
        assert 'identical' in call['user']

    def test_previous_weeks_one_per_week(self, week_run):
        records = [{'date': f'2026-10-{d}', 'summary': f'DAY {d}'} for d in (13, 14, 15, 16, 19, 20, 21)]
        records.append({'date': '2026-10-30', 'summary': 'THIS WEEK', 'week_start': '2026-10-26'})
        with patch.object(ai_summary, 'get_recent_summaries', return_value=records):
            _, call = self._run()
        assert 'DAY 16' in call['user'] and 'DAY 21' in call['user']
        for d in (13, 14, 15, 19, 20):
            assert f'DAY {d}\n' not in call['user']
        assert 'THIS WEEK\n' not in call['user']

    def test_structured_stored_with_markdown(self, week_run):
        result, _ = self._run()
        assert result['structured']['headline'].startswith('Inflation cooled')
        record = ai_summary.get_latest_summary()
        assert record['summary'] == VALID_GENERAL
        assert record['format'] == 'structured'
        assert record['run_type'] == 'scheduled'

    def test_malformed_falls_back_to_markdown(self, week_run):
        prose = 'Markets were mixed this week.\n\nYields fell.'
        result, _ = self._run(content=prose)
        assert result['success'] and result['structured'] is None
        shown = ai_summary.get_summary_for_display()
        assert shown['summary'] == prose
        assert shown['structured'] is None and shown['format'] == 'markdown'

    def test_truncated_falls_back(self, week_run):
        result, _ = self._run(truncated=True)
        assert result['structured'] is None
        assert ai_summary.get_summary_for_display()['summary'] == VALID_GENERAL

    def test_fallback_copy_is_weekly(self):
        text = ai_summary._generate_fallback_briefing(None)
        assert not BANNED.search(text) and 'week' in text


class TestSectionPrompts:
    @pytest.mark.parametrize('section', list(ai_summary.SECTION_REVIEWS))
    def test_section_prompt_contract(self, week_run, section):
        calls, fake = _capture(VALID_SECTION)
        with patch.object(ai_summary, 'call_ai_with_tools', side_effect=fake):
            result = getattr(ai_summary, f'generate_{section}_summary')('## DATA\nlevels')
        call = calls[0]
        assert result['success'] and result['structured']['watch'][0]['day'] == 'Wed'
        assert not BANNED.search(call['system']) and not BANNED.search(call['user'])
        assert 'No headline' in call['system'] and '1-3 bullets' in call['system']
        assert 'at most 80 words' in call['system']
        assert 'financially literate non-professional' in call['system']
        assert call['effort'] == weekly_review.REVIEW_EFFORT
        assert 'Next week runs Mon Nov 2' in call['user']
        shown = getattr(ai_summary, f'get_{section}_summary_for_display')()
        assert shown['structured'] == result['structured']
        assert shown['summary'] == VALID_SECTION
        assert shown['week_start'] == '2026-10-26'

    def test_section_week_numbers(self, week_run):
        calls, fake = _capture(VALID_SECTION)
        with patch.object(ai_summary, 'call_ai_with_tools', side_effect=fake):
            ai_summary.generate_crypto_summary('data')
        assert 'Bitcoin: 100.00 -> 110.00 (+10.0%)' in calls[0]['user']

    def test_general_and_section_share_figures(self, week_run):
        """The same series yields the same figure in both prompts."""
        calls, fake = _capture(VALID_SECTION)
        with patch.object(ai_summary, 'call_ai_with_tools', side_effect=fake):
            ai_summary.generate_rates_summary('data')
        calls_g, fake_g = _capture(VALID_GENERAL)
        with patch.object(ai_summary, 'call_ai_with_tools', side_effect=fake_g):
            ai_summary.generate_weekly_summary('data', [])
        line = '10-year Treasury yield: 4.20% -> 4.02% (-18 bp)'
        assert line in calls[0]['user'] and line in calls_g[0]['user']

    def test_section_news_header_weekly(self):
        stored = {'topic_summaries': {'rates': 'Yields fell.'}}
        with patch.object(news_pipeline, 'get_stored_news', return_value=stored):
            ctx = ai_summary._get_stored_news_context('rates')
        assert ctx == "## This Week's Rates News\nYields fell."


# ---------------------------------------------------------------------------
# Display APIs: structured parts plus markdown
# ---------------------------------------------------------------------------

class TestDisplay:
    def test_legacy_record(self):
        legacy = {'summaries': [{'date': '2026-09-01', 'generated_at': '2026-09-01T17:40:00',
                                 'summary': 'Old daily prose.'}]}
        with patch.object(ai_summary, 'load_summaries', return_value=legacy):
            shown = ai_summary.get_summary_for_display()
        assert shown['summary'] == 'Old daily prose.'
        assert shown['structured'] is None and shown['format'] == 'markdown'
        assert shown['run_type'] == 'legacy'

    def test_markup_returned_as_data(self, week_run):
        text = VALID_SECTION.replace('2s10s: steepened to +31 bp.', '<script>alert(1)</script> & 5%')
        calls, fake = _capture(text)
        with patch.object(ai_summary, 'call_ai_with_tools', side_effect=fake):
            ai_summary.generate_rates_summary('data')
        shown = ai_summary.get_rates_summary_for_display()
        assert shown['structured']['changed'][1] == '<script>alert(1)</script> & 5%'
        assert json.loads(json.dumps(shown))['structured']['changed'][1].startswith('<script>')

    def test_real_summary_files_do_not_crash(self, tmp_path):
        data_dir = SIGNALTRACKERS_DIR / 'data'
        pairs = [('SUMMARIES_FILE', 'ai_summaries.json', 'get_summary_for_display'),
                 ('CRYPTO_SUMMARIES_FILE', 'crypto_summaries.json', 'get_crypto_summary_for_display'),
                 ('EQUITY_SUMMARIES_FILE', 'equity_summaries.json', 'get_equity_summary_for_display'),
                 ('RATES_SUMMARIES_FILE', 'rates_summaries.json', 'get_rates_summary_for_display'),
                 ('DOLLAR_SUMMARIES_FILE', 'dollar_summaries.json', 'get_dollar_summary_for_display'),
                 ('CREDIT_SUMMARIES_FILE', 'credit_summaries.json', 'get_credit_summary_for_display')]
        checked = 0
        for attr, name, getter in pairs:
            if not (data_dir / name).exists():
                continue
            copy = tmp_path / name
            shutil.copy(data_dir / name, copy)
            with patch.object(ai_summary, attr, copy):
                shown = getattr(ai_summary, getter)()
            if shown:
                assert shown['summary'] and 'structured' in shown
                checked += 1
        if not checked:
            pytest.skip('no stored summaries in this checkout')

    def test_dashboard_api_returns_parts(self, week_run):
        import dashboard
        calls, fake = _capture(VALID_GENERAL)
        with patch.object(ai_summary, 'call_ai_with_tools', side_effect=fake):
            ai_summary.generate_weekly_summary('data', [])
        with dashboard.app.test_client() as client:
            body = client.get('/api/ai-summary').get_json()
        assert body['summary'] == VALID_GENERAL
        assert body['structured']['watch'][1]['event'] == 'October payrolls'
        assert body['format'] == 'structured'


# ---------------------------------------------------------------------------
# Truncation flag from the call path
# ---------------------------------------------------------------------------

class TestTruncationFlag:
    def _response(self, stop_reason):
        resp = MagicMock()
        resp.stop_reason = stop_reason
        resp.model = 'claude-test'
        text = MagicMock(type='text', text='partial')
        thinking = MagicMock(type='thinking', spec=['type'])
        resp.content = [thinking, text]
        return resp

    @pytest.mark.parametrize('stop_reason,expected', [('max_tokens', True), ('end_turn', False)])
    def test_anthropic_reports_truncation(self, stop_reason, expected):
        client = MagicMock()
        client.beta.messages.create.return_value = self._response(stop_reason)
        # Other test modules can leave a stub in sys.modules['services'], which
        # the call path imports from; give it a real-enough metering module.
        metering = types.SimpleNamespace(extract_usage_anthropic=lambda r: {},
                                         accumulate_usage=lambda a, b: {})
        with patch.object(ai_summary, 'is_tavily_configured', return_value=False), \
             patch.object(ai_summary, '_dump_prompt_to_file'), \
             patch.dict(sys.modules, {'services.usage_metering': metering}), \
             patch.object(briefing_week, 'meter_ai_call'):
            result = ai_summary.call_ai_with_tools(client, 's', 'u', provider='anthropic', effort='high')
        assert result['truncated'] is expected
        kwargs = client.beta.messages.create.call_args.kwargs
        assert kwargs['output_config'] == {'effort': 'high'}
        # The tool loop re-sends every search result each turn: cache the prefix
        assert kwargs['cache_control'] == {'type': 'ephemeral'}


    def test_last_turn_forbids_tools_so_the_loop_always_answers(self):
        """A real dollar run spent all 6 turns searching and returned nothing."""
        tool_turn = MagicMock(stop_reason='tool_use', model='claude-test')
        tool_turn.content = [MagicMock(type='tool_use', id='t1', input={'query': 'q'})]
        tool_turn.content[0].name = 'search_web'
        client = MagicMock()
        responses = [tool_turn] * 5 + [self._response('end_turn')]
        client.beta.messages.create.side_effect = responses
        metering = types.SimpleNamespace(extract_usage_anthropic=lambda r: {},
                                         accumulate_usage=lambda a, b: {})
        with patch.object(ai_summary, 'is_tavily_configured', return_value=True), \
             patch.object(ai_summary, 'execute_search_function', return_value='{}'), \
             patch.object(ai_summary, '_dump_prompt_to_file'), \
             patch.dict(sys.modules, {'services.usage_metering': metering}), \
             patch.object(briefing_week, 'meter_ai_call'):
            result = ai_summary.call_ai_with_tools(client, 's', 'u', provider='anthropic')
        assert result['success'] and result['content'] == 'partial'
        kwargs = [c.kwargs for c in client.beta.messages.create.call_args_list]
        assert all('tool_choice' not in k for k in kwargs[:5])
        assert kwargs[5]['tool_choice'] == {'type': 'none'}


# ---------------------------------------------------------------------------
# Weekly news prompts
# ---------------------------------------------------------------------------

def _article(day, n, topic='macro'):
    return {'headline': f'{day} story {n}', 'url': f'https://x/{day}/{n}', 'source': 'x.com',
            'timestamp': f'{day}T21:00:00+00:00', 'raw_content': 'Body text.', 'topic': topic}


class TestNewsPrompts:
    def _capture(self):
        calls = []

        def fake(system_prompt, user_prompt, max_tokens=4000):
            calls.append((system_prompt, user_prompt))
            return 'Para one.\n\nPara two.'
        return calls, fake

    def test_cross_market_prompt(self):
        arts = [_article('2026-10-26', 1), _article('2026-10-28', 1, 'rates'), _article('2026-10-30', 1)]
        calls, fake = self._capture()
        with patch.dict('os.environ', {'AI_PROVIDER': 'anthropic'}), \
             patch.object(news_pipeline, '_summarize_with_anthropic', side_effect=fake):
            news_pipeline._generate_cross_market_summary(arts, date(2026, 10, 26), date(2026, 10, 30))
        system, user = calls[0]
        assert not BANNED.search(system) and not BANNED.search(user)
        assert 'separated by blank lines' in system and 'Do NOT use bullet points' in system
        assert 'financially literate non-professional' in system
        assert 'Week covered: Mon Oct 26 to Fri Oct 30, 2026' in user
        for tag in ('[Mon Oct 26]', '[Wed Oct 28]', '[Fri Oct 30]'):
            assert tag in user
        assert 'not instructions' in system

    def test_topic_prompt(self):
        arts = [_article('2026-10-27', 1, 'rates')]
        calls, fake = self._capture()
        with patch.dict('os.environ', {'AI_PROVIDER': 'anthropic'}), \
             patch.object(news_pipeline, '_summarize_with_anthropic', side_effect=fake):
            out = news_pipeline._generate_all_topic_summaries(arts, date(2026, 10, 26), date(2026, 10, 30))
        assert set(out) == {'rates'}
        system, user = calls[0]
        assert 'weekly news summary' in system
        assert not BANNED.search(system) and not BANNED.search(user)


class TestChatbotLabels:
    def test_context_labels_weekly(self):
        import dashboard
        general = {'date': '2026-10-30', 'summary': 'G'}
        with patch.object(dashboard, 'generate_market_summary', return_value=''), \
             patch.object(dashboard, 'calculate_top_movers', return_value=[]), \
             patch.object(ai_summary, 'get_latest_summary', return_value=general), \
             patch.object(ai_summary, '_get_stored_news_context', return_value=None):
            ctx = dashboard._build_chatbot_enrichment_context()
        assert 'General Week in Review (2026-10-30)' in ctx
        assert 'Daily Briefing' not in ctx
