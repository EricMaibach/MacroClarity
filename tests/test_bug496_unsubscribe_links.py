"""
Bug #496: Unsubscribe links in briefing and alert emails 404.

Both emails built their unsubscribe links with f-strings that matched no route.
The round-trip tests below render each email through its real service
function, take ``unsubscribe_url`` out of the captured send context and follow
it with the test client. They never restate the path, so they fail if the
email and the route drift apart again.
"""

import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent.parent / 'signaltrackers'))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from dashboard import app
from extensions import db
from models import User, AlertPreference, Alert
import email_links
# Imported at module level: other test modules replace sys.modules['services']
# with a MagicMock at collection time, so a lazy import here would get the mock.
from services import briefing_email_service, alert_email_service, email_service

BASE_URL = 'https://mail-test.example.com'


@pytest.fixture
def client():
    """Test client on a private in-memory database.

    Setting SQLALCHEMY_DATABASE_URI after the app is imported has no effect in
    Flask-SQLAlchemy 3 (engines are created in init_app), so the engine is
    swapped directly. Otherwise the test would write to the checkout's real
    signaltrackers.db.
    """
    engine = create_engine(
        'sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False}
    )
    old = {k: app.config.get(k) for k in ('TESTING', 'BASE_URL', 'WTF_CSRF_ENABLED')}
    app.config.update(TESTING=True, BASE_URL=BASE_URL, WTF_CSRF_ENABLED=True)
    with app.app_context():
        engines = db._app_engines[app]
        real_engine = engines[None]
        db.session.remove()
        engines[None] = engine
        try:
            db.create_all()
            yield app.test_client()
        finally:
            db.session.remove()
            engines[None] = real_engine
            engine.dispose()
            app.config.update(old)


def _make_user(name='reader', with_prefs=True):
    user = User(username=name, email=f'{name}@example.com')
    user.set_password('TestPass123')
    db.session.add(user)
    db.session.commit()
    if with_prefs:
        user.create_default_alert_preferences()
    return user


def _prefs(user_id):
    db.session.expire_all()
    return AlertPreference.query.filter_by(user_id=user_id).first()


def _path(url):
    parts = urlsplit(url)
    return parts.path + (f'?{parts.query}' if parts.query else '')


def _briefing_send(user):
    """Run the real briefing sender and return (context, headers) it sent."""
    with patch.object(briefing_email_service, 'send_email', return_value=True) as send, \
         patch.object(briefing_email_service, 'get_market_briefing_content', return_value=None), \
         patch.object(briefing_email_service, 'get_market_conditions_summary', return_value=None), \
         patch.object(briefing_email_service, 'get_top_movers', return_value=[]), \
         patch.object(briefing_email_service, 'get_user_triggered_alerts', return_value=[]), \
         patch.object(briefing_email_service, 'get_portfolio_analysis_snippet', return_value=None):
        sent = briefing_email_service.send_daily_briefing_to_user(user)
    return sent, send


def _alert_send(user):
    alert = Alert(
        user_id=user.id, alert_type='vix_spike', title='VIX crossed 30',
        severity='warning', metric_name='VIX', metric_value=31.5, threshold_value=30,
    )
    db.session.add(alert)
    db.session.commit()
    with patch.object(alert_email_service, 'send_email', return_value=True) as send:
        sent = alert_email_service.send_alert_notification(user, [alert])
    return sent, send


# ---------------------------------------------------------------------------
# Round trip: email -> link -> route -> preference
# ---------------------------------------------------------------------------

class TestRoundTrip:

    def test_briefing_link_unsubscribes(self, client):
        user = _make_user()
        sent, send = _briefing_send(user)
        assert sent and send.call_count == 1
        url = send.call_args.kwargs['unsubscribe_url']

        assert url.startswith(BASE_URL + '/')
        assert client.get(_path(url)).status_code == 200
        assert _prefs(user.id).daily_briefing_enabled is True  # GET changes nothing

        resp = client.post(_path(url))
        assert resp.status_code == 200
        assert b"You've Been Unsubscribed" in resp.data
        assert b'daily briefing' in resp.data
        assert _prefs(user.id).daily_briefing_enabled is False
        assert _prefs(user.id).alerts_enabled is True

    def test_alert_link_unsubscribes(self, client):
        user = _make_user()
        sent, send = _alert_send(user)
        assert sent and send.call_count == 1
        url = send.call_args.kwargs['unsubscribe_url']

        assert url.startswith(BASE_URL + '/')
        assert client.get(_path(url)).status_code == 200
        assert _prefs(user.id).alerts_enabled is True

        resp = client.post(_path(url))
        assert resp.status_code == 200
        assert b'alert notification' in resp.data
        assert _prefs(user.id).alerts_enabled is False
        assert _prefs(user.id).daily_briefing_enabled is True

    def test_unsubscribed_user_gets_no_further_briefing(self, client):
        user = _make_user()
        _, send = _briefing_send(user)
        client.post(_path(send.call_args.kwargs['unsubscribe_url']))

        db.session.expire_all()
        sent, send = _briefing_send(db.session.get(User, user.id))
        assert sent is False
        send.assert_not_called()

    def test_unsubscribed_user_gets_no_further_alerts(self, client):
        user = _make_user()
        _, send = _alert_send(user)
        client.post(_path(send.call_args.kwargs['unsubscribe_url']))

        db.session.add(Alert(
            user_id=user.id, alert_type='vix_spike', title='VIX crossed 35',
            severity='critical', metric_name='VIX', metric_value=36, threshold_value=35,
        ))
        db.session.commit()
        with patch.object(alert_email_service, 'send_email', return_value=True) as send:
            alert_email_service.send_pending_alert_notifications()
        send.assert_not_called()

    def test_every_link_in_both_emails_resolves(self, client):
        """No other email link is built from a hand-written path."""
        user = _make_user()
        _, briefing = _briefing_send(user)
        _, alert = _alert_send(user)
        adapter = app.url_map.bind('mail-test.example.com', url_scheme='https')
        for send in (briefing, alert):
            for key, value in send.call_args.kwargs.items():
                if key.endswith('_url'):
                    assert value.startswith(BASE_URL + '/'), key
                    adapter.match(urlsplit(value).path, method='GET')  # raises NotFound


# ---------------------------------------------------------------------------
# RFC 8058 one-click headers
# ---------------------------------------------------------------------------

class TestListUnsubscribeHeaders:

    @pytest.mark.parametrize('sender', [_briefing_send, _alert_send])
    def test_one_click_header_posts_to_the_email_link(self, client, sender):
        user = _make_user()
        _, send = sender(user)
        headers = send.call_args.kwargs['extra_headers']
        url = send.call_args.kwargs['unsubscribe_url']

        assert headers['List-Unsubscribe'] == f'<{url}>'
        assert headers['List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'

        # A mail client's one-click POST carries no CSRF token or session.
        resp = client.post(
            _path(url), data={'List-Unsubscribe': 'One-Click'},
            content_type='application/x-www-form-urlencoded',
        )
        assert resp.status_code == 200

    def test_send_email_passes_headers_to_message(self, client):
        with patch.object(email_service.mail, 'send') as mail_send:
            assert email_service.send_email(
                to='a@example.com', subject='s',
                template_html='email/test_email.html',
                template_txt='email/test_email.txt',
                extra_headers={'List-Unsubscribe': '<https://x/>'},
                recipient_email='a@example.com',
            )
        msg = mail_send.call_args.args[0]
        assert msg.extra_headers == {'List-Unsubscribe': '<https://x/>'}
        assert 'List-Unsubscribe: <https://x/>' in msg.as_string()


# ---------------------------------------------------------------------------
# Token security and edge cases
# ---------------------------------------------------------------------------

class TestTokens:

    def test_briefing_token_cannot_unsubscribe_alerts(self, client):
        user = _make_user()
        token = email_links.make_unsubscribe_token(user.id, 'briefing')
        assert client.post(f'/unsubscribe/alerts/{token}').status_code == 404
        assert _prefs(user.id).alerts_enabled is True

    def test_tampered_token_rejected(self, client):
        user = _make_user()
        token = email_links.make_unsubscribe_token(user.id, 'briefing')
        tampered = token[:-2] + ('AA' if not token.endswith('AA') else 'BB')
        assert client.post(f'/unsubscribe/briefing/{tampered}').status_code == 404
        assert _prefs(user.id).daily_briefing_enabled is True

    def test_token_signed_with_other_key_rejected(self, client):
        user = _make_user()
        from itsdangerous import URLSafeSerializer
        forged = URLSafeSerializer('not-the-key', salt='unsubscribe-briefing').dumps(user.id)
        assert client.post(f'/unsubscribe/briefing/{forged}').status_code == 404

    def test_valid_token_for_deleted_user_is_404(self, client):
        token = email_links.make_unsubscribe_token('00000000-0000-0000-0000-000000000000', 'alerts')
        assert client.get(f'/unsubscribe/alerts/{token}').status_code == 404
        assert client.post(f'/unsubscribe/alerts/{token}').status_code == 404

    @pytest.mark.parametrize('token', [
        'garbage', '..%2F..%2Fetc', 'x' * 500, '%C3%A9t%C3%A9', 'e30.sig',
    ])
    def test_malformed_token_is_404(self, client, token):
        assert client.get(f'/unsubscribe/briefing/{token}').status_code == 404
        assert client.post(f'/unsubscribe/briefing/{token}').status_code == 404

    def test_unknown_email_type_is_404(self, client):
        user = _make_user()
        token = email_links.make_unsubscribe_token(user.id, 'briefing')
        assert client.get(f'/unsubscribe/marketing/{token}').status_code == 404

    def test_old_routes_are_gone(self, client):
        user = _make_user()
        for path in (f'/unsubscribe/{user.id}', f'/alerts/unsubscribe/{user.id}',
                     f'/unsubscribe/alerts/{user.id}', f'/unsubscribe/briefing/{user.id}'):
            assert client.post(path).status_code in (404, 405), path
        assert _prefs(user.id).daily_briefing_enabled is True
        assert _prefs(user.id).alerts_enabled is True

    @pytest.mark.parametrize('email_type', ['briefing', 'alerts'])
    @pytest.mark.parametrize('method', ['get', 'head', 'options'])
    def test_safe_methods_do_not_unsubscribe(self, client, method, email_type):
        # Bug #498: Flask routes HEAD to the view, and link checkers send it.
        user = _make_user()
        url = email_links.unsubscribe_url(user, email_type)
        assert getattr(client, method)(_path(url)).status_code == 200
        assert _prefs(user.id).daily_briefing_enabled is True
        assert _prefs(user.id).alerts_enabled is True

    def test_double_post_is_idempotent(self, client):
        user = _make_user()
        url = email_links.unsubscribe_url(user, 'alerts')
        assert client.post(_path(url)).status_code == 200
        assert client.post(_path(url)).status_code == 200
        assert _prefs(user.id).alerts_enabled is False

    def test_user_without_preferences_row_records_opt_out(self, client):
        user = _make_user(with_prefs=False)
        assert _prefs(user.id) is None
        url = email_links.unsubscribe_url(user, 'briefing')
        assert client.post(_path(url)).status_code == 200
        prefs = _prefs(user.id)
        assert prefs is not None
        assert prefs.daily_briefing_enabled is False
        assert prefs.alerts_enabled is True


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

class TestExternalUrl:

    def test_works_without_request_context(self, client):
        """Scheduled jobs send with an app context only."""
        from flask import has_request_context
        assert not has_request_context()
        assert email_links.external_url('index') == BASE_URL + '/'

    def test_base_url_with_path_prefix(self, client):
        app.config['BASE_URL'] = 'https://example.com/app/'
        assert email_links.external_url('alert_history') == 'https://example.com/app/alerts/history'
