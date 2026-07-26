"""
FastAPI app: serves the JSON API the dashboard reads from, plus the static
dashboard files themselves.

Run with:
    uvicorn app.main:app --reload
or:
    python scripts/run_server.py
"""
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import aggregate, config, db

# docs/ rather than static/ so GitHub Pages and this server publish the same
# files. index.html reads Supabase directly; local.html reads the /api routes
# below off local SQLite.
STATIC_DIR = Path(__file__).resolve().parent.parent / "docs"

app = FastAPI(title="HowsMyTrain")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    db.init_db()


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "tracked_trains": config.TRAIN_NUMBERS,
        "db_path": str(config.DB_PATH),
    }


@app.get("/api/trains")
def trains() -> list[dict]:
    return aggregate.train_summary()


@app.get("/api/stats/daily")
def stats_daily(
    train_number: Optional[str] = None,
    days: int = Query(default=30, ge=1, le=365),
) -> list[dict]:
    return aggregate.daily_stats(train_number=train_number, days=days)


@app.get("/api/stats/weekly")
def stats_weekly(
    train_number: Optional[str] = None,
    weeks: int = Query(default=8, ge=1, le=52),
) -> list[dict]:
    return aggregate.weekly_stats(train_number=train_number, weeks=weeks)


@app.get("/api/polls")
def polls(
    train_number: Optional[str] = None,
    since_date: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[dict]:
    # raw_json is ~100KB/row and no client uses it -- excluded so large
    # limits stay a sane payload. Query the SQLite file directly for raw.
    rows = db.list_polls(train_number=train_number, since_date=since_date, limit=limit)
    return [{k: v for k, v in dict(r).items() if k != "raw_json"} for r in rows]


# Static dashboard, mounted last so /api/* routes above take precedence.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
