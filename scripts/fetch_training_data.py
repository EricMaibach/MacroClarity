#!/usr/bin/env python3
"""Fetch quarterly report data for the past 5 quarters for ML training.

Usage:
    python3 scripts/fetch_training_data.py

Output is saved to signaltrackers/data/quarterly_reports/ as JSONL files.
"""

import os
import sys

# Add signaltrackers to path so we can import the pipeline
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'signaltrackers'))

from sector_tone_pipeline import fetch_quarterly_reports

QUARTERS = [
    ("Q1", 2025),
    ("Q2", 2025),
    ("Q3", 2025),
    ("Q4", 2025),
    ("Q1", 2026),
]


def main():
    total = 0
    for quarter, year in QUARTERS:
        print(f"\n{'='*60}")
        print(f"Fetching {quarter} {year}...")
        print(f"{'='*60}")
        reports = fetch_quarterly_reports(quarter, year)
        count = len(reports)
        total += count
        print(f"  -> {count} reports saved")

    print(f"\nDone. {total} total reports across {len(QUARTERS)} quarters.")


if __name__ == "__main__":
    main()
