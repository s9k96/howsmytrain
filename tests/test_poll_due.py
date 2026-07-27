"""Due-window logic for scripts/poll_due.py.

_due_now is pure (dicts in, train numbers out), so these run without a DB or
any network access.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from poll_due import _due_now

NOW = datetime(2026, 7, 26, 22, 30)  # 22:30 IST


def _train(number, arrival):
    return {"train_number": number, "scheduled_arrival": arrival}


def test_train_arriving_now_is_due():
    trains = [_train("12584", "22:25:00")]  # arrived 5 min ago
    assert _due_now(trains, NOW, set()) == ["12584"]


def test_train_arriving_much_later_is_not_due():
    trains = [_train("12951", "08:32:00")]  # 14 hours away
    assert _due_now(trains, NOW, set()) == []


def test_already_polled_today_is_skipped():
    # One reading per journey_date is all daily_delays needs -- don't spend a
    # second API request on a train we already have today.
    trains = [_train("12584", "22:25:00")]
    assert _due_now(trains, NOW, {"12584"}) == []


def test_unknown_schedule_is_polled_to_learn_it():
    # Bootstrap: a train we've never polled has no scheduled_arrival yet.
    trains = [_train("99999", None)]
    assert _due_now(trains, NOW, set()) == ["99999"]


def test_late_train_still_due_inside_the_after_window():
    # Scheduled 21:30, now 22:30 -- an hour past, still running if delayed.
    assert _due_now([_train("05580", "21:30:00")], NOW, set()) == ["05580"]


def test_well_past_arrival_is_not_due():
    # 4 hours past: RailRadar would return the NEXT run (not-started,
    # delay=0), which would be recorded as a fake on-time.
    assert _due_now([_train("05580", "18:30:00")], NOW, set()) == []


# ---- run days -------------------------------------------------------------
# NOW is Sunday 2026-07-26 22:30 IST.

def _weekly(number, arrival, run_days, offset=0):
    return {"train_number": number, "scheduled_arrival": arrival,
            "run_days": run_days, "arrival_day_offset": offset}


def test_train_not_running_today_is_skipped():
    # 13066 runs Saturdays only; today is Sunday.
    assert _due_now([_weekly("13066", "22:25:00", ["sat"])], NOW, set()) == []


def test_train_running_today_is_due():
    assert _due_now([_weekly("12584", "22:25:00", ["tue", "thu", "fri", "sun"])], NOW, set()) == ["12584"]


def test_overnight_train_maps_arrival_back_to_departure_day():
    # Departs Saturday, arrives Sunday (offset 1). We poll on the ARRIVAL day,
    # so a Sunday poll must check Saturday against run_days.
    assert _due_now([_weekly("13066", "22:25:00", ["sat"], offset=1)], NOW, set()) == ["13066"]


def test_overnight_train_not_due_when_departure_day_excluded():
    # Same offset, but it departs Sundays -- so it arrives Monday, not today.
    assert _due_now([_weekly("13066", "22:25:00", ["sun"], offset=1)], NOW, set()) == []


def test_unknown_run_days_still_polls():
    # Never polled -> we don't know its days yet; the poll is what teaches us.
    assert _due_now([_weekly("99999", "22:25:00", None)], NOW, set()) == ["99999"]
