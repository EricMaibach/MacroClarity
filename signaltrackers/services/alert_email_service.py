"""
Alert Email Service

Handles generation and delivery of alert notification emails.
Supports single alerts and batching multiple alerts into one email.
"""

from datetime import datetime
import pytz
from services.email_service import send_email
from models.alert import Alert
from extensions import db
from email_links import external_url, list_unsubscribe_headers, unsubscribe_url
import logging

logger = logging.getLogger(__name__)


def get_severity_colors(severity):
    """
    Get color scheme for alert severity

    Args:
        severity: 'info', 'warning', or 'critical'

    Returns:
        dict: Color values for email styling
    """
    colors = {
        'info': {
            'header_bg': '#d1ecf1',
            'header_text': '#0c5460',
            'border': '#17a2b8',
            'badge_bg': '#d1ecf1',
            'badge_text': '#0c5460'
        },
        'warning': {
            'header_bg': '#fff3cd',
            'header_text': '#856404',
            'border': '#ffc107',
            'badge_bg': '#fff3cd',
            'badge_text': '#856404'
        },
        'critical': {
            'header_bg': '#f8d7da',
            'header_text': '#721c24',
            'border': '#dc3545',
            'badge_bg': '#f8d7da',
            'badge_text': '#721c24'
        }
    }
    return colors.get(severity, colors['info'])


def format_metric_value(value):
    """
    Format metric value for display

    Args:
        value: Numeric value to format

    Returns:
        str: Formatted value string
    """
    if value is None:
        return 'N/A'

    # Handle different value types
    if isinstance(value, float):
        # For percentages (values between 0 and 1)
        if 0 <= abs(value) <= 1:
            return f'{value * 100:.2f}%'
        # For large numbers
        elif abs(value) > 100:
            return f'{value:,.1f}'
        # For small decimals
        else:
            return f'{value:.2f}'

    return str(value)


def send_alert_notification(user, alerts):
    """
    Send alert notification email for one or more alerts

    Args:
        user: User object
        alerts: List of Alert objects or single Alert object

    Returns:
        bool: True if email sent successfully
    """
    try:
        # Normalize to list
        if not isinstance(alerts, list):
            alerts = [alerts]

        if not alerts:
            return False

        # Get highest severity for header color
        severity_order = {'info': 0, 'warning': 1, 'critical': 2}
        max_severity = max(alerts, key=lambda a: severity_order.get(a.severity, 0)).severity
        colors = get_severity_colors(max_severity)

        # Format alerts for template
        formatted_alerts = []
        for alert in alerts:
            alert_colors = get_severity_colors(alert.severity)
            formatted_alerts.append({
                'title': alert.title,
                'message': alert.message or '',
                'severity': alert.severity,
                'severity_badge_color': alert_colors['badge_bg'],
                'severity_text_color': alert_colors['badge_text'],
                'metric_name': alert.metric_name,
                'metric_value_formatted': format_metric_value(alert.metric_value),
                'threshold_value_formatted': format_metric_value(alert.threshold_value),
            })

        unsubscribe_link = unsubscribe_url(user, 'alerts')

        eastern = pytz.timezone('US/Eastern')
        # Prepare template context
        context = {
            'user': user,
            'alerts': formatted_alerts,
            'triggered_time': datetime.now(eastern).strftime('%A, %B %d, %Y at %I:%M %p ET'),
            'header_bg_color': colors['header_bg'],
            'header_text_color': colors['header_text'],
            'border_color': colors['border'],
            'dashboard_url': external_url('index'),
            'alert_history_url': external_url('alert_history'),
            'settings_url': external_url('settings_alerts'),
            'unsubscribe_url': unsubscribe_link
        }

        # Send email
        alert_count = len(alerts)
        subject = f"Market Alert: {alerts[0].title}" if alert_count == 1 else f"{alert_count} Market Alerts Triggered"

        send_email(
            to=user.email,
            subject=subject,
            template_html='email/alert_notification.html',
            template_txt='email/alert_notification.txt',
            extra_headers=list_unsubscribe_headers(unsubscribe_link),
            **context
        )

        # Update alert records
        for alert in alerts:
            alert.email_sent = True
            alert.email_sent_at = datetime.utcnow()
        db.session.commit()

        logger.info(f"Alert notification sent to user {user.id}: {alert_count} alert(s)")
        return True

    except Exception as e:
        logger.error(f"Failed to send alert notification to user {user.id}: {str(e)}", exc_info=True)
        return False


def send_pending_alert_notifications():
    """
    Send notifications for all alerts that haven't been emailed yet
    Called by background job or immediately after alert creation

    Returns:
        dict: Summary of emails sent
    """
    # Get all unsent alerts
    pending_alerts = Alert.query.filter_by(email_sent=False).all()

    if not pending_alerts:
        return {'emails_sent': 0, 'alerts_sent': 0}

    # Group by user
    alerts_by_user = {}
    for alert in pending_alerts:
        if alert.user_id not in alerts_by_user:
            alerts_by_user[alert.user_id] = []
        alerts_by_user[alert.user_id].append(alert)

    emails_sent = 0
    total_alerts = 0

    # Send one email per user (batching alerts if multiple)
    for user_id, user_alerts in alerts_by_user.items():
        from models.user import User
        user = User.query.get(user_id)

        if not user:
            logger.warning(f"User {user_id} not found, skipping alerts")
            continue

        # Check if user has alert emails enabled
        if not user.alert_preferences or not user.alert_preferences.alerts_enabled:
            # Mark as sent anyway to avoid re-processing
            for alert in user_alerts:
                alert.email_sent = True
                alert.email_sent_at = datetime.utcnow()
            db.session.commit()
            continue

        if send_alert_notification(user, user_alerts):
            emails_sent += 1
            total_alerts += len(user_alerts)

    return {
        'emails_sent': emails_sent,
        'alerts_sent': total_alerts
    }
