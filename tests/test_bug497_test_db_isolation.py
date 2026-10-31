"""
Bug #497: the test suite must never touch the checkout's real database.

tests/conftest.py sets DATABASE_URL to an in-memory SQLite URL before the app
is imported. These tests check that the app's engine really is that database,
and that an exported DATABASE_URL cannot redirect the suite to a real file.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / 'signaltrackers'))

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from dashboard import app
from extensions import db


def test_conftest_sets_in_memory_database_url():
    assert os.environ['DATABASE_URL'] == 'sqlite://'


def test_app_engine_is_in_memory():
    with app.app_context():
        url = db.engine.url
        assert url.drivername == 'sqlite'
        assert url.database in (None, '', ':memory:')
        assert 'signaltrackers.db' not in str(url)


def test_in_memory_engine_shares_one_connection():
    # Without StaticPool every pooled connection is a separate empty
    # database, and tables created on one vanish on the next.
    with app.app_context():
        assert isinstance(db.engine.pool, StaticPool)


def test_import_time_create_all_built_the_schema_in_memory():
    with app.app_context():
        tables = set(sa.inspect(db.engine).get_table_names())
    assert {'users', 'alert_preferences'} <= tables


def test_exported_database_url_does_not_win(tmp_path):
    """An exported DATABASE_URL must not redirect the suite to a real file.

    Runs the alert model tests (which drop and re-create every table) in a
    child pytest with DATABASE_URL pointing at a sentinel path. The sentinel
    must never be created.
    """
    sentinel = tmp_path / 'sentinel.db'
    env = dict(os.environ, DATABASE_URL=f'sqlite:///{sentinel}')
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests/test_alert_models.py',
         '-q', '-p', 'no:cacheprovider', '-x'],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert not sentinel.exists()
