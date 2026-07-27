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


def _due_now(trains: list[dict], now: datetime, already_polled: set[str]) -> list[str]:
    """Train numbers whose scheduled arrival falls in the window around now."""
    due = []
    for t in trains:
        number = t["train_number"]
        if number in already_polled:
            continue  # one reading per journey_date is all the daily stat needs
        if not _runs_today(t, now):
            continue  # doesn't run today -- polling returns the next run, not this one
        arrival = t.get("scheduled_arrival")
        if not arrival:
            due.append(number)  # unknown schedule -> poll once to learn it
            continue
        hh, mm, *_ = str(arrival).split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target - BEFORE <= now <= target + AFTER:
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
        already = store.polled_journey_dates(db.today_ist())
        # Order by configured list so behaviour is deterministic.
        due = _due_now(
            [known.get(n, {"train_number": n, "scheduled_arrival": None}) for n in configured],
            now,
            already,
        )

    if not due:
        logging.info("Nothing due at %s IST. Exiting without any API calls.", now.strftime("%H:%M"))
        return 0

    logging.info("Due now (%s): %s", now.strftime("%H:%M IST"), ", ".join(due))
    if args.dry_run:
        return 0

    config.require_api_key()
    results = poll_all(due)
    logging.info("Done. ok=%s failed=%s", results["ok"], results["failed"])
    return 0 if not results["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
