"""
Tests for US-258.1: AI icon system migration — replace bi-robot with custom SVG icon family.

Verifies:
- CSS tokens --ai-color and --ai-accent are defined
- bi-robot class no longer appears in base.html or index.html
- Chatbot FAB uses custom SVG (white-on-indigo compact variant)
- Chatbot panel header uses sparkle mark SVG + "SignalTrackers AI" label
- AI provenance badge uses sparkle mark SVG
- SVG icons are clean (no scripts, no external references)
- full 64px SVG asset exists in static/img/
"""

import os
import re
import pytest


BASE_HTML = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'templates', 'base.html')
INDEX_HTML = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'templates', 'index.html')
SETTINGS_HTML = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'templates', 'settings.html')
DASHBOARD_CSS = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'static', 'css', 'dashboard.css')
AI_ICON_FULL = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'static', 'img', 'ai-icon-full.svg')


@pytest.fixture(scope='module')
def base_html():
    with open(BASE_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def index_html():
    with open(INDEX_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def settings_html():
    with open(SETTINGS_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def dashboard_css():
    with open(DASHBOARD_CSS) as f:
        return f.read()


# ---------------------------------------------------------------------------
# CSS Tokens
# ---------------------------------------------------------------------------

class TestCSSTokens:
    def test_ai_color_token_defined(self, dashboard_css):
        assert '--ai-color:' in dashboard_css

    def test_ai_color_value(self, dashboard_css):
        assert '#6366F1' in dashboard_css

    def test_ai_accent_token_defined(self, dashboard_css):
        assert '--ai-accent:' in dashboard_css

    def test_ai_accent_value(self, dashboard_css):
        assert '#F59E0B' in dashboard_css

    def test_tokens_in_root(self, dashboard_css):
        root_block = re.search(r':root\s*\{([^}]+)\}', dashboard_css, re.DOTALL)
        assert root_block is not None
        root_content = root_block.group(1)
        assert '--ai-color' in root_content
        assert '--ai-accent' in root_content


# ---------------------------------------------------------------------------
# bi-robot removal
# ---------------------------------------------------------------------------

class TestBiRobotRemoval:
    def test_no_bi_robot_in_base_html(self, base_html):
        assert 'bi-robot' not in base_html

    def test_no_bi_robot_in_index_html(self, index_html):
        assert 'bi-robot' not in index_html

    def test_no_bi_robot_in_settings_html(self, settings_html):
        assert 'bi-robot' not in settings_html


# ---------------------------------------------------------------------------
# Chatbot FAB (base.html)
# ---------------------------------------------------------------------------

class TestChatbotFABIcon:
    def test_fab_icon_span_present(self, base_html):
        assert 'chatbot-fab-icon' in base_html

    def test_fab_uses_svg(self, base_html):
        # FAB should contain an inline SVG within the fab icon span
        fab_section = re.search(r'chatbot-fab-icon[^>]*>(.+?)</span>', base_html, re.DOTALL)
        assert fab_section is not None
        assert '<svg' in fab_section.group(1)

    def test_fab_svg_white_on_indigo(self, base_html):
        # Compact white-on-indigo variant: bars are white/rgba white
        assert 'rgba(255,255,255' in base_html or '#ffffff' in base_html.lower()

    def test_fab_svg_amber_sparkle(self, base_html):
        # Amber sparkle present in FAB section
        fab_section = re.search(r'chatbot-fab.*?chatbot-badge', base_html, re.DOTALL)
        assert fab_section is not None
        assert '#F59E0B' in fab_section.group(0)

    def test_fab_svg_aria_hidden(self, base_html):
        # FAB SVG must be aria-hidden (decorative, button has own label)
        fab_section = re.search(r'chatbot-fab-icon[^>]*>(.+?)</span>', base_html, re.DOTALL)
        assert fab_section is not None
        assert 'aria-hidden="true"' in fab_section.group(1)

    def test_fab_no_script_in_svg(self, base_html):
        fab_section = re.search(r'chatbot-fab-icon[^>]*>(.+?)</span>', base_html, re.DOTALL)
        assert fab_section is not None
        assert '<script' not in fab_section.group(1).lower()


# ---------------------------------------------------------------------------
# Chatbot Panel Header (base.html)
# ---------------------------------------------------------------------------

class TestChatbotPanelHeader:
    def test_panel_title_signaletrackers_ai(self, base_html):
        assert 'MacroClarity AI' in base_html

    def test_panel_header_sparkle_mark(self, base_html):
        # Mark SVG (16px sparkle) should appear before "MacroClarity AI"
        title_section = re.search(r'id="chatbot-title"[^>]*>(.+?)</h2>', base_html, re.DOTALL)
        assert title_section is not None
        title_content = title_section.group(1)
        assert '<svg' in title_content
        assert 'MacroClarity AI' in title_content

    def test_panel_header_mark_indigo(self, base_html):
        title_section = re.search(r'id="chatbot-title"[^>]*>(.+?)</h2>', base_html, re.DOTALL)
        assert title_section is not None
        assert '#6366F1' in title_section.group(1)


# ---------------------------------------------------------------------------
# AI Provenance Badge (index.html)
# ---------------------------------------------------------------------------

class TestAIProvenanceBadge:
    def test_briefing_attribution_present(self, index_html):
        assert 'id="briefing-attribution"' in index_html

    def test_provenance_badge_has_svg(self, index_html):
        badge_section = re.search(r'id="briefing-attribution"[^>]*>(.+?)</small>', index_html, re.DOTALL)
        assert badge_section is not None
        assert '<svg' in badge_section.group(1)

    def test_provenance_badge_ai_generated_text(self, index_html):
        assert "AI-generated from this week's market data" in index_html

    def test_provenance_badge_sparkle_indigo(self, index_html):
        badge_section = re.search(r'id="briefing-attribution"[^>]*>(.+?)</small>', index_html, re.DOTALL)
        assert badge_section is not None
        assert '#6366F1' in badge_section.group(1)


# ---------------------------------------------------------------------------
# SVG Security
# ---------------------------------------------------------------------------

class TestSVGSecurity:
    def test_no_script_in_base_html_svgs(self, base_html):
        # Extract SVG blocks and check for script tags
        svgs = re.findall(r'<svg[^>]*>.*?</svg>', base_html, re.DOTALL)
        for svg in svgs:
            assert '<script' not in svg.lower()
            assert 'onload' not in svg.lower()
            assert 'onclick' not in svg.lower()

    def test_no_external_href_in_svgs(self, base_html):
        svgs = re.findall(r'<svg[^>]*>.*?</svg>', base_html, re.DOTALL)
        for svg in svgs:
            assert 'xlink:href' not in svg.lower()

    def test_no_script_in_index_html_svgs(self, index_html):
        svgs = re.findall(r'<svg[^>]*>.*?</svg>', index_html, re.DOTALL)
        for svg in svgs:
            assert '<script' not in svg.lower()


# ---------------------------------------------------------------------------
# Static SVG Asset
# ---------------------------------------------------------------------------

class TestStaticSVGAsset:
    def test_full_svg_file_exists(self):
        assert os.path.exists(AI_ICON_FULL), \
            "ai-icon-full.svg should exist in static/img/ for design reference purposes"

    def test_full_svg_is_valid_svg(self):
        with open(AI_ICON_FULL) as f:
            content = f.read()
        assert '<svg' in content
        assert '</svg>' in content

    def test_full_svg_no_script(self):
        with open(AI_ICON_FULL) as f:
            content = f.read()
        assert '<script' not in content.lower()
        assert 'onload' not in content.lower()
