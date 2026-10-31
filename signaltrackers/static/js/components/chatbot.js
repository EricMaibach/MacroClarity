/**
 * Chatbot Widget
 * Feature 3.2: Chatbot Mobile-First Redesign
 * US-3.2.1: Core Widget Structure & Interaction
 * US-3.2.2: Message Interaction and AI Integration
 * US-3.2.3: Conversation Persistence, Notifications, and Polish Features
 * US-144.1: Simplified binary state model (closed ↔ expanded)
 *
 * Binary state model:
 *   closed   — FAB visible, panel hidden
 *   expanded — Panel visible, FAB hidden (all viewports)
 */

class ChatbotWidget {
    constructor() {
        this.fab = document.getElementById('chatbot-fab');
        this.panel = document.getElementById('chatbot-panel');
        this.minimizeBtn = document.querySelector('.chatbot-minimize');
        this.clearBtn = document.querySelector('.chatbot-clear-link');
        this.form = document.querySelector('.chatbot-input-form');
        this.input = document.getElementById('chatbot-input');
        this.messages = document.querySelector('.chatbot-messages');
        this.badge = document.querySelector('.chatbot-badge');
        this.performanceBanner = document.querySelector('.chatbot-performance-banner');
        this.performanceDismiss = document.querySelector('.chatbot-performance-dismiss');
        this.clearDialog = document.getElementById('chatbot-clear-dialog');

        // Binary state model: 'closed' | 'expanded'
        this.state = 'closed';
        this.isAnimating = false;
        this.conversation = [];
        this.messageCount = 0;
        this.unreadCount = 0;
        this.performanceBannerDismissed = false;
        this.lastUserMessage = null;

        // Section context set by AI section buttons (US-258.2)
        this.activeSectionId = null;
        this.activeSectionName = null;

        if (!this.fab || !this.panel) {
            console.warn('ChatbotWidget: Required elements not found in DOM');
            return;
        }

        this.init();
    }

    init() {
        // Configure marked for markdown rendering in AI responses
        if (typeof marked !== 'undefined') {
            marked.setOptions({ breaks: true, gfm: true, sanitize: false });
        }

        // Core toggle events
        this.fab.addEventListener('click', () => this.expand());
        this.minimizeBtn.addEventListener('click', () => this.closeChatbot()); // × → closed (FAB only)

        // Clear conversation
        if (this.clearBtn) {
            this.clearBtn.addEventListener('click', () => this.clearConversation());
        }

        // Message form
        if (this.form) {
            this.form.addEventListener('submit', (e) => this.sendMessage(e));
        }

        // Enter key sends message; Shift+Enter creates newline
        if (this.input) {
            this.input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.form.dispatchEvent(new Event('submit', { cancelable: true }));
                }
            });
        }

        // Performance banner dismiss
        if (this.performanceDismiss) {
            this.performanceDismiss.addEventListener('click', () => this.dismissPerformanceBanner());
        }

        // Clear dialog buttons
        if (this.clearDialog) {
            const cancelBtn = this.clearDialog.querySelector('.chatbot-dialog-cancel');
            const confirmBtn = this.clearDialog.querySelector('.chatbot-dialog-confirm');
            if (cancelBtn) cancelBtn.addEventListener('click', () => this.hideClearDialog());
            if (confirmBtn) confirmBtn.addEventListener('click', () => this.executeClearConversation());

            // Close dialog on overlay click (click outside dialog box)
            this.clearDialog.addEventListener('click', (e) => {
                if (e.target === this.clearDialog) this.hideClearDialog();
            });

            // Trap focus within dialog and handle Escape
            this.clearDialog.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    this.hideClearDialog();
                    return;
                }
                if (e.key === 'Tab') {
                    const focusable = Array.from(
                        this.clearDialog.querySelectorAll('button:not([disabled])')
                    );
                    if (focusable.length === 0) return;
                    const first = focusable[0];
                    const last = focusable[focusable.length - 1];
                    if (e.shiftKey && document.activeElement === first) {
                        e.preventDefault();
                        last.focus();
                    } else if (!e.shiftKey && document.activeElement === last) {
                        e.preventDefault();
                        first.focus();
                    }
                }
            });
        }

        // Keyboard shortcut: Escape when expanded → closed (FAB only)
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && this.state === 'expanded' && (!this.clearDialog || this.clearDialog.hasAttribute('hidden'))) {
                this.closeChatbot();
            }
        });

        // Restore conversation state from sessionStorage
        this.restoreConversation();
        this.performanceBannerDismissed = sessionStorage.getItem('chatbot-perf-dismissed') === 'true';
    }

    /**
     * Expand to full panel (from closed state).
     * Transition: closed → expanded
     */
    expand() {
        if (this.isAnimating) return;
        if (this.state === 'expanded') return;
        this.isAnimating = true;

        this.state = 'expanded';

        // Set aria-expanded (FAB hides via CSS on all viewports)
        this.fab.setAttribute('aria-expanded', 'true');

        // Show panel
        this.panel.setAttribute('aria-hidden', 'false');

        // Clear badge
        this.clearBadge();

        // Focus input after animation completes
        setTimeout(() => {
            this.isAnimating = false;
            if (this.input) {
                this.input.focus();
            }
        }, 250);

        this.announce('AI Chatbot opened');
    }

    /**
     * Close chatbot to FAB only (from expanded state).
     * Transition: expanded → closed
     */
    closeChatbot() {
        if (this.isAnimating) return;
        if (this.state !== 'expanded') return;
        this.isAnimating = true;
        this.state = 'closed';

        // Hide panel (slides down via CSS transition)
        this.panel.setAttribute('aria-hidden', 'true');
        this.fab.setAttribute('aria-expanded', 'false');

        // Return focus to FAB after animation
        setTimeout(() => {
            this.isAnimating = false;
            this.fab.focus();
        }, 250);

        this.announce('AI Chatbot closed');
    }

    async sendMessage(e) {
        e.preventDefault();

        const message = this.input.value.trim();
        if (!message) return;

        // Add user message immediately (optimistic UI)
        this.addMessage('user', message);
        this.conversation.push({ role: 'user', content: message });
        this.messageCount++;
        this.lastUserMessage = message;
        this.input.value = '';

        // Show clear link once messages exist
        if (this.clearBtn) {
            this.clearBtn.classList.add('visible');
        }

        // Check for performance banner at 30 messages
        if (this.messageCount === 30 && !this.performanceBannerDismissed) {
            this.showPerformanceBanner();
        }

        // Show typing indicator
        this.showTypingIndicator();

        // Disable submit while waiting
        if (this.form.querySelector('.chatbot-submit')) {
            this.form.querySelector('.chatbot-submit').disabled = true;
        }

        try {
            const response = await this.fetchAIResponse(message);
            this.hideTypingIndicator();
            this.addMessage('ai', response);
            this.conversation.push({ role: 'ai', content: response });

            // If not expanded (closed), show badge on FAB
            if (this.state !== 'expanded') {
                this.showBadge();
            }

            this.saveConversation();

        } catch (error) {
            this.hideTypingIndicator();
            if (error.message === 'AI_UNAVAILABLE') {
                this.showError('AI Temporarily Unavailable. Please try again later.', false, '🤖');
            } else if (error.message === 'RATE_LIMITED') {
                this.showRateLimitError(error.userMessage, error.signupUrl, error.signupLabel);
            } else {
                // Network errors (fetch threw) or other server errors — show retry option
                this.showError('Connection Error. Could not reach the AI. Check your internet connection.', true, '⚠️');
            }
        } finally {
            if (this.form.querySelector('.chatbot-submit')) {
                this.form.querySelector('.chatbot-submit').disabled = false;
            }
        }
    }

    async fetchAIResponse(message) {
        const response = await fetch('/api/chatbot', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''
            },
            body: JSON.stringify({
                message,
                conversation: this.conversation,
                context: {
                    page: window.location.pathname,
                    section: this.activeSectionId || null,
                    section_name: this.activeSectionName || null
                }
            })
        });

        if (response.status === 503) throw new Error('AI_UNAVAILABLE');
        if (response.status === 429) {
            const data = await response.json();
            const err = new Error('RATE_LIMITED');
            err.userMessage = data.message;
            err.signupUrl = data.signup_url || null;
            err.signupLabel = data.signup_label || null;
            err.limitType = data.limit_type || null;
            throw err;
        }
        if (!response.ok) throw new Error('AI_REQUEST_FAILED');

        const data = await response.json();
        return data.response;
    }

    addMessage(role, text) {
        // Hide empty state once messages appear
        const emptyState = this.messages.querySelector('.chatbot-empty-state');
        if (emptyState) {
            emptyState.style.display = 'none';
        }

        const messageEl = document.createElement('div');
        messageEl.className = `chatbot-message chatbot-message--${role}`;

        if (role === 'ai') {
            const renderedText = (typeof marked !== 'undefined')
                ? marked.parse(text)
                : this.escapeHTML(text);
            messageEl.innerHTML = `
                <span class="chatbot-message-icon" aria-hidden="true">🤖</span>
                <span class="chatbot-message-label sr-only">AI said:</span>
                <div class="chatbot-message-text">${renderedText}</div>
            `;
        } else {
            messageEl.innerHTML = `
                <span class="chatbot-message-label sr-only">You said:</span>
                <p class="chatbot-message-text">${this.escapeHTML(text)}</p>
            `;
        }

        this.messages.appendChild(messageEl);
        this.scrollToBottom();

        // Announce AI messages via live region (the role="log" on .chatbot-messages handles this)
        if (role === 'ai') {
            this.announce(`AI says: ${text}`);
        }
    }

    /**
     * Inject a pre-loaded AI opening message for a section entry point.
     * The message is client-side only (no API call) so it appears instantly.
     * It is also added to conversation history so the AI has context for
     * follow-up replies.
     * @param {string} markdownText - The opening message (may contain **bold**)
     */
    addSectionOpeningMessage(markdownText) {
        const html = (typeof marked !== 'undefined')
            ? marked.parse(markdownText)
            : markdownText.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

        // Hide empty state
        const emptyState = this.messages.querySelector('.chatbot-empty-state');
        if (emptyState) emptyState.style.display = 'none';

        const messageEl = document.createElement('div');
        messageEl.className = 'chatbot-message chatbot-message--ai';
        messageEl.innerHTML = `
            <span class="chatbot-message-icon" aria-hidden="true">🤖</span>
            <span class="chatbot-message-label sr-only">AI said:</span>
            <p class="chatbot-message-text">${html}</p>
        `;
        this.messages.appendChild(messageEl);
        this.scrollToBottom();

        // Show clear link
        if (this.clearBtn) this.clearBtn.classList.add('visible');

        // Add to conversation history as an assistant message so the AI has context
        this.conversation.push({ role: 'ai', content: markdownText });
        this.messageCount++;
        this.saveConversation();

        this.announce(`AI says: ${markdownText}`);
    }

    showTypingIndicator() {
        const existing = this.messages.querySelector('.chatbot-typing');
        if (existing) return;

        const indicator = document.createElement('div');
        indicator.className = 'chatbot-typing';
        indicator.setAttribute('aria-label', 'AI is typing');
        indicator.innerHTML = `
            <span class="chatbot-typing-dot"></span>
            <span class="chatbot-typing-dot"></span>
            <span class="chatbot-typing-dot"></span>
        `;
        this.messages.appendChild(indicator);
        this.scrollToBottom();
        this.announce('AI is typing');
    }

    hideTypingIndicator() {
        const indicator = this.messages.querySelector('.chatbot-typing');
        if (indicator) {
            indicator.remove();
        }
    }

    showBadge() {
        this.unreadCount++;
        const count = String(this.unreadCount);
        const label = `${this.unreadCount} new message${this.unreadCount > 1 ? 's' : ''}`;

        // Always show badge on the FAB
        if (this.badge) {
            this.badge.textContent = count;
            this.badge.setAttribute('aria-label', label);
        }
    }

    clearBadge() {
        this.unreadCount = 0;
        // Clear FAB badge
        if (this.badge) {
            this.badge.textContent = '';
            this.badge.removeAttribute('aria-label');
        }
    }

    scrollToBottom() {
        this.messages.scrollTop = this.messages.scrollHeight;
    }

    showError(errorMessage, canRetry = false, icon = '⚠️') {
        const errorEl = document.createElement('div');
        errorEl.className = 'chatbot-message chatbot-message--error';
        errorEl.innerHTML = `
            <span aria-hidden="true">${icon}</span>
            <div>
                <p style="margin:0">${this.escapeHTML(errorMessage)}</p>
                ${canRetry ? '<button class="chatbot-retry-btn">Try Again</button>' : ''}
            </div>
        `;

        if (canRetry) {
            const retryBtn = errorEl.querySelector('.chatbot-retry-btn');
            retryBtn.addEventListener('click', () => {
                errorEl.remove();
                this.retryLastMessage();
            });
        }

        this.messages.appendChild(errorEl);
        this.scrollToBottom();
        this.announce(`Error: ${errorMessage}`, 'assertive');
    }

    showRateLimitError(message, signupUrl, signupLabel) {
        const fallback = 'Rate limit reached. Please try again later.';
        const errorEl = document.createElement('div');
        errorEl.className = 'chatbot-message chatbot-message--error chatbot-message--rate-limit';

        const msgText = this.escapeHTML(message || fallback);
        let ctaHtml = '';
        if (signupUrl) {
            const label = this.escapeHTML(signupLabel || 'Sign Up');
            ctaHtml = `<a href="${this.escapeHTML(signupUrl)}" class="chatbot-signup-cta">${label}</a>`;
        }

        errorEl.innerHTML = `
            <span aria-hidden="true">⚡</span>
            <div>
                <p style="margin:0">${msgText}</p>
                ${ctaHtml}
            </div>
        `;

        this.messages.appendChild(errorEl);
        this.scrollToBottom();
        this.announce(`Rate limit: ${message || fallback}`, 'assertive');

        // Disable input to indicate no more messages allowed
        if (signupUrl) {
            this.disableInput('Session limit reached');
        }
    }

    disableInput(placeholder) {
        if (this.input) {
            this.input.disabled = true;
            this.input.placeholder = placeholder || 'Rate limit reached';
        }
        const submitBtn = this.form?.querySelector('.chatbot-submit');
        if (submitBtn) {
            submitBtn.disabled = true;
        }
    }

    async retryLastMessage() {
        if (!this.lastUserMessage) return;

        // Remove last user message from conversation history (will be re-added on send)
        const lastIdx = this.conversation.findLastIndex(m => m.role === 'user' && m.content === this.lastUserMessage);
        if (lastIdx !== -1) {
            this.conversation.splice(lastIdx, 1);
            this.messageCount--;
        }

        // Put message back in input and submit
        this.input.value = this.lastUserMessage;
        this.form.dispatchEvent(new Event('submit', { cancelable: true }));
    }

    showPerformanceBanner() {
        if (this.performanceBanner) {
            this.performanceBanner.removeAttribute('hidden');
            this.announce('Long conversation may affect performance', 'polite');
        }
    }

    dismissPerformanceBanner() {
        if (this.performanceBanner) {
            this.performanceBanner.setAttribute('hidden', '');
        }
        this.performanceBannerDismissed = true;
        sessionStorage.setItem('chatbot-perf-dismissed', 'true');
    }

    clearConversation() {
        this.showClearDialog();
    }

    showClearDialog() {
        if (!this.clearDialog) return;
        this.clearDialog.removeAttribute('hidden');
        // Default focus on Cancel (safer choice per spec)
        const cancelBtn = this.clearDialog.querySelector('.chatbot-dialog-cancel');
        if (cancelBtn) cancelBtn.focus();
    }

    hideClearDialog() {
        if (!this.clearDialog) return;
        this.clearDialog.setAttribute('hidden', '');
        // Return focus to the clear link
        if (this.clearBtn) this.clearBtn.focus();
    }

    executeClearConversation() {
        this.hideClearDialog();

        this.conversation = [];
        this.messageCount = 0;

        // Reset message area UI
        this.messages.innerHTML = `
            <button class="chatbot-clear-link" aria-label="Clear conversation">
                Clear conversation
            </button>
            <div class="chatbot-empty-state">
                <p class="chatbot-empty-text">Hello! How can I help you understand the markets today?</p>
            </div>
        `;

        // Reset performance banner
        if (this.performanceBanner) {
            this.performanceBanner.setAttribute('hidden', '');
        }
        this.performanceBannerDismissed = false;
        sessionStorage.removeItem('chatbot-perf-dismissed');

        // Re-attach event listener to rebuilt clear button
        this.clearBtn = this.messages.querySelector('.chatbot-clear-link');
        if (this.clearBtn) {
            this.clearBtn.addEventListener('click', () => this.clearConversation());
        }

        this.saveConversation();
        this.closeChatbot();
        this.announce('Conversation cleared');
    }

    saveConversation() {
        const state = {
            conversation: this.conversation,
            messageCount: this.messageCount
        };
        sessionStorage.setItem('chatbot-conversation', JSON.stringify(state));
    }

    restoreConversation() {
        const saved = sessionStorage.getItem('chatbot-conversation');
        if (!saved) return;

        try {
            const state = JSON.parse(saved);
            this.conversation = state.conversation || [];
            this.messageCount = state.messageCount || 0;

            this.conversation.forEach(msg => {
                this.addMessage(msg.role, msg.content);
            });

            if (this.messageCount >= 30 && !this.performanceBannerDismissed) {
                this.showPerformanceBanner();
            }

            // Show clear link if conversation exists
            if (this.conversation.length > 0 && this.clearBtn) {
                this.clearBtn.classList.add('visible');
            }
        } catch (e) {
            console.warn('ChatbotWidget: Could not restore conversation from sessionStorage', e);
        }
    }

    announce(message, priority = 'polite') {
        const announcement = document.createElement('div');
        announcement.setAttribute('role', 'status');
        announcement.setAttribute('aria-live', priority);
        announcement.className = 'sr-only';
        announcement.textContent = message;
        document.body.appendChild(announcement);

        setTimeout(() => announcement.remove(), 1000);
    }

    escapeHTML(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * Open chatbot and immediately fire an AI call for selected briefing text.
     * US-258.3: Sentence-level drill-in (desktop selection toolbar).
     *
     * @param {string} selectedText - The text the user selected
     * @param {string} briefingText - The full briefing text (for AI context)
     */
    async openWithTextDrillIn(selectedText, briefingText) {
        // Expand chatbot if not already open
        if (this.state !== 'expanded') {
            this.expand();
        } else {
            // Already open — just ensure input is focused after interaction
        }

        const userMessage = `Please explain this from today's market briefing: "${selectedText}"`;

        // Add user message to chat
        this.addMessage('user', userMessage);
        this.conversation.push({ role: 'user', content: userMessage });
        this.messageCount++;
        this.lastUserMessage = userMessage;

        if (this.clearBtn) {
            this.clearBtn.classList.add('visible');
        }

        if (this.messageCount === 30 && !this.performanceBannerDismissed) {
            this.showPerformanceBanner();
        }

        this.showTypingIndicator();

        if (this.form && this.form.querySelector('.chatbot-submit')) {
            this.form.querySelector('.chatbot-submit').disabled = true;
        }

        try {
            const response = await fetch('/api/chatbot', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''
                },
                body: JSON.stringify({
                    message: userMessage,
                    conversation: this.conversation,
                    context: {
                        page: window.location.pathname,
                        section: 'briefing-section',
                        section_name: 'Week in Review',
                        briefing_text: briefingText || null
                    }
                })
            });

            this.hideTypingIndicator();

            if (response.status === 429) {
                const errData = await response.json();
                const err = new Error('RATE_LIMITED');
                err.userMessage = errData.message;
                err.signupUrl = errData.signup_url || null;
                err.limitType = errData.limit_type || null;
                throw err;
            }
            if (!response.ok) {
                throw new Error(response.status === 503 ? 'AI_UNAVAILABLE' : 'AI_REQUEST_FAILED');
            }

            const data = await response.json();
            const aiResponse = data.response;
            this.addMessage('ai', aiResponse);
            this.conversation.push({ role: 'ai', content: aiResponse });
            this.saveConversation();

        } catch (error) {
            this.hideTypingIndicator();
            // Fallback: show selected text quoted as a user message so session is not broken
            if (error.message === 'AI_UNAVAILABLE') {
                this.showError('AI Temporarily Unavailable. Please try again later.', false, '🤖');
            } else if (error.message === 'RATE_LIMITED') {
                this.showRateLimitError(error.userMessage, error.signupUrl, error.signupLabel);
            } else {
                this.showError('Connection Error. Could not reach the AI. Check your internet connection.', true, '⚠️');
            }
        } finally {
            if (this.form && this.form.querySelector('.chatbot-submit')) {
                this.form.querySelector('.chatbot-submit').disabled = false;
            }
        }
    }
}

// Initialize when DOM is ready — expose instance globally for AI section buttons (US-258.2)
document.addEventListener('DOMContentLoaded', () => {
    window.chatbotWidget = new ChatbotWidget();
});
