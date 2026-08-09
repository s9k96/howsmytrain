#!/usr/bin/env python3
"""
Poll only the trains that are DUE right now -- i.e. close to their scheduled
arrival, when delay_minutes is effectively final.

Why not one cron per train: arrival times shift with the timetable, and
GitHub Actions' scheduled runs drift 5-20 minutes under load. So instead the
workflow fires on a fixed interval and this script decides what's due, using
the scheduled_arrival that app/poller.py refreshes on every poll. The
schedule maintains itself.

Budget: RailRadar free tier is 50 requests/day and each train costs one
request per poll. Polling each train once near arrival = one request per
train per day.

    python scripts/poll_due.py            # poll what's due
    python scripts/poll_due.py --all      # poll everything (bootstrap/manual)
    python scripts/poll_due.py --dry-run  # show what would be polled
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, store
from app.poller import poll_all

# How wide a window around scheduled arrival counts as "due". Generous on the
# late side: a delayed train is still running (and still reports a useful
# delay) well past its scheduled arrival, and Actions' cron drifts.
BEFORE = timedelta(minutes=10)
AFTER = timedelta(minutes=90)

# How far a known delay may push the window. 12 h covers every floor reading
# on record (the worst was 05580 at 630 min) while keeping the search to
# today-or-yesterday in _window, and stops a nonsense delay from a bad payload
# sending the poller chasing a train days out.
MAX_SHIFT = 12 * 60

# Hard ceiling per train per day, whatever the delay does. Normal is one; this
# leaves room for two follow-ups on a badly delayed run and no more, so a
# train that slips repeatedly cannot eat the 50/day budget on its own.
MAX_POLLS_PER_DAY = 3

# How many polls a follow-up may spend on one late run before giving up.
#
# A follow-up can come back describing a *different* departure: for a service
# with two runs in the air the endpoint picks, and it does not pick again in
# our favour fifteen minutes later. Asking again is therefore speculative, and
# replayed over 2026-08-01..07 it costs more than the budget has:
#
#     old code                  34.6/day  peak 38
#     1 attempt (this)          37.4/day  peak 44
#     2 attempts                40.1/day  peak 51   <- over the 50/day cap
#
# and the extra spend landed on 12615, 12423 and 15274 -- precisely the trains
# whose arriving run the endpoint never returns, so it buys nothing. One
# attempt still fixes the case that motivated the guard: a substitution seen
# *before* the shifted window opens no longer cancels the chase, which is where
# the +2.8/day goes.
MAX_CHASE_ATTEMPTS = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # matches datetime.weekday()


def _runs_today(train: dict, now: datetime) -> bool:
    """
    Is this train mid-journey right now, given the days it departs its source?

    run_days are DEPARTURE days, but we poll near arrival -- so for a train
    that arrives the day after it departs (arrival_day_offset=1), a Friday
    arrival belongs to a Thursday departure. Unknown run_days means poll: the
    response is what teaches us them.
    """
    run_days = train.get("run_days")
    if not run_days:
        return True
    offset = train.get("arrival_day_offset") or 0
    departure_day = (now - timedelta(days=offset)).weekday()
    return DAYS[departure_day] in run_days


def _window(arrival: str, now: datetime, shift: int = 0):
    """
    The polling window containing `now`, or None if there isn't one.

    `shift` moves the whole window later by that many minutes: the delay the
    train was last seen carrying. Without it the window is anchored to a
    scheduled time a late train was never going to make, so it closes 90
    minutes after a time the train is still hours away from -- and the only
    reading we ever take is a floor. 22 of the first 239 journeys were
    recorded that way, including 05580 at "+630" while it still had 630
    minutes left to run. Shifting means the follow-up lands near the arrival
    that is actually going to happen.

    No new dedup is needed for that follow-up. The rule is "have we read this
    train since its window opened?", and the earlier poll necessarily happened
    before the shifted window opens, so the train simply becomes due again.

    The window may now cross midnight, which it used to refuse to do. That
    clip existed because `journey_date` was the poll date, so a poll at 00:10
    filed under a day where dedup couldn't see it. `journey_date` now comes
    from RailRadar's `startDate` and dedup keys on poll time, so neither side
    cares what date a window ends on. Yesterday's arrival is checked as well
    as today's, which is what a wrapped or shifted window needs.
    """
    hh, mm, *_ = str(arrival).split(":")
    for days in (0, -1):
        target = (now + timedelta(days=days)).replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0
        ) + timedelta(minutes=shift)
        start, end = target - BEFORE, target + AFTER
        if start <= now <= end:
            return start, end
    return None


def _reading_delay(reading: dict) -> int | None:
    """
    How late this reading says the train will be AT ITS DESTINATION.

    RailRadar's projected arrival delay when we have it, otherwise the delay
    where the train was last seen. The two differ by however much time the
    train is still going to lose, which for a follow-up is exactly the part
    that matters: shifting by the position delay re-aims at where the train
    already was, shifting by the arrival delay re-aims at where it is going.
    """
    arrival = reading.get("arrival_delay_minutes")
    return arrival if arrival is not None else reading.get("delay_minutes")


def _late_run(seen: dict | None) -> tuple[str, int] | None:
    """
    (journey_date, delay) of a run we are still chasing, or None.

    A run qualifies when its most recent reading has it still running and
    later than the plain window can reach -- AFTER is 90 minutes, so anything
    under that arrives while the original window is open and a second request
    buys nothing.

    Keyed by run rather than by train because the answer to a follow-up is not
    guaranteed to be about the run we asked after: for a service with two runs
    in the air, the endpoint may return the newer one. Reading the newest
    poll alone, that substitution looks like the late train having recovered.
    """
    if not seen:
        return None
    best = None
    for journey_date, reading in seen.get("journeys", {}).items():
        if reading["status"] != "running":
            continue
        delay = _reading_delay(reading)
        if not delay or delay <= AFTER.total_seconds() / 60:
            continue
        if best is None or reading["at"] > best[2]:
            best = (journey_date, delay, reading["at"])
    return (best[0], best[1]) if best else None


def _due_now(trains: list[dict], now: datetime, last: dict[str, dict]) -> list[str]:
    """
    Train numbers whose arrival window contains now and which we have not
    already read during that window.

    Dedup is by poll time, not by journey identity -- see store.last_readings
    for why predicting the journey could not be made to work. One reading per
    window is all the daily stat needs, and a failed poll writes no row, so it
    is retried on the next tick.

    A train last seen *running* and late has its window shifted to where it is
    now expected, which is what turns a floor reading into a real one. That
    costs a second request for that train, so it is capped: only a `running`
    reading shifts anything (a `completed` one is final and there is nothing
    left to chase), the shift is bounded by MAX_SHIFT, and no train is polled
    more than MAX_POLLS_PER_DAY times whatever its delay does.
    """
    due = []
    for t in trains:
        number = t["train_number"]
        if not _runs_today(t, now):
            continue  # doesn't run today -- polling returns the next run, not this one
        seen = last.get(number)
        at = seen["at"] if seen else None
        arrival = t.get("scheduled_arrival")
        if not arrival:
            # Unknown schedule -> one poll to learn it. Compared as an elapsed
            # span rather than a calendar day because the stored timestamps are
            # UTC while `now` is IST, and a train we know nothing about needs
            # one reading a day, not one per tick.
            if at is None or (now - at) > timedelta(hours=12):
                due.append(number)
            continue

        # A budget guard before anything else: a train that keeps slipping
        # would otherwise shift its own window forever, one request each time.
        if seen and sum(1 for x in seen["times"] if x.date() == now.date()) >= MAX_POLLS_PER_DAY:
            continue

        # Only a delay the window cannot already reach is worth a second
        # request. AFTER is 90 minutes, so a train an hour down is covered by
        # the original window and shifting for it buys nothing while costing a
        # call: shifting on *any* delay measured out at 42 requests/day against
        # a 50/day cap, with a peak of 49. Thresholding puts it back to 32.
        chase = _late_run(seen)
        shift = min(chase[1], MAX_SHIFT) if chase else 0

        window = _window(arrival, now, shift)
        following = shift > 0 and window is not None
        # A shifted window can leave the unshifted one still open (the train is
        # late but not yet past its scheduled slot). Either is a fair moment to
        # read it, so fall back rather than going quiet in between.
        if window is None and shift:
            window = _window(arrival, now)
        if window is None:
            continue
        start, _end = window

        if following:
            # Only a reading OF THAT RUN closes the follow-up. A poll that came
            # back describing a different departure answered a different
            # question, and treating it as the answer would quietly drop the
            # late run. Worth one more ask -- but only one, because the endpoint
            # substituting once usually means it has moved on for good.
            answered = seen["journeys"].get(chase[0])
            if answered and answered["at"] >= start:
                continue
            if sum(1 for x in seen["times"] if x >= start) >= MAX_CHASE_ATTEMPTS:
                continue
        elif at is not None and at >= start:
            continue
        due.append(number)
    return due


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="poll every configured train")
    parser.add_argument("--dry-run", action="store_true", help="report without calling the API")
    args = parser.parse_args()

    if not store.enabled():
        db.init_db()

    configured = config.require_train_numbers()
    now = datetime.now(db.IST)

    if args.all:
        due = configured
    else:
        known = {t["train_number"]: t for t in store.list_trains()}
        # A window is at most 100 min wide, so two days of poll history is
        # ample -- it only has to cover the current window plus the 12 h guard
        # on trains whose schedule we haven't learned yet.
        last = store.last_readings((now - timedelta(days=2)).strftime("%Y-%m-%d"))
        # Order by configured list so behaviour is deterministic.
        due = _due_now(
            [known.get(n, {"train_number": n, "scheduled_arrival": None}) for n in configured],
            now,
            last,
        )

    if args.dry_run:
        logging.info("Would poll (%s): %s", now.strftime("%H:%M IST"), ", ".join(due) or "nothing")
        return 0

    if not due:
        # Still record the heartbeat: a run that finds nothing due is the
        # normal case, and it's what distinguishes a healthy quiet period
        # from a dead scheduler.
        store.record_run(0, 0, 0)
        logging.info("Nothing due at %s IST. Exiting without any API calls.", now.strftime("%H:%M"))
        return 0

    logging.info("Due now (%s): %s", now.strftime("%H:%M IST"), ", ".join(due))
    config.require_api_key()
    results = poll_all(due)
    store.record_run(len(due), len(results["ok"]), len(results["failed"]),
                     failure_reason=results["reason"])
    logging.info("Done. ok=%s failed=%s reason=%s",
                 results["ok"], results["failed"], results["reason"])
    return 0 if not results["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
