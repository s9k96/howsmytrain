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

# Shortest window we'll accept after clipping at midnight -- comfortably more
# than the ~15 min trigger interval, so a late-night train still gets a tick.
MIN_WINDOW = timedelta(minutes=45)

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


def _window(arrival: str, now: datetime) -> tuple[datetime, datetime]:
    """
    When a poll of this train would give a near-final delay, on now's date.

    The window never crosses midnight. It can't: `journey_date` is the poll
    date, so a poll at 00:10 for a 23:58 arrival files under the next day --
    where `already_polled` can't see it, so the same journey gets polled
    again on the next tick, and the next. Clipping at 23:59 and sliding the
    start back keeps one journey in one bucket.

    ponytail: costs accuracy on trains arriving near midnight, which get read
    up to ~45 min early. Take `journey_date` from RailRadar's `startDate`
    (see TODO.md) and the window can wrap properly instead.
    """
    hh, mm, *_ = str(arrival).split(":")
    target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    end = min(target + AFTER, target.replace(hour=23, minute=59))
    return min(target - BEFORE, end - MIN_WINDOW), end


def _journey_date(train: dict, now: datetime) -> str:
    """
    The date the run arriving now departed its source -- i.e. what RailRadar's
    `startDate` will say, and therefore the journey_date the poll lands under.

    Same mapping _runs_today uses to turn an arrival day into a departure day.
    """
    return (now - timedelta(days=train.get("arrival_day_offset") or 0)).strftime("%Y-%m-%d")


def _due_now(trains: list[dict], now: datetime, already_polled: set[tuple[str, str]]) -> list[str]:
    """Train numbers whose scheduled arrival falls in the window around now."""
    due = []
    for t in trains:
        number = t["train_number"]
        if (number, _journey_date(t, now)) in already_polled:
            continue  # one reading per journey is all the daily stat needs
        if not _runs_today(t, now):
            continue  # doesn't run today -- polling returns the next run, not this one
        arrival = t.get("scheduled_arrival")
        if not arrival:
            due.append(number)  # unknown schedule -> poll once to learn it
            continue
        start, end = _window(arrival, now)
        if start <= now <= end:
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
        # Wide enough to cover the largest arrival_day_offset in the fleet (2)
        # with slack, so an overnight run's earlier poll is still visible.
        already = store.polled_journeys((now - timedelta(days=3)).strftime("%Y-%m-%d"))
        # Order by configured list so behaviour is deterministic.
        due = _due_now(
            [known.get(n, {"train_number": n, "scheduled_arrival": None}) for n in configured],
            now,
            already,
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
    store.record_run(len(due), len(results["ok"]), len(results["failed"]))
    logging.info("Done. ok=%s failed=%s", results["ok"], results["failed"])
    return 0 if not results["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
