#!/usr/bin/env python3
"""
Macro news pipeline.

Two halves on two cadences (US-490.1):

- **Daily (no AI):** fetch news across six market topic areas via Tavily and
  store the full article content keyed by ET date.
- **Weekly (AI):** after Friday's close, summarize the whole week's stored
  articles into one cross-market summary plus per-topic summaries, and store
  that as a weekly review.

Usage:
    from news_pipeline import run_news_pipeline, summarize_week_news, get_stored_news

    run_news_pipeline()                       # daily: fetch + store, no AI
    summarize_week_news(week_start, through)  # weekly: summarize + store
    data = get_stored_news()                  # latest weekly summary + its sources
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytz
import requests

import briefing_week

# The pipeline runs from a scheduled data-collection job with no Flask app
# context, so it cannot read current_app.config. Importing the resolved value
# from config.py keeps config.py the single source of truth for model IDs.
from config import ANTHROPIC_MODEL
from anthropic_request import (
    build_request_params,
    extract_text,
    refusal_reason,
    served_model,
    warn_if_truncated,
)

# Short-output path: 1-2 paragraphs of prose. Thinking is always on and shares
# max_tokens with the visible answer, so the configured ANTHROPIC_EFFORT (tuned
# for briefings) would spend most of the budget on reasoning nobody reads.
# Pinning low effort here keeps the summaries cheap and keeps visible text
# inside the ceiling.
_SUMMARY_EFFORT = 'low'

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).parent / 'data'
NEWS_CACHE_FILE = DATA_DIR / 'news_data.json'
NEWS_REVIEWS_FILE = DATA_DIR / 'news_reviews.json'

RETENTION_DAYS = 90

# ---------------------------------------------------------------------------
# Topic queries
# ---------------------------------------------------------------------------
TOPIC_QUERIES = [
    ('macro',   'macroeconomic news today federal reserve GDP inflation employment'),
    ('crypto',  'cryptocurrency Bitcoin Ethereum crypto market news today'),
    ('equity',  'stock market equity S&P 500 earnings news today'),
    ('rates',   'interest rates treasury yields bond market news today'),
    ('dollar',  'US dollar DXY currency forex news today'),
    ('credit',  'credit markets corporate bonds spreads high yield news today'),
]

TAVILY_API_URL = 'https://api.tavily.com/search'

# Weekly prompt caps. A week holds ~5x the articles a day did, so the old
# daily caps (40 cross-market, 10 per topic) would cover barely two days.
MAX_WEEKLY_ARTICLES = 60
MAX_WEEKLY_TOPIC_ARTICLES = 20

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_topic(api_key: str, topic: str, query: str) -> list[dict]:
    """Fetch articles for one topic. Returns list of article dicts."""
    try:
        payload = {
            'api_key': api_key,
            'query': query,
            'search_depth': 'advanced',
            'max_results': 7,
            'include_raw_content': True,
            'include_answer': False,
        }
        resp = requests.post(TAVILY_API_URL, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning('[news_pipeline] Tavily fetch failed for topic=%s: %s', topic, exc)
        return []

    articles = []
    for item in data.get('results', []):
        url = item.get('url', '')
        try:
            domain = urlparse(url).netloc.lstrip('www.')
        except Exception:
            domain = ''
        raw = item.get('raw_content') or item.get('content', '')
        articles.append({
            'headline': item.get('title', ''),
            'url': url,
            'source': domain,
            'timestamp': datetime.now(tz=pytz.utc).isoformat(),
            'raw_content': raw,
            'topic': topic,
        })
    return articles


# ---------------------------------------------------------------------------
# AI summarization
# ---------------------------------------------------------------------------

def _generate_cross_market_summary(articles: list[dict]) -> str | None:
    """
    Generate a single cross-market prose summary from all articles.
    Returns the summary string, or None if AI is unavailable.
    """
    # Build article digest for the prompt (cap per-article length to save tokens)
    digest_parts = []
    for art in articles[:MAX_WEEKLY_ARTICLES]:
        content_snippet = (art['raw_content'] or '')[:600]
        digest_parts.append(
            f"[{art['topic'].upper()}] {art['headline']}\n"
            f"Source: {art['source']}\n"
            f"{content_snippet}"
        )
    digest = '\n\n---\n\n'.join(digest_parts)

    system_prompt = (
        "You are a senior macro analyst writing a weekly market briefing. "
        "Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. "
        "Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., 'near historic highs,' 'the largest move in months'). "
        "Write a comprehensive cross-market summary of this week's news in 3-5 prose paragraphs. "
        "Do NOT use bullet points or headers. Write in flowing, analytical prose. "
        "Cover the major themes across macro, equities, rates, credit, crypto, and currencies. "
        "Focus on what matters most and how the pieces connect. Be direct and authoritative."
    )
    user_prompt = (
        f"Today's date: {_today()}\n\n"
        f"Here are this week's key articles across all market areas:\n\n{digest}\n\n"
        "Write your cross-market summary now."
    )

    # Try Anthropic first, fall back to OpenAI
    provider = os.environ.get('AI_PROVIDER', 'openai').lower()

    if provider == 'anthropic':
        return _summarize_with_anthropic(system_prompt, user_prompt)
    else:
        return _summarize_with_openai(system_prompt, user_prompt)


def _generate_topic_summary(topic: str, articles: list[dict]) -> str | None:
    """
    Generate a focused 1-2 paragraph AI summary for a single market topic.
    Returns the summary string, or None on failure.
    """
    if not articles:
        return None

    digest_parts = []
    for art in articles[:MAX_WEEKLY_TOPIC_ARTICLES]:
        content_snippet = (art['raw_content'] or '')[:600]
        digest_parts.append(
            f"{art['headline']}\n"
            f"Source: {art['source']}\n"
            f"{content_snippet}"
        )
    digest = '\n\n---\n\n'.join(digest_parts)

    topic_label = topic.replace('_', ' ').title()
    system_prompt = (
        f"You are a senior {topic_label.lower()} market analyst writing a brief news summary. "
        "Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. "
        "Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., 'near historic highs,' 'the largest move in months'). "
        f"Summarize this week's {topic_label.lower()} news in 1-2 concise paragraphs of flowing prose. "
        "Do NOT use bullet points, headers, or lists. Focus on the most significant developments, "
        "key numbers, and market implications. Be direct and authoritative."
    )
    user_prompt = (
        f"Today's date: {_today()}\n\n"
        f"Here are this week's {topic_label.lower()} articles:\n\n{digest}\n\n"
        f"Write your {topic_label.lower()} news summary now."
    )

    provider = os.environ.get('AI_PROVIDER', 'openai').lower()
    if provider == 'anthropic':
        return _summarize_with_anthropic(system_prompt, user_prompt, max_tokens=4000)
    else:
        return _summarize_with_openai(system_prompt, user_prompt, max_tokens=500)


def _generate_all_topic_summaries(all_articles: list[dict]) -> dict[str, str]:
    """
    Generate per-topic AI summaries for market-specific briefings.
    Returns a dict mapping topic name to summary string.
    Topics with no articles are skipped.
    """
    # Group articles by topic (exclude 'macro' — it's only for cross-market)
    topic_articles: dict[str, list[dict]] = {}
    for art in all_articles:
        topic = art.get('topic', '')
        if topic and topic != 'macro':
            topic_articles.setdefault(topic, []).append(art)

    topic_summaries = {}
    for topic, articles in topic_articles.items():
        logger.info('[news_pipeline] Generating topic summary: %s (%d articles)', topic, len(articles))
        try:
            summary = _generate_topic_summary(topic, articles)
            if summary:
                topic_summaries[topic] = summary
                logger.info('[news_pipeline] Topic summary for %s: %d chars', topic, len(summary))
            else:
                logger.warning('[news_pipeline] Topic summary for %s returned empty', topic)
        except Exception as exc:
            logger.warning('[news_pipeline] Topic summary for %s failed: %s', topic, exc)

    return topic_summaries


def _meter(response, provider: str, model: str) -> None:
    """Meter a summarization call. Only records inside the scheduled weekly run."""
    try:
        from services.usage_metering import extract_usage
        usage = extract_usage(response, provider)
    except Exception:
        usage = {}
    briefing_week.meter_ai_call(usage, model)


def _summarize_with_openai(system_prompt: str, user_prompt: str, max_tokens: int = 1200) -> str | None:
    try:
        from openai import OpenAI
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            return None
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model='gpt-5.2',
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            max_completion_tokens=max_tokens,
            temperature=0.6,
        )
        _meter(resp, 'openai', 'gpt-5.2')
        content = resp.choices[0].message.content
        return content.strip() if content else None
    except Exception as exc:
        logger.warning('[news_pipeline] OpenAI summarization failed: %s', exc)
        return None


def _summarize_with_anthropic(system_prompt: str, user_prompt: str, max_tokens: int = 4000) -> str | None:
    """Summarize via Anthropic.

    `max_tokens` is a combined reasoning + visible-output ceiling: thinking is
    always on and is generated against the same budget.
    """
    try:
        import anthropic as anthropic_lib
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            return None
        client = anthropic_lib.Anthropic(api_key=api_key)
        msg = client.beta.messages.create(
            **build_request_params(
                model=ANTHROPIC_MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_prompt}],
                effort=_SUMMARY_EFFORT,
            )
        )
        _meter(msg, 'anthropic', served_model(msg, ANTHROPIC_MODEL))
        refusal = refusal_reason(msg)
        if refusal:
            logger.warning('[news_pipeline] Anthropic refused the summary: %s', refusal)
            return None
        warn_if_truncated(msg, '[news_pipeline]', max_tokens)
        # Never index content[0] — with thinking always on the first block is a
        # thinking block, which has no .text.
        return extract_text(msg)
    except Exception as exc:
        status = getattr(exc, 'status_code', None)
        if status is not None and 400 <= status < 500:
            # A 4xx is a broken request, not a transient blip. Folding it into
            # the generic warning below is how a model migration could silently
            # stop producing news summaries.
            logger.error(
                '[news_pipeline] Anthropic rejected the summarization request '
                '(HTTP %s) — this is a request-shape bug, not a transient '
                'failure: %s', status, exc,
            )
        else:
            logger.warning('[news_pipeline] Anthropic summarization failed: %s', exc)
        return None


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    """Today's date in ET. Week ranges are ET, so day keys must be too; the
    container clock is UTC and would file an evening fetch under tomorrow."""
    return briefing_week.now_et().date().isoformat()


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _load_cache() -> dict:
    return _load_json(NEWS_CACHE_FILE, {})


def _save_cache(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(NEWS_CACHE_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def _load_reviews() -> dict:
    data = _load_json(NEWS_REVIEWS_FILE, {'reviews': []})
    return data if isinstance(data.get('reviews'), list) else {'reviews': []}


def _save_reviews(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(NEWS_REVIEWS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def _prune(data: dict) -> dict:
    """Remove entries older than RETENTION_DAYS."""
    cutoff = (date.fromisoformat(_today()) - timedelta(days=RETENTION_DAYS)).isoformat()
    return {k: v for k, v in data.items() if k >= cutoff}


def _week_articles(cache: dict, week_start: str, through: str) -> list[dict]:
    """Every article stored from `week_start` to `through`, de-duplicated.

    Tavily often returns the same story on consecutive days; without
    de-duplication one story could appear five times and crowd out the rest of
    the week. A story is kept on the day it first appeared. The result is
    interleaved across days, newest day first, so the per-prompt article caps
    sample the whole week instead of filling up with Monday.
    """
    seen = set()
    per_day = []
    for day in sorted(k for k in cache if week_start <= k <= through):
        kept = []
        for art in cache[day].get('articles') or []:
            key = art.get('url') or art.get('headline')
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            kept.append(art)
        per_day.append(kept)

    interleaved = []
    per_day.reverse()
    for i in range(max((len(d) for d in per_day), default=0)):
        interleaved.extend(d[i] for d in per_day if i < len(d))
    return interleaved


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_news_pipeline() -> bool:
    """
    Daily: fetch macro news across all topics and store the articles.

    Makes **no AI calls**. Summarization runs weekly, in `summarize_week_news`.
    Returns True on success, False if Tavily is unavailable. Failures in
    individual topic fetches are non-fatal.
    """
    api_key = os.environ.get('TAVILY_API_KEY')
    if not api_key:
        logger.info('[news_pipeline] TAVILY_API_KEY not set — skipping news pipeline')
        return False

    today = _today()
    logger.info('[news_pipeline] Starting news fetch for %s', today)

    all_articles = []
    for topic, query in TOPIC_QUERIES:
        logger.info('[news_pipeline] Fetching topic: %s', topic)
        articles = _fetch_topic(api_key, topic, query)
        all_articles.extend(articles)
        logger.info('[news_pipeline] Got %d articles for topic %s', len(articles), topic)

    logger.info('[news_pipeline] Total articles fetched: %d', len(all_articles))

    eastern = pytz.timezone('US/Eastern')
    record = {
        'date': today,
        'fetched_at': datetime.now(eastern).isoformat(),
        'articles': all_articles,
    }

    cache = _load_cache()
    cache[today] = record
    cache = _prune(cache)
    _save_cache(cache)

    logger.info('[news_pipeline] News fetch complete for %s', today)
    return True


def summarize_week_news(week_start: date, through: date) -> bool:
    """
    Weekly: summarize every article stored from `week_start` to `through`.

    Produces the cross-market summary and the per-topic summaries the section
    briefings read, and stores them as one weekly review. Returns True if a
    review was stored.
    """
    articles = _week_articles(_load_cache(), week_start.isoformat(), through.isoformat())
    logger.info('[news_pipeline] Summarizing %d articles for %s to %s',
                len(articles), week_start, through)
    if not articles:
        logger.warning('[news_pipeline] No stored articles for the week — skipping summary')
        return False

    summary = _generate_cross_market_summary(articles)
    if summary:
        logger.info('[news_pipeline] Summary generated (%d chars)', len(summary))
    else:
        logger.warning('[news_pipeline] Summary generation failed or returned empty')

    topic_summaries = _generate_all_topic_summaries(articles)
    logger.info('[news_pipeline] Generated %d topic summaries', len(topic_summaries))

    if not summary and not topic_summaries:
        return False

    metadata = briefing_week.record_metadata()
    today = _today()
    review = {
        'date': today,
        'fetched_at': metadata['published_at'],
        # Sources only: the raw article text already lives in the daily records.
        'articles': [
            {k: art.get(k) for k in ('headline', 'url', 'source', 'timestamp', 'topic')}
            for art in articles
        ],
        'summary': summary,
        'topic_summaries': topic_summaries,
        **metadata,
    }

    data = _load_reviews()
    reviews = [r for r in data['reviews'] if r.get('date') != today]
    reviews.append(review)
    cutoff = (date.fromisoformat(today) - timedelta(days=RETENTION_DAYS)).isoformat()
    data['reviews'] = sorted(
        (r for r in reviews if r.get('date', '') >= cutoff), key=lambda r: r['date'])
    _save_reviews(data)
    return True


def get_stored_news(max_stale_days: int = 14) -> dict | None:
    """
    Return the news record to display and to feed into briefings.

    The weekly review is the record that carries summaries. Daily records hold
    articles only, so "today's record first" would let Wednesday's summary-less
    fetch mask last Friday's review. Lookup order:

    1. the most recent record with a summary within `max_stale_days`: a weekly
       review, or a daily record written before US-490.1 (those carry their own
       summaries). On the same date the weekly review wins;
    2. otherwise the most recent daily record (articles, no summary).

    The window defaults to two weeks so one missed Friday run still shows the
    previous review rather than nothing.

    Returns:
        dict with keys 'date', 'fetched_at', 'articles', 'summary',
        'topic_summaries' (and week fields on weekly reviews), or None.
    """
    try:
        cache = _load_cache()
        reviews = _load_reviews()['reviews']
    except Exception:
        return None

    cutoff = (date.fromisoformat(_today()) - timedelta(days=max_stale_days)).isoformat()

    summarized = [(r.get('date', ''), 1, r) for r in reviews if r.get('summary')]
    summarized += [(k, 0, v) for k, v in cache.items() if v.get('summary')]
    summarized = [c for c in summarized if c[0] >= cutoff]
    if summarized:
        return max(summarized, key=lambda c: (c[0], c[1]))[2]

    recent = [k for k in cache if k >= cutoff]
    if not recent:
        return None
    return cache[max(recent)]
