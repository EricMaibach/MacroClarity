"""
Tests for US-258.2: Section-level AI entry points — ghost pill buttons.

Verifies:
- Every major homepage section has an .ai-section-btn with correct aria-label
- Every asset page header has an .ai-section-btn
- Each button has a <button> tag (not div/a)
- Each button has a visible "AI" text span (aria-hidden)
- Each button SVG has aria-hidden="true"
- ai-section-btn.css exists and contains correct styles
- ai-section-btn.js exists and references expected section IDs
- chatbot.js exposes window.chatbotWidget
- chatbot.js includes addSectionOpeningMessage method
- base.html includes ai-section-btn.css and ai-section-btn.js
- dashboard.py passes section context to system prompt
"""

import os
import re
import pytest


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'templates')
STATIC_CSS_COMPONENTS = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'static', 'css', 'components')
STATIC_JS_COMPONENTS = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'static', 'js', 'components')
DASHBOARD_PY = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'dashboard.py')

BASE_HTML = os.path.join(TEMPLATES_DIR, 'base.html')
INDEX_HTML = os.path.join(TEMPLATES_DIR, 'index.html')
CREDIT_HTML = os.path.join(TEMPLATES_DIR, 'credit.html')
EQUITY_HTML = os.path.join(TEMPLATES_DIR, 'equity.html')
RATES_HTML = os.path.join(TEMPLATES_DIR, 'rates.html')
DOLLAR_HTML = os.path.join(TEMPLATES_DIR, 'dollar.html')
CRYPTO_HTML = os.path.join(TEMPLATES_DIR, 'crypto.html')
SAFE_HAVENS_HTML = os.path.join(TEMPLATES_DIR, 'safe_havens.html')
AI_SECTION_BTN_CSS = os.path.join(STATIC_CSS_COMPONENTS, 'ai-section-btn.css')
AI_SECTION_BTN_JS = os.path.join(STATIC_JS_COMPONENTS, 'ai-section-btn.js')
CHATBOT_JS = os.path.join(STATIC_JS_COMPONENTS, 'chatbot.js')


@pytest.fixture(scope='module')
def base_html():
    with open(BASE_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def index_html():
    with open(INDEX_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def credit_html():
    with open(CREDIT_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def equity_html():
    with open(EQUITY_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def rates_html():
    with open(RATES_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def dollar_html():
    with open(DOLLAR_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def crypto_html():
    with open(CRYPTO_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def safe_havens_html():
    with open(SAFE_HAVENS_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def ai_section_btn_css():
    with open(AI_SECTION_BTN_CSS) as f:
        return f.read()


@pytest.fixture(scope='module')
def ai_section_btn_js():
    with open(AI_SECTION_BTN_JS) as f:
        return f.read()


@pytest.fixture(scope='module')
def chatbot_js():
    with open(CHATBOT_JS) as f:
        return f.read()


@pytest.fixture(scope='module')
def dashboard_py():
    with open(DASHBOARD_PY) as f:
        return f.read()


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------

class TestFileExistence:
    def test_ai_section_btn_css_exists(self):
        assert os.path.isfile(AI_SECTION_BTN_CSS)

    def test_ai_section_btn_js_exists(self):
        assert os.path.isfile(AI_SECTION_BTN_JS)


# ---------------------------------------------------------------------------
# base.html includes
# ---------------------------------------------------------------------------

class TestBaseHtmlIncludes:
    def test_ai_section_btn_css_included(self, base_html):
        assert 'ai-section-btn.css' in base_html

    def test_ai_section_btn_js_included(self, base_html):
        assert 'ai-section-btn.js' in base_html


# ---------------------------------------------------------------------------
# Homepage sections — button presence and attributes
# ---------------------------------------------------------------------------

HOMEPAGE_SECTIONS = [
    ('recession-panel-section', 'Recession Probability'),
    ('sector-tone-section', 'Sector Management Tone'),
    ('market-conditions', 'Market Conditions at a Glance'),
    ('trade-pulse-section', 'Global Trade Pulse'),
    ('briefing-section', "this week's review"),
    ('movers-section', "What's Moving Today"),
    ('signals-section', 'Cross-Market Indicators'),
]


class TestHomepageSectionButtons:
    @pytest.mark.parametrize('section_id,section_name', HOMEPAGE_SECTIONS)
    def test_section_button_present(self, index_html, section_id, section_name):
        assert f'data-section-id="{section_id}"' in index_html, \
            f"Missing AI button for section: {section_id}"

    @pytest.mark.parametrize('section_id,section_name', HOMEPAGE_SECTIONS)
    def test_section_button_aria_label(self, index_html, section_id, section_name):
        assert f'aria-label="Ask AI about {section_name}"' in index_html, \
            f"Missing or incorrect aria-label for section: {section_id}"

    def test_all_buttons_use_button_tag(self, index_html):
        # Every ai-section-btn should be a <button> element
        matches = re.findall(r'<(\w+)[^>]+class="[^"]*ai-section-btn[^"]*"', index_html)
        for tag in matches:
            assert tag == 'button', f"Expected <button> tag for ai-section-btn, got <{tag}>"

    def test_all_buttons_have_ai_text_span(self, index_html):
        # Every ai-section-btn should contain <span aria-hidden="true">AI</span>
        # Count occurrences matches count of buttons
        btn_count = index_html.count('class="btn ai-section-btn"')
        ai_span_count = index_html.count('<span aria-hidden="true">AI</span>')
        assert ai_span_count >= btn_count, \
            f"Expected {btn_count} AI text spans, found {ai_span_count}"

    def test_svgs_have_aria_hidden(self, index_html):
        # All SVGs inside ai-section-btn should have aria-hidden
        # Check that each data-section-id line also contains aria-hidden="true" on svg
        for section_id, _ in HOMEPAGE_SECTIONS:
            pattern = rf'data-section-id="{section_id}"[^>]*>.*?aria-hidden="true"'
            assert re.search(pattern, index_html, re.DOTALL), \
                f"SVG aria-hidden missing for section: {section_id}"

    def test_total_homepage_button_count(self, index_html):
        count = index_html.count('class="btn ai-section-btn"')
        assert count == len(HOMEPAGE_SECTIONS), \
            f"Expected {len(HOMEPAGE_SECTIONS)} AI section buttons, found {count}"


# ---------------------------------------------------------------------------
# Asset page headers
# ---------------------------------------------------------------------------

ASSET_PAGES = [
    ('asset-credit', 'Credit Markets', 'credit_html'),
    ('asset-equity', 'Equity Markets', 'equity_html'),
    ('asset-rates', 'Rates &amp; Fixed Income', 'rates_html'),
    ('asset-dollar', 'US Dollar', 'dollar_html'),
    ('asset-crypto', 'Crypto / Bitcoin', 'crypto_html'),
    ('asset-safe-havens', 'Safe Havens', 'safe_havens_html'),
]


class TestAssetPageButtons:
    @pytest.mark.parametrize('section_id,section_name,fixture_name', ASSET_PAGES)
    def test_asset_button_present(self, request, section_id, section_name, fixture_name):
        html = request.getfixturevalue(fixture_name)
        assert f'data-section-id="{section_id}"' in html, \
            f"Missing AI button for asset page: {section_id}"

    @pytest.mark.parametrize('section_id,section_name,fixture_name', ASSET_PAGES)
    def test_asset_button_aria_label(self, request, section_id, section_name, fixture_name):
        html = request.getfixturevalue(fixture_name)
        assert f'aria-label="Ask AI about {section_name}"' in html, \
            f"Missing or incorrect aria-label for asset: {section_id}"

    @pytest.mark.parametrize('section_id,section_name,fixture_name', ASSET_PAGES)
    def test_asset_button_uses_button_tag(self, request, section_id, section_name, fixture_name):
        html = request.getfixturevalue(fixture_name)
        matches = re.findall(r'<(\w+)[^>]+class="[^"]*ai-section-btn[^"]*"', html)
        for tag in matches:
            assert tag == 'button', f"Expected <button>, got <{tag}> for {section_id}"


# ---------------------------------------------------------------------------
# CSS styles
# ---------------------------------------------------------------------------

class TestAISectionBtnCSS:
    def test_ai_section_btn_class_defined(self, ai_section_btn_css):
        assert '.ai-section-btn' in ai_section_btn_css

    def test_indigo_border_color(self, ai_section_btn_css):
        assert 'var(--ai-color' in ai_section_btn_css or '#6366F1' in ai_section_btn_css

    def test_hover_state_defined(self, ai_section_btn_css):
        assert '.ai-section-btn:hover' in ai_section_btn_css

    def test_active_state_defined(self, ai_section_btn_css):
        assert '.ai-section-btn:active' in ai_section_btn_css

    def test_focus_visible_defined(self, ai_section_btn_css):
        assert '.ai-section-btn:focus-visible' in ai_section_btn_css

    def test_hover_fill_value(self, ai_section_btn_css):
        assert 'rgba(99, 102, 241, 0.08)' in ai_section_btn_css

    def test_active_fill_value(self, ai_section_btn_css):
        assert 'rgba(99, 102, 241, 0.16)' in ai_section_btn_css

    def test_section_header_flex_layout(self, ai_section_btn_css):
        assert 'display: flex' in ai_section_btn_css


# ---------------------------------------------------------------------------
# JS — ai-section-btn.js
# ---------------------------------------------------------------------------

class TestAISectionBtnJS:
    def test_all_section_ids_defined(self, ai_section_btn_js):
        expected_ids = [
            'briefing-section',
            'sector-tone-section',
            'market-conditions',
            'movers-section',
            'signals-section',
            'recession-panel-section',
            'trade-pulse-section',
            'asset-credit',
            'asset-equity',
            'asset-rates',
            'asset-dollar',
            'asset-crypto',
            'asset-safe-havens',
        ]
        for section_id in expected_ids:
            assert section_id in ai_section_btn_js, \
                f"Missing section context entry for: {section_id}"

    def test_openChatbotWithSection_function_defined(self, ai_section_btn_js):
        assert 'function openChatbotWithSection' in ai_section_btn_js

    def test_event_listener_wired(self, ai_section_btn_js):
        assert 'ai-section-btn' in ai_section_btn_js
        assert 'data-section-id' in ai_section_btn_js

    def test_chatbot_widget_referenced(self, ai_section_btn_js):
        assert 'window.chatbotWidget' in ai_section_btn_js

    def test_opening_messages_defined(self, ai_section_btn_js):
        assert 'opening:' in ai_section_btn_js


# ---------------------------------------------------------------------------
# chatbot.js updates
# ---------------------------------------------------------------------------

class TestChatbotJSUpdates:
    def test_window_chatbot_widget_exposed(self, chatbot_js):
        assert 'window.chatbotWidget' in chatbot_js

    def test_add_section_opening_message_method(self, chatbot_js):
        assert 'addSectionOpeningMessage' in chatbot_js

    def test_active_section_id_property(self, chatbot_js):
        assert 'activeSectionId' in chatbot_js

    def test_section_context_passed_to_api(self, chatbot_js):
        assert 'section:' in chatbot_js or "section:" in chatbot_js

    def test_fab_open_behavior_unchanged(self, chatbot_js):
        # expand() should still exist
        assert 'expand()' in chatbot_js or "expand =" in chatbot_js
        # FAB click handler still present
        assert "this.fab.addEventListener('click'" in chatbot_js


# ---------------------------------------------------------------------------
# Backend — dashboard.py section context
# ---------------------------------------------------------------------------

class TestBackendSectionContext:
    def test_section_extracted_from_context(self, dashboard_py):
        assert "context.get('section')" in dashboard_py

    def test_section_name_extracted_from_context(self, dashboard_py):
        assert "context.get('section_name')" in dashboard_py

    def test_section_name_injected_into_system_prompt(self, dashboard_py):
        assert 'section_name' in dashboard_py
        assert 'section_context' in dashboard_py
