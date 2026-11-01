"""
When the Saturday weekly-review email goes out (US-490.5).

One email a week, after the Friday week-in-review run, at about 08:00 on
Saturday in each reader's own timezone. This module owns the scheduling rules
as pure functions so they can be tested without a database or a clock:

- `user_timezone`: a reader's timezone, falling back to America/New_York for a
  value pytz does not know (one bad row must never stop the job for everyone);
- `review_due_week`: which review a reader is due right now, or None;
- `already_sent`: the once-per-week dedup check;
- `next_email_date`: the "Next email: Sat, Oct 31" line on the settings page;
- `subject_range`: the date range in the subject line.

**Why the send waits for the review.** Saturday 08:00 in Asia/Tokyo is Friday
19:00 ET and in Pacific/Auckland it is Friday 15:00 ET, before the 18:00 ET
weekly run even starts. Sending on the clock alone would give those readers
the *previous* week's review every week. So a reader is due from 08:00 local
on Saturday until the end of their Saturday, and only once that week's
scheduled review exists. The job runs every 15 minutes, so far-east readers
get the email on their first run after the review lands. If the Friday run
never produces a review, nobody is emailed that week: a week-old review is
never sent under this week's date.

Like `briefing_week.py`, this lives at the top level (not under `services/`)
and imports only stdlib + pytz.
"""

from datetime import date, datetime, timedelta
import logging

import pytz

import briefing_week

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = 'America/New_York'

SEND_WEEKDAY = 5  # Saturday
SEND_HOUR = 8     # 08:00 local, "around 8:00 AM Saturday, your time"


def is_valid_timezone(name) -> bool:
    return isinstance(name, str) and name in pytz.all_timezones_set


_warned_timezones = set()


def user_timezone(name):
    """The reader's tzinfo. An unknown name falls back to America/New_York."""
    if is_valid_timezone(name):
        return pytz.timezone(name)
    if name not in _warned_timezones:  # the job runs every 15 minutes: warn once
        _warned_timezones.add(name)
        logger.warning('[weekly_email] Unknown timezone %r; using %s', name, DEFAULT_TIMEZONE)
    return pytz.timezone(DEFAULT_TIMEZONE)


def _aware_utc(now: datetime | None) -> datetime:
    """`now` as an aware UTC datetime. Naive input is rejected: its meaning
    depends on the container's TZ, which is the bug this replaces."""
    if now is None:
        return datetime.now(pytz.utc)
    if now.tzinfo is None:
        raise ValueError('weekly_email needs a timezone-aware datetime')
    return now.astimezone(pytz.utc)


def local_now(tz_name, now: datetime | None = None) -> datetime:
    return _aware_utc(now).astimezone(user_timezone(tz_name))


def review_due_week(tz_name, now: datetime | None = None) -> date | None:
    """The `week_end` (a Friday) of the review a reader is due now, or None.

    Due from 08:00 local on Saturday through the rest of that Saturday. The
    review is the one for the trading week ending the day before the reader's
    Saturday. That is the same Friday in ET for every timezone, because a
    reader's Saturday never starts before Friday 10:00 UTC.
    """
    local = local_now(tz_name, now)
    if local.weekday() != SEND_WEEKDAY or local.hour < SEND_HOUR:
        return None
    return local.date() - timedelta(days=1)


def already_sent(last_sent: date | None, week_end: date) -> bool:
    """True if this week's review has already gone to the reader.

    `last_briefing_sent_date` holds the reader's local date of the last send.
    Anything on or after the Saturday that follows `week_end` counts, so a
    timezone change made after the send cannot trigger a second email. A send
    from the old daily email on the Friday itself does not count.
    """
    return last_sent is not None and last_sent > week_end


def next_email_date(tz_name, last_sent: date | None, now: datetime | None = None) -> date:
    """The Saturday the reader's next weekly email is due, in their timezone.

    On a Saturday that has not had its email yet (before 08:00, or later while
    waiting on a delayed Friday run) that is today. Otherwise the next Saturday.
    """
    today = local_now(tz_name, now).date()
    days_ahead = (SEND_WEEKDAY - today.weekday()) % 7
    saturday = today + timedelta(days=days_ahead)
    if days_ahead == 0 and already_sent(last_sent, saturday - timedelta(days=1)):
        saturday += timedelta(days=7)
    return saturday


def format_short_date(day: date) -> str:
    """"Sat, Oct 31". Fixed English names: strftime follows the process locale."""
    return (f'{briefing_week._WEEKDAYS[day.weekday()]}, '
            f'{briefing_week._MONTHS[day.month - 1]} {day.day}')


def subject_range(fields: dict | None) -> str | None:
    """The review's date range for the subject line (Designer, on #495).

    "Oct 26–30", "Oct 29 – Nov 2", "Dec 29, 2025 – Jan 2, 2026": the page's
    week label without "Week of" and without the year unless the range spans
    two years. A week-to-date review ends at its `through` day.
    """
    if not fields:
        return None
    start = briefing_week._iso_day(fields.get('week_start'))
    end = briefing_week._iso_day(fields.get('week_end'))
    if not start or not end:
        return None
    if fields.get('week_to_date'):
        end = briefing_week._iso_day(fields.get('through')) or end
    months = briefing_week._MONTHS
    if start.year != end.year:
        return (f'{months[start.month - 1]} {start.day}, {start.year}'
                f' – {months[end.month - 1]} {end.day}, {end.year}')
    if start.month != end.month:
        return f'{months[start.month - 1]} {start.day} – {months[end.month - 1]} {end.day}'
    if start == end:
        return f'{months[start.month - 1]} {start.day}'
    return f'{months[start.month - 1]} {start.day}–{end.day}'
