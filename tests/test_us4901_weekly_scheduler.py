"""
US-490.1: Split scheduler — daily data refresh, weekly Friday AI run.

Covers the week arithmetic (always ET), the record/display week fields, the
daily job's zero-AI contract (checked at the SDK constructors, not at the
generator functions), the weekly job's ordering, locking, idempotency, retry
and catch-up, the news pipeline's daily-fetch / weekly-summarize split, and
metering of the scheduled run.
"""

import importlib.util
import json
import os
import sys
import threading
import time
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SIGNALTRACKERS_DIR = Path(__file__).parent.parent / 'signaltrackers'
sys.path.insert(0, str(SIGNALTRACKERS_DIR))

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.pool import StaticPool

import briefing_week
import dashboard
import news_pipeline
import ai_summary
from dashboard import app
from extensions import db
from models import User, AIUsageRecord


def _load_by_path(name, relpath):
    """Load a services module fresh, bound to the current `extensions.db`.

    `from services import ...` can return a stub (other test modules poison
    sys.modules['services']) or a copy first imported against an earlier
    `extensions` module, whose `db` is not attached to this app.
    """
    spec = importlib.util.spec_from_file_location(name, SIGNALTRACKERS_DIR / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


usage_metering = _load_by_path('us4901_usage_metering', 'services/usage_metering.py')
admin_analytics = _load_by_path('us4901_admin_analytics', 'services/admin_analytics.py')

ET = briefing_week.ET


def et(*args):
    return ET.localize(datetime(*args))


def scheduled_display(now):
    """Display fields of a record written by a scheduled run at `now`."""
    with briefing_week.scheduled_run(*briefing_week.trading_week(now.date())):
        record = briefing_week.record_metadata(now)
    return record, briefing_week.display_fields(record, now=now)


# ---------------------------------------------------------------------------
# Week arithmetic
# ---------------------------------------------------------------------------

WEEK_CASES = [
    # run time (ET), week_start, week_end, next review date
    ((2026, 10, 30, 17, 45), '2026-10-26', '2026-10-30', '2026-11-06'),  # crosses DST end Nov 1
    ((2026, 10, 30, 23, 0), '2026-10-26', '2026-10-30', '2026-11-06'),   # 03:00 UTC Saturday
    ((2026, 10, 2, 18, 30), '2026-09-28', '2026-10-02', '2026-10-09'),   # spans two months
    ((2026, 1, 2, 18, 30), '2025-12-29', '2026-01-02', '2026-01-09'),    # spans two years
    ((2027, 1, 1, 18, 30), '2026-12-28', '2027-01-01', '2027-01-08'),    # holiday + year span
    ((2026, 7, 3, 18, 30), '2026-06-29', '2026-07-03', '2026-07-10'),    # Independence Day observed
    ((2026, 4, 3, 18, 30), '2026-03-30', '2026-04-03', '2026-04-10'),    # Good Friday
]


class TestWeekArithmetic:
    @pytest.mark.parametrize('run, start, end, next_review', WEEK_CASES)
    def test_scheduled_week_fields(self, run, start, end, next_review):
        record, shown = scheduled_display(et(*run))
        assert (shown['week_start'], shown['week_end']) == (start, end)
        assert shown['next_review_at'].startswith(next_review)
        assert shown['run_type'] == 'scheduled'
        assert shown['week_to_date'] is False
        assert record['through'] == end

    def test_next_review_offset_follows_dst(self):
        # Oct 30 is EDT (-04:00); the next review on Nov 6 is EST (-05:00).
        _, shown = scheduled_display(et(2026, 10, 30, 18, 20))
        assert shown['published_at'].endswith('-04:00')
        assert shown['next_review_at'] == '2026-11-06T18:00:00-05:00'

    @pytest.mark.parametrize('tz', ['Asia/Tokyo', 'UTC', 'America/Los_Angeles'])
    def test_independent_of_host_timezone(self, tz):
        old = os.environ.get('TZ')
        os.environ['TZ'] = tz
        time.tzset()
        try:
            for run, start, end, next_review in WEEK_CASES:
                _, shown = scheduled_display(et(*run))
                assert (shown['week_start'], shown['week_end']) == (start, end)
                assert shown['next_review_at'].startswith(next_review)
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            time.tzset()

    def test_published_at_is_timezone_aware(self):
        record = briefing_week.record_metadata(et(2026, 10, 30, 18, 5))
        assert datetime.fromisoformat(record['published_at']).tzinfo is not None

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            briefing_week.to_et(datetime(2026, 10, 30, 18))

    @pytest.mark.parametrize('day, expected', [
        (date(2026, 10, 31), ('2026-10-26', '2026-10-30')),  # Saturday -> week just finished
        (date(2026, 11, 1), ('2026-10-26', '2026-10-30')),   # Sunday -> week just finished
        (date(2026, 11, 2), ('2026-11-02', '2026-11-06')),   # Monday -> new week
    ])
    def test_weekend_belongs_to_finished_week(self, day, expected):
        start, end = briefing_week.trading_week(day)
        assert (start.isoformat(), end.isoformat()) == expected


# ---------------------------------------------------------------------------
# Manual regenerate and display edge cases
# ---------------------------------------------------------------------------

class TestManualRegenerate:
    def _manual(self, now):
        with patch.object(briefing_week, 'now_et', return_value=now):
            record = briefing_week.record_metadata()
        return record, briefing_week.display_fields(record, now=now)

    def test_midweek_is_week_to_date(self):
        record, shown = self._manual(et(2026, 11, 4, 12, 0))
        assert record['run_type'] == 'manual'
        assert shown['week_to_date'] is True
        assert shown['through'] == '2026-11-04'
        assert (shown['week_start'], shown['week_end']) == ('2026-11-02', '2026-11-06')
        assert shown['next_review_at'].startswith('2026-11-06')

    def test_saturday_regenerate_is_through_friday(self):
        _, shown = self._manual(et(2026, 10, 31, 10, 0))
        assert shown['through'] == '2026-10-30'
        assert shown['week_end'] == '2026-10-30'
        assert shown['next_review_at'].startswith('2026-11-06')

    def test_monday_regenerate_is_through_monday(self):
        # Documented: before Monday's close the week to date holds no data from
        # this week yet; the label still reads "through Mon".
        _, shown = self._manual(et(2026, 11, 2, 9, 0))
        assert (shown['week_start'], shown['through']) == ('2026-11-02', '2026-11-02')
        assert shown['week_to_date'] is True

    def test_friday_before_run_points_at_tonights_run(self):
        _, shown = self._manual(et(2026, 10, 30, 12, 0))
        assert shown['next_review_at'].startswith('2026-10-30')

    def test_manual_record_inside_scheduled_context_is_scheduled(self):
        with briefing_week.scheduled_run(date(2026, 10, 26), date(2026, 10, 30)):
            assert briefing_week.record_metadata()['run_type'] == 'scheduled'
        assert briefing_week.record_metadata()['run_type'] == 'manual'

    def test_scheduled_context_is_per_thread(self):
        """A request thread regenerating during the weekly run stays manual."""
        seen = {}

        def other_thread():
            seen['run_type'] = briefing_week.record_metadata()['run_type']

        with briefing_week.scheduled_run(date(2026, 10, 26), date(2026, 10, 30)):
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
        assert seen['run_type'] == 'manual'


class TestDisplayBackwardCompat:
    def test_legacy_record_derives_week(self):
        legacy = {'date': '2026-10-27', 'generated_at': '2026-10-27T17:40:00-04:00', 'summary': 'x'}
        shown = briefing_week.display_fields(legacy, now=et(2026, 10, 28, 9))
        assert shown['run_type'] == 'legacy'
        assert (shown['week_start'], shown['week_end']) == ('2026-10-26', '2026-10-30')
        assert shown['next_review_at'].startswith('2026-10-30')

    def test_legacy_naive_generated_at(self):
        legacy = {'date': '2026-10-27', 'generated_at': '2026-10-27T17:40:00', 'summary': 'x'}
        shown = briefing_week.display_fields(legacy, now=et(2026, 10, 28, 9))
        assert shown['published_at'] == '2026-10-27T17:40:00-04:00'

    def test_legacy_synthesis_record_without_date(self):
        legacy = {'synthesis': 's', 'generated_at': '2026-10-27T21:40:00.123Z'}
        shown = briefing_week.display_fields(legacy, now=et(2026, 10, 28, 9))
        assert shown['week_end'] == '2026-10-30'

    @pytest.mark.parametrize('record', [{}, {'date': 'garbage'}, {'week_start': 'x', 'week_end': 'y'}])
    def test_unreadable_record_returns_nulls(self, record):
        shown = briefing_week.display_fields(record)
        assert shown['week_start'] is None and shown['next_review_at'] is None

    def test_missed_run_points_at_next_due_run(self):
        record, _ = scheduled_display(et(2026, 10, 30, 18, 20))
        shown = briefing_week.display_fields(record, now=et(2026, 11, 8, 9))
        assert shown['next_review_at'].startswith('2026-11-13')

    def test_real_summary_files_render(self):
        """Display helpers must not crash on the checkout's real stored files."""
        for getter in (
            ai_summary.get_summary_for_display,
            ai_summary.get_crypto_summary_for_display,
            ai_summary.get_equity_summary_for_display,
            ai_summary.get_rates_summary_for_display,
            ai_summary.get_dollar_summary_for_display,
            ai_summary.get_credit_summary_for_display,
        ):
            shown = getter()
            if shown is not None:
                assert 'next_review_at' in shown and 'week_start' in shown


class TestDisplayApis:
    ROUTES = ['/api/ai-summary', '/api/crypto-summary', '/api/equity-summary',
              '/api/rates-summary', '/api/dollar-summary', '/api/credit-summary']

    @pytest.mark.parametrize('route', ROUTES)
    def test_api_returns_week_fields(self, route, tmp_path):
        record = {'date': '2026-10-30', 'generated_at': '2026-10-30T18:20:00-04:00',
                  'summary': 'Weekly text', 'web_search_used': False}
        with briefing_week.scheduled_run(date(2026, 10, 26), date(2026, 10, 30)):
            record.update(briefing_week.record_metadata(et(2026, 10, 30, 18, 20)))
        files = ['SUMMARIES_FILE', 'CRYPTO_SUMMARIES_FILE', 'EQUITY_SUMMARIES_FILE',
                 'RATES_SUMMARIES_FILE', 'DOLLAR_SUMMARIES_FILE', 'CREDIT_SUMMARIES_FILE']
        patches = []
        for name in files:
            path = tmp_path / f'{name}.json'
            path.write_text(json.dumps({'summaries': [record]}))
            patches.append(patch.object(ai_summary, name, path))
        for p in patches:
            p.start()
        try:
            resp = app.test_client().get(route)
        finally:
            for p in patches:
                p.stop()
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['week_start'] == '2026-10-26'
        assert body['week_end'] == '2026-10-30'
        assert body['published_at'] == '2026-10-30T18:20:00-04:00'
        assert body['next_review_at'] == '2026-11-06T18:00:00-05:00'
        assert body['run_type'] == 'scheduled'


# ---------------------------------------------------------------------------
# Briefing storage overwrite rules
# ---------------------------------------------------------------------------

class TestOverwriteRules:
    @pytest.fixture
    def summaries_file(self, tmp_path):
        path = tmp_path / 'ai_summaries.json'
        with patch.object(ai_summary, 'SUMMARIES_FILE', path), \
             patch.object(ai_summary, 'DATA_DIR', tmp_path):
            yield path

    def test_scheduled_run_replaces_same_day_manual(self, summaries_file):
        friday_noon = et(2026, 10, 30, 12)
        with patch.object(briefing_week, 'now_et', return_value=friday_noon):
            ai_summary.save_summary('2026-10-30', 'manual text')
        with briefing_week.scheduled_run(date(2026, 10, 26), date(2026, 10, 30)):
            ai_summary.save_summary('2026-10-30', 'scheduled text')
        rows = json.loads(summaries_file.read_text())['summaries']
        assert len(rows) == 1
        assert rows[0]['run_type'] == 'scheduled' and rows[0]['summary'] == 'scheduled text'

    def test_midweek_manual_keeps_last_friday(self, summaries_file):
        with briefing_week.scheduled_run(date(2026, 10, 26), date(2026, 10, 30)):
            ai_summary.save_summary('2026-10-30', 'friday review')
        with patch.object(briefing_week, 'now_et', return_value=et(2026, 11, 4, 12)):
            ai_summary.save_summary('2026-11-04', 'wednesday regenerate')
        rows = {r['date']: r for r in json.loads(summaries_file.read_text())['summaries']}
        assert rows['2026-10-30']['run_type'] == 'scheduled'
        assert rows['2026-11-04']['run_type'] == 'manual'
        assert rows['2026-11-04']['through'] == '2026-11-04'


# ---------------------------------------------------------------------------
# Daily job: zero AI calls
# ---------------------------------------------------------------------------

FAKE_ARTICLE = {
    'headline': 'Fed holds', 'url': 'https://example.com/fed', 'source': 'example.com',
    'timestamp': '2026-10-28T21:00:00+00:00', 'raw_content': 'body', 'topic': 'macro',
}


def _no_ai_patches():
    """Patch every AI SDK constructor to fail loudly if anything builds a client."""
    boom = MagicMock(side_effect=AssertionError('AI client constructed in the daily job'))
    return [
        patch('anthropic.Anthropic', boom),
        patch('openai.OpenAI', boom),
        patch.object(ai_summary, 'OpenAI', boom),
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'k', 'OPENAI_API_KEY': 'k',
                                'TAVILY_API_KEY': 'k'}),
    ], boom


class TestDailyJob:
    def test_daily_refresh_makes_no_ai_calls(self, tmp_path):
        ai_patches, boom = _no_ai_patches()
        order = []
        ok = MagicMock(returncode=0, stderr='')

        def fake_run(cmd, **kwargs):
            order.append(cmd[1])
            return ok

        saved = {}
        with patch.object(dashboard.subprocess, 'run', side_effect=fake_run), \
             patch.object(dashboard, 'update_recession_probability',
                          side_effect=lambda: order.append('recession')), \
             patch.object(dashboard, 'update_market_conditions_cache',
                          side_effect=lambda: order.append('conditions')), \
             patch.object(news_pipeline, '_fetch_topic', return_value=[FAKE_ARTICLE]), \
             patch.object(news_pipeline, '_load_cache', return_value={}), \
             patch.object(news_pipeline, '_save_cache', side_effect=saved.update), \
             patch.object(ai_summary, 'get_ai_client',
                          side_effect=AssertionError('AI client requested')):
            for p in ai_patches:
                p.start()
            try:
                assert dashboard._pipeline_lock.acquire(blocking=False)
                dashboard._run_holding_lock(dashboard.run_data_collection)
            finally:
                for p in reversed(ai_patches):
                    p.stop()

        assert boom.call_count == 0
        assert dashboard.reload_status['success'] is True, dashboard.reload_status['error']
        assert order == ['market_signals.py', 'divergence_analysis.py', 'recession', 'conditions']
        (record,) = saved.values()
        assert record['articles'] and record.get('summary') is None
        assert 'topic_summaries' not in record
        assert not dashboard._pipeline_lock.locked()

    def test_daily_job_body_has_no_generators(self):
        src = Path(SIGNALTRACKERS_DIR / 'dashboard.py').read_text()
        body = src[src.index('def _run_data_phase('):src.index('def run_data_collection(')]
        for name in ('generate_', 'summarize_week_news', 'call_ai_with_tools'):
            assert name not in body


# ---------------------------------------------------------------------------
# Weekly job
# ---------------------------------------------------------------------------

class TestWeeklyOrder:
    def test_ai_steps_run_in_dependency_order(self):
        order = []

        def step(name, result=None):
            def fn(*args, **kwargs):
                order.append(name)
                return result if result is not None else {'success': True, 'error': None}
            return fn

        names = ['crypto', 'equity', 'rates', 'dollar', 'credit']
        patches = [patch.object(news_pipeline, 'summarize_week_news', side_effect=step('news', True))]
        for n in names:
            patches.append(patch.object(dashboard, f'generate_{n}_market_summary', return_value=''))
            patches.append(patch.object(dashboard, f'generate_{n}_summary', side_effect=step(n)))
        patches += [
            patch.object(dashboard, 'generate_market_summary', return_value=''),
            patch.object(dashboard, 'calculate_top_movers', return_value=[]),
            patch.object(dashboard, 'generate_weekly_summary', side_effect=step('general')),
            patch.object(dashboard, 'generate_market_conditions_synthesis', side_effect=step('synthesis')),
        ]
        for p in patches:
            p.start()
        try:
            succeeded = dashboard.run_weekly_ai_review(date(2026, 10, 26), date(2026, 10, 30))
        finally:
            for p in reversed(patches):
                p.stop()
        assert order == ['news', *names, 'general', 'synthesis']
        assert succeeded == 8


@pytest.fixture
def weekly_env(tmp_path):
    """Run scheduled_weekly_review with its collaborators recorded."""
    calls = []
    env = MagicMock()
    env.calls = calls
    env.state_file = tmp_path / 'weekly_review_state.json'
    retries = []
    with patch.object(dashboard, 'WEEKLY_REVIEW_STATE_FILE', env.state_file), \
         patch.object(dashboard, 'DATA_DIR', tmp_path), \
         patch.object(dashboard, 'weekly_review_due',
                      side_effect=lambda now=None: env.due) as due, \
         patch.object(dashboard, '_run_data_phase',
                      side_effect=lambda: (calls.append('data'), env.data_effect())) as data, \
         patch.object(dashboard, 'run_weekly_ai_review',
                      side_effect=lambda s, e: (calls.append(('ai', s, e, briefing_week.is_scheduled_run())), env.ai_result)[1]), \
         patch.object(dashboard, '_schedule_weekly_retry', side_effect=retries.append):
        env.due = (date(2026, 10, 26), date(2026, 10, 30))
        env.data_effect = lambda: None
        env.ai_result = 8
        env.retries = retries
        env.data = data
        yield env
    assert not dashboard._pipeline_lock.locked()


class TestWeeklyJob:
    def test_reuses_todays_refresh(self, weekly_env):
        dashboard._last_data_refresh['date'] = briefing_week.now_et().date()
        dashboard.scheduled_weekly_review()
        assert weekly_env.calls == [('ai', date(2026, 10, 26), date(2026, 10, 30), True)]
        state = json.loads(weekly_env.state_file.read_text())
        assert state['week_end'] == '2026-10-30'

    def test_refreshes_data_when_todays_refresh_missing(self, weekly_env):
        dashboard._last_data_refresh['date'] = None
        dashboard.scheduled_weekly_review()
        assert weekly_env.calls[0] == 'data'
        assert weekly_env.calls[1][0] == 'ai'

    def test_data_failure_skips_ai_and_retries(self, weekly_env):
        dashboard._last_data_refresh['date'] = None

        def fail():
            raise RuntimeError('FRED down')
        weekly_env.data_effect = fail
        dashboard.scheduled_weekly_review()
        assert weekly_env.calls == ['data']
        assert weekly_env.retries == [1]
        assert not weekly_env.state_file.exists()
        assert dashboard.reload_status['success'] is False

    def test_all_ai_steps_failing_is_retried_not_published(self, weekly_env):
        dashboard._last_data_refresh['date'] = briefing_week.now_et().date()
        weekly_env.ai_result = 0
        dashboard.scheduled_weekly_review(attempt=2)
        assert weekly_env.retries == [2]
        assert not weekly_env.state_file.exists()

    def test_already_published_week_is_skipped(self, weekly_env):
        weekly_env.due = None
        dashboard.scheduled_weekly_review()
        assert weekly_env.calls == []

    def test_waits_for_slow_daily_refresh(self, weekly_env):
        """The weekly job starts only after the in-flight refresh releases the lock."""
        dashboard._last_data_refresh['date'] = None
        assert dashboard._pipeline_lock.acquire(blocking=False)

        def slow_refresh():
            time.sleep(0.3)
            weekly_env.calls.append('daily-done')
            dashboard._last_data_refresh['date'] = briefing_week.now_et().date()
            dashboard._pipeline_lock.release()

        t = threading.Thread(target=slow_refresh)
        t.start()
        dashboard.scheduled_weekly_review()
        t.join()
        # Friday's refresh finished first, so the weekly job did not redo it.
        assert weekly_env.calls[0] == 'daily-done'
        assert weekly_env.calls[1][0] == 'ai'
        assert 'data' not in weekly_env.calls

    def test_reload_rejected_while_weekly_runs(self, weekly_env):
        assert dashboard._pipeline_lock.acquire(blocking=False)
        try:
            resp = app.test_client().post('/api/reload-data')
        finally:
            dashboard._pipeline_lock.release()
        assert resp.status_code == 409


class TestWeeklyDueAndCatchUp:
    @pytest.fixture
    def state_file(self, tmp_path):
        path = tmp_path / 'weekly_review_state.json'
        with patch.object(dashboard, 'WEEKLY_REVIEW_STATE_FILE', path):
            yield path

    @pytest.mark.parametrize('now, expected', [
        (et(2026, 10, 30, 17, 59), None),  # before the trigger; last week's window closed Sunday
        (et(2026, 10, 30, 18, 0, 1), (date(2026, 10, 26), date(2026, 10, 30))),
        (et(2026, 10, 31, 9, 0), (date(2026, 10, 26), date(2026, 10, 30))),  # Saturday catch-up
        (et(2026, 11, 1, 23, 0), (date(2026, 10, 26), date(2026, 10, 30))),  # Sunday night
        (et(2026, 11, 2, 9, 0), None),                                     # Monday: too late
        (et(2026, 11, 4, 12, 0), None),                                    # midweek
    ])
    def test_due_window(self, state_file, now, expected):
        assert dashboard.weekly_review_due(now) == expected

    def test_published_week_not_due(self, state_file):
        state_file.write_text(json.dumps({'week_end': '2026-10-30'}))
        assert dashboard.weekly_review_due(et(2026, 10, 31, 9)) is None

    def test_restart_after_publish_schedules_nothing(self, state_file):
        state_file.write_text(json.dumps({'week_end': '2026-10-30'}))
        fake = MagicMock()
        with patch.object(dashboard, 'scheduler', fake), \
             patch.dict(os.environ, {'WEEKLY_REVIEW_CATCH_UP': 'true'}), \
             patch.object(briefing_week, 'now_et', return_value=et(2026, 10, 30, 21)):
            dashboard._schedule_weekly_catch_up()
            dashboard._schedule_weekly_catch_up()
        fake.add_job.assert_not_called()

    def test_restart_across_trigger_schedules_catch_up(self, state_file):
        state_file.write_text(json.dumps({'week_end': '2026-10-23'}))
        fake = MagicMock()
        with patch.object(dashboard, 'scheduler', fake), \
             patch.dict(os.environ, {'WEEKLY_REVIEW_CATCH_UP': 'true'}), \
             patch.object(briefing_week, 'now_et', return_value=et(2026, 10, 31, 8)):
            dashboard._schedule_weekly_catch_up()
        fake.add_job.assert_called_once()
        assert fake.add_job.call_args.kwargs['id'] == 'weekly_ai_review_catch_up'

    def test_catch_up_disabled_by_env(self, state_file):
        fake = MagicMock()
        with patch.object(dashboard, 'scheduler', fake), \
             patch.dict(os.environ, {'WEEKLY_REVIEW_CATCH_UP': 'false'}), \
             patch.object(briefing_week, 'now_et', return_value=et(2026, 10, 31, 8)):
            dashboard._schedule_weekly_catch_up()
        fake.add_job.assert_not_called()

    def test_retry_capped(self):
        fake = MagicMock()
        with patch.object(dashboard, 'scheduler', fake):
            dashboard._schedule_weekly_retry(1)
            dashboard._schedule_weekly_retry(dashboard.WEEKLY_MAX_ATTEMPTS)
        assert fake.add_job.call_count == 1
        assert fake.add_job.call_args.kwargs['args'] == [2]


# ---------------------------------------------------------------------------
# Scheduler registration and status
# ---------------------------------------------------------------------------

class TestSchedulerRegistration:
    @staticmethod
    def _fields(job):
        return {f.name: str(f) for f in job.trigger.fields}

    def test_daily_refresh_trigger(self):
        job = dashboard.scheduler.get_job('daily_refresh')
        fields = self._fields(job)
        assert (fields['day_of_week'], fields['hour'], fields['minute']) == ('mon-fri', '17', '30')
        assert str(job.trigger.timezone) in ('US/Eastern', 'America/New_York')
        # By name: another test module re-imports dashboard, so identity differs.
        assert job.func.__name__ == 'scheduled_data_refresh'

    def test_weekly_review_trigger(self):
        job = dashboard.scheduler.get_job('weekly_ai_review')
        fields = self._fields(job)
        assert (fields['day_of_week'], fields['hour'], fields['minute']) == ('fri', '18', '0')
        assert str(job.trigger.timezone) in ('US/Eastern', 'America/New_York')
        # By name: another test module re-imports dashboard, so identity differs.
        assert job.func.__name__ == 'scheduled_weekly_review'
        assert job.misfire_grace_time >= 3600
        assert job.next_run_time.astimezone(ET).weekday() == 4

    def test_scheduler_status_lists_both_jobs(self):
        body = app.test_client().get('/api/scheduler-status').get_json()
        ids = {j['id'] for j in body['jobs']}
        assert ids == {'daily_refresh', 'weekly_ai_review'}
        assert all(j['next_run'] for j in body['jobs'])
        # Legacy top-level fields still describe the daily refresh.
        assert body['next_run'] and 'Daily' in body['schedule']


# ---------------------------------------------------------------------------
# News: daily fetch, weekly summarize
# ---------------------------------------------------------------------------

def _article(day, n, url=None, topic='macro'):
    return {'headline': f'{day} story {n}', 'url': url or f'https://ex.com/{day}/{n}',
            'source': 'ex.com', 'timestamp': f'{day}T21:00:00+00:00',
            'raw_content': f'content {day} {n}', 'topic': topic}


class TestWeeklyNews:
    @pytest.fixture
    def store(self, tmp_path):
        with patch.object(news_pipeline, 'NEWS_CACHE_FILE', tmp_path / 'news_data.json'), \
             patch.object(news_pipeline, 'NEWS_REVIEWS_FILE', tmp_path / 'news_reviews.json'), \
             patch.object(news_pipeline, 'DATA_DIR', tmp_path):
            yield tmp_path

    def _seed(self, days):
        news_pipeline._save_cache({d: {'date': d, 'articles': arts} for d, arts in days.items()})

    def test_week_summary_covers_every_weekday(self, store):
        week = ['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30']
        days = {d: [_article(d, 1)] for d in week}
        days['2026-10-23'] = [_article('2026-10-23', 1)]  # previous Friday
        days['2026-10-31'] = [_article('2026-10-31', 1)]  # Saturday fetch
        self._seed(days)
        seen = {}
        with patch.object(news_pipeline, '_generate_cross_market_summary',
                          side_effect=lambda arts, *week: seen.setdefault('cross', arts) and 'weekly'), \
             patch.object(news_pipeline, '_generate_all_topic_summaries', return_value={'rates': 't'}), \
             patch.object(briefing_week, 'now_et', return_value=et(2026, 10, 30, 18, 10)), \
             briefing_week.scheduled_run(date(2026, 10, 26), date(2026, 10, 30)):
            assert news_pipeline.summarize_week_news(date(2026, 10, 26), date(2026, 10, 30))
        headlines = {a['headline'] for a in seen['cross']}
        assert headlines == {f'{d} story 1' for d in week}
        review = json.loads((store / 'news_reviews.json').read_text())['reviews'][0]
        assert review['run_type'] == 'scheduled'
        assert (review['week_start'], review['week_end']) == ('2026-10-26', '2026-10-30')
        assert 'raw_content' not in review['articles'][0]

    def test_duplicate_story_counted_once(self, store):
        same = 'https://ex.com/big-story'
        self._seed({d: [_article(d, 1, url=same), _article(d, 2)]
                    for d in ['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30']})
        arts = news_pipeline._week_articles(news_pipeline._load_cache(), '2026-10-26', '2026-10-30')
        assert [a['url'] for a in arts].count(same) == 1
        assert len(arts) == 6

    def test_caps_sample_the_whole_week(self, store):
        days = {d: [_article(d, i) for i in range(30)]
                for d in ['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30']}
        self._seed(days)
        arts = news_pipeline._week_articles(news_pipeline._load_cache(), '2026-10-26', '2026-10-30')
        first = arts[:news_pipeline.MAX_WEEKLY_ARTICLES]
        assert {a['timestamp'][:10] for a in first} == set(days)
        assert first[0]['timestamp'].startswith('2026-10-30')  # newest day leads

    def test_summary_less_weekday_does_not_mask_weekly_review(self, store):
        """QA's trap: Wednesday's articles-only record must not hide Friday's review."""
        news_pipeline._save_cache({'2026-11-04': {'date': '2026-11-04', 'articles': [_article('2026-11-04', 1)]}})
        news_pipeline._save_reviews({'reviews': [{
            'date': '2026-10-30', 'summary': 'Weekly cross-market', 'articles': [],
            'topic_summaries': {'crypto': 'Weekly crypto'},
            'week_start': '2026-10-26', 'week_end': '2026-10-30', 'run_type': 'scheduled',
            'published_at': '2026-10-30T18:10:00-04:00', 'through': '2026-10-30'}]})
        with patch.object(briefing_week, 'now_et', return_value=et(2026, 11, 4, 12)):
            stored = news_pipeline.get_stored_news()
            assert stored['summary'] == 'Weekly cross-market'
            assert ai_summary._get_stored_news_context() == 'Weekly cross-market'
            assert 'Weekly crypto' in ai_summary._get_stored_news_context(topic='crypto')
            page = app.test_client().get('/news')
        assert page.status_code == 200
        assert b'Weekly cross-market' in page.data

    def test_legacy_daily_summary_still_served(self, store):
        news_pipeline._save_cache({'2026-10-27': {'date': '2026-10-27', 'articles': [],
                                                  'summary': 'old daily'}})
        with patch.object(briefing_week, 'now_et', return_value=et(2026, 10, 28, 12)):
            assert news_pipeline.get_stored_news()['summary'] == 'old daily'

    def test_daily_fetch_keys_by_et_date(self, store):
        # 22:30 ET Friday is already Saturday in UTC; the record belongs to Friday.
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'k'}), \
             patch.object(news_pipeline, '_fetch_topic', return_value=[]), \
             patch.object(briefing_week, 'now_et', return_value=et(2026, 10, 30, 22, 30)):
            news_pipeline.run_news_pipeline()
        assert list(news_pipeline._load_cache()) == ['2026-10-30']


# ---------------------------------------------------------------------------
# Metering of the scheduled run
# ---------------------------------------------------------------------------

@pytest.fixture
def metering_db():
    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    with app.app_context():
        engines = db._app_engines[app]
        real_engine = engines[None]
        db.session.remove()
        engines[None] = engine
        try:
            db.create_all()
            yield
        finally:
            db.session.remove()
            engines[None] = real_engine
            engine.dispose()


class TestMetering:
    def test_meter_is_noop_outside_scheduled_run(self):
        with patch.object(usage_metering, 'record_usage') as rec:
            briefing_week.meter_ai_call({'input_tokens': 1}, 'claude-x')
        rec.assert_not_called()

    def test_call_ai_with_tools_meters_scheduled_calls(self):
        result = {'success': True, 'content': 'x', 'error': None,
                  'usage': {'input_tokens': 100, 'output_tokens': 50}, 'model': 'served-model'}
        with patch.object(ai_summary, '_call_anthropic_with_tools', return_value=result), \
             patch.object(ai_summary, '_dump_prompt_to_file'), \
             patch.object(briefing_week, 'meter_ai_call') as meter:
            ai_summary.call_ai_with_tools(object(), 's', 'u', provider='anthropic')
        meter.assert_called_once_with(result['usage'], 'served-model')

    def test_news_summarizer_meters(self):
        msg = MagicMock(model='served-model', stop_reason='end_turn')
        msg.usage.input_tokens, msg.usage.output_tokens = 10, 5
        client = MagicMock()
        client.beta.messages.create.return_value = msg
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'k'}), \
             patch('anthropic.Anthropic', return_value=client), \
             patch.object(news_pipeline, 'extract_text', return_value='summary'), \
             patch.object(news_pipeline, 'refusal_reason', return_value=None), \
             patch.object(news_pipeline, 'warn_if_truncated'), \
             patch.object(briefing_week, 'meter_ai_call') as meter:
            assert news_pipeline._summarize_with_anthropic('s', 'u') == 'summary'
        assert meter.call_args.args[1] == 'served-model'

    def test_scheduled_record_has_no_user_and_skips_user_views(self, metering_db):
        user = User(username='reader', email='reader@example.com')
        user.set_password('TestPass123')
        db.session.add(user)
        db.session.commit()
        usage_metering.record_usage(user.id, 'chatbot', 'claude-sonnet-4-6', 10, 10)
        # meter_ai_call imports lazily; pin the real module over the
        # sys.modules['services'] stub other test modules leave behind.
        services_pkg = types.ModuleType('services')
        services_pkg.__path__ = []
        services_pkg.usage_metering = usage_metering
        with patch.dict(sys.modules, {'services': services_pkg,
                                      'services.usage_metering': usage_metering}), \
             briefing_week.scheduled_run(date(2026, 10, 26), date(2026, 10, 30)):
            briefing_week.meter_ai_call(
                {'input_tokens': 1000, 'output_tokens': 500}, 'claude-sonnet-4-6')

        rows = AIUsageRecord.query.order_by(AIUsageRecord.id).all()
        assert [(r.user_id, r.interaction_type) for r in rows] == [
            (user.id, 'chatbot'), (None, 'scheduled_briefing')]
        assert rows[1].estimated_cost > 0
        assert 'scheduled_briefing' in AIUsageRecord.VALID_INTERACTION_TYPES

        top = admin_analytics.get_top_users()
        assert [(t['username'], t['call_count']) for t in top] == [('reader', 1)]
        # System spend still shows in the totals.
        assert admin_analytics.get_today_summary()['total_calls'] == 2


class TestMigration:
    def _load(self):
        path = SIGNALTRACKERS_DIR / 'migrations/versions/j5c6d7e8f9a0_allow_system_ai_usage_records.py'
        spec = importlib.util.spec_from_file_location('mig_j5c6', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_single_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config()
        cfg.set_main_option('script_location', str(SIGNALTRACKERS_DIR / 'migrations'))
        assert ScriptDirectory.from_config(cfg).get_heads() == ['j5c6d7e8f9a0']

    def test_upgrade_makes_user_id_nullable_and_keeps_rows(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        engine = create_engine('sqlite://')
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)'))
            conn.execute(text(
                'CREATE TABLE ai_usage_records (id INTEGER PRIMARY KEY, '
                'user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE, '
                'interaction_type VARCHAR(30) NOT NULL, timestamp DATETIME NOT NULL, '
                'input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, '
                'cache_creation_tokens INTEGER, model VARCHAR(100) NOT NULL, '
                'estimated_cost NUMERIC(12, 8) NOT NULL)'))
            conn.execute(text("INSERT INTO users VALUES ('u1')"))
            conn.execute(text(
                "INSERT INTO ai_usage_records (user_id, interaction_type, timestamp, model, "
                "estimated_cost) VALUES ('u1', 'chatbot', '2026-10-01', 'm', 0.5)"))
            with Operations.context(MigrationContext.configure(conn)):
                self._load().upgrade()
            cols = {c['name']: c for c in sa_inspect(conn).get_columns('ai_usage_records')}
            assert cols['user_id']['nullable'] is True
            fks = sa_inspect(conn).get_foreign_keys('ai_usage_records')
            assert fks and fks[0]['referred_table'] == 'users'
            assert conn.execute(text('SELECT COUNT(*) FROM ai_usage_records')).scalar() == 1
            conn.execute(text(
                "INSERT INTO ai_usage_records (user_id, interaction_type, timestamp, model, "
                "estimated_cost) VALUES (NULL, 'scheduled_briefing', '2026-10-30', 'm', 0.1)"))
