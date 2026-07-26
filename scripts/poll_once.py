#!/usr/bin/env python3
"""
Run one polling cycle for all trains configured in .env (TRAIN_NUMBERS).

Meant to be invoked periodically by cron (see crontab.example) or run
manually while developing:

    python scripts/poll_once.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db
from app.poller import poll_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)


def main() -> int:
    db.init_db()
    train_numbers = config.require_train_numbers()
    config.require_api_key()

    results = poll_all(train_numbers)
    logging.info("Poll cycle done. ok=%s failed=%s", results["ok"], results["failed"])
    return 0 if not results["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
