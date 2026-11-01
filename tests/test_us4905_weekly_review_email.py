"""
US-490.5: Saturday weekly review email and simplified settings.

Covers the preference migration, the Saturday send window (timezones, the
far-east wait for the Friday run, dedup, a bad timezone row), the admin
force-send, the email templates (structured, fallback, escaping) and the
settings card and POST handler.
"""

from datetime import date, datetime, timedelta
import importlib.util
from pathlib import Path
import re
import sys
from unittest.mock import patch

SIGNALTRACKERS_DIR = Path(__file__).parent.parent / 'signaltrackers'
sys.path.insert(0, str(SIGNALTRACKERS_DIR))

import pandas as pd
import pytest
import pytz
import sqlalchemy as sa

from dashboard import app
from extensions import db
from models import User, AlertPreference, Alert
import briefing_week
import weekly_email
import weekly_review
from jobs import email_jobs

SERVICE_NAME = 'services.briefing_email_service'


def _load_service():
    """Load the service by path. Other test modules leave a MagicMock in
    sys.modules['services'], so a plain import gets the mock in a full run."""
    spec = importlib.util.spec_from_file_location(
        SERVICE_NAME, SIGNALTRACKERS_DIR / 'services' / 'briefing_email_service.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bes = _load_service()


@pytest.fixture(autouse=True)
def _real_service(monkeypatch):
    """The job and the admin route import the service lazily: give them this one."""
    monkeypatch.setitem(sys.modules, SERVICE_NAME, bes)

TEMPLATES = SIGNALTRACKERS_DIR / 'templates'
MIGRATION = SIGNALTRACKERS_DIR / 'migrations' / 'versions' / 'k6d7e8f9a0b1_weekly_review_email_preference.py'

ET = pytz.timezone('America/New_York')
UTC = pytz.utc

# The week of Mon Oct 26 – Fri Oct 30, 2026; Saturday is Oct 31.
WEEK_START, WEEK_END, SATURDAY = date(2026, 10, 26), date(2026, 10, 30), date(2026, 10, 31)

STRUCTURED = {
    'headline': 'Yields eased as inflation cooled, and stocks took the hint.',
    'changed': ['10-year yield fell 12 bp to 4.10%.', 'CPI rose 0.2% in September.', 'S&P 500 gained 1.4%.'],
    'why': 'Cooler inflation gives the Fed room.\n\nCredit stayed calm throughout.',
    'watch': [
        {'day': 'Wed', 'event': 'FOMC decision', 'reason': 'first meeting since the soft CPI print'},
        {'day': 'Fri', 'event': 'Jobs report', 'reason': 'tests the soft-landing story'},
    ],
}


def _record(week_start=WEEK_START, week_end=WEEK_END, run_type='scheduled',
            structured=STRUCTURED, summary='Headline.\n\n## What changed\n- a', published=None):
    published = published or ET.localize(datetime.combine(week_end, datetime.min.time()).replace(hour=18, minute=40))
    return {
        'date': week_end.isoformat(),
        'generated_at': published.isoformat(),
        'summary': summary,
        'week_start': week_start.isoformat(),
        'week_end': week_end.isoformat(),
        'through': week_end.isoformat(),
        'published_at': published.isoformat(),
        'run_type': run_type,
        'structured': structured,
        'format': 'structured' if structured else 'markdown',
    }


def _utc(tz_name, y, m, d, hh, mm=0):
    return pytz.timezone(tz_name).localize(datetime(y, m, d, hh, mm)).astimezone(UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app_ctx():
    old = {k: app.config.get(k) for k in ('TESTING', 'BASE_URL', 'WTF_CSRF_ENABLED')}
    app.config.update(TESTING=True, BASE_URL='https://mail-test.example.com', WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.drop_all()
        db.create_all()
        try:
            yield app
        finally:
            db.session.remove()
            db.drop_all()
            db.create_all()
            app.config.update(old)


def _user(name, tz='America/New_York', enabled=True, admin=False):
    user = User(username=name, email=f'{name}@example.com', is_admin=admin)
    user.set_password('TestPass123')
    db.session.add(user)
    db.session.commit()
    prefs = user.create_default_alert_preferences()
    prefs.briefing_timezone = tz
    prefs.daily_briefing_enabled = enabled
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def _load_migration():
    spec = importlib.util.spec_from_file_location('mig_k6d7', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigration:

    ROWS = [
        # (enabled, frequency) -> expected enabled
        ((True, 'daily'), True),
        ((True, 'weekly'), True),
        ((True, 'off'), False),
        ((False, 'daily'), False),   # unsubscribed: must stay off
        ((False, 'weekly'), False),
        ((False, 'off'), False),
        ((True, 'monthly'), False),  # unknown value
        ((True, None), False),
    ]

    def test_mapping_preserves_opt_out(self):
        mig = _load_migration()
        engine = sa.create_engine('sqlite://')
        with engine.begin() as conn:
            conn.execute(sa.text('CREATE TABLE alert_preferences (id INTEGER PRIMARY KEY, '
                                 'daily_briefing_enabled BOOLEAN NOT NULL, briefing_frequency VARCHAR(20))'))
            for i, ((enabled, freq), _) in enumerate(self.ROWS):
                conn.execute(sa.text('INSERT INTO alert_preferences VALUES (:i, :e, :f)'),
                             {'i': i, 'e': enabled, 'f': freq})
            statement, params = mig.disable_off_frequency_sql()
            conn.execute(statement, params)
            got = dict(conn.execute(sa.text('SELECT id, daily_briefing_enabled FROM alert_preferences')).all())
        for i, (start, expected) in enumerate(self.ROWS):
            assert bool(got[i]) is expected, start

    def test_single_alembic_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config()
        cfg.set_main_option('script_location', str(SIGNALTRACKERS_DIR / 'migrations'))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert heads == ['k6d7e8f9a0b1']

    def test_migration_runs_against_model_table(self, app_ctx):
        """The UPDATE matches the real alert_preferences schema."""
        on, off = _user('on'), _user('off')
        off.alert_preferences.briefing_frequency = 'off'
        db.session.commit()
        statement, params = _load_migration().disable_off_frequency_sql()
        db.session.execute(statement, params)
        db.session.commit()
        db.session.expire_all()
        assert AlertPreference.query.filter_by(user_id=on.id).one().daily_briefing_enabled is True
        assert AlertPreference.query.filter_by(user_id=off.id).one().daily_briefing_enabled is False


# ---------------------------------------------------------------------------
# Send window (pure)
# ---------------------------------------------------------------------------

class TestSendWindow:

    @pytest.mark.parametrize('hh,mm', [(8, 0), (8, 15), (8, 30), (8, 45), (9, 0), (23, 45)])
    def test_due_saturday_from_eight(self, hh, mm):
        now = _utc('America/New_York', 2026, 10, 31, hh, mm)
        assert weekly_email.review_due_week('America/New_York', now) == WEEK_END

    @pytest.mark.parametrize('when', [
        (2026, 10, 31, 7, 45), (2026, 10, 31, 0, 0),
        (2026, 11, 1, 8, 0), (2026, 11, 2, 8, 0), (2026, 11, 4, 8, 0), (2026, 10, 30, 8, 0),
    ])
    def test_not_due_other_times(self, when):
        assert weekly_email.review_due_week('America/New_York', _utc('America/New_York', *when)) is None

    @pytest.mark.parametrize('tz', ['America/Los_Angeles', 'Europe/London', 'Asia/Kolkata',
                                    'Asia/Tokyo', 'Pacific/Auckland'])
    def test_same_review_week_in_every_timezone(self, tz):
        assert weekly_email.review_due_week(tz, _utc(tz, 2026, 10, 31, 8)) == WEEK_END

    def test_container_tz_does_not_matter(self, monkeypatch):
        """The old job used naive utcnow().astimezone(), which reads host local time."""
        import time
        monkeypatch.setenv('TZ', 'America/New_York')
        if hasattr(time, 'tzset'):
            time.tzset()
        try:
            now = _utc('Europe/London', 2026, 10, 31, 8, 5)
            assert weekly_email.review_due_week('Europe/London', now) == WEEK_END
        finally:
            monkeypatch.delenv('TZ')
            if hasattr(time, 'tzset'):
                time.tzset()

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            weekly_email.review_due_week('America/New_York', datetime(2026, 10, 31, 8))

    def test_invalid_timezone_falls_back(self):
        assert weekly_email.user_timezone('Not/AZone').zone == 'America/New_York'
        assert weekly_email.review_due_week('Not/AZone', _utc('America/New_York', 2026, 10, 31, 8)) == WEEK_END

    def test_already_sent(self):
        assert not weekly_email.already_sent(None, WEEK_END)
        assert not weekly_email.already_sent(date(2026, 10, 24), WEEK_END)  # last week
        assert not weekly_email.already_sent(WEEK_END, WEEK_END)  # old daily email on Friday
        assert weekly_email.already_sent(SATURDAY, WEEK_END)
        assert weekly_email.already_sent(SATURDAY + timedelta(days=1), WEEK_END)

    @pytest.mark.parametrize('now,last_sent,expected', [
        (('America/New_York', 2026, 10, 30, 10), None, SATURDAY),            # Friday
        (('America/New_York', 2026, 10, 31, 7), None, SATURDAY),             # Sat before send
        (('America/New_York', 2026, 10, 31, 9), SATURDAY, date(2026, 11, 7)),  # Sat after send
        (('America/New_York', 2026, 10, 31, 9), date(2026, 10, 24), SATURDAY),  # Sat, still waiting
        (('America/New_York', 2026, 11, 1, 9), SATURDAY, date(2026, 11, 7)),  # Sunday
    ])
    def test_next_email_date(self, now, last_sent, expected):
        assert weekly_email.next_email_date(now[0], last_sent, _utc(*now)) == expected

    def test_next_email_uses_user_timezone(self):
        # Fri 20:00 ET is already Saturday 09:00 in Tokyo.
        now = _utc('America/New_York', 2026, 10, 30, 20)
        assert weekly_email.next_email_date('Asia/Tokyo', None, now) == SATURDAY
        assert weekly_email.next_email_date('Asia/Tokyo', SATURDAY, now) == date(2026, 11, 7)

    def test_short_date(self):
        assert weekly_email.format_short_date(SATURDAY) == 'Sat, Oct 31'

    @pytest.mark.parametrize('start,end,expected', [
        ('2026-10-26', '2026-10-30', 'Oct 26–30'),
        ('2026-10-29', '2026-11-02', 'Oct 29 – Nov 2'),
        ('2025-12-29', '2026-01-02', 'Dec 29, 2025 – Jan 2, 2026'),
    ])
    def test_subject_range(self, start, end, expected):
        assert weekly_email.subject_range({'week_start': start, 'week_end': end}) == expected

    def test_subject_range_missing(self):
        assert weekly_email.subject_range({}) is None
        assert weekly_email.subject_range(None) is None


# ---------------------------------------------------------------------------
# Job: a full week in 15-minute steps
# ---------------------------------------------------------------------------

class _Clock:
    """Records visible to the email job at the simulated time."""

    def __init__(self, records):
        self.now = None
        self.records = records  # list of (visible_from_utc, record)

    def load(self):
        return {'summaries': [r for t, r in self.records if t <= self.now]}


def _run_week(clock, start, end, step=timedelta(minutes=15)):
    sends = []

    def fake_send(**kwargs):
        sends.append((clock.now, kwargs['to'], kwargs['subject']))
        return True

    with patch.object(bes, 'load_summaries', side_effect=clock.load), \
         patch.object(bes, 'send_email', side_effect=fake_send), \
         patch.object(bes, 'get_weekly_moves', return_value=[]):
        t = start
        while t <= end:
            clock.now = t
            email_jobs.send_weekly_reviews(now=t)
            t += step
    return sends


FRIDAY_RUN_DONE = ET.localize(datetime(2026, 10, 30, 18, 40)).astimezone(UTC)
PREVIOUS_REVIEW = _record(date(2026, 10, 19), date(2026, 10, 23))
THIS_REVIEW = _record()

TIMEZONES = ['America/New_York', 'America/Los_Angeles', 'Europe/London',
             'Asia/Kolkata', 'Asia/Tokyo', 'Pacific/Auckland']


class TestJob:

    def test_one_send_per_user_at_saturday_eight_local(self, app_ctx):
        users = {tz: _user(tz.replace('/', '_').lower(), tz) for tz in TIMEZONES}
        bad = _user('badzone')
        bad.alert_preferences.briefing_timezone = 'Not/AZone'
        db.session.commit()
        _user('optedout', enabled=False)

        clock = _Clock([(datetime(2026, 1, 1, tzinfo=UTC), PREVIOUS_REVIEW),
                        (FRIDAY_RUN_DONE, THIS_REVIEW)])
        # Sunday noon ET: the previous Saturday is over in every zone tested.
        start = ET.localize(datetime(2026, 10, 25, 12, 0)).astimezone(UTC)
        end = ET.localize(datetime(2026, 11, 1, 23, 45)).astimezone(UTC)    # next Sunday
        sends = _run_week(clock, start, end)

        by_user = {}
        for t, to, subject in sends:
            by_user.setdefault(to, []).append((t, subject))
        assert 'optedout@example.com' not in by_user
        assert len(by_user['badzone@example.com']) == 1  # falls back to New York

        for tz, user in users.items():
            got = by_user[user.email]
            assert len(got) == 1, tz
            t, subject = got[0]
            assert subject == 'Your Week in Review: Oct 26–30'
            local = t.astimezone(pytz.timezone(tz))
            assert local.weekday() == 5, tz
            if tz == 'Pacific/Auckland':
                # Sat 08:00 NZDT is Fri 15:00 ET, before the Friday run:
                # the email waits for this week's review instead of sending last week's.
                assert t >= FRIDAY_RUN_DONE
            elif tz in ('Asia/Tokyo',):
                assert t >= FRIDAY_RUN_DONE
            else:
                assert (local.hour, local.minute) == (8, 0), tz

    def test_no_new_review_means_no_email(self, app_ctx):
        _user('ny')
        clock = _Clock([(datetime(2026, 1, 1, tzinfo=UTC), PREVIOUS_REVIEW)])
        start = ET.localize(datetime(2026, 10, 31, 0, 0)).astimezone(UTC)
        end = ET.localize(datetime(2026, 11, 1, 23, 45)).astimezone(UTC)
        assert _run_week(clock, start, end) == []

    def test_manual_regenerate_is_not_the_scheduled_review(self, app_ctx):
        _user('ny')
        manual = _record(run_type='manual')
        clock = _Clock([(datetime(2026, 1, 1, tzinfo=UTC), manual)])
        now = _utc('America/New_York', 2026, 10, 31, 8)
        assert _run_week(clock, now, now) == []

    def test_dedup_survives_restart_and_timezone_change(self, app_ctx):
        user = _user('ldn', 'Europe/London')
        clock = _Clock([(FRIDAY_RUN_DONE, THIS_REVIEW)])
        t1 = _utc('Europe/London', 2026, 10, 31, 8)
        assert len(_run_week(clock, t1, t1)) == 1
        # "Restart": a fresh session, same DB.
        db.session.remove()
        assert _run_week(clock, t1 + timedelta(minutes=15), t1 + timedelta(minutes=15)) == []
        # Move to Los Angeles after the send; LA's Saturday 08:00 comes later.
        prefs = AlertPreference.query.filter_by(user_id=user.id).one()
        prefs.briefing_timezone = 'America/Los_Angeles'
        db.session.commit()
        t2 = _utc('America/Los_Angeles', 2026, 10, 31, 8)
        assert _run_week(clock, t2, t2) == []

    def test_force_send_on_a_weekday(self, app_ctx):
        _user('ny')
        _user('off', enabled=False)
        clock = _Clock([(FRIDAY_RUN_DONE, THIS_REVIEW)])
        clock.now = _utc('America/New_York', 2026, 11, 4, 11)
        sent = []
        with patch.object(bes, 'load_summaries', side_effect=clock.load), \
             patch.object(bes, 'send_email', side_effect=lambda **kw: sent.append(kw['to']) or True), \
             patch.object(bes, 'get_weekly_moves', return_value=[]):
            result = email_jobs.send_weekly_reviews(now=clock.now, force=True)
        assert sent == ['ny@example.com']
        assert result['sent'] == 1 and result['review_week_end'] == '2026-10-30'


# ---------------------------------------------------------------------------
# Email content
# ---------------------------------------------------------------------------

def _render(content, **overrides):
    """Render both templates through the real sender, capture the send call."""
    user = _user('reader')
    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return True

    with patch.object(bes, 'send_email', side_effect=fake_send), \
         patch.object(bes, 'get_portfolio_analysis_snippet', return_value=overrides.pop('portfolio', None)):
        assert bes.send_weekly_review_to_user(user, content, moves=overrides.pop('moves', []))
    from flask import render_template
    ctx = {k: v for k, v in captured.items() if k not in ('to', 'subject', 'template_html', 'template_txt', 'extra_headers')}
    html = re.sub(r'<!--(?!\s*\d\.).*?-->', '', render_template(captured['template_html'], **ctx), flags=re.DOTALL)
    text = render_template(captured['template_txt'], **ctx)
    return captured, html, text


MOVES = [{'name': 'S&P 500 (SPY)', 'value': '580.12', 'percentile': 97, 'change': '+1.4%', 'change_arrow': '↑'}]


class TestEmailContent:

    def test_structured_email(self, app_ctx):
        sent, html, text = _render(bes.build_review_content(_record()), moves=MOVES)
        assert sent['subject'] == 'Your Week in Review: Oct 26–30'
        assert sent['template_html'] == 'email/weekly_review.html'

        # Hidden preheader is the headline.
        assert re.search(r'display: none;[^>]*>' + re.escape(STRUCTURED['headline']), html)

        order = ['Your Week in Review', 'Week of Oct 26–30, 2026', 'This week in one line',
                 'The Week in Review', 'What changed', 'Why it matters',
                 'Market Conditions at a Glance', 'Biggest Moves This Week',
                 'What to Watch Next Week', 'Read the Full Review →']
        positions = [html.index(s) for s in order]
        assert positions == sorted(positions)

        assert '#09264c' in html and '#1E40AF' in html
        assert '#667eea' not in html
        hero = html[html.index('<!-- 1. Hero -->'):html.index('<!-- 2.')]
        assert '📊' not in hero and 'background-color: #09264c' in hero
        assert 'Today' not in html and 'Last 24h' not in html
        assert '>Week</th>' in html
        assert 'FOMC decision' in html and 'width="60"' in html
        assert '10-year yield fell 12 bp' in html

        text_order = ['YOUR WEEK IN REVIEW', 'THIS WEEK IN ONE LINE', 'THE WEEK IN REVIEW',
                      'WHAT CHANGED', 'WHY IT MATTERS', 'MARKET CONDITIONS AT A GLANCE',
                      'BIGGEST MOVES THIS WEEK', 'WHAT TO WATCH NEXT WEEK']
        positions = [text.index(s) for s in text_order]
        assert positions == sorted(positions)
        assert 'Wed: FOMC decision - first meeting since the soft CPI print' in text

    def test_no_blank_padding_around_full_bleed_rows(self, app_ctx):
        """Design review: the hero sits flush under the navy header, and the CTA
        is followed straight by the footer (no empty padded content rows)."""
        _, html, _ = _render(bes.build_review_content(_record()), moves=MOVES)
        header_end = html.index('Macro Finance Data &amp; Analysis')
        hero = html.index('<!-- 1. Hero -->')
        between = html[header_end:hero]
        assert 'padding: 0;' in between
        assert '30px 30px 40px 30px' not in between
        cta = html.index('Read the Full Review')
        footer = html.index('This email was sent by')
        assert '30px 30px 40px 30px' not in html[cta:footer]
        assert '30px 30px 40px 30px' not in html

    def test_other_emails_keep_default_content_padding(self, app_ctx):
        from flask import render_template
        html = render_template('email/test_email.html', unsubscribe_url='#', user=None, username='x')
        assert 'padding: 30px 30px 40px 30px;' in html

    def test_alerts_and_portfolio_blocks(self, app_ctx):
        content = bes.build_review_content(_record())
        _, html, text = _render(content, portfolio='Your mix is balanced.')
        assert 'Your Portfolio Context' in html and 'YOUR PORTFOLIO CONTEXT' in text
        positions = [html.index('What to Watch Next Week'), html.index('Your Portfolio Context'),
                     html.index('Read the Full Review')]
        assert positions == sorted(positions)

    def test_fallback_markdown_body(self, app_ctx):
        summary = '**Stocks rose as yields fell.** More detail here.\n\n- First point\n- Second point'
        content = bes.build_review_content(_record(structured=None, summary=summary))
        sent, html, text = _render(content)
        assert sent['subject'] == 'Your Week in Review: Oct 26–30'
        assert sent['preheader'] == 'Stocks rose as yields fell.'
        assert '<strong>Stocks rose as yields fell.</strong>' in html
        assert '<li style="margin-bottom: 8px;">First point</li>' in html
        assert 'This week in one line' not in html
        assert 'What to Watch Next Week' not in html
        assert 'THE WEEK IN REVIEW' in text and '- First point' in text and '**' not in text
        assert 'None' not in html and 'None' not in text

    def test_escaping(self, app_ctx):
        evil = '<img src=x onerror=alert(1)>'
        structured = dict(STRUCTURED, headline=evil, changed=[evil],
                          watch=[{'day': 'Wed', 'event': evil, 'reason': evil}])
        _, html, _ = _render(bes.build_review_content(_record(structured=structured)))
        assert '<img src=x' not in html
        assert '&lt;img src=x onerror=alert(1)&gt;' in html

    def test_fallback_escaping(self, app_ctx):
        content = bes.build_review_content(_record(structured=None, summary='Hi <script>x</script> **b**'))
        _, html, _ = _render(content)
        assert '<script>' not in html and '&lt;script&gt;' in html

    def test_alerts_past_seven_days(self, app_ctx):
        user = _user('alerted')
        now = datetime.utcnow()
        for days, title in ((6, 'six days'), (8, 'eight days')):
            db.session.add(Alert(user_id=user.id, alert_type='vix_spike', title=title,
                                 triggered_at=now - timedelta(days=days)))
        db.session.commit()
        titles = [a['title'] for a in bes.get_user_triggered_alerts(user.id)]
        assert titles == ['six days']

    def test_opted_out_user_is_never_sent(self, app_ctx):
        user = _user('off', enabled=False)
        with patch.object(bes, 'send_email', return_value=True) as send:
            assert bes.send_weekly_review_to_user(user, bes.build_review_content(_record()), moves=[]) is False
        send.assert_not_called()

    def test_test_email_copy(self):
        for name in ('test_email.html', 'test_email.txt'):
            body = (TEMPLATES / 'email' / name).read_text()
            assert 'A weekly market review every Saturday morning' in body
            assert 'Daily market briefings' not in body

    def test_old_templates_removed(self):
        assert not (TEMPLATES / 'email' / 'daily_briefing.html').exists()
        assert not (TEMPLATES / 'email' / 'daily_briefing.txt').exists()


# ---------------------------------------------------------------------------
# Weekly moves
# ---------------------------------------------------------------------------

def test_biggest_moves_uses_the_review_week(tmp_path):
    days = pd.bdate_range('2025-01-01', '2026-10-30')
    # SPY: steady +0.1/day, then +5% in the review week. Gold: flat-ish noise.
    spy = [500 + i * 0.1 for i in range(len(days))]
    week = days >= pd.Timestamp('2026-10-26')
    base = spy[list(week).index(True) - 1]
    for i, in_week in enumerate(week):
        if in_week:
            spy[i] = base * 1.05
    pd.DataFrame({'date': days, 'sp500_price': spy}).to_csv(tmp_path / 'sp500_price.csv', index=False)
    gold = [200 + (i % 3) for i in range(len(days))]
    pd.DataFrame({'date': days, 'gold_price': gold}).to_csv(tmp_path / 'gold_price.csv', index=False)
    tsy = [4.0 + (i % 7) * 0.01 for i in range(len(days))]
    pd.DataFrame({'date': days, 'treasury_10y': tsy}).to_csv(tmp_path / 'treasury_10y.csv', index=False)

    moves = weekly_review.biggest_moves(WEEK_START, WEEK_END, data_dir=tmp_path)
    assert moves[0]['metric'] == 'sp500_price'
    assert moves[0]['change'] == '+5.0%'
    assert moves[0]['change_arrow'] == '↑'
    assert moves[0]['percentile'] == 100
    by_metric = {m['metric']: m for m in moves}
    assert by_metric['treasury_10y']['change'].endswith(' bp')
    assert by_metric['treasury_10y']['value'].endswith('%')


# ---------------------------------------------------------------------------
# Settings page and POST
# ---------------------------------------------------------------------------

class TestSettings:

    def test_card_copy_and_removed_fields(self, app_ctx):
        user = _user('setter')
        client = app.test_client()
        _login(client, user)
        html = client.get('/settings/alerts').get_data(as_text=True)
        assert 'Weekly Review Email' in html
        assert 'Email me the weekly review' in html
        assert "Arrives Saturday morning: the week's biggest changes" in html or \
               'Arrives Saturday morning: the week&#39;s biggest changes' in html
        assert 'Delivered around 8:00 AM Saturday, your time' in html
        assert 'include_portfolio_analysis' in html
        assert 'briefing_frequency' not in html
        assert not re.search(r'(name|id|for)="briefing_time"', html)
        assert re.search(r'Next email: (Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w{3} \d{1,2}', html)
        assert not re.search(r"daily briefing|today's briefing|daily .* briefing", html, re.IGNORECASE)

    def test_next_email_hidden_when_disabled(self, app_ctx):
        user = _user('quiet', enabled=False)
        client = app.test_client()
        _login(client, user)
        html = client.get('/settings/alerts').get_data(as_text=True)
        assert 'Next email:' not in html
        assert 'id="next-email"' not in html

    def test_post_without_removed_fields(self, app_ctx):
        user = _user('poster')
        client = app.test_client()
        _login(client, user)
        resp = client.post('/settings/alerts/save', data={
            'briefing_timezone': 'Asia/Tokyo', 'include_portfolio_analysis': 'on'})
        assert resp.status_code == 200 and resp.get_json()['success'] is True
        db.session.expire_all()
        prefs = AlertPreference.query.filter_by(user_id=user.id).one()
        assert prefs.daily_briefing_enabled is False  # toggle absent = off
        assert prefs.briefing_timezone == 'Asia/Tokyo'

    def test_post_ignores_stale_fields(self, app_ctx):
        user = _user('stale')
        client = app.test_client()
        _login(client, user)
        resp = client.post('/settings/alerts/save', data={
            'daily_briefing_enabled': 'on', 'briefing_frequency': 'off',
            'briefing_time': 'garbage', 'briefing_timezone': 'Europe/London'})
        assert resp.get_json()['success'] is True
        db.session.expire_all()
        prefs = AlertPreference.query.filter_by(user_id=user.id).one()
        assert prefs.daily_briefing_enabled is True
        assert prefs.briefing_frequency == 'daily'  # untouched, ignored

    def test_post_invalid_timezone_falls_back(self, app_ctx):
        user = _user('tzbad', 'Asia/Tokyo')
        client = app.test_client()
        _login(client, user)
        client.post('/settings/alerts/save', data={
            'daily_briefing_enabled': 'on', 'briefing_timezone': 'Not/AZone'})
        db.session.expire_all()
        assert AlertPreference.query.filter_by(user_id=user.id).one().briefing_timezone == 'America/New_York'


# ---------------------------------------------------------------------------
# Admin trigger
# ---------------------------------------------------------------------------

class TestAdminTrigger:

    def test_admin_force_send(self, app_ctx):
        admin = _user('boss', admin=True)
        client = app.test_client()
        _login(client, admin)
        with patch.object(bes, 'load_summaries', return_value={'summaries': [THIS_REVIEW]}), \
             patch.object(bes, 'send_email', return_value=True) as send, \
             patch.object(bes, 'get_weekly_moves', return_value=[]):
            resp = client.get('/admin/trigger-daily-briefing')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['sent'] == 1
        assert 'Weekly review email sent to 1 user(s)' == body['status']
        assert 'Daily' not in body['status']
        assert send.call_args.kwargs['subject'] == 'Your Week in Review: Oct 26–30'

    def test_non_admin_forbidden(self, app_ctx):
        user = _user('pleb')
        client = app.test_client()
        _login(client, user)
        assert client.get('/admin/trigger-daily-briefing').status_code == 403

    def test_anonymous_redirected(self, app_ctx):
        assert app.test_client().get('/admin/trigger-daily-briefing').status_code == 302
