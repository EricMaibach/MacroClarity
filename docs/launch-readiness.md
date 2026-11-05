# Launch Readiness Review: macroclarity.com

**Story:** US-513.1 (#514), part of feature #513
**Reviewed:** 2026-11-04, against `main` @ `b5487c5`
**Result: NOT READY.** 15 of the 31 audit findings block launch, and this review found 9 more blocking problems the audit didn't have (section 2).

> This is a reasonableness check, not legal advice. Nobody involved is a lawyer. Section 7 lists what the founder may want a lawyer to look at.

> **This repo is public, and so is this file.** It records which weaknesses exist, as `docs/security-audit.md` already does, but not which leaked credentials are still in use. To check that locally, run `python3 scripts/check_secret_history.py`. It prints key names and flags only. Keep its output out of issues and PRs.

## 1. Results by acceptance criterion

| # | Item | Result | Blocking? | Evidence |
|---|------|--------|-----------|----------|
| 1 | Audit findings re-checked against current code | **FAIL**: 5 fixed, not exploitable or accepted, and 26 open or partly open. 15 of them block launch | Yes | Section 3 |
| 2 | Dependency CVE scan | **PASS**: no known vulnerabilities | No | Section 4 |
| 3 | Debug off in production | **FAIL**: debug is on with the current prod compose file | Yes, #517 | Section 5.1 |
| 4 | No secrets in the image | **PASS** | No | Section 5.2 |
| 5 | No secrets in logs | **FAIL**: the FRED key is printed on request errors | Yes, #530 | Section 5.3 |
| 6 | AI limits and caps reviewed, worst-case spend written down | **FAIL**: regenerates are uncapped, so spend has no bound today | Yes, #532 | Section 6 |
| 7 | Data-source terms checked, notices listed | **FAIL**: core data is restricted, and no required notice is shown | Yes, #534, #519 | Section 8 |
| 8 | Lawyer items flagged | **PASS** | No | Section 7 |
| 9 | Blocking findings filed and linked | **PASS** | No | Section 9 |
| 10 | Go-live checklist present | **PASS**: not signed | Signed in US-513.11 | Section 10 |

## 2. Blocking findings the audit didn't have

| Finding | Evidence | Tracked in |
|---------|----------|------------|
| The repo is public, so every secret in history is readable without login. Real keys are also in the history of `.env.example`, not only `.env` | `gh repo view` reports `PUBLIC`. `raw.githubusercontent.com/.../038063c/.env` returned HTTP 200 (status code only). Commit `f372caf` holds real-looking keys in `.env.example`. At least one current `.env` value still matches history | #525 |
| FRED API key printed to logs on any failed request | Sentinel key at `market_signals.py:227` printed `...observations?series_id=DGS10&api_key=<KEY>&file_type=json`. Same pattern at `credit_signals.py:112` | #530 |
| Production mode is broken: `SECRET_KEY` is a `property` object, so login returns 500, and the app also starts with no key at all | Section 5.1, runs B, C and E | #517 |
| Manual briefing regenerates have no limit | 500 `briefing_regenerate` rows recorded for a user, then 10 of 10 requests returned 200 with the limit at 5. `rate_limiting.py:83` counts only `portfolio_analysis` | #532 |
| Chatbot request size has no limit | `MAX_CONTENT_LENGTH` is `None`, and a 5 MB JSON body parsed fine. `dashboard.py:3719` forwards the client's `conversation` as sent | #532 |
| Chat text is saved to disk | A sentinel chat message was found in `data/prompt_dumps/chatbot.txt` inside the container. Same mechanism as audit M-1, which rated only prompt exposure | #531 |
| `remember_token` cookie has no `Secure` or `SameSite` | Login response: `remember_token ; HttpOnly`. `REMEMBER_COOKIE_*` isn't set anywhere | #528 |
| An empty `INVITE_CODE` opens registration, even in `invite_only` mode | `dashboard.py:1914` skips the check when empty. Registered with no code: 302 to `/login`, user count went from 1 to 2. `docker-compose.prod.yml` defaults it to empty | #528 |
| App starts after a failed migration | `signaltrackers/docker-entrypoint.sh` prints "Migration failed, but continuing" and runs gunicorn | #533 |

**Audit corrections:**
- "Production SECRET_KEY enforcement" is listed as a strength (observation 8), but it doesn't work. `from_object` reads the class attribute, so the `@property` is never called.
- Observation 9 says Flask-Limiter is applied to AI endpoints. There are no `limiter.limit` calls in the code. Only the `100 per minute` default and the custom AI limiter apply.
- C-1 lists Brevo and the invite code as leaked. Neither appears in the `.env` history I scanned, and no `xsmtpsib-` prefix appears in any blob. Rotate them anyway if there's any doubt.

**Non-blocking, noted for later:**
- All requests run on one gunicorn sync worker (`Using worker: sync`), so a multi-minute AI call stalls the site for everyone. #532 reduces how easily this is triggered.
- `/register` returns "Username already exists" and "Email already registered" even when the invite code is wrong, so accounts can be enumerated.
- FRED series `NAPM` returns HTTP 400, and `USSLIND` stopped updating in February 2020.
- With `SITE_MODE=paid`, a logged-in user without paid access passes every limit layer. This must be fixed before leaving invite-only. Noted on #532.

## 3. Audit re-check: all 31 findings

Line numbers are for `b5487c5`. "Probe" means tested with a Flask test client inside an image built from a clean clone. Otherwise the check was by reading the code.

### Critical

| ID | Finding | Status | Evidence | Blocking? | Tracked in |
|----|---------|--------|----------|-----------|------------|
| C-1 | Secrets Committed to Git History | **OPEN**, worse than rated | See section 2. `.env` is in commits `038063c`, `d459166`, `1e7d5d6`, `8086726` | **Yes** | #525 |
| C-2 | Unauthenticated Unsubscribe Endpoints — IDOR | **FIXED** | Signed token at `dashboard.py:2592`, `email_links.load_unsubscribe_token`. Probe: `/unsubscribe/alerts/1` and an invalid token both return 404 | No | Bug #496 |
| C-3 | Unauthenticated Data Reload Endpoint | **OPEN** | Probe: anonymous `POST /api/reload-data` returned 200 `Data reload started` and started a thread. `dashboard.py:3160` | **Yes** | #526 |
| C-4 | Debug Endpoint Leaks API Key Prefix | **OPEN** | Probe: anonymous GET returned 200 with `tavily_api_key_preview`. `dashboard.py:4852,4860` | **Yes** | #526 |

### High

| ID | Finding | Status | Evidence | Blocking? | Tracked in |
|----|---------|--------|----------|-----------|------------|
| H-1 | XSS via Marked.js — Sanitization Disabled | **OPEN**, wider than rated | `chatbot.js:52,293,325`, `portfolio.html:884`, `week-review.js:211`, `index.html:770`. No `DOMPurify` in templates | **Yes** | #527 |
| H-2 | XSS via `\|safe` Filter in Email Template | **FIXED** | No `\|safe` left in `templates/`. `briefing_email_service.markdown_to_email_html` escapes before building `Markup` | No | None |
| H-3 | Prompt Injection via Unsanitized Context Fields | **OPEN** | `page` at `dashboard.py:3610` and `section_name` at `3572` are still interpolated. Login is now required, and a user only affects their own session | No | Deferred |
| H-4 | Missing Rate Limiting on Auth Endpoints | **OPEN** | Probe: 30 bad logins, all processed. No `limiter.limit` anywhere | **Yes** | #528 |
| H-5 | CSRF Protection Disabled on State-Changing API Endpoints | **OPEN**, mitigated | `@csrf.exempt` at `dashboard.py:3160,3543,4127,4550,4583,4613,4666`. Session cookie is `SameSite=Lax`, so the session cookie isn't sent cross-site on POST. The remember cookie gap is fixed in #528 | No, once #528 ships | Deferred |
| H-6 | Missing Security Headers | **OPEN** | Probe: all five headers are `None` on `/` | **Yes** | #529 |
| H-7 | Raw Exception Messages Returned to Users | **OPEN** | 16 `str(e)` in `dashboard.py`, for example lines 3261, 4546, 4579 | **Yes** | #530 |
| H-8 | Account Enumeration via Deactivation Message | **NOT EXPLOITABLE AS WRITTEN** | `dashboard.py:1878-1881`: the message appears only after the correct password. Register has a different enumeration issue, in section 2 | No | None |
| H-9 | Open Redirect in Login | **OPEN** | Probe: `next=//attacker.example/x` returned 302 to `//attacker.example/x`. `dashboard.py:1888` | **Yes** | #528 |
| H-10 | Unpinned Dependencies | **OPEN** | `requirements.txt`: 22 `>=`, 1 `==`. A fresh resolve gives `anthropic` 2.5.0, while the cached image has 2.4.0 | No | Deferred |
| H-11 | Unmaintained flask-mail Package | **OPEN** | `flask-mail==0.9.1` still in use. `pip-audit` reports no known vulnerability in it | No | Deferred |

### Medium

| ID | Finding | Status | Evidence | Blocking? | Tracked in |
|----|---------|--------|----------|-----------|------------|
| M-1 | System Prompts Written to Plaintext Files | **OPEN**, worse than rated | `ai_summary.py:187`, `dashboard.py:3638,4181`. Holds holdings and chat text, not only prompts | **Yes** | #531 |
| M-2 | Docker Container Runs as Root | **OPEN** | No `USER` in `Dockerfile`, and packages live in `/root/.local`. Only Caddy will publish ports (#523) | No | Deferred |
| M-3 | No Session Lifetime Configured | **OPEN** | No `PERMANENT_SESSION_LIFETIME` set (Flask default 31 days). No remember duration set | No | #528 (remember) |
| M-4 | Unvalidated Web Search Queries from AI | **OPEN** | `web_search.py:16` has no validation or audit log | No | Deferred |
| M-5 | Uncontrolled AI Tool Iteration Cost | **PARTLY FIXED** | Still 6 turns (`ai_summary.py:344`) and 5 for chat (`dashboard.py:3724`). The weekly prompt has `MAX_SEARCHES=4`. No per-request cost cap | **Yes** for the uncapped routes | #532 |
| M-6 | Missing Email Format Validation | **OPEN** | Probe: `not-an-email` was accepted | **Yes** | #528 |
| M-7 | Symbol Validation Endpoint — No Auth, No Rate Limit | **OPEN** | `dashboard.py:4639` has no `login_required`. Probe: anonymous request reached the handler | **Yes** | #526 |
| M-8 | Excessive `print()` Statements in Production Code | **OPEN** | `dashboard.py` 70, `ai_summary.py` 36, `scheduler.py` 0. The harmful one is covered by #530 | No | Deferred |
| M-9 | SameSite Cookie Set to Lax (Not Strict) | **ACCEPTED** | `config.py`. `Strict` would log users out when they follow an email link to the site | No | None |
| M-10 | No Missing Subresource Integrity (SRI) on CDN Resources | **OPEN** | `base.html:22-23,322-325`. `marked` and `chart.js` also have no version pin | **Yes** | #527 |

### Low

| ID | Finding | Status | Evidence | Blocking? | Tracked in |
|----|---------|--------|----------|-----------|------------|
| L-1 | No Password Reset/Recovery Flow | **OPEN** | No reset route or template found | No, tolerable at invite-only | Deferred |
| L-2 | Weak Invite Code Storage | **OPEN** | `config.py:74`, plaintext env var. Compared with `compare_digest` at `dashboard.py:1914` | No | Accepted |
| L-3 | System Status Endpoints Expose Internal State | **OPEN** | Probe: both return 200 anonymously | **Yes**, same fix as C-3 | #526 |
| L-4 | SQLite in Production | **ACCEPTED** | Feature decision at this scale. Backups need a SQLite-safe method | No | #523 |
| L-5 | No HTTPS Redirect Enforcement | **OPEN**, planned | No redirect in the app. Caddy will do it | Yes | #523 |
| L-6 | Session Storage for Conversation History | **OPEN** | `chatbot.js:555,559` | No | Deferred |

**Status counts:** 2 fixed (C-2, H-2), 1 not exploitable (H-8), 2 accepted (M-9, L-4), 1 partly fixed (M-5), 25 open.

**Blocking (15):** C-1, C-3, C-4, H-1, H-4, H-6, H-7, H-9, M-1, M-5, M-6, M-7, M-10, L-3, L-5.

## 4. Dependency CVE scan

| Scan | Tool | Date (UTC) | Packages | Vulnerabilities |
|------|------|------------|----------|-----------------|
| `pip-audit -r signaltrackers/requirements.txt` (fresh resolve) | pip-audit 2.10.1 | 2026-11-05 | 112 | **0** |
| `pip freeze` from the clean-clone image, `--no-deps --disable-pip` | pip-audit 2.10.1 | 2026-11-05 | 116 | **0** |

No vulnerabilities means there are no decisions to record. **Not covered:** OS packages in the image and JavaScript loaded from the CDN. The image build reused cached Docker layers, which is why the two package sets differ. Dependencies aren't pinned (H-10), so a result only holds for the day it was run. Re-run it for the release that goes live.

## 5. Debug mode and secrets

### 5.1 Debug and production mode

Run in the clean-clone image, reading the live Flask config:

| Run | Environment | `app.debug` | `SECRET_KEY` type | Session cookie `Secure` | Outcome |
|-----|-------------|-------------|-------------------|-------------------------|---------|
| A | Today's `docker-compose.prod.yml`: no `FLASK_ENV` | **True** | `str`, dev default | False | Starts in debug with a public default key |
| B | `FLASK_ENV=production`, key set | False | **`property`** | True | Starts, see E |
| C | `FLASK_ENV=production`, **no key** | False | **`property`** | True | **Starts anyway**, doesn't refuse |
| D | `FLASK_ENV=prod` (typo) | **True** | `str` | False | Falls back to `DevelopmentConfig` |
| E | As B, then a real login | | | | **500**: `TypeError: key: expected bytes or bytearray, but got 'property'` |

`Debug=True` under gunicorn doesn't expose the Werkzeug console, since that needs `app.run` (`dashboard.py:6533`, reached only when run directly). It's still wrong for production. **Blocking**, tracked in #517. Run D isn't in #517's criteria as written: an unknown `FLASK_ENV` should fail rather than fall back to debug.

### 5.2 Image

| Check | Clean-clone image (`b5487c5`) | Published image |
|-------|-------------------------------|-----------------|
| Which image | Built from a fresh clone | `ghcr.io/ericmaibach/financial:latest` (0.15.0, April 2026). `macroclarity:latest` **doesn't exist yet** (`manifest unknown`) |
| `docker history` matching key patterns | 0 | 0 |
| `.env`, `.db`, `.pem` or `id_rsa` under app paths | None | None |
| Files in `/app/data` | 0 | 0 |
| Key prefixes in `/app` (`sk-ant-api`, `sk-proj-`, `sk_live_`, `xsmtpsib-`, `tvly-`, `whsec_`, `ghp_`, private keys) | 0 | 0 |
| Current `.env` values in `/app` | 0 of 7 | 0 of 7 |

**Result: PASS, with limits.** Only `/app` and image metadata were byte-searched, and the root scan looked for file names.
- `.dockerignore` doesn't exclude `signaltrackers/data/`, and its `.env` line matches only the repo root. So a **local** build bakes in local data (QA counted 110 entries). CI builds from a clean checkout are unaffected. Only deploy images built by CI.
- The workflow publishes to `ghcr.io/${{ github.repository }}`, so the next release should create `macroclarity`. The first run is unproven until #524.
- The old `financial` image is still public and contains code with the open findings above. The founder may want to delete it.

### 5.3 Logs

| Check | Result |
|-------|--------|
| Container started with sentinel secrets, then registered, logged in, sent a chat and a wrong password. 173 log lines | 0 sentinels in logs, including password and chat text |
| Failed FRED request with a sentinel key | **Key printed in full.** Blocking, #530 |

The app has no logs from real traffic to sample. The long-running dev container had only 4 lines, so I didn't rely on it.

## 6. AI limits and worst-case spend

### What's in place

| Layer | Limit | Where counted | Survives a restart? |
|-------|-------|---------------|---------------------|
| Anonymous global cap | 100 per day | Memory | No |
| Anonymous session | 5 chat, 2 analysis | Session cookie | Cleared with the cookie |
| Per-user chat | 25 per day | `ai_usage_records` | Yes |
| Per-user analysis | 5 per day, **`portfolio_analysis` only** | `ai_usage_records` | Yes |
| Flask-Limiter default | 100 per minute, per IP | Memory (`RATELIMIT_STORAGE_URI = memory://`) | No |

**Anonymous layers:** these can't currently be reached. Probe: every AI POST route returns 401 to anonymous users, so anonymous AI spend is $0. They're not protecting anything, so they aren't evidence of a cap.

**What actually stops spending at the cap:** a per-user 429, and nothing else. There's no global or monthly budget in the app, and all the checks fail open if they raise an exception. **The only hard stop available is a spend limit in the provider's console**, which is on the checklist in section 10.

### Worst-case monthly spend

Assumes **10 invited users** (founder to confirm), a 30-day month, and the prices in `services/usage_metering.py`: Sonnet 5.5 $2/$10 and Opus 5.5 $4/$20 per MTok, no caching.

**Ceiling per call** (every turn hits `max_tokens`):

| Call | Output | Input (assumed) | Ceiling |
|------|--------|-----------------|---------|
| Chat (Sonnet) | 5 turns × 16,000 = 80,000 × $10/M = $0.80 | 80,000 × $2/M = $0.16 (dev prompt dump is 32,450 bytes, about 8k tokens per turn, plus tool results) | **$0.96** |
| Portfolio or regenerate (Opus) | 6 turns × 16,000 = 96,000 × $20/M = $1.92 | 90,000 × $4/M = $0.36 (65-89k was measured per call without caching) | **$2.28** |

**Monthly:**

| Line | Working | Today | After #532 |
|------|---------|-------|------------|
| Chat | 25 × 10 users × 30 × $0.96 | $7,200 | $7,200 |
| Portfolio analysis | 5 × 10 users × 30 × $2.28 | $3,420 | $3,420 (bucket shared with regenerates) |
| Scheduled weekly run | $2.67 measured (QA session 292) × 4.33 weeks | $11.56 | $11.56 |
| Regenerates | See below | **No limit in the app** | $0, inside the analysis bucket |
| **Total** | | **No bound** | **$10,631.56 at most** |

These are ceilings. Real use should be far lower, but no measured chat cost exists to prove that, because the local usage table is empty. Re-check the $2.67 after any model change.

**Regenerates today:** the only bound is throughput. One sync worker at an assumed 60 seconds per call gives 1,440 calls a day. At about $0.33 a call ($2.67 ÷ 8 weekly calls), that's about $475 a day or **$14,256 a month**. At the $2.28 ceiling, it's about $3,283 a day or **$98,496 a month**. One invited account with a script is enough.

**Padded chat input today:** with no size limit, cost grows with whatever a client sends. For example, 200,000 extra tokens × 5 turns × $2/M adds $2.00 a message, or $15,000 a month across 10 users at the daily limit, if the model accepts that size.

## 7. Items the founder may want a lawyer to see

Before opening sign-up beyond invite-only:

1. **Terms of Service text**, draft copy from US-513.6.
2. **Privacy Policy text**, draft copy from US-513.6. It must match what's actually stored once #531 and #518 ship.
3. **Whether the register page needs a consent checkbox.** The spec uses a consent line with no checkbox.
4. **Market data licensing** (#534): ICE, Yahoo and the other copyrighted series, and whether showing them to invited users already counts as public display.
5. **FRED terms on caching and AI** (section 8). The wording may or may not cover a local cache and passing data to an AI model.
6. **Tavily's "financial investment decisions" clause** for AI summaries on a markets site.
7. **"Not financial advice" wording** after US-513.3 and US-513.5.
8. **Governing law** choice (`GOVERNING_LAW`).

## 8. Data sources: attribution and redistribution

Checked 2026-11-04. Quotes are from the pages linked. Pages were read through a fetch tool, and the Tavily, Yahoo and alternative.me quotes were re-confirmed directly. Direct requests to `fred.stlouisfed.org` timed out, so the FRED quotes rely on the fetch tool's read and on the FRED API for series notes.

| Source | Used for | Display allowed? | What the terms say | Blocking? |
|--------|----------|------------------|--------------------|-----------|
| [FRED API terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html) | 44 of 45 series respond (`NAPM` returns 400) | **Yes with notice** | "Place the following notice prominently on your application", and display a link to the Terms of Use. Third-party series: "you must contact the data owner to obtain permission" beyond personal use | Notice: yes, #519 |
| [FRED legal terms](https://fred.stlouisfed.org/legal/) | Same | **Unclear** | Bars use "for any commercial use without the prior written consent of the Bank", and to "Store, cache, or archive". Also bars use in developing or training machine learning. Unclear how this applies to a local cache and to prompts | Lawyer flag |
| ICE BofA on FRED ([BAMLH0A0HYM2](https://fred.stlouisfed.org/series/BAMLH0A0HYM2), BAMLC0A0CM, BAMLH0A3HYC) | Credit page, Market Conditions | **Restricted** | "Copyrighted: Pre-Approval Required". "Reproduction of this data in any form is prohibited except with the prior written permission of ICE Data Indices." Page read for BAMLH0A0HYM2. The other two have the same copyright note via the API | **Yes**, #534 |
| Other FRED series with copyright notes: CSUSHPISA (S&P), MICH and UMCSENT (U. Michigan), VXVCLS (Cboe), ECBASSETSW (ECB), JPNASSETS (BoJ) | Various | **Check each** | FRED API notes contain "Copyright", which the terms say marks copyrighted series. Exact tag (citation or pre-approval) not read | **Yes**, #534 |
| FRED series with no copyright note (35, from the API on 2026-11-04) | Various | **Yes with notice** | Covered by the FRED notice and attribution | No |
| [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) and [yfinance README](https://github.com/ranaroussi/yfinance) | Prices, ETFs, VIX, portfolio symbols | **Restricted** | "Unless you have explicit written permission, you must not reproduce... distribute... publicly perform... or exploit for any commercial purposes". yfinance: "intended for personal use only". The product-specific API terms weren't read | **Yes**, #534 |
| [Tavily terms](https://www.tavily.com/terms) (updated May 4, 2026) | News, chatbot search | **Unclear** | "solely for Customer's internal business purposes, including for integration with Customer Applications". Won't use AI Functionality or Outputs "in connection with... financial investment decisions". No clause on displaying results | **Yes**, #534 |
| News publishers, via Tavily | News page: headline, source domain, link, timestamp, AI summaries | **Not checked per publisher** | Publishers vary and each has its own terms. Full article text is fetched and stored (`include_raw_content`), and only summaries, headlines and links are shown. Check Tavily's answer, and avoid displaying stored text | Lawyer flag |
| [alternative.me Fear & Greed](https://alternative.me/crypto/fear-and-greed-index/) | Crypto page | **Yes with notice** | "Commercial use is allowed as long as the attribution is given right next to the display of the data." | Notice: yes, #519 |
| [USDA NASS Quick Stats](https://quickstats.nass.usda.gov/api) | Farmland on the Property page | **Yes with notice** | "This product uses the NASS API but is not endorsed or certified by NASS." | Notice: yes, #519 |
| [NY Fed terms](https://www.newyorkfed.org/privacy/termsofuse) (updated 6/9/2023) | Recession probability | **Yes with notice** | License to "use, copy, and distribute Content for your personal or business purposes". Third-party section not verified | Notice: yes, #519 |
| [Richmond Fed terms](https://www.richmondfed.org/terms_and_conditions) and [indicator page](https://www.richmondfed.org/research/national_economy/sos_recession_indicator) | Recession probability | **Unclear** | "Most materials created by the Federal Reserve Bank of Richmond... are protected from unauthorized use by copyright law." The page offers a suggested citation | Check with #534 |
| SEC EDGAR | Sector tone only | **N/A** | Being removed in #515, so not reviewed. Re-check if a replacement uses it | No |

### Required notices for US-513.6 (#519)

None of these appear in the templates today.

1. FRED: "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis." Plus a link to https://fred.stlouisfed.org/docs/api/terms_of_use.html
2. FRED series: credit FRED and the original source where the data is shown, for example "Source: BLS via FRED".
3. NASS: "This product uses the NASS API but is not endorsed or certified by NASS."
4. alternative.me: name and link the source **right next to** the Fear & Greed display. A footer notice isn't enough.
5. NY Fed: "© [year] Federal Reserve Bank of New York. Content from the New York Fed subject to the Terms of Use at newyorkfed.org."
6. Richmond Fed: use the page's suggested citation.

Notices don't remove the restrictions on ICE, Yahoo and the others, which are for #534 to settle.

## 9. Blocking findings and where they're tracked

| Finding | Issue | Needs |
|---------|-------|-------|
| Credentials in public history | #525 (P0) | **Founder** |
| Restricted market data | #534 | **Founder decision** |
| Open operational endpoints (C-3, C-4, L-3, M-7) | #526 | Engineer |
| XSS in AI output, CDN pins and SRI (H-1, M-10) | #527 | Engineer |
| Login and registration (H-4, H-9, M-6, remember cookie, invite gate) | #528 | Engineer |
| Security headers (H-6) | #529 | Engineer |
| Keys in logs, exception text (H-7) | #530 | Engineer |
| Prompt dumps with user data (M-1) | #531 | Engineer |
| Regenerate cap, request size (M-5) | #532 | Engineer |
| Failed migration (entrypoint) | #533 | Engineer |
| `SECRET_KEY`, debug, secure cookies, proxy | #517 (US-513.4) | Already planned |
| AI advice language | #516 (US-513.3) | Already planned |
| Disclaimers | #520 (US-513.7) | Already planned |
| Terms, Privacy, notices | #519 (US-513.6) | Already planned |
| HTTPS redirect, backup and restore | #523 (US-513.10) | Already planned |

All new issues are in the Phase 16 milestone and are sub-issues of #513.

## 10. Go-live checklist

Every box must be ticked on the production server before DNS goes live. Filled in by US-513.11.

**Secrets**
- [ ] #525 done: all leaked credentials rotated, and `scripts/check_secret_history.py` reports none
- [ ] `SECRET_KEY` newly generated, and not any earlier value

**Configuration**
- [ ] `FLASK_ENV=production`, and `app.debug` confirmed False on the server
- [ ] `SITE_MODE=invite_only`
- [ ] `INVITE_CODE` set, and registering without it is rejected
- [ ] `BASE_URL=https://macroclarity.com`
- [ ] `CONTACT_EMAIL` set
- [ ] `GOVERNING_LAW` set
- [ ] `MAIL_DEFAULT_SENDER` updated: the default still says SignalTrackers, and the address must be verified with the mail provider
- [ ] Mail credentials set, and verification, unsubscribe and weekly briefing links tested on the real domain

**Cost**
- [ ] Monthly spend limit set in the Anthropic console (and OpenAI, if its key is still configured). This is the only hard stop
- [ ] Invited-user count confirmed, and section 6 recalculated if it's above 10

**Image and deploy**
- [ ] `ghcr.io/ericmaibach/macroclarity:latest` exists and was built by CI, not locally
- [ ] `pip-audit` re-run for the release going live
- [ ] Decide whether to delete the old public `financial` image

**Data and legal**
- [ ] #534 decided, and the decision recorded
- [ ] Data-source notices from section 8 live on the site
- [ ] Terms and Privacy live, and confirmed as **draft copy for founder review, not lawyer-reviewed**
- [ ] Section 7 lawyer items reviewed, and each one either taken to a lawyer or knowingly deferred
- [ ] Stored AI output written under the old prompts cleared (US-513.3 step)
- [ ] Any existing `data/prompt_dumps/` files deleted

**Safety**
- [ ] Every issue in section 9 closed or explicitly waived below
- [ ] Backup and restore tested on the server

### Waivers

| Finding | Reason accepted | Revisit by |
|---------|-----------------|------------|
| | | |

### Founder sign-off

I have read this review and accept the remaining risks for an invite-only launch.

- **Name:**
- **Date:**
- **Signature:**
