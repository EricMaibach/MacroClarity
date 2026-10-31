"""
US-237.3: Alert Integration, Rate-Limiting (≤5/week), and Display Update

Tests for:
- Correct alert_type values for filter compatibility
- Layer-level preference toggles (layer_1_enabled, layer_2_enabled, layer_3_enabled)
- ≤5/week rate limiter with priority suppression (L3 > L2 > L1)
- Unified 3-layer pipeline in check_all_alerts_for_user
- Legacy detectors inactive in pipeline
- Backtest function returns correct structure
- Template static checks: layer badge CSS, filter options, settings toggles
- AlertPreference model has new layer columns (Flask-stack only)
"""

import sys
import importlib.util as _iutil
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

# ---------------------------------------------------------------------------
# Path & module setup — load alert_detection_service without Flask stack
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_ST_DIR = _REPO_ROOT / "signaltrackers"
sys.path.insert(0, str(_ST_DIR))

# Detect Flask-stack availability
_FLASK_STACK_AVAILABLE = bool(_iutil.find_spec("flask_login"))

# Provide minimal stubs for Flask-stack imports before loading the service
if not _FLASK_STACK_AVAILABLE:
    _db_stub = MagicMock()
    _db_stub.session.add = MagicMock()
    _db_stub.session.commit = MagicMock()
    sys.modules.setdefault("extensions", MagicMock(db=_db_stub))
    sys.modules.setdefault("models", MagicMock())
    sys.modules.setdefault("models.alert", MagicMock())
    sys.modules.setdefault("models.user", MagicMock())
    # market_signals stub: installed temporarily then removed after module load
    # to prevent it from polluting the real market_signals in other test files.
    _market_signals_was_absent = "market_signals" not in sys.modules
    sys.modules.setdefault("market_signals", MagicMock())
    sys.modules.setdefault("services.alert_email_service", MagicMock(send_pending_alert_notifications=MagicMock(return_value={})))

# Load alert_detection_service directly
_ADS_PATH = _ST_DIR / "services" / "alert_detection_service.py"
_ads_spec = _iutil.spec_from_file_location("alert_detection_service", str(_ADS_PATH))
_ads_mod = _iutil.module_from_spec(_ads_spec)

# Ensure service-level sub-imports are stubbed before exec
sys.modules.setdefault("services", MagicMock())
sys.modules.setdefault("services.layer2_extreme_percentile", MagicMock())
sys.modules.setdefault("services.layer3_convergence", MagicMock())

_ads_spec.loader.exec_module(_ads_mod)
sys.modules["alert_detection_service"] = _ads_mod

# Remove the market_signals stub if we installed it — the loaded module already
# holds a direct reference to the stub object, so other test files can now
# import the real market_signals without getting this mock.
if not _FLASK_STACK_AVAILABLE and _market_signals_was_absent:
    sys.modules.pop("market_signals", None)
    del _market_signals_was_absent

# Expose symbols
ExtremePercentileLayer2Detector = _ads_mod.ExtremePercentileLayer2Detector
ConvergenceLayer3Detector = _ads_mod.ConvergenceLayer3Detector
WEEKLY_ALERT_LIMIT = _ads_mod.WEEKLY_ALERT_LIMIT
LAYER_ALERT_TYPES = _ads_mod.LAYER_ALERT_TYPES
_count_layer_alerts_this_week = _ads_mod._count_layer_alerts_this_week
check_all_alerts_for_user = _ads_mod.check_all_alerts_for_user
run_alert_backtest = _ads_mod.run_alert_backtest


# ---------------------------------------------------------------------------
# Flask-stack fixtures
# ---------------------------------------------------------------------------

if _FLASK_STACK_AVAILABLE:
    from dashboard import app
    from extensions import db
    from models import User, AlertPreference, Alert

    @pytest.fixture
    def test_app():
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
    def user_with_prefs(test_app):
        with test_app.app_context():
            u = User(username='testuser237', email='us237@example.com')
            u.set_password('TestPass123!')
            db.session.add(u)
            db.session.commit()

            prefs = AlertPreference(
                user_id=u.id,
                alerts_enabled=True,
                layer_1_enabled=True,
                layer_2_enabled=True,
                layer_3_enabled=True,
            )
            db.session.add(prefs)
            db.session.commit()
            return u


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_weekly_limit_is_five(self):
        assert WEEKLY_ALERT_LIMIT == 5

    def test_layer_alert_types_contains_active_layers(self):
        assert 'extreme_percentile' in LAYER_ALERT_TYPES
        assert 'multi_signal_convergence' in LAYER_ALERT_TYPES
        assert 'regime_transition' not in LAYER_ALERT_TYPES

    def test_priority_order_l3_first(self):
        """Layer 3 (highest conviction) must be first in LAYER_ALERT_TYPES."""
        assert LAYER_ALERT_TYPES[0] == 'multi_signal_convergence'
        assert LAYER_ALERT_TYPES[1] == 'extreme_percentile'


# ---------------------------------------------------------------------------
# Alert type names
# ---------------------------------------------------------------------------

class TestAlertTypeNames:
    def test_layer2_alert_type(self):
        assert ExtremePercentileLayer2Detector().alert_type == 'extreme_percentile'

    def test_layer3_alert_type(self):
        assert ConvergenceLayer3Detector().alert_type == 'multi_signal_convergence'


# ---------------------------------------------------------------------------
# Layer toggle preference checks (unit-level, no DB)
# ---------------------------------------------------------------------------

class TestLayerToggleSuppression:
    """Test that layer_N_enabled=False suppresses should_trigger without DB."""

    def _make_prefs(self, **overrides):
        prefs = MagicMock()
        prefs.alerts_enabled = True
        prefs.layer_1_enabled = overrides.get('layer_1_enabled', True)
        prefs.layer_2_enabled = overrides.get('layer_2_enabled', True)
        prefs.layer_3_enabled = overrides.get('layer_3_enabled', True)
        return prefs

    def _make_user(self, prefs):
        user = MagicMock()
        user.alert_preferences = prefs
        return user

    def test_layer2_disabled_returns_none(self):
        prefs = self._make_prefs(layer_2_enabled=False)
        user = self._make_user(prefs)
        detector = ExtremePercentileLayer2Detector()
        result = detector.should_trigger(user, {})
        assert result is None

    def test_layer3_disabled_returns_none(self):
        prefs = self._make_prefs(layer_3_enabled=False)
        user = self._make_user(prefs)
        detector = ConvergenceLayer3Detector()
        result = detector.should_trigger(user, {})
        assert result is None

    def test_master_alerts_disabled_returns_none(self):
        prefs = self._make_prefs()
        prefs.alerts_enabled = False
        user = self._make_user(prefs)
        result = ExtremePercentileLayer2Detector().should_trigger(user, {})
        assert result is None


# ---------------------------------------------------------------------------
# Rate limiter unit tests (mock DB)
# ---------------------------------------------------------------------------

class TestWeeklyRateLimitUnit:
    """Rate limiter tests without Flask DB using mocked Alert.query."""

    def _make_user_with_prefs(self, alerts_enabled=True):
        prefs = MagicMock()
        prefs.alerts_enabled = alerts_enabled
        prefs.layer_1_enabled = True
        prefs.layer_2_enabled = True
        prefs.layer_3_enabled = True
        user = MagicMock()
        user.id = 'test-user-id'
        user.alert_preferences = prefs
        return user

    def _l2_alert_data(self):
        return {
            'title': 'Extreme Percentile: VIX',
            'message': 'Context.',
            'metric_name': 'VIX',
            'metric_value': 95.0,
            'threshold_value': 90.0,
        }

    def _l3_alert_data(self):
        return {
            'title': 'Multi-Signal Convergence: 3 indicators',
            'message': 'Context.',
            'metric_name': 'Multi-Signal Convergence',
            'metric_value': 3.0,
            'threshold_value': 3.0,
        }

    def test_budget_zero_no_alerts_created(self):
        user = self._make_user_with_prefs()
        with patch.object(_ads_mod, '_count_layer_alerts_this_week', return_value=5), \
             patch.object(_ads_mod, 'get_latest_metrics', return_value={}):
            count = check_all_alerts_for_user(user)
        assert count == 0

    def test_budget_one_only_l3_created(self):
        """With budget=1, highest-priority (L3) wins."""
        user = self._make_user_with_prefs()
        created = []

        def mock_create(user_id, **kwargs):
            a = MagicMock()
            a.alert_type = kwargs.get('metric_name', '')
            created.append(kwargs)
            return a

        with patch.object(_ads_mod, '_count_layer_alerts_this_week', return_value=4), \
             patch.object(_ads_mod, 'get_latest_metrics', return_value={'vix': {'value': 30}}), \
             patch.object(ExtremePercentileLayer2Detector, 'was_recently_triggered', return_value=False), \
             patch.object(ConvergenceLayer3Detector, 'was_recently_triggered', return_value=False), \
             patch('services.layer2_extreme_percentile.check_extreme_percentile',
                   return_value=[{'signals_triggered': ['VIX'], 'context_sentence': 'ctx', 'current_percentile': 95.0}]), \
             patch('services.layer3_convergence.check_convergence',
                   return_value={'signals_triggered': ['HY', 'VIX', 'NFCI'], 'context_sentence': 'ctx'}), \
             patch.object(ConvergenceLayer3Detector, 'create_alert', side_effect=mock_create), \
             patch.object(ExtremePercentileLayer2Detector, 'create_alert', side_effect=mock_create), \
             patch.object(_ads_mod.db.session, 'commit', return_value=None):
            count = check_all_alerts_for_user(user)

        assert count == 1
        # The created alert should be the L3 (multi-signal), not L1
        assert created[0]['metric_name'] == 'Multi-Signal Convergence'

    def test_no_candidates_returns_zero(self):
        user = self._make_user_with_prefs()
        with patch.object(_ads_mod, '_count_layer_alerts_this_week', return_value=0), \
             patch.object(_ads_mod, 'get_latest_metrics', return_value={}), \
             patch.object(ExtremePercentileLayer2Detector, 'was_recently_triggered', return_value=False), \
             patch.object(ConvergenceLayer3Detector, 'was_recently_triggered', return_value=False), \
             patch('services.layer2_extreme_percentile.check_extreme_percentile', return_value=[]), \
             patch('services.layer3_convergence.check_convergence', return_value=None):
            count = check_all_alerts_for_user(user)
        assert count == 0

    def test_alerts_disabled_returns_zero_immediately(self):
        user = self._make_user_with_prefs(alerts_enabled=False)
        with patch.object(_ads_mod, 'get_latest_metrics', return_value={}):
            count = check_all_alerts_for_user(user)
        assert count == 0

    def test_full_budget_two_candidates_creates_two(self):
        """With budget=5 and 2 candidates, all 2 are created."""
        user = self._make_user_with_prefs()

        with patch.object(_ads_mod, '_count_layer_alerts_this_week', return_value=0), \
             patch.object(_ads_mod, 'get_latest_metrics', return_value={'vix': {'value': 30}}), \
             patch.object(ExtremePercentileLayer2Detector, 'was_recently_triggered', return_value=False), \
             patch.object(ConvergenceLayer3Detector, 'was_recently_triggered', return_value=False), \
             patch('services.layer2_extreme_percentile.check_extreme_percentile',
                   return_value=[{'signals_triggered': ['VIX'], 'context_sentence': 'ctx', 'current_percentile': 95.0}]), \
             patch('services.layer3_convergence.check_convergence',
                   return_value={'signals_triggered': ['HY', 'VIX', 'NFCI'], 'context_sentence': 'ctx'}), \
             patch.object(ConvergenceLayer3Detector, 'create_alert', return_value=MagicMock()), \
             patch.object(ExtremePercentileLayer2Detector, 'create_alert', return_value=MagicMock()), \
             patch.object(_ads_mod.db.session, 'commit', return_value=None):
            count = check_all_alerts_for_user(user)

        assert count == 2


# ---------------------------------------------------------------------------
# Backtest function
# ---------------------------------------------------------------------------

class TestRunAlertBacktest:
    def test_returns_expected_keys(self):
        with patch('services.layer2_extreme_percentile.check_extreme_percentile', return_value=[]), \
             patch('services.layer3_convergence.check_convergence', return_value=None):
            result = run_alert_backtest(months=1)

        required_keys = {'weeks_analysed', 'weekly_counts', 'max_weekly_count', 'total_alerts', 'passes_limit'}
        assert required_keys.issubset(result.keys())

    def test_passes_limit_true_when_no_signals(self):
        with patch('services.layer2_extreme_percentile.check_extreme_percentile', return_value=[]), \
             patch('services.layer3_convergence.check_convergence', return_value=None):
            result = run_alert_backtest(months=1)

        assert result['passes_limit'] is True
        assert result['max_weekly_count'] == 0

    def test_weekly_counts_length_matches_weeks_analysed(self):
        with patch('services.layer2_extreme_percentile.check_extreme_percentile', return_value=[]), \
             patch('services.layer3_convergence.check_convergence', return_value=None):
            result = run_alert_backtest(months=2)

        assert result['weeks_analysed'] == len(result['weekly_counts'])
        assert result['weeks_analysed'] > 0

    def test_all_signals_fires_max_limit(self):
        """When all 2 layers fire, weekly count is capped at WEEKLY_ALERT_LIMIT."""
        l2 = [{'signals_triggered': ['VIX'], 'context_sentence': 'x', 'current_percentile': 95.0}]
        l3 = {'signals_triggered': ['HY', 'VIX', 'NFCI'], 'context_sentence': 'x'}

        with patch('services.layer2_extreme_percentile.check_extreme_percentile', return_value=l2), \
             patch('services.layer3_convergence.check_convergence', return_value=l3):
            result = run_alert_backtest(months=1)

        # 2 layers can fire at most → max 2, which is ≤5 → passes
        assert result['passes_limit'] is True
        assert result['max_weekly_count'] <= WEEKLY_ALERT_LIMIT


# ---------------------------------------------------------------------------
# Template static checks
# ---------------------------------------------------------------------------

class TestAlertsTemplate:
    TEMPLATE = _REPO_ROOT / 'signaltrackers' / 'templates' / 'alerts.html'

    def test_filter_has_no_regime_transition_option(self):
        content = self.TEMPLATE.read_text()
        assert 'value="regime_transition"' not in content

    def test_filter_has_extreme_percentile_option(self):
        content = self.TEMPLATE.read_text()
        assert 'value="extreme_percentile"' in content

    def test_filter_has_multi_signal_convergence_option(self):
        content = self.TEMPLATE.read_text()
        assert 'value="multi_signal_convergence"' in content

    def test_old_vix_filter_option_removed(self):
        content = self.TEMPLATE.read_text()
        assert 'value="vix_spike"' not in content

    def test_old_credit_spread_filter_removed(self):
        content = self.TEMPLATE.read_text()
        assert 'value="credit_spread"' not in content

    def test_layer_badge_classes_present(self):
        content = self.TEMPLATE.read_text()
        assert '.layer-2' in content
        assert '.layer-3' in content

    def test_context_sentence_class_present(self):
        content = self.TEMPLATE.read_text()
        assert 'context-sentence' in content

    def test_regime_transition_badge_removed(self):
        content = self.TEMPLATE.read_text()
        assert "alert.alert_type == 'regime_transition'" not in content

    def test_extreme_percentile_badge_conditional(self):
        content = self.TEMPLATE.read_text()
        assert "alert.alert_type == 'extreme_percentile'" in content

    def test_multi_signal_badge_conditional(self):
        content = self.TEMPLATE.read_text()
        assert "alert.alert_type == 'multi_signal_convergence'" in content


class TestSettingsTemplate:
    TEMPLATE = _REPO_ROOT / 'signaltrackers' / 'templates' / 'settings_alerts.html'

    def test_layer_1_toggle_present(self):
        content = self.TEMPLATE.read_text()
        assert 'name="layer_1_enabled"' in content

    def test_layer_2_toggle_present(self):
        content = self.TEMPLATE.read_text()
        assert 'name="layer_2_enabled"' in content

    def test_layer_3_toggle_present(self):
        content = self.TEMPLATE.read_text()
        assert 'name="layer_3_enabled"' in content

    def test_alert_severity_badge_classes_present(self):
        content = self.TEMPLATE.read_text()
        assert 'alert-severity-badge' in content
        assert 'alert-severity-badge--l1' in content
        assert 'alert-severity-badge--l2' in content
        assert 'alert-severity-badge--l3' in content

    def test_old_vix_checkbox_removed(self):
        content = self.TEMPLATE.read_text()
        assert 'name="vix_threshold_25"' not in content

    def test_old_credit_spread_checkbox_removed(self):
        content = self.TEMPLATE.read_text()
        assert 'name="credit_spread_threshold_50bp"' not in content

    def test_weekly_limit_mentioned_in_sidebar(self):
        content = self.TEMPLATE.read_text()
        assert '5 alerts per 7-day' in content

    def test_layer_3_described_as_highest_conviction(self):
        content = self.TEMPLATE.read_text()
        assert 'Highest Conviction' in content


# ---------------------------------------------------------------------------
# AlertPreference model has layer columns (Flask-stack only)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _FLASK_STACK_AVAILABLE, reason="Requires Flask stack")
class TestAlertPreferenceModelColumns:
    def test_layer_columns_default_true(self, test_app, user_with_prefs):
        with test_app.app_context():
            u = User.query.filter_by(email='us237@example.com').first()
            prefs = u.alert_preferences
            assert prefs.layer_1_enabled is True
            assert prefs.layer_2_enabled is True
            assert prefs.layer_3_enabled is True

    def test_layer_columns_can_be_set_false(self, test_app, user_with_prefs):
        with test_app.app_context():
            u = User.query.filter_by(email='us237@example.com').first()
            prefs = u.alert_preferences
            prefs.layer_1_enabled = False
            prefs.layer_2_enabled = False
            prefs.layer_3_enabled = False
            db.session.commit()

            fresh = AlertPreference.query.filter_by(user_id=u.id).first()
            assert fresh.layer_1_enabled is False
            assert fresh.layer_2_enabled is False
            assert fresh.layer_3_enabled is False


@pytest.mark.skipif(not _FLASK_STACK_AVAILABLE, reason="Requires Flask stack")
class TestWeeklyCountHelper:
    def test_counts_only_layer_alerts_in_window(self, test_app, user_with_prefs):
        with test_app.app_context():
            u = User.query.filter_by(email='us237@example.com').first()

            # 2 layer alerts in past 7 days
            for atype in ['extreme_percentile', 'multi_signal_convergence']:
                db.session.add(Alert(
                    user_id=u.id, alert_type=atype, title='Test', severity='warning',
                    triggered_at=datetime.utcnow() - timedelta(days=1),
                ))
            # 1 legacy alert (should not count)
            db.session.add(Alert(
                user_id=u.id, alert_type='vix_spike_25', title='Legacy', severity='warning',
                triggered_at=datetime.utcnow() - timedelta(days=1),
            ))
            # 1 layer alert from 10 days ago (outside window)
            db.session.add(Alert(
                user_id=u.id, alert_type='multi_signal_convergence', title='Old', severity='warning',
                triggered_at=datetime.utcnow() - timedelta(days=10),
            ))
            db.session.commit()

            count = _count_layer_alerts_this_week(u.id)
            assert count == 2

    def test_count_zero_with_no_alerts(self, test_app, user_with_prefs):
        with test_app.app_context():
            u = User.query.filter_by(email='us237@example.com').first()
            assert _count_layer_alerts_this_week(u.id) == 0
