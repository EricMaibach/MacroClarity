"""
Weekly review email job (US-490.5).

Runs every 15 minutes. Each run emails the opted-in users who are due: it is
Saturday 08:00 or later in their timezone, this week's scheduled review
exists, and they have not had it yet. See `weekly_email.py` for why the send
waits on the review rather than the clock alone.
"""

import logging

import weekly_email

logger = logging.getLogger(__name__)


def send_weekly_reviews(now=None, force=False):
    """
    Email the weekly review to every opted-in user who is due.

    Args:
        now: aware datetime to evaluate the send window at (tests); default now.
        force: admin manual trigger. Ignores the Saturday window and the
            weekly dedup and sends the latest review to every opted-in user.
            Opt-in is still respected.

    Returns:
        dict: {'sent': n, 'failed': n, 'waiting': n, 'review_week_end': str|None}
    """
    from models.user import User
    from models.alert import AlertPreference
    from services.briefing_email_service import (
        build_review_content, find_review, get_weekly_moves, send_weekly_review_to_user,
    )

    result = {'sent': 0, 'failed': 0, 'waiting': 0, 'review_week_end': None}
    logger.info("Starting weekly review email job%s...", " (forced)" if force else "")

    try:
        users = User.query.join(AlertPreference).filter(
            AlertPreference.daily_briefing_enabled == True  # noqa: E712
        ).all()
    except Exception as e:
        logger.error(f"Weekly review email job could not load users: {e}", exc_info=True)
        return result

    # One review and one movers table per week, shared by every user.
    content_by_week = {}

    def content_for(week_end):
        if week_end not in content_by_week:
            if force:
                record = find_review(scheduled_only=False)
            else:
                record = find_review(week_end)
            if record is None:
                content_by_week[week_end] = None
            else:
                content = build_review_content(record)
                content_by_week[week_end] = (content, get_weekly_moves(content))
        return content_by_week[week_end]

    for user in users:
        # One bad row (e.g. an unknown timezone) must not stop the job for everyone.
        try:
            prefs = user.alert_preferences
            if force:
                week_end = 'forced'
            else:
                week_end = weekly_email.review_due_week(prefs.briefing_timezone, now)
                if week_end is None:
                    continue
                if weekly_email.already_sent(prefs.last_briefing_sent_date, week_end):
                    continue

            prepared = content_for(week_end)
            if prepared is None:
                # The Friday run has not produced this week's review yet.
                result['waiting'] += 1
                continue
            content, moves = prepared
            result['review_week_end'] = content['fields'].get('week_end')

            if send_weekly_review_to_user(user, content, moves=moves, now=now):
                result['sent'] += 1
            else:
                result['failed'] += 1
        except Exception as e:
            result['failed'] += 1
            logger.error(f"Weekly review email failed for user {user.id}: {e}", exc_info=True)

    if result['waiting']:
        logger.info("Weekly review email: %d due users are waiting for this week's review",
                    result['waiting'])
    logger.info("Weekly review email job completed. Sent %d emails.", result['sent'])
    return result


def send_weekly_reviews_wrapper():
    """Wrapper to run job with app context"""
    # Import here to avoid circular imports
    from dashboard import app

    with app.app_context():
        send_weekly_reviews()
