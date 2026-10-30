"""
Tests for US-258.4: Sentence-level drill-in — mobile tap flow (amber flash + confirm pill).

Verifies (static analysis — template, CSS, JS):
- Confirm pill DOM element exists in base.html with role="dialog"
- Confirm pill has one button: "Ask AI about this"
- Confirm pill button has 44px+ touch target via padding
- ai-briefing-mobile.js exists and exports expected functionality
- Sentence tokenizer handles abbreviations, financial notation, ellipsis
- Sentence wrapping logic targets #briefing-narrative
- Amber flash CSS: .ai-sentence.is-flashing uses rgba(245,158,11,0.2)
- Confirm pill CSS: position:absolute, z-index, is-visible, is-dismissing classes
- Mobile JS runs only on pointer:coarse devices (matchMedia guard)
- base.html includes ai-briefing-mobile.js
- Confirm pill dismiss: 150ms fade transition defined
- openWithTextDrillIn called on confirm
- MutationObserver used for async content detection
- No interference with desktop toolbar (coarse-pointer guard)
"""

import os
import re
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'templates')
STATIC_CSS = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'static', 'css', 'components')
STATIC_JS = os.path.join(os.path.dirname(__file__), '..', 'signaltrackers', 'static', 'js', 'components')

BASE_HTML = os.path.join(TEMPLATES_DIR, 'base.html')
INDEX_HTML = os.path.join(TEMPLATES_DIR, 'index.html')
TOOLBAR_CSS = os.path.join(STATIC_CSS, 'ai-briefing-toolbar.css')
MOBILE_JS = os.path.join(STATIC_JS, 'ai-briefing-mobile.js')
CHATBOT_JS = os.path.join(STATIC_JS, 'chatbot.js')


@pytest.fixture(scope='module')
def base_html():
    with open(BASE_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def index_html():
    with open(INDEX_HTML) as f:
        return f.read()


@pytest.fixture(scope='module')
def toolbar_css():
    with open(TOOLBAR_CSS) as f:
        return f.read()


@pytest.fixture(scope='module')
def mobile_js():
    with open(MOBILE_JS) as f:
        return f.read()


@pytest.fixture(scope='module')
def chatbot_js():
    with open(CHATBOT_JS) as f:
        return f.read()


# ── Confirm pill DOM (base.html) ────────────────────────────────────────────

class TestConfirmPillDOM:

    def test_confirm_pill_element_exists(self, base_html):
        """Confirm pill element exists in DOM."""
        assert 'id="ai-briefing-confirm-pill"' in base_html

    def test_confirm_pill_has_role_dialog(self, base_html):
        """Confirm pill has role=dialog."""
        assert 'role="dialog"' in base_html

    def test_confirm_pill_has_aria_label(self, base_html):
        """Confirm pill has aria-label."""
        assert 'aria-label="Ask AI about this sentence"' in base_html

    def test_confirm_pill_has_button(self, base_html):
        """Confirm pill contains a button element."""
        # Find the pill section and check it has a button
        pill_start = base_html.find('id="ai-briefing-confirm-pill"')
        pill_end = base_html.find('</div>', pill_start)
        pill_html = base_html[pill_start:pill_end + 6]
        assert '<button' in pill_html

    def test_confirm_pill_button_text(self, base_html):
        """Confirm pill button says 'Ask AI about this'."""
        assert 'Ask AI about this' in base_html

    def test_confirm_pill_button_class(self, base_html):
        """Confirm pill button has correct class."""
        assert 'ai-briefing-confirm-pill__btn' in base_html

    def test_confirm_pill_button_has_aria_label(self, base_html):
        """Confirm pill button has accessible aria-label."""
        # The button inside the pill should have aria-label
        pill_start = base_html.find('id="ai-briefing-confirm-pill"')
        section = base_html[pill_start:pill_start + 500]
        assert 'aria-label=' in section

    def test_confirm_pill_has_sparkle_svg(self, base_html):
        """Confirm pill button contains sparkle SVG icon."""
        # SVG sparkle path near the pill
        pill_start = base_html.find('id="ai-briefing-confirm-pill"')
        section = base_html[pill_start:pill_start + 500]
        assert '<svg' in section
        assert 'aria-hidden="true"' in section

    def test_confirm_pill_has_correct_css_class(self, base_html):
        """Confirm pill root element has class ai-briefing-confirm-pill."""
        assert 'class="ai-briefing-confirm-pill"' in base_html


# ── Script tag in base.html ─────────────────────────────────────────────────

class TestBaseHTMLScriptTag:

    def test_mobile_js_script_tag_exists(self, base_html):
        """base.html loads ai-briefing-mobile.js."""
        assert 'ai-briefing-mobile.js' in base_html

    def test_mobile_js_after_toolbar_js(self, base_html):
        """Mobile JS loaded after toolbar JS (dependencies in order)."""
        toolbar_pos = base_html.find('ai-briefing-toolbar.js')
        mobile_pos = base_html.find('ai-briefing-mobile.js')
        assert toolbar_pos > 0 and mobile_pos > toolbar_pos

    def test_mobile_js_after_chatbot_js(self, base_html):
        """Mobile JS loaded after chatbot.js (chatbotWidget must exist first)."""
        chatbot_pos = base_html.find('chatbot.js')
        mobile_pos = base_html.find('ai-briefing-mobile.js')
        assert chatbot_pos > 0 and mobile_pos > chatbot_pos


# ── Mobile JS: file existence and guards ───────────────────────────────────

class TestMobileJSExists:

    def test_mobile_js_file_exists(self):
        """ai-briefing-mobile.js file exists."""
        assert os.path.isfile(MOBILE_JS)

    def test_mobile_js_pointer_coarse_guard(self, mobile_js):
        """Script exits early if not pointer:coarse."""
        assert 'pointer: coarse' in mobile_js

    def test_mobile_js_uses_matchmedia(self, mobile_js):
        """Script uses window.matchMedia to detect touch device."""
        assert 'matchMedia' in mobile_js

    def test_mobile_js_dom_content_loaded(self, mobile_js):
        """Script initialises on DOMContentLoaded."""
        assert 'DOMContentLoaded' in mobile_js

    def test_mobile_js_uses_iife(self, mobile_js):
        """Script wrapped in IIFE to avoid global scope pollution."""
        assert '(function' in mobile_js and '})()' in mobile_js


# ── Mobile JS: sentence wrapping ───────────────────────────────────────────

class TestSentenceWrapping:

    def test_targets_briefing_narrative(self, mobile_js):
        """JS targets #briefing-narrative element."""
        assert 'briefing-narrative' in mobile_js

    def test_wraps_in_ai_sentence_spans(self, mobile_js):
        """Sentence spans have class ai-sentence."""
        assert 'ai-sentence' in mobile_js

    def test_uses_mutation_observer(self, mobile_js):
        """Uses MutationObserver to detect async content load."""
        assert 'MutationObserver' in mobile_js

    def test_handles_paragraph_elements(self, mobile_js):
        """Wraps sentences inside <p> elements (and <li>s, US-490.3)."""
        assert "TEXT_BLOCK_SELECTOR = 'p, li" in mobile_js

    def test_wraps_paragraph_element(self, mobile_js):
        """wrapParagraph function exists."""
        assert 'wrapParagraph' in mobile_js

    def test_uses_dom_api_not_innerhtml(self, mobile_js):
        """Uses DOM API (createDocumentFragment / createElement) to avoid XSS via unsanitized innerHTML."""
        assert 'createDocumentFragment' in mobile_js or 'createElement' in mobile_js

    def test_no_inline_onclick_handlers(self, mobile_js):
        """Listeners attached programmatically, not as inline onclick attributes."""
        assert 'onclick=' not in mobile_js


# ── Mobile JS: sentence tokenizer ──────────────────────────────────────────

class TestSentenceTokenizer:

    def test_tokenizer_function_exists(self, mobile_js):
        """tokenizeSentences function defined."""
        assert 'tokenizeSentences' in mobile_js

    def test_protects_single_uppercase_initials(self, mobile_js):
        """Regex protects single uppercase initials (U.S., F.B.I.)."""
        # Pattern: \b([A-Z])\.
        assert r'\b([A-Z])' in mobile_js or "\\b([A-Z])" in mobile_js

    def test_protects_common_abbreviations(self, mobile_js):
        """Regex includes common abbreviations: vs, fed, bps, etc."""
        assert 'vs' in mobile_js
        assert 'fed' in mobile_js.lower()
        assert 'bps' in mobile_js

    def test_protects_ellipsis(self, mobile_js):
        """Regex protects ellipsis (...)."""
        assert '\\.\\.\\.' in mobile_js or r'\.\.\.' in mobile_js

    def test_protects_digits_before_period(self, mobile_js):
        """Regex protects digits before period ($1.2T, 3.4%)."""
        # Pattern: (\d)\.
        assert r'(\d)' in mobile_js or '(\\d)' in mobile_js

    def test_uses_placeholder(self, mobile_js):
        """Uses a placeholder character to protect periods during split."""
        # Should contain \u2060 (word joiner) or similar
        assert '\\u2060' in mobile_js or '\u2060' in mobile_js

    def test_decision_documented(self, mobile_js):
        """Tokenizer decision (regex vs NLP) documented in source."""
        # Should have a comment explaining the choice
        assert 'regex' in mobile_js.lower()
        assert 'tokenizer' in mobile_js.lower()


# ── Mobile JS: tap interaction ─────────────────────────────────────────────

class TestTapInteraction:

    def test_click_listener_on_paragraph(self, mobile_js):
        """Click event listener used for tap delegation."""
        assert "addEventListener('click'" in mobile_js

    def test_closest_ai_sentence(self, mobile_js):
        """Uses closest('.ai-sentence') for event delegation."""
        assert "closest('.ai-sentence')" in mobile_js

    def test_set_active_sentence_function(self, mobile_js):
        """setActiveSentence function exists for persistent amber highlight."""
        assert 'setActiveSentence' in mobile_js

    def test_clear_active_sentence_function(self, mobile_js):
        """clearActiveSentence function exists to remove highlight on dismiss."""
        assert 'clearActiveSentence' in mobile_js

    def test_adds_is_flashing_class(self, mobile_js):
        """Adds is-flashing class for persistent amber highlight."""
        assert 'is-flashing' in mobile_js

    def test_highlight_persists_until_dismissed(self, mobile_js):
        """Highlight is NOT removed by a setTimeout — it persists until dismiss or re-tap."""
        # activeSentence tracks the highlighted span
        assert 'activeSentence' in mobile_js
        # clearActiveSentence removes the class on dismiss
        assert 'classList.remove' in mobile_js

    def test_retap_same_sentence_toggles_off(self, mobile_js):
        """Re-tapping the same sentence dismisses the pill and removes the highlight."""
        assert 'span === activeSentence' in mobile_js

    def test_shows_confirm_pill(self, mobile_js):
        """showConfirmPill function exists."""
        assert 'showConfirmPill' in mobile_js

    def test_pill_positioned_near_tap(self, mobile_js):
        """Pill positioned using tap coordinates (clientX, clientY)."""
        assert 'clientX' in mobile_js and 'clientY' in mobile_js

    def test_pill_clamped_to_viewport(self, mobile_js):
        """Pill position clamped to viewport width."""
        assert 'innerWidth' in mobile_js

    def test_stores_sentence_text_on_pill(self, mobile_js):
        """Sentence text stored in dataset on confirm pill."""
        assert 'dataset.sentenceText' in mobile_js


# ── Mobile JS: confirm pill ─────────────────────────────────────────────────

class TestConfirmPillLogic:

    def test_targets_confirm_pill_by_id(self, mobile_js):
        """JS targets confirm pill by ID."""
        assert 'ai-briefing-confirm-pill' in mobile_js

    def test_opens_chatbot_on_confirm(self, mobile_js):
        """Tapping confirm pill calls openWithTextDrillIn."""
        assert 'openWithTextDrillIn' in mobile_js

    def test_passes_briefing_text_context(self, mobile_js):
        """Full briefing text passed as context to openWithTextDrillIn."""
        assert 'briefingText' in mobile_js or 'briefing_text' in mobile_js or 'briefingNarrative' in mobile_js

    def test_hide_confirm_pill_function(self, mobile_js):
        """hideConfirmPill function exists."""
        assert 'hideConfirmPill' in mobile_js

    def test_fade_dismiss_150ms(self, mobile_js):
        """Fade dismiss uses ~150ms timeout."""
        assert '150' in mobile_js

    def test_is_dismissing_class(self, mobile_js):
        """Uses is-dismissing class for fade-out animation."""
        assert 'is-dismissing' in mobile_js

    def test_is_visible_class(self, mobile_js):
        """Uses is-visible class to show/hide pill."""
        assert 'is-visible' in mobile_js

    def test_dismiss_on_outside_touch(self, mobile_js):
        """Dismisses pill on touch outside."""
        assert 'touchstart' in mobile_js

    def test_dismiss_on_escape(self, mobile_js):
        """Dismisses pill on Escape key."""
        assert 'Escape' in mobile_js

    def test_does_not_dismiss_if_touching_pill(self, mobile_js):
        """Does not dismiss pill if user touches inside pill."""
        assert 'confirmPill.contains' in mobile_js

    def test_graceful_fallback_no_chatbot(self, mobile_js):
        """Gracefully handles chatbotWidget being unavailable."""
        assert 'chatbotWidget' in mobile_js
        assert 'return' in mobile_js  # early return guard exists


# ── CSS: confirm pill styles ────────────────────────────────────────────────

class TestConfirmPillCSS:

    def test_confirm_pill_class_exists(self, toolbar_css):
        """CSS defines .ai-briefing-confirm-pill."""
        assert '.ai-briefing-confirm-pill' in toolbar_css

    def test_confirm_pill_position_absolute(self, toolbar_css):
        """Confirm pill uses absolute positioning."""
        # Extract the confirm pill rule block
        assert 'position: absolute' in toolbar_css

    def test_confirm_pill_z_index(self, toolbar_css):
        """Confirm pill has a z-index above the main toolbar (>1080)."""
        # toolbar is z-index 1080; pill should be 1090+
        match = re.search(r'\.ai-briefing-confirm-pill\s*\{[^}]*z-index:\s*(\d+)', toolbar_css, re.DOTALL)
        assert match is not None, "z-index not found in .ai-briefing-confirm-pill"
        z = int(match.group(1))
        assert z >= 1080

    def test_confirm_pill_hidden_by_default(self, toolbar_css):
        """Confirm pill hidden by default (display:none)."""
        assert 'display: none' in toolbar_css

    def test_confirm_pill_is_visible_class(self, toolbar_css):
        """is-visible class shows the pill."""
        assert '.ai-briefing-confirm-pill.is-visible' in toolbar_css

    def test_confirm_pill_is_dismissing_class(self, toolbar_css):
        """is-dismissing class exists for fade-out transition."""
        assert '.ai-briefing-confirm-pill.is-dismissing' in toolbar_css

    def test_confirm_pill_opacity_transition(self, toolbar_css):
        """Confirm pill uses opacity transition for fade."""
        # Both .ai-briefing-confirm-pill and .is-dismissing should reference opacity
        assert 'opacity' in toolbar_css
        assert 'transition' in toolbar_css

    def test_confirm_pill_button_min_height(self, toolbar_css):
        """Confirm pill button has min-height for 44px touch target."""
        assert 'min-height: 44px' in toolbar_css

    def test_confirm_pill_border_radius(self, toolbar_css):
        """Confirm pill has border-radius."""
        assert 'border-radius' in toolbar_css

    def test_confirm_pill_box_shadow(self, toolbar_css):
        """Confirm pill has box-shadow for visual elevation."""
        assert 'box-shadow' in toolbar_css

    def test_confirm_pill_ai_color(self, toolbar_css):
        """Confirm pill button text uses --ai-color."""
        assert 'ai-color' in toolbar_css

    def test_sentence_flash_class_exists(self, toolbar_css):
        """CSS defines .ai-sentence.is-flashing."""
        assert '.ai-sentence.is-flashing' in toolbar_css

    def test_sentence_flash_amber_color(self, toolbar_css):
        """Flash uses amber rgba(245,158,11,0.2) per spec."""
        assert 'rgba(245, 158, 11, 0.2)' in toolbar_css or 'rgba(245,158,11,0.2)' in toolbar_css

    def test_sentence_flash_border_radius(self, toolbar_css):
        """Flash has 4px border-radius per spec."""
        # The flash rule should have border-radius
        flash_start = toolbar_css.find('.ai-sentence.is-flashing')
        flash_block = toolbar_css[flash_start:toolbar_css.find('}', flash_start) + 1]
        assert 'border-radius' in flash_block

    def test_desktop_toolbar_hidden_on_coarse(self, toolbar_css):
        """Desktop toolbar hidden on pointer:coarse devices."""
        assert 'pointer: coarse' in toolbar_css
        assert 'display: none !important' in toolbar_css


# ── Index.html: discoverability hints ─────────────────────────────────────

class TestDiscoverabilityHints:

    def test_mobile_hint_text(self, index_html):
        """Mobile hint text matches spec."""
        assert 'Tap any sentence to explore with AI' in index_html

    def test_desktop_hint_text(self, index_html):
        """Desktop hint text exists."""
        assert 'Select any text to ask AI' in index_html

    def test_mobile_hint_class(self, index_html):
        """Mobile hint has ai-briefing-hint--mobile class."""
        assert 'ai-briefing-hint--mobile' in index_html

    def test_desktop_hint_class(self, index_html):
        """Desktop hint has ai-briefing-hint--desktop class."""
        assert 'ai-briefing-hint--desktop' in index_html


# ── chatbot.js: openWithTextDrillIn compatibility ─────────────────────────

class TestChatbotCompatibility:

    def test_open_with_text_drill_in_exists(self, chatbot_js):
        """chatbot.js has openWithTextDrillIn method (required by mobile flow)."""
        assert 'openWithTextDrillIn' in chatbot_js

    def test_chatbot_widget_exposed_globally(self, chatbot_js):
        """window.chatbotWidget exposed so mobile JS can call it."""
        assert 'window.chatbotWidget' in chatbot_js
