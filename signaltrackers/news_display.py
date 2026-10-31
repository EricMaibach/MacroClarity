"""
News page presentation helpers (US-490.4).

The news page shows one weekly review and the week's source articles. This
module turns the stored record into what the template renders:

- `summary_paragraphs`: the summary split on blank lines, rendered as one
  `<p>` each. The text stays plain text: Jinja autoescapes each paragraph.
- `group_sources_by_day`: the sources grouped under ET day headings, newest
  day first, with articles that have no readable timestamp last.

Top level (not under `services/`) and stdlib, pytz and `briefing_week` only, like
`briefing_week.py`, so tests can import it without the app.
"""

from datetime import datetime
import re

import pytz

import briefing_week

_BLANK_LINES = re.compile(r'(?:\r?\n[ \t]*){2,}')

EARLIER_LABEL = 'Earlier this week'

_DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def summary_paragraphs(text: str | None) -> list[str]:
    """Split a summary on blank lines (\\n\\n, \\r\\n\\r\\n, or more), dropping empty ones."""
    if not text:
        return []
    return [p.strip() for p in _BLANK_LINES.split(str(text)) if p.strip()]


def _article_et_date(article: dict):
    """The ET calendar day an article was fetched on, or None if unreadable.

    `timestamp` is the fetch time in UTC (news_pipeline writes
    `datetime.now(tz=pytz.utc)`), so a winter Friday 20:30 ET fetch is stored
    as Saturday 01:30 UTC and must be converted before taking the date. A
    naive timestamp is read as UTC, the zone the pipeline writes in.
    """
    raw = article.get('timestamp')
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = pytz.utc.localize(moment)
    return briefing_week.to_et(moment).date()


def day_label(day) -> str:
    """'Friday, Oct 30': the template uppercases it with CSS (spec: 'FRIDAY, OCT 30')."""
    return f'{_DAY_NAMES[day.weekday()]}, {_MONTHS[day.month - 1]} {day.day}'


def safe_href(url) -> str | None:
    """The URL if it is http(s), else None, so a stored `javascript:` URL is never a link."""
    if isinstance(url, str) and re.match(r'^https?://', url.strip(), re.IGNORECASE):
        return url.strip()
    return None


def group_sources_by_day(articles: list[dict] | None) -> list[dict]:
    """Group source articles by the ET day they were fetched.

    Returns `[{'label', 'date', 'articles'}]`, newest day first, then one
    "Earlier this week" group for articles with a missing or unreadable
    timestamp. Days with no articles get no group. Within a group the stored
    order is kept.

    A story fetched on several days (same URL, or same headline when there is
    no URL) appears once, under the **earliest** day it was fetched. That
    matches `news_pipeline._week_articles`, which keeps a story on the day it
    first appeared and tags it with that day in the summary prompt.
    """
    articles = [a for a in (articles or []) if isinstance(a, dict)]
    dated = [(a, _article_et_date(a)) for a in articles]

    # First appearance = earliest day; undated copies lose to any dated one.
    first_seen = {}
    for index, (article, day) in enumerate(dated):
        key = article.get('url') or article.get('headline')
        if not key:
            continue
        rank = (day is None, day or 0, index)
        if key not in first_seen or rank < first_seen[key][0]:
            first_seen[key] = (rank, index)
    keep = {index for _, index in first_seen.values()}

    by_day = {}
    undated = []
    for index, (article, day) in enumerate(dated):
        key = article.get('url') or article.get('headline')
        if key and index not in keep:
            continue
        item = {**article, 'href': safe_href(article.get('url'))}
        if day is None:
            undated.append(item)
        else:
            by_day.setdefault(day, []).append(item)

    groups = [
        {'label': day_label(day), 'date': day.isoformat(), 'articles': by_day[day]}
        for day in sorted(by_day, reverse=True)
    ]
    if undated:
        groups.append({'label': EARLIER_LABEL, 'date': None, 'articles': undated})
    return groups


def source_count(groups: list[dict]) -> int:
    return sum(len(g['articles']) for g in groups)
