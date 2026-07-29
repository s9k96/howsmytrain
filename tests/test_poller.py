"""
journey_date comes from RailRadar's startDate, not from the clock.

Everything downstream keys on it -- the daily_delays view, the once-per-journey
dedup in poll_due.py -- so this is the one extractor worth pinning.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.poller import _extract_start_date


def test_start_date_is_taken_verbatim():
    assert _extract_start_date({"startDate": "2026-07-29"}) == "2026-07-29"


def test_start_date_tolerates_a_full_timestamp():
    # Documented as a plain date, but a timestamp would otherwise be written
    # into a DATE column and rejected.
    assert _extract_start_date({"startDate": "2026-07-29T17:00:00+05:30"}) == "2026-07-29"


def test_missing_start_date_falls_back_to_the_caller_default():
    # None means insert_poll uses today_ist() -- the old behaviour, so a
    # payload without the field degrades instead of failing.
    assert _extract_start_date({}) is None
    assert _extract_start_date({"startDate": None}) is None
