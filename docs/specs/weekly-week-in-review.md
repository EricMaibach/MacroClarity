# Weekly "Week in Review" Briefing Design Spec

**Issue:** #490
**Created:** 2026-10-29
**Status:** Draft

## Overview

Every scheduled AI briefing moves from daily to one coordinated weekly run after Friday's close, and the Saturday-morning email follows from that run. This spec covers what the user sees:

1. A **week-in-review layout** for the homepage briefing, built from a headline plus three fixed parts: *What changed*, *Why it matters* and *What to watch next week*.
2. A **freshness label** that makes a briefing from several days ago read as current and on schedule, not broken.
3. The same structure, shorter, on the five **section pages** and the **news page**.
4. A **settings** card simplified to one weekly email.
5. The weekly **email** template.
6. A **copy sweep** that removes every remaining "daily briefing" reference.

### Design principle: two clocks, clearly labeled

After this change the homepage has two update cadences side by side. The **AI review is weekly**, while the **conditions, charts and movers stay daily**. Users should never have to guess which clock a block follows, so:

- Every AI block carries a **week label** ("Week of Oct 26–30, 2026").
- Data blocks keep their current daily wording ("Today's Movers", "Data updated daily"). These are accurate and must **not** be changed to "weekly".
- AI copy never says "today". Data copy never says "this week" unless it means a 5-day change.

---

## User Flow

1. User opens the homepage on any day of the week.
2. The briefing card shows "WEEK IN REVIEW" with the week it covers ("Week of Oct 26–30, 2026") and when the next review is due ("Next review: Fri, Nov 6").
3. User reads the one-line headline, scans *What changed*, reads *Why it matters*, then checks *What to watch next week*.
4. Below the briefing, conditions and movers show today's data as they do now.
5. On a section page (e.g. Rates), the user expands "Rates Week in Review (AI)" and gets the same three parts scoped to rates.
6. On Saturday morning, opted-in users receive "Your Week in Review" by email with the same content plus the week's biggest moves.

---

## Content Contract (what the AI run must produce)

The layout depends on predictable structure, so the general briefing must return these parts **separately**: as structured fields (recommended), or as markdown with the exact headings below. The Engineer chooses the mechanism. The rendered result must match this table.

| Part | Rendered heading | Length | Notes |
|---|---|---|---|
| Headline | *(none — styled lead sentence)* | 1 sentence, ≤ 30 words | The week in one line. Reused as the email's "This week in one line" and as the email preheader. |
| What changed | **What changed** | 3–5 bullets, each ≤ 2 lines | Facts: data releases, notable weekly moves. Lead each bullet with the subject ("10-year yield", "CPI"). Numbers allowed. |
| Why it matters | **Why it matters** | 1–2 short paragraphs (≤ 120 words total) | Interpretation for a financially literate non-professional. Calm tone: no "plunge" or "soar" unless it is literally historic. |
| What to watch next week | **What to watch next week** | 2–5 items | Each item has a **day** (Mon–Fri, or "Ongoing"), an **event** and a **one-clause reason**. Built from next week's calendar. |

The **section briefings** use the same three parts, scoped to the section, with shorter limits: no headline, 2–4 bullets, ≤ 80 words for *Why it matters*, and 1–3 watch items.

**Fallback:** if structured parsing fails, render the raw markdown into the same card exactly as the current code does. The briefing must never fail to display because of a format problem.

---

## Wireframes

### Homepage briefing — Mobile (375px)

```
┌─────────────────────────────────────┐
│ 📰 WEEK IN REVIEW            [✦ AI] │  ← section header (AI btn: logged-in only)
│ Week of Oct 26–30, 2026             │  ← week label, own line on mobile
├─────────────────────────────────────┤
│ ┌─────────────────────────────────┐ │
│ │ Inflation cooled more than      │ │  ← headline (lead style)
│ │ expected while long yields      │ │
│ │ fell, easing pressure on        │ │
│ │ rate-sensitive assets.          │ │
│ │                                 │ │
│ │ WHAT CHANGED                    │ │  ← part label
│ │ • CPI rose 0.1% in Sept, below  │ │
│ │   the 0.3% forecast.            │ │
│ │ • 10-year yield fell 18 bp to   │ │
│ │   4.02% over the week.          │ │
│ │ • High-yield spreads were flat. │ │
│ │                                 │ │
│ │ WHY IT MATTERS                  │ │
│ │ Two soft inflation prints in a  │ │
│ │ row make a December cut more    │ │
│ │ likely...                       │ │
│ │ ─────────────────────────────── │ │
│ │ WHAT TO WATCH NEXT WEEK         │ │
│ │ ┌─────┐ FOMC decision           │ │
│ │ │ WED │ First meeting since the │ │  ← watch item
│ │ └─────┘ soft CPI print          │ │
│ │ ┌─────┐ October payrolls        │ │
│ │ │ FRI │ Tests the cooling-labor │ │
│ │ └─────┘ story                   │ │
│ └─────────────────────────────────┘ │
│ ✦ AI-generated from this week's     │  ← footer, stacks on mobile
│   market data                       │
│ Published Fri, Oct 30 · Next review │
│ Fri, Nov 6                          │
│ ✦ Tap any sentence to explore w/ AI │  ← existing hint (unchanged)
└─────────────────────────────────────┘
```

### Homepage briefing — Tablet / Desktop (768px+)

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📰 WEEK IN REVIEW        Week of Oct 26–30, 2026            [✦ AI]  │
├──────────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │ Inflation cooled more than expected while long yields fell,      │ │
│ │ easing pressure on rate-sensitive assets.                        │ │
│ │                                                                  │ │
│ │ ┌───────────────────────────────┐ ┌────────────────────────────┐ │ │
│ │ │ WHAT CHANGED                  │ │ WHAT TO WATCH NEXT WEEK    │ │ │
│ │ │ • CPI rose 0.1% in Sept...    │ │ WED  FOMC decision         │ │ │
│ │ │ • 10-year yield fell 18 bp... │ │      First meeting since…  │ │ │
│ │ │ • High-yield spreads flat.    │ │ FRI  October payrolls      │ │ │
│ │ │                               │ │      Tests the cooling…    │ │ │
│ │ │ WHY IT MATTERS                │ │                            │ │ │
│ │ │ Two soft inflation prints...  │ │                            │ │ │
│ │ └───────────────────────────────┘ └────────────────────────────┘ │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│ ✦ AI-generated from this week's market data                         │
│                          Published Fri, Oct 30 · Next review Fri, Nov 6 │
└──────────────────────────────────────────────────────────────────────┘
```

- **768–1023px:** single column, same order as mobile. The week label moves into the header row (right-aligned, before the AI button), as the current date does today.
- **≥ 1024px:** two columns inside the card. The left column (~62%) holds *What changed* and *Why it matters*. The right column (~38%) holds *What to watch next week* as a side rail. The headline spans both columns.

### Section page briefing (all breakpoints)

```
┌───────────────────────────────────────────────┐
│ 💬 Rates Week in Review (AI)    Oct 26–30  ⌄ │  ← collapsed by default (unchanged)
└───────────────────────────────────────────────┘
                     expanded ▼
┌───────────────────────────────────────────────┐
│ Week of Oct 26–30, 2026                       │  ← week label, neutral-500, text-sm
│                                               │
│ WHAT CHANGED                                  │
│ • 10-year yield fell 18 bp to 4.02%.          │
│ • 2s10s steepened to +31 bp.                  │
│                                               │
│ WHY IT MATTERS                                │
│ Falling long yields after a soft CPI...       │
│                                               │
│ WHAT TO WATCH NEXT WEEK                       │
│ WED  FOMC decision — sets the path for...     │
│                                               │
│ Published Fri, Oct 30 · Next review Fri, Nov 6│  ← replaces current timestamp line
└───────────────────────────────────────────────┘
```

- The collapsed header shows a compact week range ("Oct 26–30") before the chevron on **≥ 768px only**, so users can judge freshness without expanding. It is hidden on mobile to keep the title on one line.
- Watch items on section pages use the single-line inline form ("WED FOMC decision — reason") at every breakpoint. No chip column, because the space is narrower and there are fewer items.

---

## Component Specifications

### 1. Week label (`.briefing-week-label`)

Used everywhere an AI briefing appears: homepage, section pages, news page and email.

| Situation | Text | Style |
|---|---|---|
| Scheduled weekly run | `Week of Oct 26–30, 2026` | neutral-500, text-sm (0.875rem), weight 500 |
| Week spans two months | `Week of Oct 29 – Nov 2, 2026` | same |
| Week spans two years | `Week of Dec 29, 2025 – Jan 2, 2026` | same |
| Manual regenerate mid-week | `Week to date · through Wed, Nov 4` | same |
| Stale (see §2) | label unchanged, plus a stale notice | see §2 |

Rules:
- Use an en dash (–) for ranges. Put spaces around it only when it joins two full dates ("Oct 29 – Nov 2").
- The range is the **trading week** (Mon–Fri), even when a holiday shortens it.
- **Compute the week range on the server, in America/New_York**, and return it from the API (e.g. `week_start`, `week_end`, `published_at`, `next_review_at`). Do **not** derive it from the browser clock: a Friday 6pm ET run is already Saturday for a reader in Asia, and the label must not shift.
- Wrap it in a `<time datetime="2026-10-26/2026-10-30">` element for screen readers and machines.

### 2. Freshness footer and stale notice

This replaces the current `Generated: <time>` line and the ">24h → Briefing may be outdated" warning, which would fire every Saturday through Thursday under a weekly cadence.

**Normal footer:** `Published Fri, Oct 30 · Next review Fri, Nov 6`, in neutral-500 at text-xs. Days and dates only, with no times, because the run time after the close is an internal detail.

**After a manual regenerate:** `Updated Wed, Nov 4 · Next review Fri, Nov 6`.

**Stale threshold:** the briefing counts as stale only when `published_at` is **more than 8 days old**, meaning a whole Friday run was missed. When stale:
- Add an inline notice above the footer: `⚠ This review is from an earlier week. A new one is on the way.` Text in #92400E (the amber text already used by the transition-watch badge, US-15.3.1) on warning-100 (#FEF3C7), 6px radius, 8px × 12px padding, with `role="status"`.
- Leave the week label unchanged, so it still states which week the content covers.
- Do not show a Retry button. The fix is a backend run, not a user action.

The 8-day threshold covers a normal week (published Friday, read the next Thursday, about 6–7 days old) without false alarms.

### 3. Headline (`.briefing-headline`)

- Font: `var(--font-interface)` (Inter), 1.125rem on mobile and 1.25rem at ≥ 768px, weight 600, line-height 1.4, color `var(--on-surface)` (#0b1c30).
- Margin-bottom: `var(--space-4)`.
- This is **not** an H1. The page already has its one serif hero, so keep this sans and render it as a `<p>`.
- If no headline field exists (fallback mode), omit it.

### 4. Part labels (`.briefing-part__label`)

- Text: "WHAT CHANGED", "WHY IT MATTERS", "WHAT TO WATCH NEXT WEEK".
- Style: matches existing small-caps section labels. Use text-xs (0.75rem), weight 600, letter-spacing 0.05em, uppercase via CSS (source text is sentence case for screen readers), color neutral-500.
- Markup: `<h4>` on the homepage, which sits under the section's `<h3>`. Use `<h3>` inside section-page collapsibles, which sit under an `<h2>` title.
- Spacing: `var(--space-2)` below the label and `var(--space-5)` above each part except the first.

### 5. "What changed" list

- `<ul>` with the existing body text style (1rem / 1.6 / #334155) and 8px between items.
- Bullet marker in neutral-400.
- Numbers stay in the body font. Do not apply color to up or down moves here, because the text says the direction and red/green would make it read as a trading feed.

### 6. "Why it matters" prose

Same as the current `.conditions-briefing__narrative p`. No change.

### 7. Watch list (`.briefing-watch`)

Homepage, ≥ 1024px side rail and mobile stacked:

```
┌─────┐  FOMC decision                 ← event: 0.9375rem, weight 600, neutral-800
│ WED │  First meeting since the       ← reason: text-sm, neutral-600
└─────┘  soft CPI print
```

- **Day chip:** 44px wide, auto height (min 28px), brand-blue-100 (#DBEAFE) background, brand-blue-500 (#1E40AF) text, text-xs, weight 700, uppercase, 6px radius, centered. Contrast: #1E40AF on #DBEAFE ≈ 7.2:1 ✅. "Ongoing" items use the chip text `—` with `aria-label="Ongoing"`.
- 12px gap between chip and text, and 12px between items.
- **Side rail (≥ 1024px):** left border 1px neutral-200, padding-left `var(--space-5)`.
- **Mobile:** a 1px neutral-200 divider separates the watch list from *Why it matters* above it.
- Markup: `<ol>` (items are in chronological order), with each `<li>` holding a `<span class="briefing-watch__day">` for the chip.
- **Section-page inline form:** `<strong>WED</strong> FOMC decision — First meeting since…` in text-sm, with no chip.
- Items are **not** interactive. They are not links and not buttons. The existing select-to-ask-AI and tap-to-explore behavior applies to their text like any other briefing text.

### 8. Loading, error and empty states (all AI blocks)

| Where | Current copy | New copy |
|---|---|---|
| Homepage loading | "Loading today's briefing..." | "Loading this week's review..." |
| Homepage error | "Market briefing is being generated. Check back shortly." | "This week's review isn't ready yet. Check back shortly." |
| Section loading | "Loading daily briefing..." | "Loading this week's review..." |
| Section empty | "No briefing available yet. Click "Reload Data" on the main dashboard to generate one." | "No review yet. The next one publishes after Friday's close." |
| Credit empty | "No analysis available yet. Click "Reload Data"…" | same as Section empty |

The skeleton loader stays as it is. Add one more `skeleton-line-75` row so the placeholder height is closer to the taller structured card.

---

## Page-by-page changes

### Homepage (`index.html`)

| Element | Current | New |
|---|---|---|
| Section `aria-label` | "Daily Briefing" | "Week in Review" |
| Section title | DAILY BRIEFING | WEEK IN REVIEW |
| Title icon | `bi-newspaper` | unchanged |
| Date slot `#briefing-date` | "Oct 29, 2026" (browser-generated date) | Week label (§1), from the server |
| AI button `aria-label` | "Ask AI about Daily Briefing" | "Ask AI about this week's review" |
| Narrative | free-form markdown | headline + three parts (§3–7) |
| Attribution | "AI-generated based on current market data" | "AI-generated from this week's market data" |
| Timestamp | "Generated: 5:42 PM EDT" | freshness footer (§2) |
| Template comment | "§0: AI BRIEFING — the daily draw" | "§0: WEEK IN REVIEW — the weekly draw" |
| Today's Movers footer | — | **unchanged** (daily data) |

The briefing stays in position §0, above Market Conditions. It is still the hook. It now changes once a week, so it no longer needs to be "new every day".

### Section pages (`equity`, `crypto`, `rates`, `dollar`, `credit`)

| Page | Current title | New title |
|---|---|---|
| Equity | Daily Equity Briefing (AI) | Equity Week in Review (AI) |
| Crypto | Daily Bitcoin Briefing (AI) | Bitcoin Week in Review (AI) |
| Rates | Daily Rates Briefing (AI) | Rates Week in Review (AI) |
| Dollar | Daily Currency Briefing (AI) | Currency Week in Review (AI) |
| Credit | AI Credit Analysis | Credit Week in Review (AI) |

Also update the HTML comments ("AI Daily Briefing" → "AI Week in Review"). Keep the collapsible pattern, its default-collapsed state and the `data-section-id="ai-briefing"` hook unchanged.

### News page (`news.html`)

| Element | Current | New |
|---|---|---|
| H1 | Today's Macro News | This Week's Macro News |
| Date under H1 | `October 29, 2026` | Week label (§1) |
| Summary card label | ✦ AI Summary | ✦ Week in Review |
| Summary card footer | "Generated from N sources" | "Generated from N sources this week" |
| Stale banner | "Showing news from <date> — today's fetch is unavailable." | "Showing the review for <week label> — this week's review is on the way." Shown only under the 8-day rule (§2). |
| Empty (no prior data) | "We couldn't fetch today's news… will retry automatically tomorrow." | "We couldn't load this week's news and have no earlier review to show.<br>The pipeline retries automatically." |
| Empty (never run) | "The daily macro news pipeline hasn't run yet.<br>Check back tomorrow morning." | "The first weekly news review hasn't been published yet.<br>It arrives after Friday's close." |

**Sources list:** a week's sources can run to 30+ items, so group them by day under small day headings. Newest day comes first, and items within a day keep their current order.

```
SOURCES (34)
FRIDAY, OCT 30                          ← text-xs, 600, uppercase, neutral-500, sticky: no
  [source card]
  [source card]
THURSDAY, OCT 29
  [source card]
```

- Day headings are `<h3>` elements under the "Sources" `<h2>`.
- Cards themselves are unchanged.
- If source timestamps are missing, put those cards in a final group headed "Earlier this week".

The summary text is the news pipeline's weekly output. Render it as paragraphs: split on blank lines rather than putting everything in a single `<p>` as today. The news summary does **not** use the three-part structure, because it summarizes the news, and the macro reading lives on the homepage.

### Settings (`settings_alerts.html`)

Replace the "Daily Briefing Email" card:

```
┌─────────────────────────────────────────────┐
│ ✉ Weekly Review Email                       │  ← card header (bg-primary, unchanged style)
├─────────────────────────────────────────────┤
│ [●━] Email me the weekly review             │  ← form-switch
│      Arrives Saturday morning: the week's   │
│      biggest changes, why they matter and   │
│      what to watch next week.               │
│                                             │
│ Timezone                                    │  ← shown when enabled
│ [ America/New_York              ▾ ]         │
│ Delivered around 8:00 AM Saturday, your time│  ← helper text
│                                             │
│ ☐ Include personalized portfolio analysis   │  ← unchanged
│   Adds AI-generated context for your …      │
│                                             │
│ Next email: Sat, Oct 31                     │  ← text-sm, neutral-600; only when enabled
└─────────────────────────────────────────────┘
```

- **Remove** the Frequency select. There is only one cadence now.
- **Remove** the Preferred Time input and use a fixed Saturday-morning delivery. One weekly send at a fixed local time is easier to understand and easier to batch. *PM: please confirm. If a time picker is wanted, keep the existing `<input type="time">` with the label "Delivery time (Saturday)" and the helper "Saturday, your time". The layout otherwise stays the same.*
- **Keep** Timezone, so "Saturday morning" means Saturday morning for the reader.
- "Next email" line: on the server, compute the next Saturday after the latest Friday run, in the user's timezone. Hide the line when the toggle is off. Updating it live in JS when the toggle changes is optional.
- Toggle label text: "Email me the weekly review". The form field name can stay `daily_briefing_enabled` for the migration (Engineer's call), but every visible label and `id`-linked `<label>` text must say weekly.
- **Migration impact on the UI:** users who were `daily` or `weekly` see the toggle **on**, and users who were `off` see it **off**. Don't show any notice about the change, since there are no public users yet.

### Email (`email/daily_briefing.html` + `.txt`)

Rename the templates to `weekly_review.html` / `.txt` (Engineer's call). The structure, top to bottom:

| # | Block | Content | Notes |
|---|---|---|---|
| — | Subject | `Your Week in Review: Oct 26–30` | |
| — | Preheader | The headline sentence | hidden preheader text |
| 1 | Hero | "Your Week in Review" + week label | Background **#09264c** (brand navy) instead of the old purple gradient, white text. Drop the 📊 emoji. |
| 2 | This week in one line | Headline | Replaces "Today's Snapshot". Keep the existing grey band and left border, with the border in #1E40AF. |
| 3 | What changed / Why it matters | General briefing parts | H2 "The Week in Review" with the two parts as small labeled sub-blocks |
| 4 | Market Conditions at a Glance | unchanged | Conditions as of Friday's close |
| 5 | Biggest Moves This Week | Top movers by **5-day** change | Replaces "What's Moving Today". Change column header: "Week". |
| 6 | What to Watch Next Week | Watch list | Table rows: bold day (60px column) + event + reason. No chip background, because email clients render it inconsistently. |
| 7 | Your Portfolio Context | unchanged | conditional |
| 8 | Alerts Triggered This Week | Alerts from the past 7 days | Replaces "(Last 24h)" |
| 9 | CTA | "Read the Full Review →" | Button background #1E40AF (brand-blue-500) instead of #667eea |

- Width, base template and footer/unsubscribe stay unchanged.
- The plain-text version mirrors the same order and headings in uppercase: "YOUR WEEK IN REVIEW", "WHAT CHANGED", and so on.
- `test_email.html` / `.txt`: change "Daily market briefings" to "A weekly market review every Saturday morning".

### Global copy sweep

| File | Current | New |
|---|---|---|
| `base.html` meta description + og:description | "Daily macro intelligence briefings for individual investors…" | "Weekly macro intelligence briefings for individual investors…" (rest unchanged) |
| `base.html` footer | "Data updated daily" | **unchanged** (accurate) |
| `portfolio.html` | "…and today's briefings to evaluate…" | "…and this week's review to evaluate…" |

**Acceptance check for QA:** a case-insensitive search of `signaltrackers/templates/` for `daily briefing`, `today's briefing` and `daily .* briefing` returns no user-visible matches. The only remaining "daily" text should describe data refresh, such as "Data updated daily".

---

## Interaction Patterns

- **No new interactions.** Select-to-ask-AI (desktop) and tap-to-explore (mobile) work on all briefing text, including headline, bullets and watch items, through the existing handlers on `#briefing-narrative`. Render the new structure **inside** `#briefing-narrative` so those handlers still apply.
- Section collapsibles keep their current expand and collapse behavior and remembered state.
- No animation added.

---

## Responsive Behavior

| Breakpoint | Homepage briefing | Section briefing | Settings |
|---|---|---|---|
| < 768px | Week label on its own line under the title. Single column. Watch list below *Why it matters* with a divider. Footer lines stack. | Header shows the title only. Inline watch items. | Single column (unchanged) |
| 768–1023px | Week label in the header row. Single column. Footer on one row (attribution left, freshness right). | Header shows the title plus the compact week range. | unchanged |
| ≥ 1024px | Two-column card: changes and impact on the left (~62%), watch rail on the right (~38%). Headline spans both. | same as tablet | unchanged |

Long week labels ("Week of Dec 29, 2025 – Jan 2, 2026") must wrap cleanly on the 768px header row. Let the label shrink and wrap (`min-width: 0`) instead of pushing the AI button off the row.

---

## Accessibility Requirements

- **Contrast:** body #334155 on white ≈ 10.4:1. Labels in neutral-500 #697386 on white ≈ 4.8:1. Day chip #1E40AF on #DBEAFE ≈ 7.2:1. Stale notice #92400E on #FEF3C7 ≈ 6.4:1 (warning-700 #A16207 measures only 4.4:1 on this background, so it is not used). All ≥ 4.5:1 ✅.
- **Headings:** the outline must not skip levels. Homepage: section `<h3>`, then part `<h4>`. Section pages: collapsible `<h2>`, then part `<h3>`. News sources: `<h2>` Sources, then `<h3>` day.
- **Week label:** `<time datetime="YYYY-MM-DD/YYYY-MM-DD">`, read as "Week of October 26 to 30, 2026". Write the visible text so it reads correctly aloud, and avoid ambiguous abbreviations like "10/26".
- **Watch list:** `<ol>`, with the day chip text read as part of the item ("Wednesday: FOMC decision. First meeting since…"). Give the chip a visually hidden full day name, e.g. `<abbr title="Wednesday">WED</abbr>` or an sr-only span.
- **Stale notice:** `role="status"` (polite). It is not an alert.
- **Touch targets:** no new controls on the briefing. On settings, the form-switch and checkbox keep their ≥ 44px hit area through the label (unchanged).
- **Settings:** the "Next email" line is plain text, not live-announced.

---

## Design System References

- **Colors:** Primary Palette (brand-blue-100/500, neutral-400/500/600/800), Semantic warning-100 plus amber text #92400E, and the rebrand navy #09264c (`docs/specs/rebrand-macroclarity.md`) for the email hero.
- **Typography:** Type Scale (text-xs, text-sm, base), Inter for all new elements (the "one serif hero" rule stands).
- **Components:** Cards, Collapsible Sections (Progressive Disclosure), Badges (the day chip follows the subtle badge pattern), Loading States, Empty States.
- **AI Feature Tokens:** unchanged attribution sparkle icon and AI button.

## Out of scope

- Event-driven alerts (FOMC, CPI surprise, spread blowout, regime flip). That is a separate future feature. When it arrives, it should appear as a separate dated "Alert" block above the weekly review, not as an edit to the review.
- Showing the current week's not-yet-summarized news articles on the news page between runs. This could be a later refinement ("So far this week: N articles").
- Chatbot changes.

## Open questions for PM

1. **Delivery time picker.** This spec removes it and uses a fixed time around 8:00 AM Saturday in the user's timezone. Confirm, or keep the picker as described in the Settings section.
2. **Email visual refresh.** This spec moves the email hero and CTA from the pre-rebrand purple to brand navy and blue, because the template is being restructured anyway. Confirm this fits within this feature.
