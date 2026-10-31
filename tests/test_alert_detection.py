"""
Alert Detection Service Tests

Unit tests for smart alert detection logic.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Add signaltrackers directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'signaltrackers'))

import pytest
from dashboard import app
from extensions import db
from models import User, AlertPreference, Alert
from services.alert_detection_service import (
    VIXSpikeDetector,
    CreditSpreadWideningDetector,
    YieldCurveInversionDetector,
    EquityBreadthDetector,
    ExtremePercentileDetector,
    check_all_alerts_for_user,
    check_all_users_alerts
)


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
    """Create a test user with alert preferences."""
    with test_app.app_context():
        user = User(username='testuser', email='test@example.com')
        user.set_password('TestPass123')
        db.session.add(user)
        db.session.commit()

        # Create alert preferences
        prefs = AlertPreference(
            user_id=user.id,
            alerts_enabled=True,
            vix_threshold_25=True,
            vix_threshold_30=True,
            credit_spread_threshold_50bp=True,
            yield_curve_inversion=True,
            equity_breadth_deterioration=True
        )
        db.session.add(prefs)
        db.session.commit()

        return user


def test_vix_spike_detector_threshold_25(test_app, test_user):
    """Test VIX spike detection at 25 threshold."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = VIXSpikeDetector(25)

        # Mock metrics with VIX at 28
        metrics = {'vix_price': {'value': 28.0, 'percentile': 85}}

        result = detector.should_trigger(user, metrics)

        assert result is not None
        assert 'VIX' in result['title']
        assert result['metric_value'] == 28.0
        assert result['threshold_value'] == 25.0
        assert 'elevated' in result['message']
        assert detector.severity == 'warning'


def test_vix_spike_detector_threshold_30(test_app, test_user):
    """Test VIX spike detection at 30 threshold."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = VIXSpikeDetector(30)

        # Mock metrics with VIX at 32
        metrics = {'vix_price': {'value': 32.0, 'percentile': 95}}

        result = detector.should_trigger(user, metrics)

        assert result is not None
        assert result['metric_value'] == 32.0
        assert result['threshold_value'] == 30.0
        assert 'extreme' in result['message']
        assert detector.severity == 'critical'


def test_vix_detector_below_threshold(test_app, test_user):
    """Test VIX detector does not trigger below threshold."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = VIXSpikeDetector(25)

        # Mock metrics with VIX at 20
        metrics = {'vix_price': {'value': 20.0, 'percentile': 50}}

        result = detector.should_trigger(user, metrics)

        assert result is None


def test_vix_detector_disabled_preference(test_app, test_user):
    """Test VIX detector respects user preferences."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        # Disable VIX 25 threshold
        user.alert_preferences.vix_threshold_25 = False
        db.session.commit()

        detector = VIXSpikeDetector(25)
        metrics = {'vix_price': {'value': 28.0, 'percentile': 85}}

        result = detector.should_trigger(user, metrics)

        assert result is None


def test_alert_deduplication(test_app, test_user):
    """Test that alerts don't spam users with duplicates."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        # Create recent alert
        alert = Alert(
            user_id=user.id,
            alert_type='vix_spike_25',
            title='VIX crossed 25',
            severity='warning',
            triggered_at=datetime.utcnow()
        )
        db.session.add(alert)
        db.session.commit()

        # Try to trigger again
        detector = VIXSpikeDetector(25)
        metrics = {'vix_price': {'value': 28.0, 'percentile': 85}}

        result = detector.should_trigger(user, metrics)

        # Should not trigger again (recent alert exists)
        assert result is None


def test_alert_can_retrigger_after_cooldown(test_app, test_user):
    """Test that alerts can trigger again after cooldown period."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        # Create old alert (3 days ago)
        old_alert = Alert(
            user_id=user.id,
            alert_type='vix_spike_25',
            title='VIX crossed 25',
            severity='warning',
            triggered_at=datetime.utcnow() - timedelta(hours=72)
        )
        db.session.add(old_alert)
        db.session.commit()

        # Try to trigger again
        detector = VIXSpikeDetector(25)
        metrics = {'vix_price': {'value': 28.0, 'percentile': 85}}

        result = detector.should_trigger(user, metrics)

        # Should trigger (old alert is outside 48-hour window)
        assert result is not None


@patch('services.alert_detection_service.get_historical_metrics')
def test_credit_spread_widening_detector(mock_historical, test_app, test_user):
    """Test credit spread widening detection."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = CreditSpreadWideningDetector()

        # Mock current metrics (3.5% spread)
        current_metrics = {
            'high_yield_spread': {'value': 3.5, 'percentile': 70}
        }

        # Mock week-ago metrics (3.0% spread)
        mock_historical.return_value = {
            'high_yield_spread': {'value': 3.0, 'percentile': 60}
        }

        result = detector.should_trigger(user, current_metrics)

        # Change is 50bp (0.5 percentage points * 100)
        assert result is not None
        assert 'widened by 50bp' in result['message']
        assert result['metric_value'] == 3.5


@patch('services.alert_detection_service.get_historical_metrics')
def test_credit_spread_no_widening(mock_historical, test_app, test_user):
    """Test credit spread detector when spreads haven't widened enough."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = CreditSpreadWideningDetector()

        # Mock current metrics
        current_metrics = {
            'high_yield_spread': {'value': 3.2, 'percentile': 65}
        }

        # Mock week-ago metrics (only 20bp widening)
        mock_historical.return_value = {
            'high_yield_spread': {'value': 3.0, 'percentile': 60}
        }

        result = detector.should_trigger(user, current_metrics)

        # Should not trigger (less than 50bp)
        assert result is None


@patch('services.alert_detection_service.get_historical_metrics')
def test_yield_curve_inversion_detector(mock_historical, test_app, test_user):
    """Test yield curve inversion detection."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = YieldCurveInversionDetector()

        # Mock current metrics (inverted: -0.25%)
        current_metrics = {
            'yield_curve_10y2y': {'value': -0.25, 'percentile': 5}
        }

        # Mock yesterday metrics (was positive: 0.10%)
        mock_historical.return_value = {
            'yield_curve_10y2y': {'value': 0.10, 'percentile': 45}
        }

        result = detector.should_trigger(user, current_metrics)

        # Should trigger (inversion occurred)
        assert result is not None
        assert 'inverted' in result['title']
        assert 'recession' in result['message']


@patch('services.alert_detection_service.get_historical_metrics')
def test_yield_curve_uninversion_detector(mock_historical, test_app, test_user):
    """Test yield curve un-inversion detection."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = YieldCurveInversionDetector()

        # Mock current metrics (now positive: 0.15%)
        current_metrics = {
            'yield_curve_10y2y': {'value': 0.15, 'percentile': 50}
        }

        # Mock yesterday metrics (was inverted: -0.10%)
        mock_historical.return_value = {
            'yield_curve_10y2y': {'value': -0.10, 'percentile': 10}
        }

        result = detector.should_trigger(user, current_metrics)

        # Should trigger (un-inversion occurred)
        assert result is not None
        assert 'un-inverted' in result['title']


@patch('services.alert_detection_service.get_historical_metrics')
def test_yield_curve_no_change(mock_historical, test_app, test_user):
    """Test yield curve detector when no inversion state change."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = YieldCurveInversionDetector()

        # Mock current metrics (still positive)
        current_metrics = {
            'yield_curve_10y2y': {'value': 0.25, 'percentile': 55}
        }

        # Mock yesterday metrics (was also positive)
        mock_historical.return_value = {
            'yield_curve_10y2y': {'value': 0.20, 'percentile': 52}
        }

        result = detector.should_trigger(user, current_metrics)

        # Should not trigger (no state change)
        assert result is None


def test_equity_breadth_detector(test_app, test_user):
    """Test equity breadth deterioration detection."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = EquityBreadthDetector()

        # Mock metrics with poor breadth (15th percentile)
        metrics = {
            'market_breadth_ratio': {'value': 95.5, 'percentile': 15}
        }

        result = detector.should_trigger(user, metrics)

        # Should trigger (below 20th percentile)
        assert result is not None
        assert 'deteriorating' in result['title']
        assert 'concentration' in result['message']


def test_equity_breadth_detector_healthy(test_app, test_user):
    """Test equity breadth detector with healthy breadth."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = EquityBreadthDetector()

        # Mock metrics with good breadth (60th percentile)
        metrics = {
            'market_breadth_ratio': {'value': 100.2, 'percentile': 60}
        }

        result = detector.should_trigger(user, metrics)

        # Should not trigger
        assert result is None


def test_extreme_percentile_detector(test_app, test_user):
    """Test extreme percentile detection."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = ExtremePercentileDetector()

        # Mock metrics with 3 extreme readings
        metrics = {
            'vix_price': {'value': 35.0, 'percentile': 98, 'display_name': 'VIX'},
            'high_yield_spread': {'value': 8.5, 'percentile': 96, 'display_name': 'HY Spread'},
            'gold_price': {'value': 2500, 'percentile': 99, 'display_name': 'Gold'},
            'sp500_price': {'value': 4500, 'percentile': 60, 'display_name': 'S&P 500'}
        }

        result = detector.should_trigger(user, metrics)

        # Should trigger (3+ extreme readings)
        assert result is not None
        assert '3 metrics' in result['message']
        assert result['metric_value'] == 3.0


def test_extreme_percentile_detector_low_percentiles(test_app, test_user):
    """Test extreme percentile detection with low percentiles."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = ExtremePercentileDetector()

        # Mock metrics with 3 extremely low readings
        metrics = {
            'metric1': {'value': 10, 'percentile': 2, 'display_name': 'Metric 1'},
            'metric2': {'value': 20, 'percentile': 3, 'display_name': 'Metric 2'},
            'metric3': {'value': 30, 'percentile': 4, 'display_name': 'Metric 3'},
            'metric4': {'value': 100, 'percentile': 50, 'display_name': 'Metric 4'}
        }

        result = detector.should_trigger(user, metrics)

        # Should trigger (3 low percentile readings)
        assert result is not None


def test_extreme_percentile_detector_insufficient(test_app, test_user):
    """Test extreme percentile detector with insufficient extremes."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = ExtremePercentileDetector()

        # Mock metrics with only 2 extreme readings
        metrics = {
            'vix_price': {'value': 35.0, 'percentile': 98, 'display_name': 'VIX'},
            'gold_price': {'value': 2500, 'percentile': 97, 'display_name': 'Gold'},
            'sp500_price': {'value': 4500, 'percentile': 60, 'display_name': 'S&P 500'}
        }

        result = detector.should_trigger(user, metrics)

        # Should not trigger (less than 3)
        assert result is None


@patch('services.alert_detection_service.get_latest_metrics')
def test_check_all_alerts_for_user(mock_metrics, test_app, test_user):
    """Test checking all alerts for a user."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        # Mock metrics that will trigger VIX alert
        mock_metrics.return_value = {
            'vix_price': {'value': 28.0, 'percentile': 85, 'display_name': 'VIX'}
        }

        count = check_all_alerts_for_user(user)

        # Should create at least one alert
        assert count >= 1

        # Verify alert was created in database
        alert = Alert.query.filter_by(user_id=user.id).first()
        assert alert is not None
        assert 'vix' in alert.alert_type


@patch('services.alert_detection_service.get_latest_metrics')
def test_check_all_alerts_disabled_user(mock_metrics, test_app, test_user):
    """Test that disabled users don't get alerts."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()

        # Disable alerts
        user.alert_preferences.alerts_enabled = False
        db.session.commit()

        # Mock metrics
        mock_metrics.return_value = {
            'vix_price': {'value': 28.0, 'percentile': 85}
        }

        count = check_all_alerts_for_user(user)

        # Should not create any alerts
        assert count == 0


@patch('services.alert_detection_service.get_latest_metrics')
def test_check_all_users_alerts(mock_metrics, test_app, test_user):
    """Test checking alerts for all users."""
    with test_app.app_context():
        # Create another user
        user2 = User(username='testuser2', email='test2@example.com')
        user2.set_password('TestPass123')
        db.session.add(user2)
        db.session.commit()

        prefs2 = AlertPreference(
            user_id=user2.id,
            alerts_enabled=True,
            vix_threshold_25=True
        )
        db.session.add(prefs2)
        db.session.commit()

        # Mock metrics
        mock_metrics.return_value = {
            'vix_price': {'value': 28.0, 'percentile': 85, 'display_name': 'VIX'}
        }

        results = check_all_users_alerts()

        # Should check both users
        assert results['users_checked'] == 2
        assert results['total_alerts'] >= 0


def test_create_alert_method(test_app, test_user):
    """Test AlertDetector.create_alert method."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = VIXSpikeDetector(25)

        alert = detector.create_alert(
            user.id,
            title='Test Alert',
            message='Test message',
            metric_name='VIX',
            metric_value=28.0,
            threshold_value=25.0
        )

        # Should create alert object (not yet committed)
        assert alert is not None
        assert alert.user_id == user.id
        assert alert.alert_type == 'vix_spike_25'
        assert alert.severity == 'warning'


def test_was_recently_triggered_method(test_app, test_user):
    """Test AlertDetector.was_recently_triggered method."""
    with test_app.app_context():
        user = User.query.filter_by(username='testuser').first()
        detector = VIXSpikeDetector(25)

        # Initially should be false
        assert detector.was_recently_triggered(user.id, hours=24) == False

        # Create alert
        alert = Alert(
            user_id=user.id,
            alert_type='vix_spike_25',
            title='Test',
            severity='warning'
        )
        db.session.add(alert)
        db.session.commit()

        # Now should be true
        assert detector.was_recently_triggered(user.id, hours=24) == True

        # Old alert (outside window)
        alert.triggered_at = datetime.utcnow() - timedelta(hours=48)
        db.session.commit()

        assert detector.was_recently_triggered(user.id, hours=24) == False
