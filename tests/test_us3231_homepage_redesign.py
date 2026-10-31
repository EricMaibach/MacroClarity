"""
Tests for US-323.1: Homepage §0 AI Briefing + §1 Market Conditions redesign.

Covers:
- §0 Briefing section position and structure
- §1 Market Conditions hero card + dimension grid
- Quadrant visualization SVG
- Dimension card expand/collapse ARIA
- Movers strip footer
- Quick-nav update (4 pills, 4 sheet items)
- Old sections removed
- Cache extension with composites/scores
- Dashboard route context data
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Resolve project root so imports work inside Docker
REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNALTRACKERS_DIR = str(REPO_ROOT / 'signaltrackers')
if SIGNALTRACKERS_DIR not in sys.path:
    sys.path.insert(0, SIGNALTRACKERS_DIR)


# ─── Helpers ─────────────────────────────────────────────────────────

def _make_cache(quadrant='Goldilocks', liq_state='Expanding', risk_state='Calm',
                risk_score=1, pol_stance='Neutral', pol_direction='Easing',
                growth=0.6, inflation=-0.4, liq_score=0.8,
                vix_level=14.8, vix_ratio=0.88, stock_bond_corr=-0.35,
                taylor_gap=-0.3, actual_rate=4.25, taylor_prescribed=4.55):
    """Build a market conditions cache dict matching the extended schema."""
    return {
        'quadrant': quadrant,
        'dimensions': {
            'liquidity': {
                'state': liq_state,
                'score': liq_score,
            },
            'quadrant': {
                'state': quadrant,
                'growth_composite': growth,
                'inflation_composite': inflation,
            },
            'risk': {
                'state': risk_state,
                'score': risk_score,
                'vix_level': vix_level,
                'vix_ratio': vix_ratio,
                'stock_bond_corr': stock_bond_corr,
            },
            'policy': {
                'stance': pol_stance,
                'direction': pol_direction,
                'taylor_gap': taylor_gap,
                'actual_rate': actual_rate,
                'taylor_prescribed': taylor_prescribed,
            },
        },
        'asset_expectations': [
            {'asset': 'S&P 500', 'direction': 'positive', 'magnitude': 'strong'},
            {'asset': 'Treasuries', 'direction': 'positive', 'magnitude': 'moderate'},
            {'asset': 'Gold', 'direction': 'neutral', 'magnitude': 'weak'},
            {'asset': 'Bitcoin', 'direction': 'positive', 'magnitude': 'moderate'},
            {'asset': 'Credit', 'direction': 'positive', 'magnitude': 'moderate'},
            {'asset': 'Commodities', 'direction': 'neutral', 'magnitude': 'weak'},
        ],
        'as_of': '2026-03-18',
        'updated_at': '2026-03-18T12:00:00+00:00',
    }


def _make_history(entries=3):
    """Build a trajectory history dict."""
    history = {}
    for i in range(entries):
        d = f'2026-0{3 - i}-01'
        history[d] = {
            'quadrant': 'Goldilocks',
            'dimensions': {
                'quadrant': {
                    'state': 'Goldilocks',
                    'growth_composite': 0.3 + i * 0.1,
                    'inflation_composite': -0.2 - i * 0.1,
                },
                'liquidity': {'state': 'Expanding', 'score': 0.5 + i * 0.1},
                'risk': {'state': 'Calm', 'score': 1},
                'policy': {'stance': 'Neutral', 'direction': 'Easing'},
            },
            'asset_expectations': [],
        }
    return history


def _get_app():
    """Import and return the Flask app."""
    import dashboard
    dashboard.app.config['TESTING'] = True
    dashboard.app.config['WTF_CSRF_ENABLED'] = False
    dashboard.app.config['LOGIN_DISABLED'] = True
    return dashboard.app


# ─── Template Structure Tests ────────────────────────────────────────

class TestBriefingSection(unittest.TestCase):
    """§0 AI Briefing is first section, has correct structure."""

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_briefing_section_exists(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            resp = c.get('/')
            html = resp.data.decode()
            self.assertIn('id="briefing-section"', html)
            self.assertIn('WEEK IN REVIEW', html)

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_briefing_before_conditions(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            resp = c.get('/')
            html = resp.data.decode()
            briefing_pos = html.index('id="briefing-section"')
            conditions_pos = html.index('id="conditions-section"')
            self.assertLess(briefing_pos, conditions_pos,
                            "Briefing section should appear before Conditions")

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_briefing_has_date_and_ai_btn(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            resp = c.get('/')
            html = resp.data.decode()
            self.assertIn('id="briefing-date"', html)
            self.assertIn('data-section-id="briefing-section"', html)

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_briefing_card_with_narrative(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            resp = c.get('/')
            html = resp.data.decode()
            self.assertIn('conditions-briefing__card', html)
            self.assertIn('id="briefing-narrative"', html)


class TestConditionsSection(unittest.TestCase):
    """§1 Market Conditions section — hero card + dimension grid."""

    def _get_html(self, cache=None, history=None, recession=None):
        cache = cache or _make_cache()
        history = history or {}
        recession = recession or {'ny_fed': 4.2, 'chauvet_piger': 2.1, 'richmond_sos': 1.0}
        with patch('dashboard.get_market_conditions', return_value=cache), \
             patch('dashboard.get_conditions_history', return_value=history), \
             patch('dashboard.get_recession_probability', return_value=recession):
            app = _get_app()
            with app.test_client() as c:
                return c.get('/').data.decode()

    def test_conditions_section_exists(self):
        html = self._get_html()
        self.assertIn('id="conditions-section"', html)
        self.assertIn('MARKET CONDITIONS', html)

    def test_quadrant_hero_card(self):
        html = self._get_html()
        self.assertIn('quadrant-hero--goldilocks', html)
        self.assertIn('GOLDILOCKS', html)

    def test_quadrant_description(self):
        html = self._get_html()
        self.assertIn('Growth accelerating, inflation cooling', html)

    def test_quadrant_hero_other_quadrants(self):
        for quad, desc_fragment in [
            ('Reflation', 'rising tide'),
            ('Stagflation', 'toughest environment'),
            ('Deflation Risk', 'flight to safety'),
        ]:
            html = self._get_html(cache=_make_cache(quadrant=quad))
            self.assertIn(quad.upper(), html)
            self.assertIn(desc_fragment, html)

    def test_quadrant_viz_svg(self):
        html = self._get_html()
        self.assertIn('class="quadrant-viz"', html)
        self.assertIn('quadrant-region--goldilocks', html)
        self.assertIn('quadrant-region--active', html)
        self.assertIn('quadrant-current-dot', html)

    def test_quadrant_trajectory_dots(self):
        history = _make_history(4)
        html = self._get_html(history=history)
        self.assertIn('quadrant-trail-dot', html)
        self.assertIn('quadrant-trail-path', html)

    def test_favored_watch_strip(self):
        html = self._get_html()
        self.assertIn('Favored:', html)
        self.assertIn('S&amp;P 500', html)

    def test_four_dimension_cards(self):
        html = self._get_html()
        self.assertIn('id="dim-liquidity"', html)
        self.assertIn('id="dim-risk"', html)
        self.assertIn('id="dim-policy"', html)
        self.assertIn('id="dim-crypto"', html)

    def test_spectrum_bars_present(self):
        html = self._get_html()
        self.assertIn('spectrum-bar--liquidity', html)
        self.assertIn('spectrum-bar--risk', html)
        self.assertIn('spectrum-bar--policy', html)

    def test_liquidity_card_state_and_arrow(self):
        html = self._get_html()
        self.assertIn('Expanding', html)
        self.assertIn('↑', html)

    def test_risk_card_state(self):
        html = self._get_html()
        # Risk state in dimension card
        self.assertIn('Calm', html)

    def test_policy_card_direction(self):
        html = self._get_html()
        self.assertIn('Easing', html)

    def test_crypto_card_label(self):
        html = self._get_html()
        self.assertIn('Favorable', html)
        self.assertIn('WHY LIQUIDITY, NOT THE QUADRANT?', html)

    def test_dimension_card_aria_controls(self):
        html = self._get_html()
        self.assertIn('aria-controls="dim-liquidity-expand"', html)
        self.assertIn('aria-controls="dim-risk-expand"', html)
        self.assertIn('aria-controls="dim-policy-expand"', html)
        self.assertIn('aria-controls="dim-crypto-expand"', html)

    def test_dimension_cards_initially_collapsed(self):
        html = self._get_html()
        self.assertIn('aria-expanded="false"', html)

    def test_risk_expand_has_vix(self):
        html = self._get_html()
        self.assertIn('14.8', html)  # VIX level

    def test_risk_expand_has_recession(self):
        html = self._get_html()
        self.assertIn('4.2%', html)  # Recession highest prob

    def test_policy_expand_has_rates(self):
        html = self._get_html()
        self.assertIn('4.25%', html)  # Actual rate
        self.assertIn('4.55%', html)  # Taylor prescribed

    def test_policy_expand_has_gap(self):
        html = self._get_html()
        self.assertIn('-0.30%', html)  # Taylor gap

    def test_dimension_metric_rows_are_links(self):
        html = self._get_html()
        self.assertIn('href="/explorer?metric=WALCL"', html)
        self.assertIn('href="/explorer?metric=VIXCLS"', html)
        self.assertIn('href="/explorer?metric=DFEDTARU"', html)

    def test_risk_score_segmented_bar(self):
        html = self._get_html()
        self.assertIn('risk-score-bar__segment--calm', html)
        self.assertIn('risk-score-bar__segment--active', html)

    def test_policy_stance_segmented_bar(self):
        html = self._get_html()
        self.assertIn('policy-stance-bar__segment--neutral', html)

    def test_cross_links(self):
        html = self._get_html()
        self.assertIn('See more:', html)


class TestConditionsUnavailable(unittest.TestCase):
    """Conditions section fallback when data is unavailable."""

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_unavailable_message(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            html = c.get('/').data.decode()
            self.assertIn('conditions-unavailable', html)
            self.assertIn('being computed', html)


class TestSectionsRemoved(unittest.TestCase):
    """Old homepage sections are removed."""

    def _get_html(self):
        with patch('dashboard.get_market_conditions', return_value=_make_cache()), \
             patch('dashboard.get_conditions_history', return_value={}), \
             patch('dashboard.get_recession_probability', return_value=None):
            app = _get_app()
            with app.test_client() as c:
                return c.get('/').data.decode()

    def test_macro_regime_section_removed(self):
        html = self._get_html()
        self.assertNotIn('id="macro-regime-section"', html)

    def test_recession_section_removed(self):
        html = self._get_html()
        self.assertNotIn('id="recession-panel-section"', html)

    def test_regime_implications_removed(self):
        html = self._get_html()
        self.assertNotIn('id="regime-implications"', html)
        self.assertNotIn('Regime Implications', html)

    def test_sector_tone_removed(self):
        html = self._get_html()
        self.assertNotIn('id="sector-tone-section"', html)

    def test_old_market_conditions_removed(self):
        html = self._get_html()
        self.assertNotIn('id="market-badges"', html)
        self.assertNotIn('Market Conditions at a Glance', html)

    def test_trade_pulse_removed(self):
        html = self._get_html()
        self.assertNotIn('id="trade-pulse-section"', html)

    def test_old_movers_section_removed(self):
        html = self._get_html()
        self.assertNotIn('id="movers-section"', html)
        self.assertNotIn("What's Moving Today", html)

    def test_signals_section_removed(self):
        html = self._get_html()
        self.assertNotIn('id="signals-section"', html)
        self.assertNotIn('Cross-Market Indicators', html)

    def test_prediction_section_removed(self):
        html = self._get_html()
        self.assertNotIn('id="prediction-section"', html)
        self.assertNotIn('Prediction Markets', html)


class TestQuickNav(unittest.TestCase):
    """Quick-nav pills and mobile sheet have 4 items."""

    def _get_html(self):
        with patch('dashboard.get_market_conditions', return_value=_make_cache()), \
             patch('dashboard.get_conditions_history', return_value={}), \
             patch('dashboard.get_recession_probability', return_value=None):
            app = _get_app()
            with app.test_client() as c:
                return c.get('/').data.decode()

    def test_desktop_pills_count(self):
        html = self._get_html()
        # Count actual pill button elements (each starts with class="section-quick-nav__pill)
        import re
        pills = re.findall(r'class="section-quick-nav__pill[^"]*"', html)
        self.assertEqual(len(pills), 4)

    def test_desktop_pill_targets(self):
        html = self._get_html()
        self.assertIn('data-target="#briefing-section"', html)
        self.assertIn('data-target="#conditions-section"', html)
        self.assertIn('data-target="#implications-section"', html)
        self.assertIn('data-target="#movers-strip"', html)

    def test_mobile_sheet_items_count(self):
        html = self._get_html()
        import re
        items = re.findall(r'class="section-quick-nav-sheet__item"', html)
        self.assertEqual(len(items), 4)

    def test_old_pill_targets_removed(self):
        html = self._get_html()
        self.assertNotIn('data-target="#macro-regime-section"', html)
        self.assertNotIn('data-target="#prediction-section"', html)


class TestMoversStrip(unittest.TestCase):
    """Footer movers strip."""

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_movers_strip_exists(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            html = c.get('/').data.decode()
            self.assertIn('id="movers-strip"', html)
            self.assertIn("TODAY'S MOVERS", html)

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_movers_strip_has_see_all(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            html = c.get('/').data.decode()
            self.assertIn('See all', html)
            self.assertIn('href="/equity"', html)

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_movers_strip_after_implications(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            html = c.get('/').data.decode()
            implications_pos = html.index('id="implications-section"')
            movers_pos = html.index('id="movers-strip"')
            self.assertLess(implications_pos, movers_pos)


class TestImplicationsSection(unittest.TestCase):
    """§2 implications matrix (replaced placeholder in US-323.2)."""

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=_make_cache())
    def test_implications_section_present(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            html = c.get('/').data.decode()
            self.assertIn('id="implications-section"', html)
            self.assertIn('WHAT THIS MEANS FOR YOUR PORTFOLIO', html)
            self.assertIn('implications-table', html)


# ─── Cache Extension Tests ───────────────────────────────────────────

class TestCacheExtension(unittest.TestCase):
    """Market conditions cache includes composites and scores."""

    @patch('market_conditions.compute_liquidity')
    @patch('market_conditions.compute_quadrant')
    @patch('market_conditions.compute_risk')
    @patch('market_conditions.compute_policy')
    @patch('market_conditions._build_asset_expectations', return_value=[])
    @patch('market_conditions._append_conditions_history')
    def test_cache_includes_growth_composite(self, mock_hist, mock_exp,
                                              mock_pol, mock_risk, mock_quad, mock_liq):
        from market_conditions import update_market_conditions_cache, LiquidityResult, QuadrantResult, RiskResult, PolicyResult

        mock_liq.return_value = LiquidityResult(state='Expanding', score=0.8)
        mock_quad.return_value = QuadrantResult(
            quadrant='Goldilocks', growth_composite=0.6, inflation_composite=-0.4,
            raw_quadrant='Goldilocks', stable=True
        )
        mock_risk.return_value = RiskResult(
            state='Calm', score=1, vix_score=0, term_structure_score=0,
            correlation_score=1, vix_level=14.8, vix_ratio=0.88, stock_bond_corr=-0.35
        )
        mock_pol.return_value = PolicyResult(
            stance='Neutral', direction='Easing', taylor_gap=-0.3,
            taylor_prescribed=4.55, actual_rate=4.25
        )

        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            cache_path = f.name

        try:
            with patch('market_conditions.MARKET_CONDITIONS_CACHE_FILE', cache_path):
                result = update_market_conditions_cache()

            self.assertIsNotNone(result)
            dims = result['dimensions']

            # Quadrant composites
            self.assertIn('growth_composite', dims['quadrant'])
            self.assertIn('inflation_composite', dims['quadrant'])
            self.assertAlmostEqual(dims['quadrant']['growth_composite'], 0.6, places=3)
            self.assertAlmostEqual(dims['quadrant']['inflation_composite'], -0.4, places=3)

            # Liquidity score
            self.assertIn('score', dims['liquidity'])
            self.assertAlmostEqual(dims['liquidity']['score'], 0.8, places=3)

            # Risk details
            self.assertIn('score', dims['risk'])
            self.assertEqual(dims['risk']['score'], 1)
            self.assertAlmostEqual(dims['risk']['vix_level'], 14.8, places=1)
            self.assertAlmostEqual(dims['risk']['vix_ratio'], 0.88, places=3)
            self.assertAlmostEqual(dims['risk']['stock_bond_corr'], -0.35, places=3)

            # Policy details
            self.assertIn('taylor_gap', dims['policy'])
            self.assertAlmostEqual(dims['policy']['taylor_gap'], -0.3, places=3)
            self.assertAlmostEqual(dims['policy']['actual_rate'], 4.25, places=3)
            self.assertAlmostEqual(dims['policy']['taylor_prescribed'], 4.55, places=3)

        finally:
            os.unlink(cache_path)


class TestDashboardRouteContext(unittest.TestCase):
    """Index route provides conditions and trajectory data."""

    @patch('dashboard.get_recession_probability')
    @patch('dashboard.get_conditions_history')
    @patch('dashboard.get_market_conditions')
    def test_route_passes_conditions(self, mock_mc, mock_hist, mock_rec):
        mock_mc.return_value = _make_cache()
        mock_hist.return_value = {}
        mock_rec.return_value = None

        app = _get_app()
        with app.test_client() as c:
            resp = c.get('/')
            html = resp.data.decode()
            self.assertEqual(resp.status_code, 200)
            self.assertIn('GOLDILOCKS', html)

    @patch('dashboard.get_recession_probability')
    @patch('dashboard.get_conditions_history')
    @patch('dashboard.get_market_conditions')
    def test_route_passes_trajectory(self, mock_mc, mock_hist, mock_rec):
        mock_mc.return_value = _make_cache()
        mock_hist.return_value = _make_history(4)
        mock_rec.return_value = None

        app = _get_app()
        with app.test_client() as c:
            html = c.get('/').data.decode()
            self.assertIn('quadrant-trail-dot', html)

    @patch('dashboard.get_recession_probability')
    @patch('dashboard.get_conditions_history')
    @patch('dashboard.get_market_conditions')
    def test_recession_highest_in_risk_expand(self, mock_mc, mock_hist, mock_rec):
        mock_mc.return_value = _make_cache()
        mock_hist.return_value = {}
        mock_rec.return_value = {
            'ny_fed': 8.5,
            'chauvet_piger': 2.1,
            'richmond_sos': 1.0,
        }

        app = _get_app()
        with app.test_client() as c:
            html = c.get('/').data.decode()
            self.assertIn('8.5%', html)


class TestCSS(unittest.TestCase):
    """CSS file exists and is linked."""

    def test_css_file_exists(self):
        css_path = REPO_ROOT / 'signaltrackers' / 'static' / 'css' / 'components' / 'conditions-summary.css'
        self.assertTrue(css_path.exists())

    @patch('dashboard.get_recession_probability', return_value=None)
    @patch('dashboard.get_conditions_history', return_value={})
    @patch('dashboard.get_market_conditions', return_value=None)
    def test_css_linked_in_template(self, mock_mc, mock_hist, mock_rec):
        app = _get_app()
        with app.test_client() as c:
            html = c.get('/').data.decode()
            self.assertIn('conditions-summary.css', html)


class TestAccessibility(unittest.TestCase):
    """ARIA attributes and accessibility features."""

    def _get_html(self):
        with patch('dashboard.get_market_conditions', return_value=_make_cache()), \
             patch('dashboard.get_conditions_history', return_value={}), \
             patch('dashboard.get_recession_probability', return_value=None):
            app = _get_app()
            with app.test_client() as c:
                return c.get('/').data.decode()

    def test_quadrant_viz_has_role_img(self):
        html = self._get_html()
        self.assertIn('role="img"', html)
        self.assertIn('aria-label="Quadrant visualization', html)

    def test_dimension_cards_aria_expanded(self):
        html = self._get_html()
        # All 4 cards start collapsed
        import re
        expanded_attrs = re.findall(r'aria-expanded="(true|false)"', html)
        # At minimum 4 false (one per card header)
        false_count = expanded_attrs.count('false')
        self.assertGreaterEqual(false_count, 4)

    def test_dimension_expand_role_region(self):
        html = self._get_html()
        self.assertIn('role="region"', html)

    def test_movers_strip_aria_label(self):
        html = self._get_html()
        self.assertIn('aria-label="Today\'s Movers"', html)

    def test_metric_rows_have_aria_labels(self):
        html = self._get_html()
        self.assertIn('aria-label="View Fed Net Liquidity in explorer"', html)
        self.assertIn('aria-label="View VIX in explorer"', html)


class TestJavaScript(unittest.TestCase):
    """JavaScript functionality embedded in template."""

    def _get_html(self):
        with patch('dashboard.get_market_conditions', return_value=_make_cache()), \
             patch('dashboard.get_conditions_history', return_value={}), \
             patch('dashboard.get_recession_probability', return_value=None):
            app = _get_app()
            with app.test_client() as c:
                return c.get('/').data.decode()

    def test_load_ai_summary_function(self):
        html = self._get_html()
        self.assertIn('async function loadAISummary()', html)

    def test_dimension_card_expand_js(self):
        html = self._get_html()
        self.assertIn('initDimensionCards', html)

    def test_movers_strip_render_function(self):
        html = self._get_html()
        self.assertIn('function renderMoversStrip(', html)

    def test_no_old_js_functions(self):
        html = self._get_html()
        self.assertNotIn('updateMarketConditionsGrid', html)
        self.assertNotIn('updateRegimeSignals', html)
        self.assertNotIn('loadPredictionMarkets', html)
        self.assertNotIn('loadMarketSynthesis', html)

    def test_quick_nav_intersection_observer(self):
        html = self._get_html()
        self.assertIn('IntersectionObserver', html)
        self.assertIn('#briefing-section', html)
        self.assertIn('#conditions-section', html)


if __name__ == '__main__':
    unittest.main()
