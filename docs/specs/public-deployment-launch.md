# Public Deployment Launch Design Spec

**Issue:** #513
**Created:** 2026-11-04
**Status:** Draft

## Overview

MacroClarity is about to become publicly reachable at macroclarity.com. Today, "not financial advice" appears only in emails and on the portfolio page, there are no Terms or Privacy pages, and an empty Sector Management Tone panel still presents fallback output as analysis.

This spec covers the user-facing parts of #513:

1. A **site-wide footer** with the disclaimer and legal links.
2. An **inline disclaimer** on every surface that shows analysis.
3. **Terms of Service and Privacy Policy** pages.
4. **Account deletion**, which the Privacy Policy has to describe (PM decision needed, see Open Questions).
5. A **consent line** on the register page.
6. A **UI copy sweep** that removes instruction-style language.
7. **Removal of the Sector Management Tone panel**.

The rest of #513 (readiness review, prompt rewrites, hardening, VPS, Caddy, Watchtower, runbook) has no UI and needs no design.

### Design principle: quiet, consistent, always present

Disclaimers appear on every analysis surface, so they must be easy to read and easy to skip. Amber warning styling on every card would teach users to ignore warnings and would dilute the real stale and error notices. So:

- Disclaimers use plain neutral text with no warning colour and no dismiss control.
- The wording comes from one source and doesn't vary from page to page.
- A disclaimer appears only when analysis is on screen. Loading, error and collapsed states don't show one.
- Only the portfolio analysis gets more emphasis, because it's the one surface based on the user's own holdings.

---

## User Flow

1. A visitor opens any page, signed in or not, and sees the disclaimer and the Terms and Privacy links in the footer.
2. A reader of the Week in Review, a section review or the news summary sees a one-line disclaimer at the end of that block.
3. A chatbot user sees a one-line disclaimer directly above the input at all times.
4. A portfolio user sees the emphasized disclaimer directly under the AI analysis.
5. A new user sees what they're agreeing to directly above the Create Account button, and can open either document without losing the form.
6. A user who wants to leave deletes their account from Settings and gets a confirmation on the homepage.

---

## 1. Site-Wide Footer

**File:** `signaltrackers/templates/base.html` (replaces lines 315–320)

### Mobile (375px)

```
┌─────────────────────────────┐
│ MacroClarity                │
│ Macro Finance Data &        │
│ Analysis · Data updated     │
│ daily                       │
│                             │
│ Terms of Service   Privacy  │ ← 44px targets
│                             │
│ MacroClarity provides       │
│ market information and      │
│ AI-generated analysis for   │
│ educational purposes only…  │
│                             │
│ © 2026 MacroClarity         │
│                             │ ← FAB clearance (see below)
└─────────────────────────────┘
```

### Tablet / Desktop (768px+)

```
┌──────────────────────────────────────────────────────────────┐
│ MacroClarity · Macro Finance Data &    Terms of Service  Privacy │
│ Analysis · Data updated daily                                │
│ ──────────────────────────────────────────────────────────── │
│ MacroClarity provides market information and AI-generated    │
│ analysis for educational purposes only… (max-width 72ch)     │
│ © 2026 MacroClarity                                          │
└──────────────────────────────────────────────────────────────┘
```

### Copy

> MacroClarity provides market information and AI-generated analysis for educational purposes only. It is not financial, investment, tax or legal advice, and AI-generated content may contain errors. Consult a qualified professional before making investment decisions.

"Data updated daily" stays as it is, following the "two clocks" rule in `weekly-week-in-review.md`.

### Specifications

| Element | Style |
|---|---|
| Background | `--brand-navy-dark` (#09264c), unchanged |
| Container | `--container-xl`, left-aligned text at all widths. Long centred text is hard to read. |
| Brand line | `text-sm`, white (15.07:1) |
| Links | `text-sm`, white, underlined, `min-height: 44px` with `inline-flex` centring |
| Link hover | Underline thickens to 2px |
| Link focus | 2px white outline, 2px offset. The default blue focus ring would be hard to see on navy. |
| Disclaimer | `text-xs` (12px), line-height 1.6, `--neutral-300` #D1D8DD (10.46:1), max-width 72ch |
| Divider (768px+) | 1px, `rgba(255,255,255,0.15)` |
| Copyright | `text-xs`, `--neutral-300`, year from the server clock |

The footer needs no ARIA beyond the existing `<footer>` landmark. Wrap the links in `<nav aria-label="Legal">`.

### FAB clearance

Fixed buttons sit over the bottom right of the screen below 768px: the chatbot FAB (signed-in users) and, on the homepage, the quick-nav FAB stacked above it.

**Acceptance rule:** at maximum scroll, no footer text or link is covered by a FAB.

Starting values for `padding-bottom` on the footer below 768px: 88px with the chatbot FAB, 152px where the quick-nav FAB is also shown. Check the result in screenshots.

---

## 2. Inline Disclaimer Component

**New:** `signaltrackers/static/css/components/analysis-disclaimer.css`, plus an `analysis_disclaimer(variant)` macro in `templates/_macros.html`, so wording lives in one place.

### Variants

| Variant | Copy | Style |
|---|---|---|
| `ai` | AI-generated for educational purposes only. Not financial advice. May contain errors. | `text-xs`, `--neutral-600`, not italic |
| `portfolio` | AI-generated educational analysis, not financial advice. It may contain errors and doesn't account for your full financial situation. Talk to a qualified financial advisor before changing your investments. | `text-sm`, `--neutral-700`, `bi-info-circle` icon (`aria-hidden`), `--neutral-50` background, `--space-3` padding, 6px radius |
| `rules` | General asset-class context based on current conditions, not a recommendation for your portfolio. | `text-xs`, `--neutral-600`, not italic |

`rules` exists because the homepage implications table is rule-based. Calling it "AI-generated" would be inaccurate.

Contrast: `--neutral-600` is 7.53:1 on white and 7.16:1 on page surface. `--neutral-700` is 11.99:1 on white. Don't use `--neutral-400` (2.5:1), which is what the news card footer currently uses.

The line contains no links, so it adds no extra touch targets. The Terms link is always available in the footer.

### Placement

| Surface | File | Position | Shown when |
|---|---|---|---|
| Homepage Week in Review | `index.html` | Below `.conditions-briefing__footer`, above the discoverability hints | Review or markdown fallback is displayed, including stale |
| Section reviews (5 pages) | `credit`, `equity`, `rates`, `dollar`, `crypto` templates | After `.section-review__footer`, inside the collapsible | Same as above. Collapsed content is hidden, so no separate rule is needed. |
| News summary | `news.html` | Inside `.news-summary-card`, below `__footer` | Summary is displayed |
| Portfolio analysis | `portfolio.html` | Inside `.card-body`, below `#portfolio-summary-content`, `portfolio` variant | Analysis is displayed. Hidden during loading and for `#portfolio-summary-error`. |
| Homepage implications | `index.html` | Below `.implications-table`, above `.implications-explore`, `rules` variant | `implications` is present |
| Chatbot | `base.html` | See below | Whenever the panel is open |
| Emails | `email/base_email.html`, `base_email.txt` | Existing footer disclaimer | Always |

**Rendering rule:** render the disclaimer outside the element that the loaders fill. It must not be overwritten when content loads, and it must not be picked up by the sentence-tap and select-to-ask behaviour on the homepage briefing.

### Chatbot

```
┌─────────────────────────────┐
│ MacroClarity AI           ✕ │
├─────────────────────────────┤
│ messages                    │
├─────────────────────────────┤
│ AI can be wrong. Educational│ ← New fixed row, doesn't scroll
│ only, not financial advice. │
├─────────────────────────────┤
│ [Ask about this chart…]  ↑  │
└─────────────────────────────┘
```

- Copy: "AI can be wrong. Educational only, not financial advice."
- Element: `<p id="chatbot-disclaimer">` between the messages and the form, `flex-shrink: 0`.
- Style: `text-xs`, `--neutral-600`, padding 4px 16px, top border `--neutral-200`, centred.
- Add it to the textarea's description: `aria-describedby="chatbot-input-hint chatbot-disclaimer"`. Don't make it a live region.
- The mobile panel can be as short as 300px, so the messages area gives up about 24px, or about 40px if the line wraps. Show this in a screenshot. If the input is pushed out of view with the keyboard open, reduce the row's padding before changing the copy.

### Email fix

The existing disclaimer is 11px #999999 on white, which is 2.85:1 and fails AA. Change it to 12px #697386 (4.78:1) and add Terms and Privacy links built from `BASE_URL`. Keep the existing wording, because it already says the same thing.

---

## 3. Terms of Service and Privacy Policy Pages

**New:** `templates/legal/terms.html` and `templates/legal/privacy.html`, plus `static/css/legal.css`.
**Routes:** `/terms` and `/privacy`. Both must be public and reachable without login or an invite code.

### Layout

```
┌─────────────────────────────┐
│ Terms of Service            │ ← H1, Noto Serif (the page's one serif hero)
│ Last updated: Nov 4, 2026   │ ← text-sm, neutral-500
├─────────────────────────────┤
│ The short version           │ ← neutral-50 box, 3–4 bullets
│ • …                         │
├─────────────────────────────┤
│ On this page                │ ← Plain anchor list, no JS
│ 1. Educational use only     │
│ 2. …                        │
├─────────────────────────────┤
│ 1. Educational use only     │ ← H2, Inter 600, neutral-700
│ Body text…                  │ ← text-base, neutral-600, 1.6
└─────────────────────────────┘
```

The layout is the same at every width: single column, `--container-sm` (640px), and `--space-4` side padding on mobile. Don't add a sticky side table of contents, because it isn't worth the complexity for pages this size.

- Each H2 has an `id`, and `scroll-margin-top` keeps it clear of the fixed navbar.
- Table of contents links have `min-height: 44px` on mobile.
- Page titles: "Terms of Service - MacroClarity" and "Privacy Policy - MacroClarity".
- The "short version" box says that the full text governs.
- The chatbot FAB and section AI buttons aren't useful on these pages. Hide the FAB here if that's simple to do, otherwise leave it.

### Required content

The text is **draft copy for founder review**. It isn't legal advice, and the readiness checklist should flag both documents for possible lawyer review before sign-up opens beyond invite-only.

**Terms of Service**
1. Educational use only: not financial, investment, tax or legal advice, and no adviser-client relationship.
2. AI-generated content: may be wrong, incomplete or out of date. Verify it independently.
3. Data accuracy: market data can be delayed or wrong, and it's provided without warranty.
4. Accounts: invite-only access, keeping credentials secure, and acceptable use.
5. No warranty and limitation of liability, including investment losses.
6. Data sources and attribution. The readiness review confirms the final list and any required notices. Expect this to include the FRED API notice.
7. Ending your account, and changes to these terms.
8. Governing law (founder to supply) and contact details.

**Privacy Policy**

Everything here must describe what the app really does. The table below is what I found in the code. **Engineer: confirm each row during implementation and correct the text if the code differs.**

| Topic | What the code shows | To confirm |
|---|---|---|
| Account data | Username, email, password hash, created and last-login times (`models/user.py`) | — |
| Portfolio data | Allocation percentages, no dollar amounts, per the portfolio page. Also stored AI summaries (`PortfolioSummary`). | Which holdings fields are stored |
| Alerts and email | Alert preferences, timezone, alert history (`models/alert.py`) | — |
| AI usage | Token counts, model, cost, timestamp, no message content (`models/ai_usage.py`) | — |
| Chat | History kept in the browser's `sessionStorage`. No chat table exists. | Whether server logs capture message text |
| AI providers | Portfolio data and chat messages go to AI providers for processing. Both Anthropic and OpenAI appear in the code. | Which providers receive user data |
| Email provider | SMTP via `MAIL_SERVER` | Name of the production provider |
| Hosting | VPS per #513 | Final provider and region |
| Cookies | Session and CSRF. No analytics tags in `base.html`. | Any others set in production |
| Page resources | Google Fonts and jsDelivr load in the browser and receive visitor IPs | — |
| IP addresses | Rate limiting and server logs | Retention period |
| Payments | Stripe code exists, but paid plans are out of scope | Omit unless enabled |

Required sections: what we collect, how we use it, who receives it, cookies, retention, how to delete your account, your choices (email preferences, unsubscribe), children (not directed at under-18s), changes, and contact.

**Contact address:** the site has no contact address today, and the register page's "Contact us for access" leads nowhere. Add a `CONTACT_EMAIL` config value, documented in `.env.example`, and use it on both pages and the register page. The founder sets the value. The go-live checklist must fail if it's empty.

---

## 4. Account Deletion

**Pending the PM decision in Open Questions.** The Privacy Policy must explain how to delete an account, but no way to do that exists today. This section is the recommended approach.

**File:** `templates/settings.html`, a new card below Account Information.

```
┌─────────────────────────────┐
│ Delete account              │
│ Permanently removes your    │
│ account, portfolio, alerts  │
│ and saved analysis. This    │
│ can't be undone.            │
│ [ Delete account… ]         │ ← Outline danger button
└─────────────────────────────┘

Dialog:
┌─────────────────────────────┐
│ Delete your account?        │
│ This permanently removes    │
│ your account, portfolio,    │
│ alerts and saved analysis.  │
│ Password [______________]   │
│ [Cancel]  [Delete account]  │ ← Solid danger, 56px on mobile
└─────────────────────────────┘
```

- Use a Bootstrap modal with `role="dialog"`, focus trapped, Escape closes it, and focus returns to the trigger. Focus starts on the password field.
- Require the password so an unattended session can't delete an account. A wrong password shows an inline error with `role="alert"` and the dialog stays open.
- Buttons: 44px minimum, 56px for the destructive button on mobile, per design-system touch target sizing. Global `.btn` renders at 38px, so set the height explicitly.
- The confirm button is a form POST with CSRF protection.
- **On success:** log the user out and redirect to the homepage with the flash message "Your account and data have been deleted."
- **Sole admin:** show the card without the button, with the text "Admin accounts can't be deleted here while they're the only admin." The server must enforce this too.
- **Data removed:** the user record and all rows tied to it. Engineer: confirm every foreign key is covered, including `AIUsageRecord`, and record in the issue what happens to any retained cost totals.

---

## 5. Register Page Consent

**File:** `templates/auth/register.html`

Directly above the Create Account button:

> By creating an account, you agree to the [Terms of Service] and acknowledge the [Privacy Policy]. MacroClarity is for educational purposes and doesn't provide financial advice.

- Style: `text-sm`, `--neutral-600`, links underlined in `--brand-blue-500`.
- Links open in a new tab so the form isn't lost, with `rel="noopener"` and visually hidden "(opens in new tab)" text.
- There's no checkbox. Flag on the readiness checklist that a lawyer may prefer one before sign-up opens beyond invite-only.
- Links in this sentence need vertical padding so that each target reaches 44px on mobile. Use line-height and padding, without changing the sentence's appearance.

---

## 6. UI Copy Sweep

**Rule:** UI copy may describe conditions and how assets have behaved. It must not tell the reader to act, or imply that it knows their situation.

| Location | Current | New |
|---|---|---|
| `index.html:666` | WHAT THIS MEANS FOR YOUR PORTFOLIO | HOW CONDITIONS RELATE TO ASSET CLASSES |
| `index.html:663` `aria-label` | Portfolio Implications | Asset Class Implications |
| `index.html:516` | …within normal ranges. Standard diversification applies. | …within normal ranges. |
| `index.html:517` | …correlations shifting. Defensive positioning may be warranted. | …correlations shifting. |
| `credit.html:746` | Risk-off shift; avoid lower-quality credit | Risk-off shift; lower-quality credit typically weakens first |
| `portfolio.html:32` | …private to your account and shared with AI for analysis. | …private to your account. It's sent to our AI provider to generate your analysis. See the Privacy Policy. (link) |
| `portfolio.html:36` | …to evaluate your allocation. | …to describe how your allocation relates to current conditions. |
| `portfolio.html:40` | The analysis provides considerations and observations, not recommendations. Always consult a financial advisor for personalized advice. | The analysis explains risks and how your allocation relates to current conditions. It won't tell you what to buy, sell or change. For personal advice, consult a qualified financial advisor. |
| `settings_alerts.html:59` | Include personalized portfolio analysis | Include AI analysis of your portfolio allocation |
| `auth/register.html:3` | Register - Financial Markets Dashboard | Register - MacroClarity |
| `auth/register.html:16` | …portfolio tracking, and personalized insights. | …portfolio tracking, and educational context. |
| `auth/register.html:48` | Registration is invite-only. Contact us for access. | Registration is invite-only. Email `CONTACT_EMAIL` for access. (`mailto:` link) |

The Favorable, Supportive, Neutral and Headwind labels stay, because they describe conditions for an asset class and not an action. "Defensive assets tend to outperform" (`index.html:518`) also stays, because it describes behaviour.

**This table isn't complete.** I swept templates and component JS only. Engineer: apply the same rule to user-visible strings generated in Python. Start with `conditions_config.py`, `credit_signals.py` and `market_signals.py`, plus the alert email services. Also check the chatbot opening messages in `ai-section-btn.js`.

**Stored AI output:** changing prompts doesn't change what's already saved. `PortfolioSummary` rows and the current briefings written under the old prompts could keep showing "Consider reducing X from 40% to 25%" after launch. Clear or regenerate them when the prompt change ships, and check this on the go-live smoke test.

---

## 7. Sector Management Tone Removal

**Decision:** remove it with no placeholder, notice or replacement. The panel shows fallback output, so nobody loses real information.

| Item | Change |
|---|---|
| `equity.html:308–390` | Delete the section. Global Trade Pulse moves up and follows the page's main content directly. Spacing comes from its existing `mb-4`, so no new spacer is needed. |
| `equity.html:1319–1352` | Delete `initSectorToneToggle` and its call |
| `base.html:28` | Remove the `sector-tone.css` link and delete the file |
| `ai-section-btn.js:17` | Remove the `sector-tone-section` entry |
| `dashboard.py` (allowed sections, names and the cache branch, ~3882–3923) | Remove along with the backend work in #513 |
| `narrative-bridge.css:7` | Update the stale comment. Keep the class if anything else uses it. |
| `templates/equity.html.tmp` | Not part of the panel, but a stale partial copy of the page (264 lines, old SignalTrackers title) that's tracked in git, and nothing references it. Delete it as cleanup before the code goes public. |

Older specs that mention the panel stay as they are, since they're historical records. Add a "Superseded by #513" line to the top of `feature-5.1-sector-management-tone.md`.

---

## Responsive Behavior

| Breakpoint | Layout change |
|---|---|
| 375px | Footer stacked and left-aligned, with FAB clearance. Legal pages single column. Chatbot disclaimer may wrap to two lines. |
| 768px | Footer brand line and links share a row, with a divider above the disclaimer. FAB clearance rules no longer apply. |
| 1280px | Footer uses `--container-xl`. Legal pages stay at 640px. |

## Accessibility Requirements

- **Contrast:** every pairing above meets 4.5:1. Ratios are listed with each component.
- **Touch targets:** 44px minimum for footer links, the table of contents, register links and buttons, and 56px for the destructive button on mobile.
- **Keyboard:** footer links, table of contents and the delete dialog are all reachable by Tab, with visible focus. Focus is visible on navy.
- **Screen readers:** disclaimers are plain `<p>` elements that follow the content they describe. None are live regions, so they aren't re-announced on load. The icon in the portfolio variant is `aria-hidden`.
- **Headings:** legal pages use one H1 followed by H2s, with no skipped levels.
- **Colour:** no meaning depends on colour alone.

## Screenshots Required for Design Review

Store under `screenshots/us<story>/` on the feature branch.

- **Footer:** 375, 768 and 1280. At 375, also signed in at maximum scroll on the homepage to show FAB clearance.
- **Disclaimers:** homepage review at 375 and 1280, one section review expanded, news summary, portfolio analysis at 375 and 1280, implications table, and fallback or stale states showing it stays visible.
- **Disclaimer absent:** homepage error state and portfolio empty state.
- **Chatbot:** 375 (shortest panel, including a wrapped line if it wraps) and 1280.
- **Legal pages:** both at 375 and 1280, plus logged out.
- **Delete account:** card, dialog, wrong-password error and post-delete flash, at 375 and 1280.
- **Register:** 375 and 1280, invite-only mode.
- **Equity page:** the area where the panel used to be, at 375 and 1280.
- **Email:** weekly review footer, HTML version.

## Design System References

- **Colors:** Primary Palette (`neutral-*`, `brand-blue-500`) and `--brand-navy-dark`.
- **Typography:** Type Scale (`text-xs`, `text-sm`, `text-base`), with Noto Serif for the H1 only.
- **Spacing and layout:** Spacing System, Container Widths (`--container-sm`, `--container-xl`).
- **Components:** Buttons, Forms, Touch Target Sizing.
- **Update on implementation:** add a "Disclaimers" entry to the Component Library in `docs/design-system.md`, on the feature branch.

## Open Questions

**PM:**

1. **Account deletion: self-serve or by email?** The Privacy Policy has to explain this, and no mechanism exists. I recommend self-serve (section 4): users are storing their holdings here, and it avoids a manual process for the founder. If you'd prefer a smaller launch, the fallback is "email `CONTACT_EMAIL` and we'll delete it within 30 days". In that case, drop section 4 and have the founder commit to that timeframe.
2. **Contact address:** the founder needs to choose one (for example, a mailbox on the domain, which also needs setting up at the registrar). Both legal pages depend on it.
3. **Governing law for the Terms:** the founder needs to supply this.
4. **Error pages:** no custom 404 or 500 pages exist (`dashboard.py` has no `errorhandler`), so public visitors would see unbranded defaults. This isn't in #513's scope. Should it go in the readiness review as a story, or wait? A simple branded page would be a small story, and I'd do it before launch.
