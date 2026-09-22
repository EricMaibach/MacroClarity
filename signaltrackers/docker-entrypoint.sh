#!/bin/bash
# Docker Entrypoint Script
# Runs database migrations before starting the application

set -e

# Set Flask app for migration commands
export FLASK_APP=dashboard:app

echo "Running database migrations..."
flask db upgrade || {
    echo "Migration failed, but continuing (might be first run)..."
}

echo "Starting application..."
# --timeout 600: the market-synthesis route (/api/market-synthesis) runs the
# briefing tool loop synchronously — up to 6 Anthropic iterations on a model
# whose turns can run for minutes with thinking always on. The old 120s ceiling
# was sized for a no-thinking model and would kill the worker mid-request.
# This is a ceiling, not a target; the real fix is moving that call off the
# request path.
exec gunicorn -w 1 --preload -b 0.0.0.0:5000 --timeout 600 dashboard:app
