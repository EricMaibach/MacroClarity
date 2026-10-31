"""
Alert Models Tests

Unit tests for AlertPreference and Alert models.
"""

import sys
from pathlib import Path
from datetime import datetime

# Add signaltrackers directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'signaltrackers'))

import pytest
from dashboard import app
from extensions import db
from models import User, AlertPreference, Alert


@pytest.fixture
def test_app():
    """Create test application with in-memory database."""
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False

    with app.app_context():
        # The database is the in-memory one from tests/conftest.py (#497).
        # Reset it so each test starts empty, and leave the schema in place
        # for tests that run after this one.
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        db.create_all()


@pytest.fixture
def test_user(test_app):
    """Create a test user."""
    with test_app.app_context():
        user = User(username='testuser', email='test@example.com')
        user.set_password('TestPass123')
        db.session.add(user)
        db.session.commit()
        return user


def test_alert_preference_defaults(test_app, test_user):
    """Test default preferences are set correctly."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        prefs = AlertPreference(user_id=user.id)
        db.session.add(prefs)
        db.session.commit()

        # Reload from database
        prefs = AlertPreference.query.filter_by(user_id=user.id).first()

        assert prefs.daily_briefing_enabled == True
        assert prefs.briefing_frequency == 'daily'
        assert prefs.briefing_time == datetime.strptime('07:00', '%H:%M').time()
        assert prefs.briefing_timezone == 'America/New_York'
        assert prefs.include_portfolio_analysis == True
        assert prefs.alerts_enabled == True
        assert prefs.vix_threshold_25 == True
        assert prefs.vix_threshold_30 == True
        assert prefs.credit_spread_threshold_50bp == True
        assert prefs.yield_curve_inversion == True
        assert prefs.equity_breadth_deterioration == True


def test_create_alert(test_app, test_user):
    """Test creating an alert."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        alert = Alert(
            user_id=user.id,
            alert_type='vix_spike',
            title='VIX crossed 30 threshold',
            severity='warning',
            metric_name='VIX',
            metric_value=31.5,
            threshold_value=30.0
        )
        db.session.add(alert)
        db.session.commit()

        # Reload from database
        alert = Alert.query.filter_by(user_id=user.id).first()

        assert alert.id is not None
        assert alert.alert_type == 'vix_spike'
        assert alert.title == 'VIX crossed 30 threshold'
        assert alert.severity == 'warning'
        assert alert.metric_value == 31.5
        assert alert.threshold_value == 30.0
        assert alert.email_sent == False
        assert alert.read == False


def test_user_alert_relationship(test_app, test_user):
    """Test user → alerts relationship."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        alert1 = Alert(
            user_id=user.id,
            alert_type='vix_spike',
            title='Test 1',
            severity='info'
        )
        alert2 = Alert(
            user_id=user.id,
            alert_type='credit_spread',
            title='Test 2',
            severity='warning'
        )
        db.session.add_all([alert1, alert2])
        db.session.commit()

        # Reload user and check relationship
        user = User.query.filter_by(username='testuser').first()
        assert user.alerts.count() == 2


def test_user_alert_preference_relationship(test_app, test_user):
    """Test user → alert_preferences relationship."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        prefs = AlertPreference(user_id=user.id)
        db.session.add(prefs)
        db.session.commit()

        # Reload user and check relationship
        user = User.query.filter_by(username='testuser').first()
        assert user.alert_preferences is not None
        assert user.alert_preferences.user_id == user.id


def test_alert_to_dict(test_app, test_user):
    """Test alert serialization to dictionary."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        alert = Alert(
            user_id=user.id,
            alert_type='vix_spike',
            title='VIX Alert',
            message='VIX has spiked above threshold',
            severity='critical',
            metric_name='VIX',
            metric_value=35.0,
            threshold_value=30.0
        )
        db.session.add(alert)
        db.session.commit()

        # Test serialization
        alert_dict = alert.to_dict()

        assert alert_dict['alert_type'] == 'vix_spike'
        assert alert_dict['title'] == 'VIX Alert'
        assert alert_dict['message'] == 'VIX has spiked above threshold'
        assert alert_dict['severity'] == 'critical'
        assert alert_dict['metric_name'] == 'VIX'
        assert alert_dict['metric_value'] == 35.0
        assert alert_dict['threshold_value'] == 30.0
        assert alert_dict['email_sent'] == False
        assert alert_dict['read'] == False
        assert 'triggered_at' in alert_dict


def test_alert_preference_unique_per_user(test_app, test_user):
    """Test that only one alert preference can exist per user."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        prefs1 = AlertPreference(user_id=user.id)
        db.session.add(prefs1)
        db.session.commit()

        # Try to create a second preference for same user
        prefs2 = AlertPreference(user_id=user.id)
        db.session.add(prefs2)

        with pytest.raises(Exception):  # Should raise IntegrityError
            db.session.commit()


def test_create_default_alert_preferences_method(test_app, test_user):
    """Test User.create_default_alert_preferences() method."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        # Ensure no preferences exist initially
        assert user.alert_preferences is None

        # Create default preferences
        prefs = user.create_default_alert_preferences()

        # Verify preferences were created
        assert prefs is not None
        assert prefs.user_id == user.id
        assert prefs.daily_briefing_enabled == True

        # Call again - should return existing preferences
        prefs2 = user.create_default_alert_preferences()
        assert prefs2.id == prefs.id


def test_alert_update_read_status(test_app, test_user):
    """Test updating alert read status."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        alert = Alert(
            user_id=user.id,
            alert_type='test',
            title='Test Alert',
            severity='info'
        )
        db.session.add(alert)
        db.session.commit()

        # Mark as read
        alert.read = True
        alert.read_at = datetime.utcnow()
        db.session.commit()

        # Reload and verify
        alert = Alert.query.filter_by(user_id=user.id).first()
        assert alert.read == True
        assert alert.read_at is not None


def test_alert_update_email_status(test_app, test_user):
    """Test updating alert email sent status."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        alert = Alert(
            user_id=user.id,
            alert_type='test',
            title='Test Alert',
            severity='info'
        )
        db.session.add(alert)
        db.session.commit()

        # Mark email as sent
        alert.email_sent = True
        alert.email_sent_at = datetime.utcnow()
        db.session.commit()

        # Reload and verify
        alert = Alert.query.filter_by(user_id=user.id).first()
        assert alert.email_sent == True
        assert alert.email_sent_at is not None
