import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app import config, db


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point config.DB_PATH at a throwaway file for the duration of a test."""
    test_db_path = tmp_path / "test_trains.db"
    monkeypatch.setattr(config, "DB_PATH", test_db_path)
    db.init_db()
    yield test_db_path
