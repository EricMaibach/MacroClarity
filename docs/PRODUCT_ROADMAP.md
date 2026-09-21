# SignalTrackers Product Roadmap

**Last updated:** 2026-03-30 (Phase 13 rescoped — monetization paused, access control & launch prep)

---

## Product Vision

SignalTrackers is a macro financial dashboard for individual investors — the only tool that combines macro regime detection, technical signals, and plain-language interpretation in one place.

**The problem we solve:** Retail investors are forced to maintain 4–6 fragmented subscriptions (TradingView for charts, Koyfin for fundamentals, MacroMicro for data, SentimenTrader for sentiment) because no single platform covers the full stack. SignalTrackers closes this gap, starting with the macro layer.

**What makes us different:**
- Macro-first: regime context drives signal interpretation, not the other way around
- Plain-language synthesis: data means nothing without explanation (only 62% of investors understand how rates affect bonds)
- Retail-priced: institutional-grade methodology using free/public data (FRED, OCC, FRED-MD)

---

## North Star Metric

Investors who use SignalTrackers as their primary macro intelligence tool — replacing at least one paid subscription.

---

## Active Phase
**Phase:** Phase 16 — Public Deployment
**State:** BUILDING

**Previous phase:** Phase 15 — Inflation Composite Redesign ✅ COMPLETE (see below)

### Phase Sequence

| Phase | Title | Focus |
|-------|-------|-------|
| **16** | **Public Deployment** | DNS → macroclarity.com, production config, SSL, monitoring |
| **17+** | **Traction & Learning** | SEO, content, observe usage, decide on monetization |

---

## Current Release State

### Phases 1–6: Complete

| Phase | Title | Shipped |
|-------|-------|---------|
| Phase 1 | Repositioning & Core Gaps | 16 stories |
| Phase 2 | Consolidation & Templates | 13 stories |
| Phase 3 | Mobile-First Redesign | 14 stories (Feb 23, 2026) |
| Phase 4 | Mobile Polish + Core UX Wins | 13 stories (Feb 28, 2026) |
| Phase 5 | Macro Intelligence Layer | 9 stories (Feb 26, 2026) |
| Phase 6 | Advanced Intelligence & Sector Analysis | 6 stories (Feb 28, 2026) |
| Phase 7 | Credit Intelligence & Completion | Mar 10, 2026 |

**Phase 5 highlights:** Macro Regime Score Panel (Feature 5.1, #138) and Multi-Model Recession Probability Sub-Panel (Feature 5.2, #146) — three recession models (NY Fed 12-month, Chauvet-Piger coincident, Richmond SOS weekly), all data free via FRED + Richmond Fed.

**Phase 6 highlights:** Regime Implications Panel (#145) — 5-level signal scale across 6 asset classes, static config from published research. Sector Management Tone Panel (#123) — SEC EDGAR 8-K ingestion, FinBERT NLP scoring, GICS sector aggregation (11 sectors), quarterly scheduling, /credit stub + nav parity.

**Total shipped:** 71+ user stories across 7 phases.

---

## Phase 7: Credit Intelligence & Completion ✅ COMPLETE
**Milestone goal:** Build out the credit detail page, integrate credit into the full AI briefing pipeline, and complete remaining market parity and UX gaps.

| Feature | Priority | Status |
|---------|----------|--------|
| #178 — Wire Macro Regime/Recession into AI Briefings & Chatbot | P2 | CLOSED ✅ (2026-03-04) |
| #171 — Homepage Section Quick-Nav | P2 | CLOSED ✅ (2026-03-05) |
| #183 — Homepage Narrative Cohesion — Full Redesign | P3 | CLOSED ✅ (2026-03-09) |
| #169 — Credit Market Detail Page | P2 | CLOSED ✅ (2026-03-06) |
| #208 — Refactor: Consolidate get_stats() / Fix Daily Briefing 52-Week Gap | P2 | CLOSED ✅ (2026-03-06) |
| #207 — Asset Detail Page Header — Shared Component Refactor | P3 | CLOSED ✅ (2026-03-09) |
| #206 — Global Trade Pulse — FRED Trade Balance Indicator | P2 | CLOSED ✅ (2026-03-06) |
| #166 — ML Container Separation (FinBERT/torch) | P3 | `needs-human-decision` (architecture trigger escalated) |
| #218 — Multi-Model Trust Signal: Why We Use Three Models | P3 | CLOSED ✅ (2026-03-10) |
| #232 — Bug: Richmond Fed SOS Indicator missing — openpyxl not installed | P1 | CLOSED ✅ (2026-03-10) |
| #219 — Fix: Regime Thread Missing from Cross-Market and Prediction Sections | P3 | CLOSED ✅ (2026-03-09) |
| #220 — Bug: Portfolio AI Credit Spread Unit Conversion (HY/IG reads as ~3 bp instead of ~280 bp) | P1 | CLOSED ✅ |
| #223 — Bug: market_signals.py crashes on empty FRED observations | P0 | CLOSED ✅ |
| #224 — Bug: Sector tone pipeline all Neutral — EDGAR fetch broken | P1 | CLOSED ✅ (2026-03-09) |

### Phase 7 Feature Detail

- **Credit Market Detail Page** (#169, P2) — ✅ COMPLETE. Three credit spread intelligence requirements delivered: (1) HY/IG OAS percentile gauge vs. rolling 20-year history, (2) regime-conditioned interpretation block, (3) HY–IG differential sparkline. AI briefing integration complete — credit context now in chatbot and daily briefing pipeline.
- **Homepage Narrative Cohesion — Full Redesign** (#183, P3) — ✅ COMPLETE. US-183.1: structural reorder (VERDICT→IMPLICATIONS→EVIDENCE→TODAY arc), visual regime threading (left-border accent), navbar regime pill, bridge sentences for §1.5/§2/§3. US-183.2: AI prompt anchoring — §1 Market Conditions and §2 Today's Briefing open with explicit regime context.
- **Refactor: Consolidate get_stats()** (#208, P2) — ✅ COMPLETE. Single `get_metric_stats(df)` at module level in `dashboard.py`; all 5 inline definitions replaced; daily briefing now includes 52-week range and distance-from-extreme context. Backend-only.
- **Asset Detail Page Header — Shared Component Refactor** (#207, P3) — ✅ COMPLETE. Consolidated 6 duplicate page-header CSS blocks into a single shared `.asset-page-header` component in `static/css/components/asset-page-header.css`. CSS custom property for category color accent. All 6 templates updated, per-page blocks removed, component documented in `docs/design-system.md`.
- **Global Trade Pulse — FRED Trade Balance Indicator** (#206, P2) — ✅ COMPLETE. Single-panel macro indicator showing US goods trade balance (BOPGSTB), YoY change, percentile framing, regime-conditioned interpretation. FRED-only scope; BDI deferred to Phase 9+. Follow-on: wire into AI Daily Briefing and Chatbot in Phase 8.
- **ML Container Separation** (#166, P3) — Extract FinBERT/torch from main app container. `needs-human-decision` — waiting on architecture decision (Redis queue vs. standalone cron vs. HTTP API).
- **Multi-Model Trust Signal** (#218, P3) — ✅ COMPLETE. Persistent UI explainer on Recession Probability panel using the 2022–2024 yield curve false alarm as the canonical example of why multi-model synthesis outperforms any single indicator. Copy + small UI component only; no new backend.
- **Fix: Regime Thread Missing from Cross-Market and Prediction Sections** (#219, P3) — ✅ COMPLETE. Added `regime-thread` class to `#signals-section` and `#prediction-section`. Regime-colored left border now renders consistently across all homepage sections.
- **Bug: Portfolio AI Credit Spread Unit Conversion** (#220, P1) — `generate_portfolio_market_context()` in `dashboard.py:3297-3304` formats HY/IG spreads without the `* 100` conversion, causing portfolio AI to see ~3 bp instead of ~280 bp. Silent accuracy failure in user-facing analysis. Two-line fix. (Approved from council discussion #37, 2026-03-08)
- **Bug: Richmond Fed SOS Indicator missing — openpyxl not installed** (#232, P1) — ✅ COMPLETE. `openpyxl` was absent from `requirements.txt`; `_fetch_richmond_sos()` silently returned `(None, None)`, causing the third recession model to not render. Fix: add `openpyxl>=3.0.0`. PR #234 merged.

---

## Phase 8: Signal Quality & Portfolio Foundation ✅ COMPLETE
**Milestone goal:** Deepen the quality and accuracy of the AI intelligence layer, fix the alert experience, and lay the foundation for real estate portfolio coverage.

*Phase 8 is a refinement phase — depth over breadth. We have added significant surface area across Phases 5–7. This phase makes what we have genuinely excellent before expanding further.*

### Hard Requirements

These are confirmed scope — no council research needed on direction.

| Requirement | Notes |
|-------------|-------|
| Daily Briefing Email audit & refresh | A lot has changed since the email was last updated: homepage redesign, regime detection, global trade pulse, new credit metrics. Audit the template, AI prompts, and data feeding the email. Bring it current. |
| Wire Global Trade Pulse into AI Daily Briefing & Chatbot | Carried forward from Phase 7 (#206). BOPGSTB panel ships without AI integration — Phase 8 closes that gap. |
| Property investment categories — value tracking | Add farmland, commercial rental property, and residential rental property as portfolio asset categories. At minimum: manual value entry, included in portfolio totals and AI recommendations. AI can reason meaningfully about these assets with values alone. |
| AI briefing prompt refresh | Audit all AI prompts against current signal coverage. Ensure regime, credit, global trade, and recession probability context is consistently included. Low-effort, high-impact. |

### Council Research Focus

These are open questions. Council researches and proposes — CEO approves/dismisses — PM creates Feature Issues.

| Topic | Council | Question |
|-------|---------|----------|
| Smart Market Alerts replacement | Researcher | Find an innovative, rare-signal alert approach — something that fires infrequently but with genuine conviction (e.g., multi-signal convergence at extremes, regime change triggers). If nothing compelling surfaces, recommend removing the feature. Do not propose incremental threshold tweaks. |
| AI briefing data quality | Engineer Council | What pre-processing, cross-signal correlation, or lightweight ML approaches could meaningfully improve LLM inputs? Look at: anomaly detection on individual metrics, convergence signals (multiple indicators stressed simultaneously), historical similarity matching. Propose what's achievable without major new dependencies. |
| Portfolio UX & analysis depth | Designer + Engineer Council | How should the portfolio section evolve? What analysis improvements are achievable given the value-tracking foundation? |
| Property macro data sources | Researcher + Engineer Council | Are there reliable public data sources for commercial cap rates, farmland price indexes, or residential rental yields? If yes, propose Phase 9 integration approach. If not, confirm value-tracking-only scope is correct. |

### Phase 8 Active Work

**Feature #237**: Smart Market Alerts — 3-Layer Architecture Replacement (P2) — CLOSED ✅ (2026-03-11)
- US-237.1 (#241): Layer 1 Regime Transition Detection — CLOSED ✅
- US-237.2 (#242): Layers 2 & 3 Extreme Percentile + Multi-Signal Convergence — CLOSED ✅
- US-237.3 (#243): Integration, Rate-Limiting (≤5/week), Display Update — CLOSED ✅

**Feature #238**: Portfolio Real Estate Categories — UX Design + Implementation (P2) — CLOSED ✅ (2026-03-11)
- US-238.1 (#245): Backend (API + breakdown + AI) — CLOSED ✅
- US-238.2 (#246): Frontend (picker, colors, labels) — CLOSED ✅
- US-238.3 (#247): UI (Real Estate summary card) — CLOSED ✅

**Bug #239**: Chatbot metrics silently broken — METRIC_INFO filename mismatches (P1) — CLOSED ✅ (PR #240 merged 2026-03-10)

### Success Metrics

| Metric | Target |
|--------|--------|
| Daily briefing email — content accuracy | Covers all major signal categories now in the product |
| Alert signal quality | Either: alerts fire ≤2x/week and are actionable, or feature is removed |
| Portfolio — property category adoption | Real estate categories available and included in AI portfolio analysis |

---

## Phase 9: Depth & Polish ✅ COMPLETE

**Milestone goal:** Build on Phase 8 council findings, add property macro coverage, a persistent news pipeline, contextual AI entry points, and address design system polish.

### Phase 9 Work

✅ **Feature #255**: Property Macro Panel — Residential & Farmland Indicators (P2) — **Complete**
- Case-Shiller HPI, CPI Rent trend, Rental Vacancy Rate, USDA NASS farmland $/acre; regime-contextualized; FRED/NASS only
- US-255.1 (#268): Backend data pipeline (FRED + NASS + interpretation config) — merged ✅
- US-255.2 (#269): Property page frontend (template, metric cards, percentile bars, collapsible sections) — merged ✅

✅ **Feature #256**: Alert Severity Design Tokens — CSS Component + Card Header Fix (P3) — **Complete**
- Extract 3 severity hex values to named CSS tokens; shared component file; fix Smart Market Alerts card header from bg-danger
- US-256.1 (#265): Extract tokens, create component CSS, fix card header — merged

✅ **Bug #257**: Credit briefing excluded from daily synthesis and portfolio context (P1) — **closed** (merged)

✅ **Feature #258**: Contextual AI Entry Points — Section-Level and Sentence-Level Drill-In (P2) — **Complete** (closed 2026-03-14)
- Part 1: AI icon per section → opens chatbot pre-loaded with section data. Part 2: sentence-level drill-in from AI briefings
- US-258.1 (#261): AI icon system migration — merged ✅
- US-258.2 (#262): Section-level ghost pill buttons — merged ✅
- US-258.3 (#263): Desktop sentence drill-in toolbar — merged ✅
- US-258.4 (#264): Mobile sentence tap flow — merged ✅
- US-258.5 (#277): Section AI buttons — AI-generated opening with live section data — merged ✅
- Bug #283: Off-screen toolbar fix — merged ✅
- Bug #287: Chatbot close button fix — merged ✅

✅ **Feature #259**: Persistent News Pipeline — Scheduled Fetch, AI Summarization, and News Page (P2) — **Complete**
- Tavily API fetch+store → aggregate AI summary → /news page + feeds AI briefings and chatbot. Feature B (custom feeds) deferred.
- US-259.1 (#266): News page backend route and data model — merged ✅
- US-259.2 (#267): News page frontend template and CSS — merged ✅
- US-259.3 (#275): News data pipeline — fetch, store, summarize — merged ✅
- US-259.4 (#276): Wire news page and briefings to stored news data — merged ✅

### Backlog Candidates (not yet created as issues)

| Candidate | Status |
|-----------|--------|
| AI briefing data pre-processing | Engineer Council proposal pending |
| Portfolio UX polish | Designer Council findings pending |
| General UX polish | Designer Council backlog items |
| BDI (Baltic Dry Index) integration | Re-evaluate after FRED trade balance signal validated |

---

## Phase 10: Market Conditions Engine ✅ COMPLETE

**Milestone goal:** Replace the k-means regime classifier (52.3/100 backtest score) with a multi-dimensional market conditions engine. Backend-only — no UI changes. Old regime system continues running in parallel.

**Reference spec:** [MARKET-CONDITIONS-FRAMEWORK.md](MARKET-CONDITIONS-FRAMEWORK.md) — contains full calculation methodology, data requirements, scoring framework, and validation strategy.

**Why this matters:** The current regime model has four structural failures: central bank intervention reverses expected outcomes (Bear regime saw S&P rise 67% of the time), stock-bond correlation flip (2022), gold's secular uptrend, and no distinction between demand crashes (2008: buy bonds) and inflation crashes (2022: sell bonds). These are not tunable — they require a fundamentally different approach.

### Phase 10 Features

| Feature | Title | Priority | Scope |
|---------|-------|----------|-------|
| #293 | Market Conditions Data Pipeline | P1 | Complete ✅ — all ~18 new FRED series collecting |
| #294 | Market Conditions Calculation Engine | P1 | Complete ✅ — four dimension engines in `market_conditions.py` |
| #295 | Market Conditions Backtest Validation | P1 | Complete ✅ — multi-asset accuracy 63.9%, quadrant ordering validated |
| #314 | Backtest Scoring Refinement | P1 | Complete ✅ — verdict removed, quadrant-led with real returns + magnitude ordering |
| #296 | Surface New FRED Series in Explorer Page | P2 | Complete ✅ — all new series in Explorer by dimension |

### Hard Gate

**The backtest validation is a hard gate.** If the new model does not meaningfully beat 52.3/100 in walk-forward validation, do not proceed to Phase 11. Iterate on the model first.

### Success Metrics

| Metric | Target |
|--------|--------|
| Walk-forward composite score | Meaningfully above 52.3/100 (current k-means baseline) |
| Quadrant stability | Average duration ≥ 3 months |
| Economic plausibility | March 2020 ≠ Goldilocks, 2022 ≠ Deflation Risk |
| Data pipeline reliability | All ~27 FRED series collecting without error |

---

## Phase 11: Market Conditions UI & Migration ✅ COMPLETE

**Milestone goal:** Ship the market conditions framework to users. Redesign the homepage around the conditions engine, migrate all category pages, enhance AI briefings, and deprecate the old regime system.

**Design specs complete.** Formal specs in `docs/specs/feature-11.1-*`, `feature-11.2-*`, `feature-11.3-*`. All stories written from Designer specs.

### Phase 11 Features

| Feature | Title | Priority | Scope |
|---------|-------|----------|-------|
| #322 | Conditions Strip Component | P1 | Complete ✅ — reusable strip on every page, quadrant headline, Liquidity leads on Crypto |
| #323 | Homepage Conditions Redesign | P1 | Complete ✅ — §0 AI briefing, §1 quadrant hero + 2×2 expand-in-place grid, §2 portfolio implications, movers footer, wide-screen layout |
| #324 | Category Page Conditions Migration | P2 | Complete ✅ — 7 pages migrated, quadrant × liquidity context, relocated sections (Recession→Credit, Sector Tone→Equities, Trade Pulse→Equities) |
| #325 | AI Conditions Integration (Briefing + Chatbot) | P1 | Complete ✅ — Four-dimension context dict + 90d conditions history + 14d briefing history, rule-based fallback, chatbot context |
| #326 | Old Regime System Deprecation | P2 | Complete ✅ — All old regime code, caches, CSS, templates removed |

**Dependency order:** #322 (strip) unblocks #323 (homepage) and #324 (category pages). #325 (AI) is independent. #326 (deprecation) runs last after everything else is migrated.

---

## Phase 12: AI Foundation & Metering ✅ COMPLETE

**Milestone goal:** Ship AI infrastructure for anonymous users: remove BYOK requirement, add anonymous AI trial with session limits, usage metering, and admin analytics. All 6 features closed, 12 user stories, release v0.12.0.

---

## Phase 13: Access Control & Launch Prep ✅ COMPLETE

**Milestone goal:** Ship invite-only registration, remove anonymous AI trial, prepare for public launch. Monetization paused pending traction validation.

**Context:** Original plan was Stripe & Subscriptions ($19/mo). After strategic review (2026-03-30), pivoted to access control — validate demand before building payments. Stripe foundation code (13.1.1–13.1.3) remains dormant in codebase. See [PHASE-13-ACCESS-CONTROL-AND-LAUNCH-PREP.md](PHASE-13-ACCESS-CONTROL-AND-LAUNCH-PREP.md).

| Feature | Status |
|---------|--------|
| #426 — Access Control & Launch Prep | CLOSED ✅ |
| US-13.2.1 (#427) — Invite Code on Registration | CLOSED ✅ |
| US-13.2.2 (#428) — Remove Anonymous AI Trial | CLOSED ✅ |
| US-13.2.3 (#429) — Update Registration & Rate Limit Messaging | CLOSED ✅ |
| US-13.2.4 (#430) — Verify Access Model for Invite-Only Users | CLOSED ✅ |

**Deferred:** Stripe issues #412–#417, #421 (closed with `deferred` label). PR #425 open, unmerged.

---

## Phase 15: Inflation Composite Redesign ✅ COMPLETE

**Milestone goal:** Fix inflation composite misclassification. Dashboard was showing "Deflation Risk" during a stagflationary environment. Root causes: acceleration-based classifier, 60-month z-score window, redundant indicators. See [INFLATION-COMPOSITE-REDESIGN.md](INFLATION-COMPOSITE-REDESIGN.md).

| Feature | Status |
|---------|--------|
| #459 — Inflation Data Pipeline Expansion | CLOSED ✅ |
| #460 — Inflation Composite Engine Redesign | CLOSED ✅ |
| #461 — Inflation Composite UI Updates | CLOSED ✅ |

**Key changes:** YoY direction (first derivative) replaces acceleration as primary classifier, 24-month z-score window, 6-indicator set (added Median CPI, 5Y5Y Forward, Michigan expectations; swapped out 5Y Breakeven), inflation breadth signal, component-level storage, graduated stability filter ("Transition Watch"), backtest validated.

---

## What We Are NOT Building

These directions have been evaluated and dismissed. Do not re-propose without new evidence:

| Direction | Reason | Date |
|-----------|--------|------|
| Options flow + dark pool synthesis as primary signal | Dark pool prints lack reliable direction signal; options flow space has better-funded competitors; fundamental data quality issue would undermine user trust | 2026-02-23 |

---

## Success Metrics by Phase

| Phase | Key Metric |
|-------|-----------|
| Phase 3 | Mobile usability: no iOS Safari zoom, all touch targets ≥44px, chatbot functional on mobile |
| Phase 4 | Alert engagement: % users with active alerts; alert visibility without dropdown |
| Phase 5 | Regime accuracy: backtested signal performance in regime-conditioned vs. raw mode |
| Phase 6 | Regime → action gap: users who view Regime Implications Panel after checking regime score |
| Phase 10 | Model accuracy: walk-forward composite score meaningfully above 52.3/100 baseline |
| Phase 11 | Conditions engagement: users who expand dimension details from verdict card |
| Phase 13 | Invite-only registration working, anonymous AI removed, site stable for public traffic ✅ |
| Phase 14 | Full rebrand shipped: all user-facing references updated to MacroClarity ✅ |
| Phase 15 | Inflation composite correctly classifies current environment; regime transitions detected within 1 month; component signals visible ✅ |
| Phase 16 | Site live at macroclarity.com, stable under public traffic |

---

## Guiding Principles

1. **Free data first.** FRED, OCC, FRED-MD, Barchart — institutional methodology at retail cost.
2. **Interpretation over raw data.** Every indicator needs a plain-language context sentence.
3. **Mobile-first.** The majority of users will check signals on a phone.
4. **Approve sparingly.** Every feature becomes engineering work. We choose depth over breadth.
