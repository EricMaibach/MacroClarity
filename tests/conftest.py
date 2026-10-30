"""Suite-wide test setup."""

import os

# Importing dashboard starts the scheduler. Without this, a suite run on a
# weekend after a missed Friday review would schedule a real catch-up AI run
# two minutes later (US-490.1).
os.environ.setdefault('WEEKLY_REVIEW_CATCH_UP', 'false')
