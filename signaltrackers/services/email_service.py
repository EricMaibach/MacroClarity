"""
Email Service

Handles all email sending functionality using Flask-Mail.
Supports both HTML and plain-text email formats.
"""

from flask import render_template
from flask_mail import Message
from extensions import mail
import logging

logger = logging.getLogger(__name__)


def send_email(to, subject, template_html, template_txt, extra_headers=None, **context):
    """
    Send an email using Flask-Mail

    Args:
        to: Recipient email address (string or list)
        subject: Email subject line
        template_html: Path to HTML template (relative to templates/)
        template_txt: Path to plain-text template (relative to templates/)
        extra_headers: Optional dict of additional message headers
        **context: Template variables to pass to the templates

    Returns:
        bool: True if email sent successfully, False otherwise

    Example:
        send_email(
            to='user@example.com',
            subject='Welcome to SignalTrackers',
            template_html='email/welcome.html',
            template_txt='email/welcome.txt',
            username='John'
        )
    """
    try:
        msg = Message(
            subject=subject,
            recipients=[to] if isinstance(to, str) else to,
            extra_headers=extra_headers
        )

        # Render templates with provided context
        msg.html = render_template(template_html, **context)
        msg.body = render_template(template_txt, **context)

        mail.send(msg)
        logger.info(f"Email sent successfully to {to}: {subject}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email to {to}: {str(e)}")
        return False


def send_test_email(to):
    """
    Send a test email to verify email configuration

    Args:
        to: Recipient email address

    Returns:
        bool: True if test email sent successfully, False otherwise
    """
    try:
        return send_email(
            to=to,
            subject='SignalTrackers Email Test',
            template_html='email/test_email.html',
            template_txt='email/test_email.txt',
            recipient_email=to
        )
    except Exception as e:
        logger.error(f"Failed to send test email: {str(e)}")
        return False


def send_alert_email(to, alert_type, alert_data):
    """
    Send an alert notification email

    Args:
        to: Recipient email address
        alert_type: Type of alert (e.g., 'VIX_SPIKE', 'CREDIT_SPREAD')
        alert_data: Dictionary containing alert details

    Returns:
        bool: True if alert email sent successfully, False otherwise
    """
    try:
        subject = f"SignalTrackers Alert: {alert_data.get('title', 'Market Alert')}"

        return send_email(
            to=to,
            subject=subject,
            template_html='email/alert_notification.html',
            template_txt='email/alert_notification.txt',
            alert_type=alert_type,
            alert_data=alert_data
        )
    except Exception as e:
        logger.error(f"Failed to send alert email: {str(e)}")
        return False
