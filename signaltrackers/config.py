"""
Flask Application Configuration

Environment-based configuration for the SignalTrackers dashboard.
"""

import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).parent


class Config:
    """Base configuration."""

    # Flask
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-change-in-production'

    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        f'sqlite:///{BASE_DIR / "data" / "signaltrackers.db"}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # System AI Keys (for scheduled briefings)
    SYSTEM_AI_PROVIDER = os.environ.get('AI_PROVIDER', 'openai').lower()
    SYSTEM_OPENAI_KEY = os.environ.get('OPENAI_API_KEY')
    SYSTEM_ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')
    ANTHROPIC_EFFORT = os.environ.get('ANTHROPIC_EFFORT', 'medium').lower()

    # Anthropic model selection. This module is the single source of truth for
    # model IDs — see the module-level aliases below for code that runs outside
    # a Flask app context.
    ANTHROPIC_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-opus-4-6')
    ANTHROPIC_CHATBOT_MODEL = os.environ.get('ANTHROPIC_CHATBOT_MODEL', 'claude-sonnet-4-6')

    # Invite-only registration (empty string disables the gate)
    INVITE_CODE = os.environ.get('INVITE_CODE', '')

    # Site mode: 'invite_only', 'paid', or 'open'
    # Controls access model and user-facing rate limit messaging
    SITE_MODE = os.environ.get('SITE_MODE', 'invite_only')

    # Session
    SESSION_COOKIE_SECURE = os.environ.get('FLASK_ENV') == 'production'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # Rate limiting
    RATELIMIT_STORAGE_URI = 'memory://'
    RATELIMIT_DEFAULT = '100 per minute'

    # Anonymous session AI limits (lifetime per session)
    ANON_SESSION_LIMIT_CHATBOT = int(os.environ.get('ANON_SESSION_LIMIT_CHATBOT', 5))
    ANON_SESSION_LIMIT_ANALYSIS = int(os.environ.get('ANON_SESSION_LIMIT_ANALYSIS', 2))

    # Global daily anonymous AI cap (total across all sessions, resets midnight UTC)
    ANON_GLOBAL_DAILY_LIMIT = int(os.environ.get('ANON_GLOBAL_DAILY_LIMIT', 100))

    # Subscriber daily AI limits (per paid user, resets midnight UTC)
    # Supports both SUBSCRIBER_DAILY_LIMIT_* and legacy REGISTERED_DAILY_LIMIT_* env vars
    SUBSCRIBER_DAILY_LIMIT_CHATBOT = int(
        os.environ.get('SUBSCRIBER_DAILY_LIMIT_CHATBOT',
                       os.environ.get('REGISTERED_DAILY_LIMIT_CHATBOT', 25))
    )
    SUBSCRIBER_DAILY_LIMIT_ANALYSIS = int(
        os.environ.get('SUBSCRIBER_DAILY_LIMIT_ANALYSIS',
                       os.environ.get('REGISTERED_DAILY_LIMIT_ANALYSIS', 5))
    )

    # Optional services
    FRED_API_KEY = os.environ.get('FRED_API_KEY')
    TAVILY_API_KEY = os.environ.get('TAVILY_API_KEY')

    # Stripe billing
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
    STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
    STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID', '')

    # Email configuration (Flask-Mail)
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp-relay.brevo.com')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'True').lower() in ('true', '1', 't')
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'False').lower() in ('true', '1', 't')
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', 'SignalTrackers <briefings@signaltrackers.com>')

    # Base URL for generating external links (used in email notifications)
    BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')


class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True


class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False

    # Require SECRET_KEY in production
    @property
    def SECRET_KEY(self):
        key = os.environ.get('SECRET_KEY')
        if not key:
            raise ValueError('SECRET_KEY environment variable required in production')
        return key


class TestingConfig(Config):
    """Testing configuration."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'


# ---------------------------------------------------------------------------
# Module-level model aliases
# ---------------------------------------------------------------------------
# ai_summary.py (scheduled briefings) and news_pipeline.py are not guaranteed to
# run inside a Flask app context, so they cannot read current_app.config. They
# import these names instead of redefining the defaults, which keeps config.py
# the only place a model ID literal appears.
ANTHROPIC_MODEL = Config.ANTHROPIC_MODEL
ANTHROPIC_CHATBOT_MODEL = Config.ANTHROPIC_CHATBOT_MODEL


config_by_name = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}


def get_config():
    """Get configuration based on FLASK_ENV."""
    env = os.environ.get('FLASK_ENV', 'development')
    return config_by_name.get(env, DevelopmentConfig)
