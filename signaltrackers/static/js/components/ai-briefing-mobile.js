/**
 * AI Briefing Mobile — Sentence-Level Drill-In (Mobile/Touch)
 * US-258.4: Sentence wrapping, amber tap flash, confirm pill.
 *
 * Design decisions:
 * - Active only on pointer:coarse devices (touch screens).
 * - Uses a regex-based sentence tokenizer (see tokenizeSentences() below).
 *   Decision: regex over NLP library — financial prose is structured and
 *   predictable; the regex handles the common edge cases (abbreviations,
 *   financial notation, ellipsis) at zero bundle weight. False-positive rate
 *   is acceptable for this use case.
 * - Sentences wrapped in <span class="ai-sentence"> via MutationObserver so
 *   wrapping fires after the async briefing content loads.
 * - Confirm pill is a fixed-position DOM element (created once in base.html).
 * - Tap outside or Escape dismisses the pill with a 150ms fade.
 * - Ask AI reuses window.chatbotWidget.openWithTextDrillIn() from US-258.3.
 */

(function () {
    'use strict';

    // Only run on touch / coarse-pointer devices
    if (!window.matchMedia('(pointer: coarse)').matches) return;

    let confirmPill = null;
    let dismissTimer = null;
    let activeSentence = null;

    const TEXT_BLOCK_SELECTOR = 'p, li, .briefing-watch__event, .briefing-watch__reason';

    function init() {
        confirmPill = document.getElementById('ai-briefing-confirm-pill');
        if (!confirmPill) return;

        const briefingNarrative = document.getElementById('briefing-narrative');
        if (!briefingNarrative) return;

        // Observe briefing narrative for content load (async AJAX insert)
        const observer = new MutationObserver(function () {
            wrapSentences(briefingNarrative);
        });
        observer.observe(briefingNarrative, { childList: true, subtree: false });

        // Wire confirm pill "Ask AI" button
        const askAiBtn = confirmPill.querySelector('.ai-briefing-confirm-pill__btn');
        if (askAiBtn) {
            askAiBtn.addEventListener('click', onConfirmAskAi);
        }

        // Dismiss on tap outside pill (not on a sentence span)
        document.addEventListener('touchstart', onDocumentTouchStart, { passive: true });

        // Escape key dismisses
        document.addEventListener('keydown', onKeyDown);
    }

    // ── Sentence wrapping ──────────────────────────────────────────────────────

    function wrapSentences(container) {
        // Skip if already wrapped or empty
        if (container.querySelector('.ai-sentence')) return;
        if (!container.innerText.trim()) return;

        // Each paragraph, list item and watch-item text is tokenized on its
        // own, so a tap never merges two bullets into one "sentence". Blocks
        // that contain another text block (a watch <li> with its chip, a
        // loose-list <li> holding <p>s) are left to their children, because
        // wrapping replaces the element's markup.
        const blocks = Array.prototype.filter.call(
            container.querySelectorAll(TEXT_BLOCK_SELECTOR),
            function (el) { return !el.querySelector(TEXT_BLOCK_SELECTOR); }
        );
        if (blocks.length > 0) {
            blocks.forEach(wrapParagraph);
        } else {
            wrapParagraph(container);
        }
    }

    function wrapParagraph(el) {
        const text = el.innerText;
        if (!text.trim()) return;

        const sentences = tokenizeSentences(text);
        if (sentences.length === 0) return;

        // Build wrapped HTML using DOM API to avoid unsanitized innerHTML
        const fragment = document.createDocumentFragment();
        sentences.forEach(function (sentence, i) {
            const span = document.createElement('span');
            span.className = 'ai-sentence';
            span.textContent = sentence;
            fragment.appendChild(span);
            // Add a space between sentences (not after the last)
            if (i < sentences.length - 1) {
                fragment.appendChild(document.createTextNode(' '));
            }
        });

        el.innerHTML = '';
        el.appendChild(fragment);

        // Wire click listener via delegation on the paragraph
        el.addEventListener('click', onParagraphClick);
    }

    // ── Tap interaction ────────────────────────────────────────────────────────

    function onParagraphClick(e) {
        const span = e.target.closest('.ai-sentence');
        if (!span) return;

        e.stopPropagation();

        // Cancel any pending dismiss
        if (dismissTimer) {
            clearTimeout(dismissTimer);
            dismissTimer = null;
        }

        // Re-tapping the same sentence toggles off the highlight and dismisses
        if (span === activeSentence) {
            clearActiveSentence();
            hideConfirmPill(true);
            return;
        }

        const sentenceText = span.textContent.trim();

        // Highlight the tapped sentence (persistent until dismissed or re-tapped)
        setActiveSentence(span);

        // Show confirm pill near tap point
        showConfirmPill(e.clientX, e.clientY, sentenceText);
    }

    function setActiveSentence(span) {
        // Remove highlight from any previously active sentence
        if (activeSentence) {
            activeSentence.classList.remove('is-flashing');
        }
        activeSentence = span;
        span.classList.add('is-flashing');
    }

    function clearActiveSentence() {
        if (activeSentence) {
            activeSentence.classList.remove('is-flashing');
            activeSentence = null;
        }
    }

    function showConfirmPill(tapX, tapY, sentenceText) {
        if (!confirmPill) return;

        // Store sentence text for use when confirmed
        confirmPill.dataset.sentenceText = sentenceText;

        // Remove any active dismiss animation
        confirmPill.classList.remove('is-dismissing');

        // Estimate pill dimensions
        var pillW = 220;
        var pillH = 52;
        var margin = 8;

        var left = tapX - pillW / 2;
        var top = tapY + window.scrollY + margin; // below tap point

        // Clamp horizontally to viewport
        var vw = window.innerWidth;
        left = Math.max(margin, Math.min(left, vw - pillW - margin));

        // If pill would clip below visible area, put it above the tap
        var viewportBottom = window.scrollY + window.innerHeight;
        if (top + pillH > viewportBottom - margin) {
            top = tapY + window.scrollY - pillH - margin;
        }

        confirmPill.style.left = left + 'px';
        confirmPill.style.top = top + 'px';
        confirmPill.classList.add('is-visible');
    }

    function hideConfirmPill(withFade) {
        if (!confirmPill) return;
        if (!confirmPill.classList.contains('is-visible')) return;

        clearActiveSentence();

        if (withFade) {
            confirmPill.classList.add('is-dismissing');
            dismissTimer = setTimeout(function () {
                confirmPill.classList.remove('is-visible', 'is-dismissing');
                dismissTimer = null;
            }, 150);
        } else {
            confirmPill.classList.remove('is-visible', 'is-dismissing');
        }
    }

    // ── Confirm pill actions ───────────────────────────────────────────────────

    function onConfirmAskAi() {
        var sentenceText = confirmPill ? confirmPill.dataset.sentenceText : '';
        if (!sentenceText) return;

        var briefingNarrative = document.getElementById('briefing-narrative');
        var briefingText = briefingNarrative ? briefingNarrative.innerText.trim() : '';

        hideConfirmPill(false);

        var widget = window.chatbotWidget;
        if (!widget) {
            console.warn('ai-briefing-mobile: chatbotWidget not available');
            return;
        }

        widget.openWithTextDrillIn(sentenceText, briefingText);
    }

    // ── Dismiss handlers ───────────────────────────────────────────────────────

    function onDocumentTouchStart(e) {
        if (!confirmPill || !confirmPill.classList.contains('is-visible')) return;

        // Don't dismiss if touching the pill itself
        if (confirmPill.contains(e.target)) return;

        // Don't dismiss if touching a sentence span (onParagraphClick will handle it)
        if (e.target.classList.contains('ai-sentence') || e.target.closest('.ai-sentence')) return;

        hideConfirmPill(true);
    }

    function onKeyDown(e) {
        if (e.key === 'Escape') hideConfirmPill(true);
    }

    // ── Sentence tokenizer (regex) ─────────────────────────────────────────────
    //
    // Decision: regex over NLP library.
    // Financial briefing prose is highly structured and AI-generated, so a
    // regex that protects known false-positive patterns is accurate enough and
    // adds zero bundle weight.
    //
    // Protected patterns (replaced with PLACEHOLDER before splitting):
    //   1. Ellipsis:  ...  →  not a sentence boundary
    //   2. Single uppercase initial: U.S., F.B.I.
    //   3. Common abbreviations: vs., Fed., Jan., bps., etc.
    //   4. Digits before period: $1.2T, 3.4%
    //
    // Split rule: [.!?] optionally followed by closing quote, then whitespace
    // + capital letter.

    function tokenizeSentences(text) {
        var PH = '\u2060'; // Word Joiner — invisible, used as period placeholder

        var t = text
            // Protect ellipsis first (before the single-char rule fires on it)
            .replace(/\.\.\./g, PH + PH + PH)
            // Protect single uppercase initials (U.S., F.B.I.)
            .replace(/\b([A-Z])\./g, '$1' + PH)
            // Protect common financial / general abbreviations (case-insensitive)
            .replace(/\b(vs|eg|ie|etc|fed|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec|mr|mrs|ms|dr|prof|sr|jr|bps|pts|pct|approx|corp|inc|ltd|co|no|vol|dept|govt|natl|intl|avg|est|ref)\./gi, '$1' + PH)
            // Protect digits before period ($1.2T, 3.4%)
            .replace(/(\d)\./g, '$1' + PH);

        // Split on: [.!?] optionally followed by closing quote/paren,
        // then whitespace, then uppercase letter (start of next sentence)
        var parts = t.split(/(?<=[.!?]["')\]]?)\s+(?=[A-Z])/);

        return parts
            .map(function (s) {
                return s.replace(new RegExp(PH, 'g'), '.').trim();
            })
            .filter(function (s) { return s.length > 0; });
    }

    document.addEventListener('DOMContentLoaded', init);
})();
