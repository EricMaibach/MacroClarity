"""
Week metadata and run context for the weekly AI review (US-490.1).

Every scheduled AI briefing is produced once a week, after Friday's close, and
covers the Mon-Fri trading week. This module owns:

- the week arithmetic (`trading_week`, `next_run_after`), always in
  America/New_York so the result is independent of the container's TZ and of
  the reader's browser clock;
- the metadata stamped onto every stored briefing (`record_metadata`) and the
  fields the display APIs return (`display_fields`);
- the run context that tells a generator whether it is running inside the
  scheduled weekly job or a manual regenerate (`scheduled_run`), and the
  metering hook that records scheduled AI calls (`meter_ai_call`).

Like `anthropic_request.py` and `email_links.py`, this lives at the top level
(not under `services/`) and imports only stdlib + pytz, so `ai_summary.py` and
`news_pipeline.py` can use it from scheduler threads with no app context.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
import logging

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')

# When the scheduled weekly review runs: Friday, after the 17:30 daily data
# refresh. The weekly job also waits on the pipeline lock, so a slow refresh
# delays the review rather than letting it read stale data.
WEEKLY_RUN_WEEKDAY = 4  # Friday
WEEKLY_RUN_TIME = time(18, 0)

RUN_SCHEDULED = 'scheduled'
RUN_MANUAL = 'manual'
# Records stored before US-490.1 carry no week fields.
RUN_LEGACY = 'legacy'

# Usage-metering interaction type for AI calls made by the weekly job.
SCHEDULED_INTERACTION_TYPE = 'scheduled_briefing'


# ---------------------------------------------------------------------------
# Week arithmetic
# ---------------------------------------------------------------------------

def now_et() -> datetime:
    return datetime.now(ET)


def to_et(dt: datetime) -> datetime:
    """Convert an aware datetime to ET. Naive datetimes are rejected: their
    meaning depends on the host TZ, which is exactly what this module avoids."""
    if dt.tzinfo is None:
        raise ValueError('to_et() needs a timezone-aware datetime')
    return dt.astimezone(ET)


def trading_week(day: date) -> tuple[date, date]:
    """Return (Monday, Friday) of the trading week a calendar date belongs to.

    Saturday and Sunday belong to the week that just finished, never the one
    about to start: a weekend reader is reading last week's review. Holidays
    do not shorten the range; it is always Mon-Fri.
    """
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=4)


def _weekly_run_on(day: date) -> datetime:
    return ET.localize(datetime.combine(day, WEEKLY_RUN_TIME))


def next_run_after(moment: datetime) -> datetime:
    """The first scheduled weekly run strictly after `moment`."""
    moment = to_et(moment)
    days_ahead = (WEEKLY_RUN_WEEKDAY - moment.weekday()) % 7
    candidate = _weekly_run_on(moment.date() + timedelta(days=days_ahead))
    if candidate <= moment:
        candidate = _weekly_run_on(candidate.date() + timedelta(days=7))
    return candidate


def latest_due_run(moment: datetime) -> datetime:
    """The most recent scheduled weekly run at or before `moment`."""
    return next_run_after(moment) - timedelta(days=7)


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------

# A ContextVar, not a module global: the weekly job runs in a scheduler thread
# while manual regenerates run in request threads at the same time, and each
# must stamp its own records. New threads start with the default (None).
_run_context: ContextVar[dict | None] = ContextVar('briefing_run', default=None)


@contextmanager
def scheduled_run(week_start: date, week_end: date):
    """Mark everything inside the block as part of the scheduled weekly run."""
    token = _run_context.set({'week_start': week_start, 'week_end': week_end})
    try:
        yield
    finally:
        _run_context.reset(token)


def is_scheduled_run() -> bool:
    return _run_context.get() is not None


def current_week() -> tuple[date, date, date]:
    """(week_start, week_end, through) for whatever is running now.

    Inside the scheduled run this is the job's week, through Friday. Outside it
    (a manual regenerate) it is the week of today in ET, through today, clamped
    to Friday so a weekend regenerate reads "through Fri".
    """
    ctx = _run_context.get()
    if ctx is not None:
        return ctx['week_start'], ctx['week_end'], ctx['week_end']
    today = now_et().date()
    week_start, week_end = trading_week(today)
    return week_start, week_end, min(today, week_end)


def record_metadata(published_at: datetime | None = None) -> dict:
    """Week fields to store on a briefing record written right now."""
    published_at = to_et(published_at) if published_at else now_et()
    week_start, week_end, through = current_week()
    return {
        'week_start': week_start.isoformat(),
        'week_end': week_end.isoformat(),
        'through': through.isoformat(),
        'published_at': published_at.isoformat(),
        'run_type': RUN_SCHEDULED if is_scheduled_run() else RUN_MANUAL,
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _parse_published(record: dict) -> datetime | None:
    for key in ('published_at', 'generated_at', 'fetched_at'):
        raw = record.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # Pre-US-490.1 records wrote ET wall-clock time without an offset.
            parsed = ET.localize(parsed)
        return to_et(parsed)
    return None


def display_fields(record: dict, now: datetime | None = None) -> dict:
    """Week fields the display APIs return for a stored record.

    Never raises: a record from before this change (no week fields) is mapped
    onto the week of its `date`, flagged `legacy`, and a record with nothing
    usable returns nulls, so old JSON cannot turn the briefing into a 500.
    """
    now = to_et(now) if now else now_et()
    empty = {
        'week_start': None, 'week_end': None, 'through': None,
        'published_at': None, 'next_review_at': None,
        'run_type': None, 'week_to_date': None,
    }
    try:
        published = _parse_published(record)
        if record.get('week_start') and record.get('week_end'):
            week_start = date.fromisoformat(record['week_start'])
            week_end = date.fromisoformat(record['week_end'])
            through = date.fromisoformat(record.get('through') or record['week_end'])
            run_type = record.get('run_type') or RUN_MANUAL
        else:
            anchor = record.get('date')
            day = date.fromisoformat(anchor) if anchor else (published.date() if published else None)
            if day is None:
                return empty
            week_start, week_end = trading_week(day)
            through = min(day, week_end)
            run_type = RUN_LEGACY

        # A scheduled review is replaced by the next Friday's run. A manual
        # regenerate is replaced by the first run after it was made.
        if run_type == RUN_SCHEDULED:
            next_review = _weekly_run_on(week_end + timedelta(days=7))
        else:
            next_review = next_run_after(published or _weekly_run_on(through))
        # If that run has already been missed, point at the next one due.
        if next_review.date() < now.date():
            next_review = next_run_after(now)

        return {
            'week_start': week_start.isoformat(),
            'week_end': week_end.isoformat(),
            'through': through.isoformat(),
            'published_at': published.isoformat() if published else None,
            'next_review_at': next_review.isoformat(),
            'run_type': run_type,
            'week_to_date': run_type != RUN_SCHEDULED and through < week_end,
        }
    except (TypeError, ValueError) as exc:
        logger.warning('[briefing_week] Unreadable week fields on record: %s', exc)
        return empty


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------

def meter_ai_call(usage: dict | None, model: str | None) -> None:
    """Record one AI call made by the scheduled weekly run.

    Outside the scheduled run this is a no-op: user-triggered calls are metered
    by their routes against the user. Metering never breaks the run:
    `record_usage` swallows its own errors, and the import is guarded because
    tests can leave a stub in `sys.modules['services']`.
    """
    if not is_scheduled_run() or not model:
        return
    usage = usage or {}
    try:
        from services.usage_metering import record_usage
        record_usage(
            user_id=None,
            interaction_type=SCHEDULED_INTERACTION_TYPE,
            model_name=model,
            input_tokens=usage.get('input_tokens'),
            output_tokens=usage.get('output_tokens'),
            cache_read_tokens=usage.get('cache_read_tokens'),
            cache_creation_tokens=usage.get('cache_creation_tokens'),
        )
    except Exception:
        logger.exception('[briefing_week] Failed to meter scheduled AI call')
