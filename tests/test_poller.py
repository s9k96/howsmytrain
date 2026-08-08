"""
The two extractors everything downstream keys on.

journey_date comes from RailRadar's startDate, not from the clock -- the
daily_delays view and the once-per-run dedup in poll_due.py both hang off it.
The arrival delay comes from the end of the route rather than the top-level
delayMinutes, and that choice is the difference between "how late it arrived"
and "how late it was when we looked".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.poller import _extract_arrival_delay, _extract_start_date


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


# ---- arrival delay --------------------------------------------------------
# A real payload, trimmed: 12584 on 2026-07-26, 58 minutes down at the station
# it had reached while projected into Lucknow 64 minutes late. The old number
# was the 58.
_12584 = {
    "delayMinutes": 58,
    "route": [
        {"sequence": 1, "stationCode": "ANVT", "delayDeparture": 55},
        {"sequence": 2, "stationCode": "DAN", "delayArrival": 58},
        {"sequence": 3, "stationCode": "LJN", "delayArrival": 64,
         "scheduledArrival": "2026-07-26T22:25:00+05:30",
         "actualArrival": "2026-07-26T23:29:00+05:30"},
    ],
}


def test_arrival_delay_comes_from_the_destination_not_the_current_position():
    assert _extract_arrival_delay(_12584) == 64      # not the 58 beside it


def test_arrival_delay_of_zero_survives():
    # 0 is the most common value and the most important one -- `or` on it
    # would turn every punctual train into "no reading".
    assert _extract_arrival_delay({"route": [{"delayArrival": 0}]}) == 0


def test_a_payload_without_a_usable_route_yields_nothing():
    # Falls back to the position delay downstream rather than failing the poll.
    assert _extract_arrival_delay({"delayMinutes": 12}) is None
    assert _extract_arrival_delay({"route": []}) is None
    assert _extract_arrival_delay({"route": [{"stationCode": "LJN"}]}) is None
    assert _extract_arrival_delay({"route": [{"delayArrival": "late"}]}) is None
