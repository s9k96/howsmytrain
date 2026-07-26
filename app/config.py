"""
Central configuration, loaded from environment variables (via .env).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _train_numbers() -> list[str]:
    raw = os.getenv("TRAIN_NUMBERS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


RAILRADAR_API_KEY: str = os.getenv("RAILRADAR_API_KEY", "")
RAILRADAR_BASE_URL: str = os.getenv("RAILRADAR_BASE_URL", "https://api.railradar.in/v1")
TRAIN_NUMBERS: list[str] = _train_numbers()

DB_PATH: Path = PROJECT_ROOT / os.getenv("DB_PATH", "data/trains.db")
LOG_DIR: Path = PROJECT_ROOT / "logs"

# When both are set, app/store.py writes to Supabase instead of SQLite.
# The service key bypasses RLS -- keep it in GitHub Actions secrets, never
# in the dashboard (which uses the anon key).
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY: str = os.getenv("SUPABASE_SERVICE_KEY", "")


def require_api_key() -> str:
    if not RAILRADAR_API_KEY or RAILRADAR_API_KEY.startswith("rr_live_your_key"):
        raise RuntimeError(
            "RAILRADAR_API_KEY is not set. Copy .env.example to .env and fill in "
            "your key from https://railradar.in/developers"
        )
    return RAILRADAR_API_KEY


def require_train_numbers() -> list[str]:
    if not TRAIN_NUMBERS:
        raise RuntimeError(
            "TRAIN_NUMBERS is empty. Set it in .env as a comma-separated list, "
            "e.g. TRAIN_NUMBERS=12301,12951"
        )
    return TRAIN_NUMBERS
