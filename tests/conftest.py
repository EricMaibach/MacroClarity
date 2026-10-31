"""Suite-wide test setup."""

import os

# Point the app at a private in-memory database before anything imports
# `config` or `dashboard` (#497). Config.SQLALCHEMY_DATABASE_URI is read at
# class-definition time, and dashboard runs db.create_all() at import, so
# setting the URI on app.config afterwards does nothing: tests used to
# create_all/drop_all the checkout's real data/signaltrackers.db.
# Unconditional on purpose: an exported DATABASE_URL (shell, container) must
# not win, or the suite would run against that database instead.
# Flask-SQLAlchemy gives an in-memory SQLite URL a StaticPool, so every
# connection shares the same database.
os.environ['DATABASE_URL'] = 'sqlite://'

# Importing dashboard starts the scheduler. Without this, a suite run on a
# weekend after a missed Friday review would schedule a real catch-up AI run
# two minutes later (US-490.1).
os.environ.setdefault('WEEKLY_REVIEW_CATCH_UP', 'false')
