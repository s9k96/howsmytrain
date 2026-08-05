"""Due-window logic for scripts/poll_due.py.

_due_now is pure (dicts in, train numbers out), so these run without a DB or
any network access.

The third argument mirrors store.last_readings: train_number -> what we last
saw and when we have looked. `seen()` below builds one so the tests read as
"we last polled it at T" rather than as a literal payload.

Dedup is by poll time, not by journey identity -- see store.last_readings.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from poll_due import _due_now

NOW = datetime(2026, 7, 26, 22, 30)  # 22:30 IST


def seen(at, delay=None, status="completed", times=None):
    """One store.last_readings entry. Defaults to a finished run, which is the
    boring case: only a *running* reading moves the window."""
    return {"at": at, "delay_minutes": delay, "status": status,
            "times": times if times is not None else [at]}


def _train(number, arrival):
    return {"train_number": number, "scheduled_arrival": arrival}


def test_train_arriving_now_is_due():
    trains = [_train("12584", "22:25:00")]  # arrived 5 min ago
    assert _due_now(trains, NOW, {}) == ["12584"]


def test_train_arriving_much_later_is_not_due():
    trains = [_train("12951", "08:32:00")]  # 14 hours away
    assert _due_now(trains, NOW, {}) == []


def test_already_read_in_this_window_is_skipped():
    # One reading per window is all daily_delays needs -- don't spend a second
    # API request on a train we just read. Window here is 22:15..23:55.
    trains = [_train("12584", "22:25:00")]
    assert _due_now(trains, NOW, {"12584": seen(datetime(2026, 7, 26, 22, 20))}) == []


def test_read_before_this_window_opened_does_not_suppress_it():
    # Yesterday's reading (or one from before the window) says nothing about
    # today's run, so the train is still due.
    trains = [_train("12584", "22:25:00")]
    assert _due_now(trains, NOW, {"12584": seen(datetime(2026, 7, 25, 22, 20))}) == ["12584"]
    assert _due_now(trains, NOW, {"12584": seen(datetime(2026, 7, 26, 22, 14))}) == ["12584"]


def test_unknown_schedule_is_polled_to_learn_it():
    # Bootstrap: a train we've never polled has no scheduled_arrival yet.
    trains = [_train("99999", None)]
    assert _due_now(trains, NOW, {}) == ["99999"]


def test_unknown_schedule_is_not_polled_every_tick():
    # No window to dedup against, so it falls back to a 12 h guard -- otherwise
    # a train whose schedule we can't learn burns the budget on every run.
    trains = [_train("99999", None)]
    assert _due_now(trains, NOW, {"99999": seen(NOW - timedelta(hours=1))}) == []
    assert _due_now(trains, NOW, {"99999": seen(NOW - timedelta(hours=13))}) == ["99999"]


def test_late_train_still_due_inside_the_after_window():
    # Scheduled 21:30, now 22:30 -- an hour past, still running if delayed.
    assert _due_now([_train("05580", "21:30:00")], NOW, {}) == ["05580"]


def test_well_past_arrival_is_not_due():
    # 4 hours past: RailRadar would return the NEXT run (not-started,
    # delay=0), which would be recorded as a fake on-time.
    assert _due_now([_train("05580", "18:30:00")], NOW, {}) == []


# ---- run days -------------------------------------------------------------
# NOW is Sunday 2026-07-26 22:30 IST.

def _weekly(number, arrival, run_days, offset=0):
    return {"train_number": number, "scheduled_arrival": arrival,
            "run_days": run_days, "arrival_day_offset": offset}


def test_train_not_running_today_is_skipped():
    # 13066 runs Saturdays only; today is Sunday.
    assert _due_now([_weekly("13066", "22:25:00", ["sat"])], NOW, {}) == []


def test_train_running_today_is_due():
    assert _due_now([_weekly("12584", "22:25:00", ["tue", "thu", "fri", "sun"])], NOW, {}) == ["12584"]


def test_overnight_train_maps_arrival_back_to_departure_day():
    # Departs Saturday, arrives Sunday (offset 1). We poll on the ARRIVAL day,
    # so a Sunday poll must check Saturday against run_days.
    assert _due_now([_weekly("13066", "22:25:00", ["sat"], offset=1)], NOW, {}) == ["13066"]


def test_overnight_train_not_due_when_departure_day_excluded():
    # Same offset, but it departs Sundays -- so it arrives Monday, not today.
    assert _due_now([_weekly("13066", "22:25:00", ["sun"], offset=1)], NOW, {}) == []


def test_unknown_run_days_still_polls():
    # Never polled -> we don't know its days yet; the poll is what teaches us.
    assert _due_now([_weekly("99999", "22:25:00", None)], NOW, {}) == ["99999"]


# ---- runs longer than a day -----------------------------------------------
# The regression this dedup exists for. 12621 is scheduled MAS 22:00 -> NDLS
# 06:30 two days later (32h30m), so two of its runs are always in the air. On
# 2026-07-30 RailRadar returned the later one, which filed under a departure
# date the old `now - arrival_day_offset` prediction never guessed -- so the key
# never matched and the train was re-polled on all seven ticks of its window.
#
# Poll time is immune to that: whichever run comes back, the read happened.

def _long_haul(number="12621", arrival="06:30:00"):
    return {"train_number": number, "scheduled_arrival": arrival,
            "run_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "arrival_day_offset": 2}


def test_long_run_is_read_once_per_window_whatever_run_came_back():
    # Window around a 06:30 arrival is 06:20..08:00.
    opened = datetime(2026, 7, 30, 6, 30)
    for tick in (6, 31), (6, 45), (7, 0), (7, 15), (7, 30), (7, 45):
        now = datetime(2026, 7, 30, *tick)
        assert _due_now([_long_haul()], now, {"12621": seen(opened)}) == [], tick


def test_long_run_is_still_due_on_the_first_tick_of_its_window():
    assert _due_now([_long_haul()], datetime(2026, 7, 30, 6, 30), {}) == ["12621"]


def test_long_run_is_due_again_the_next_day():
    # Yesterday's reading must not suppress today's run.
    assert _due_now([_long_haul()], datetime(2026, 7, 31, 6, 30),
                    {"12621": seen(datetime(2026, 7, 30, 6, 30))}) == ["12621"]


def test_a_failed_poll_is_retried_on_the_next_tick():
    # A failed poll writes no row, so last_polled has no entry and the window
    # is what retries it -- the behaviour that recovered 12049 on 2026-07-29.
    assert _due_now([_train("12049", "19:30:00")], datetime(2026, 7, 29, 19, 45), {}) == ["12049"]


# ---- midnight -------------------------------------------------------------
# 20978 arrives 23:58, so its window runs 23:48..01:28 -- across midnight.
# It used to be clipped at 23:59 with the start slid back, which read the
# train up to 45 min early. The clip existed because journey_date was the poll
# date; it comes from RailRadar's startDate now and dedup keys on poll time,
# so neither side cares which date a window ends on.

def _late_night():
    return [_train("20978", "23:58:00")]


def test_late_night_train_not_due_before_its_window_opens():
    assert _due_now(_late_night(), datetime(2026, 7, 26, 23, 0), {}) == []
    assert _due_now(_late_night(), datetime(2026, 7, 26, 23, 47), {}) == []


def test_late_night_window_opens_at_the_real_time_not_45_min_early():
    assert _due_now(_late_night(), datetime(2026, 7, 26, 23, 50), {}) == ["20978"]


def test_late_night_window_carries_on_past_midnight():
    # The half of the window that lives on the next date is now reachable.
    assert _due_now(_late_night(), datetime(2026, 7, 27, 0, 20), {}) == ["20978"]
    assert _due_now(_late_night(), datetime(2026, 7, 27, 1, 25), {}) == ["20978"]
    assert _due_now(_late_night(), datetime(2026, 7, 27, 1, 35), {}) == []


def test_wrapping_does_not_repoll_what_was_read_before_midnight():
    # The whole reason the clip existed. A reading taken at 23:50 must still
    # suppress the 00:20 tick, or the wrap costs a request every tick.
    read = seen(datetime(2026, 7, 26, 23, 50))
    assert _due_now(_late_night(), datetime(2026, 7, 27, 0, 20), {"20978": read}) == []


# ---- following a late train -----------------------------------------------
# The window used to be anchored to a scheduled time a late train was never
# going to make, so it shut 90 min after a moment the train was still hours
# from. The only reading we ever took was a floor: 22 of the first 239
# journeys, 05580 recorded at "+630" with 630 minutes still left to run.
#
# A reading of "running, N minutes down" moves the window to where the train
# is now expected. No new dedup is needed -- the earlier poll happened before
# the shifted window opens, so the train is simply due again.

def _shatabdi():
    return [_train("12005", "21:15:00")]


def test_a_running_late_train_is_read_again_near_its_real_arrival():
    # Read at 21:20 as running 3 h down -> real arrival ~00:15, window 00:05..
    late = seen(datetime(2026, 7, 26, 21, 20), delay=180, status="running")
    assert _due_now(_shatabdi(), datetime(2026, 7, 26, 22, 30), {"12005": late}) == []
    assert _due_now(_shatabdi(), datetime(2026, 7, 27, 0, 10), {"12005": late}) == ["12005"]


def test_a_finished_run_is_not_chased():
    # `completed` is final. Nothing left to learn, so no second request.
    done = seen(datetime(2026, 7, 26, 21, 20), delay=180, status="completed")
    assert _due_now(_shatabdi(), datetime(2026, 7, 27, 0, 10), {"12005": done}) == []


def test_an_on_time_running_read_does_not_shift_anything():
    ok = seen(datetime(2026, 7, 26, 21, 20), delay=0, status="running")
    assert _due_now(_shatabdi(), datetime(2026, 7, 27, 0, 10), {"12005": ok}) == []


def test_a_delay_the_window_already_covers_buys_no_second_request():
    # AFTER is 90 min, so an hour-late train arrives inside the original
    # window and a follow-up would learn nothing. Shifting on *any* delay
    # measured out at 42 requests/day against a 50/day cap; thresholding at
    # AFTER puts it back to 32.
    hour_late = seen(datetime(2026, 7, 26, 21, 10), delay=60, status="running")
    assert _due_now(_shatabdi(), datetime(2026, 7, 26, 22, 30), {"12005": hour_late}) == []
    # Just past the threshold, it is worth going back for.
    very_late = seen(datetime(2026, 7, 26, 21, 10), delay=100, status="running")
    assert _due_now(_shatabdi(), datetime(2026, 7, 26, 22, 50), {"12005": very_late}) == ["12005"]


def test_the_shift_is_capped_so_a_wild_delay_cannot_chase_for_days():
    # A garbled payload could report a 30 h delay. Uncapped that would aim the
    # window at 03:15 two days out; MAX_SHIFT (12 h) pins it to 09:15 instead.
    # Both instants below sit outside the ordinary 21:05..22:45 daily window,
    # so only the shift can be putting the train in play.
    absurd = seen(datetime(2026, 7, 26, 21, 20), delay=1800, status="running")
    assert _due_now(_shatabdi(), datetime(2026, 7, 27, 9, 10), {"12005": absurd}) == ["12005"]
    assert _due_now(_shatabdi(), datetime(2026, 7, 28, 3, 20), {"12005": absurd}) == []


def test_no_more_than_three_polls_a_day_however_late_it_gets():
    # A train that keeps slipping would otherwise shift its own window forever,
    # one request each time, and eat the 50/day budget alone.
    day = datetime(2026, 7, 26)
    three = seen(datetime(2026, 7, 26, 21, 20), delay=180, status="running",
                 times=[day.replace(hour=h, minute=20) for h in (19, 20, 21)])
    assert _due_now(_shatabdi(), datetime(2026, 7, 26, 23, 55), {"12005": three}) == []
    two = seen(datetime(2026, 7, 26, 21, 20), delay=180, status="running",
               times=[day.replace(hour=h, minute=20) for h in (20, 21)])
    assert _due_now(_shatabdi(), datetime(2026, 7, 27, 0, 10), {"12005": two}) == ["12005"]


def test_the_unshifted_window_still_works_while_it_is_open():
    # Late but not yet past its scheduled slot: both windows are fair moments
    # to read it, so the shift must not create a quiet gap in between.
    late = seen(datetime(2026, 7, 26, 20, 0), delay=180, status="running")
    assert _due_now(_shatabdi(), datetime(2026, 7, 26, 21, 30), {"12005": late}) == ["12005"]


def test_ordinary_window_is_unchanged_by_the_midnight_clip():
    # Nowhere near midnight: still exactly BEFORE..AFTER around arrival.
    assert _due_now([_train("12951", "08:32:00")], datetime(2026, 7, 26, 8, 21), {}) == []
    assert _due_now([_train("12951", "08:32:00")], datetime(2026, 7, 26, 8, 22), {}) == ["12951"]
    assert _due_now([_train("12951", "08:32:00")], datetime(2026, 7, 26, 10, 2), {}) == ["12951"]
    assert _due_now([_train("12951", "08:32:00")], datetime(2026, 7, 26, 10, 3), {}) == []
