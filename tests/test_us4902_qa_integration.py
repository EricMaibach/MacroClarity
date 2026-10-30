"""
QA additions for US-490.2 (#492).

Negative cases for the "released this week" label on weekly claims. The
Engineer's test covers only the positive case, and a mutation that labels
every claims reading as released this week passed the whole suite. That label
is what lets the model call a reading this week's news, so a stale reading
must never carry it.
"""

import sys
from datetime import date
from pathlib import Path

SIGNALTRACKERS_DIR = Path(__file__).parent.parent / 'signaltrackers'
sys.path.insert(0, str(SIGNALTRACKERS_DIR))

import pandas as pd

import weekly_review

WEEK_START = date(2026, 10, 26)
WEEK_END = date(2026, 10, 30)


def _write_claims(tmp_path, rows):
    pd.DataFrame(rows, columns=['date', 'initial_claims']).to_csv(
        tmp_path / 'initial_claims.csv', index=False)


def _claims_line(block):
    return next(x for x in block.splitlines() if x.startswith('- Initial jobless claims'))


class TestClaimsReleaseLabel:
    def test_last_weeks_release_is_not_this_weeks_news(self, tmp_path):
        # Week ending Sat Oct 17 was published Thu Oct 22, the week before.
        _write_claims(tmp_path, [('2026-10-10', 230000), ('2026-10-17', 220000)])
        line = _claims_line(weekly_review.macro_readings(WEEK_START, WEEK_END, tmp_path))
        assert 'released this week' not in line
        assert '[week ending Oct 17]' in line

    def test_not_released_yet_when_run_is_before_thursday(self, tmp_path):
        # Week ending Oct 24 is published Thu Oct 29; a Wednesday regenerate
        # must not present it as already released.
        _write_claims(tmp_path, [('2026-10-17', 220000), ('2026-10-24', 210000)])
        line = _claims_line(weekly_review.macro_readings(WEEK_START, date(2026, 10, 28), tmp_path))
        assert 'released this week' not in line

    def test_released_on_thursday_boundary(self, tmp_path):
        _write_claims(tmp_path, [('2026-10-17', 220000), ('2026-10-24', 210000)])
        line = _claims_line(weekly_review.macro_readings(WEEK_START, date(2026, 10, 29), tmp_path))
        assert line.endswith('[released this week]')
