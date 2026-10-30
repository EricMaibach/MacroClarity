"""
Email Links

Builds the absolute URLs placed in outbound emails, including signed
unsubscribe links (bug #496).

Links are built from the route table rather than f-strings so an email and the
route it points at cannot drift apart. That drift is what broke unsubscribe in
both the briefing and alert emails.

Emails are sent from scheduled jobs that have an app context but no request
context, where ``url_for(_external=True)`` cannot work without SERVER_NAME.
``external_url`` binds the URL map to ``BASE_URL`` directly instead.

Unsubscribe tokens are signed with SECRET_KEY, salted per email type so a
briefing link cannot unsubscribe a user from alerts. They do not expire: a link
in an old email must keep working.
"""

from urllib.parse import urlsplit

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

UNSUBSCRIBE_ENDPOINT = 'unsubscribe'

# email_type -> AlertPreference flag it turns off, and the label shown to users
UNSUBSCRIBE_TYPES = {
    'briefing': {'preference': 'daily_briefing_enabled', 'label': 'daily briefing'},
    'alerts': {'preference': 'alerts_enabled', 'label': 'alert notification'},
}


def external_url(endpoint, **values):
    """Build an absolute URL for ``endpoint`` rooted at the BASE_URL config."""
    base = urlsplit(current_app.config['BASE_URL'])
    adapter = current_app.url_map.bind(
        base.netloc,
        script_name=base.path or '/',
        url_scheme=base.scheme,
    )
    return adapter.build(endpoint, values, force_external=True)


def _serializer(email_type):
    if email_type not in UNSUBSCRIBE_TYPES:
        raise ValueError(f'Unknown unsubscribe email type: {email_type!r}')
    return URLSafeSerializer(
        current_app.config['SECRET_KEY'], salt=f'unsubscribe-{email_type}'
    )


def make_unsubscribe_token(user_id, email_type):
    return _serializer(email_type).dumps(str(user_id))


def load_unsubscribe_token(token, email_type):
    """Return the user ID a token was issued for, or None if it is invalid."""
    try:
        user_id = _serializer(email_type).loads(token)
    except BadSignature:
        return None
    return user_id if isinstance(user_id, str) else None


def unsubscribe_url(user, email_type):
    return external_url(
        UNSUBSCRIBE_ENDPOINT,
        email_type=email_type,
        token=make_unsubscribe_token(user.id, email_type),
    )


def list_unsubscribe_headers(url):
    """RFC 8058 one-click headers; mail clients POST to ``url`` directly."""
    return {
        'List-Unsubscribe': f'<{url}>',
        'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
    }
