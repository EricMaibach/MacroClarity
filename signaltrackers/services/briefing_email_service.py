"""
Weekly Review Email Service (US-490.5)

Builds and sends the Saturday "Your Week in Review" email from the Friday
week-in-review run. When to send is decided in `weekly_email.py`; this module
finds the review, assembles the email and sends it to one user.
"""

from datetime import datetime, timedelta
import html
import logging
import re

from markupsafe import Markup

import briefing_week
import weekly_email
import weekly_review
from ai_summary import load_summaries
from email_links import external_url, list_unsubscribe_headers, unsubscribe_url
from extensions import db
from models.alert import Alert
from services.email_service import send_email

logger = logging.getLogger(__name__)

TEMPLATE_HTML = 'email/weekly_review.html'
TEMPLATE_TXT = 'email/weekly_review.txt'

ALERT_LOOKBACK = timedelta(days=7)


# ---------------------------------------------------------------------------
# Finding the review
# ---------------------------------------------------------------------------

def _sort_key(record):
    return (record.get('published_at') or record.get('generated_at') or '', record.get('date') or '')


def find_review(week_end=None, scheduled_only=True):
    """The general week-in-review record to email, or None.

    With `week_end`, only the scheduled review of that exact week qualifies,
    so a missed Friday run is never covered by last week's review. Without it
    (the admin force-send), the latest scheduled review, or with
    `scheduled_only=False` the latest review of any kind.
    """
    records = load_summaries().get('summaries') or []
    scheduled = [r for r in records if r.get('run_type') == briefing_week.RUN_SCHEDULED]
    if week_end is not None:
        wanted = week_end.isoformat()
        scheduled = [r for r in scheduled if r.get('week_end') == wanted]
        return max(scheduled, key=_sort_key) if scheduled else None
    if scheduled:
        return max(scheduled, key=_sort_key)
    if not scheduled_only and records:
        return max(records, key=_sort_key)
    return None


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

_MD_BOLD = re.compile(r'\*\*(.+?)\*\*')
_MD_HEADING = re.compile(r'^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$')
_MD_BULLET = re.compile(r'^\s*(?:[-*•]|\d+[.)])\s+(.*)$')
_MD_MARKS = re.compile(r'(\*\*|__|`|^#+\s*)', re.MULTILINE)

_P = '<p style="margin: 0 0 15px 0;">{}</p>'
_UL = '<ul style="margin: 0 0 15px 0; padding-left: 20px;">{}</ul>'
_LI = '<li style="margin-bottom: 8px;">{}</li>'


def _inline(text):
    """Escape a line, then restore **bold** as <strong>."""
    return _MD_BOLD.sub(r'<strong>\1</strong>', html.escape(text.strip()))


def markdown_to_email_html(text):
    """Render the fallback markdown body as escaped, inline-styled email HTML.

    Supports what the briefing actually uses (paragraphs, bullets, headings
    and **bold**). Everything is HTML-escaped first, so model output can never
    inject markup into the email.
    """
    if not text or not text.strip():
        return None
    out = []
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        items, para = [], []

        def flush_para():
            if para:
                out.append(_P.format(' '.join(para)))
                para.clear()

        def flush_items():
            if items:
                out.append(_UL.format(''.join(_LI.format(i) for i in items)))
                items.clear()

        for line in lines:
            heading = _MD_HEADING.match(line)
            bullet = _MD_BULLET.match(line)
            if heading:
                flush_para(); flush_items()
                out.append(_P.format(f'<strong>{_inline(heading.group(1))}</strong>'))
            elif bullet:
                flush_para()
                items.append(_inline(bullet.group(1)))
            else:
                flush_items()
                para.append(_inline(line))
        flush_para(); flush_items()
    return Markup(''.join(out))


def markdown_to_text(text):
    """The fallback markdown body as plain text (markers stripped)."""
    if not text or not text.strip():
        return None
    return _MD_MARKS.sub('', text.strip())


def first_sentence(text, limit=200):
    """A plain-text lead sentence from the fallback markdown, for the preheader."""
    plain = ' '.join((markdown_to_text(text) or '').split())
    if not plain:
        return None
    match = re.match(r'(.+?[.!?])(?:\s|$)', plain)
    sentence = match.group(1) if match else plain
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(' ', 1)[0] + '…'
    return sentence


def build_review_content(record):
    """Everything the email needs from one stored review record."""
    fields = briefing_week.display_fields(record)
    structured = record.get('structured') if isinstance(record.get('structured'), dict) else None
    summary = record.get('summary') or ''

    headline = (structured or {}).get('headline') or None
    why = (structured or {}).get('why') or ''
    content = {
        'fields': fields,
        'week_label': briefing_week.format_week_label(fields),
        'subject_range': weekly_email.subject_range(fields),
        'structured': structured,
        'headline': headline,
        'changed': (structured or {}).get('changed') or [],
        'why_paragraphs': [p.strip() for p in why.split('\n\n') if p.strip()],
        'watch': (structured or {}).get('watch') or [],
        'fallback_html': None if structured else markdown_to_email_html(summary),
        'fallback_text': None if structured else markdown_to_text(summary),
    }
    content['preheader'] = headline or first_sentence(summary) or 'The week in markets, and what to watch next.'
    return content


def subject_line(content):
    label = content.get('subject_range')
    return f'Your Week in Review: {label}' if label else 'Your Week in Review'


def get_market_conditions_summary():
    """
    Get status for 6 market dimensions

    TODO: Implement logic to determine status from actual metrics

    Returns:
        list: List of condition dicts with icon, name, status, badge_color, text_color
    """
    # Placeholder data - will be replaced with actual metric analysis
    return [
        {'icon': '🏦', 'name': 'Credit', 'status': 'Moderate Risk', 'badge_color': '#fff3cd', 'text_color': '#856404'},
        {'icon': '📈', 'name': 'Equity', 'status': 'Neutral', 'badge_color': '#e2e3e5', 'text_color': '#383d41'},
        {'icon': '📊', 'name': 'Rates', 'status': 'Elevated Risk', 'badge_color': '#f8d7da', 'text_color': '#721c24'},
        {'icon': '💵', 'name': 'Dollar', 'status': 'Moderate Calm', 'badge_color': '#d1ecf1', 'text_color': '#0c5460'},
        {'icon': '🛡️', 'name': 'Safe Havens', 'status': 'Elevated Calm', 'badge_color': '#d4edda', 'text_color': '#155724'},
        {'icon': '₿', 'name': 'Crypto', 'status': 'Neutral', 'badge_color': '#e2e3e5', 'text_color': '#383d41'},
    ]


def get_weekly_moves(content, limit=5):
    """Biggest moves of the review's week, by the 5-day (Mon–Fri) change."""
    fields = content.get('fields') or {}
    start = briefing_week._iso_day(fields.get('week_start'))
    through = briefing_week._iso_day(fields.get('through') or fields.get('week_end'))
    if not start or not through:
        return []
    try:
        return weekly_review.biggest_moves(start, through, limit=limit)
    except Exception as exc:  # a bad CSV must not stop the email
        logger.warning('[weekly email] Could not compute weekly moves: %s', exc)
        return []


def get_user_triggered_alerts(user_id, now=None):
    """Alerts triggered for a user in the past 7 days, newest first."""
    now = now or datetime.utcnow()
    cutoff = now - ALERT_LOOKBACK
    alerts = Alert.query.filter(
        Alert.user_id == user_id,
        Alert.triggered_at >= cutoff
    ).order_by(Alert.triggered_at.desc()).all()

    return [alert.to_dict() for alert in alerts]


def get_portfolio_analysis_snippet(user):
    """
    Get 1-2 sentence portfolio context for user

    TODO: Integrate with existing portfolio AI analysis from ai_summary.py

    Args:
        user: User object

    Returns:
        str: Portfolio analysis text, or None if not applicable
    """
    # Check if user wants portfolio analysis
    if not user.alert_preferences.include_portfolio_analysis:
        return None

    # Check if user has portfolio allocations
    if not user.portfolio_allocations.count():
        return None

    # Placeholder - will integrate with db_get_latest_portfolio_summary(user.id)
    return "Your 60/40 portfolio is moderately positioned for current risk-off conditions. Consider reviewing equity allocation if VIX remains elevated."


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_weekly_review_to_user(user, content, moves=None, now=None):
    """
    Send the weekly review email to one user.

    The caller decides whether the user is due (see `jobs/email_jobs.py`).
    This only re-checks the opt-in, so nothing can email an unsubscribed user.
    On success the user's local send date is recorded for the weekly dedup.

    Returns:
        bool: True if the email was sent.
    """
    try:
        prefs = user.alert_preferences
        if not prefs or not prefs.daily_briefing_enabled:
            logger.debug(f"User {user.id}: weekly review email disabled")
            return False

        if moves is None:
            moves = get_weekly_moves(content)

        unsubscribe_link = unsubscribe_url(user, 'briefing')
        context = {
            'user': user,
            'email_subject': subject_line(content),
            'preheader': content['preheader'],
            'week_label': content['week_label'],
            'headline': content['headline'],
            'changed': content['changed'],
            'why_paragraphs': content['why_paragraphs'],
            'watch': content['watch'],
            'fallback_html': content['fallback_html'],
            'fallback_text': content['fallback_text'],
            'market_conditions': get_market_conditions_summary(),
            'top_movers': moves,
            'triggered_alerts': get_user_triggered_alerts(user.id),
            'include_portfolio': prefs.include_portfolio_analysis,
            'portfolio_analysis': get_portfolio_analysis_snippet(user),
            'dashboard_url': external_url('index'),
            'unsubscribe_url': unsubscribe_link,
        }

        success = send_email(
            to=user.email,
            subject=context['email_subject'],
            template_html=TEMPLATE_HTML,
            template_txt=TEMPLATE_TXT,
            extra_headers=list_unsubscribe_headers(unsubscribe_link),
            **context
        )

        if success:
            prefs.last_briefing_sent_date = weekly_email.local_now(prefs.briefing_timezone, now).date()
            db.session.commit()
            logger.info(f"Weekly review email sent to user {user.id}")
        else:
            logger.error(f"Failed to send weekly review email to user {user.id}")

        return success

    except Exception as e:
        logger.error(f"Failed to send weekly review email to user {user.id}: {str(e)}", exc_info=True)
        db.session.rollback()
        return False
