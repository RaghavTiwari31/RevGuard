"""
RevGuard — FastAPI application entry point

Start command (single worker, Render free tier):
  uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.db import close_db, init_db
from app.logging_config import get_logger, setup_logging
from app.policy import load_policy
from app.routers.webhook import router as webhook_router
from app.routers.stream import router as stream_router
from app.routers.simulate import router as simulate_router
from app.routers.policy_api import router as policy_router
from app.scheduler import start_scheduler, stop_scheduler

# ── Bootstrap ─────────────────────────────────────────────────────────────────
load_dotenv()                # Load .env (no-op if absent)
setup_logging()              # Structured JSON logs from the very first line
logger = get_logger(__name__)


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup:
      1. Load and validate policy.yaml
      2. Initialise the DB engine + create tables
    Shutdown:
      1. Dispose DB connection pool
    """
    policy_path = os.getenv("POLICY_FILE", "policy.yaml")
    policy = load_policy(policy_path)
    logger.info("policy.loaded", extra={
        "path": policy_path,
        "max_retry_attempts": policy.max_retry_attempts,
        "quiet_hours": f"{policy.quiet_hours_start}–{policy.quiet_hours_end}",
        "timezone": policy.timezone,
        "min_confidence": policy.min_confidence_for_autonomous_action,
    })

    await init_db()
    logger.info("db.initialised", extra={"database_url": os.getenv("DATABASE_URL", "sqlite (dev)")})

    start_scheduler()
    logger.info("scheduler.started")

    yield  # Application is running

    stop_scheduler()
    logger.info("scheduler.stopped")
    await close_db()
    logger.info("db.closed")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="RevGuard",
    description=(
        "Autonomous Revenue Recovery & Smart Dunning Engine — "
        "Razorpay AI Buildathon 2026 (Track 03)"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────────────
# ALLOWED_ORIGINS env var controls what origins can call the API.
# In production (Render): set to your Vercel URL, e.g. https://revguard.vercel.app
# In local dev: defaults to localhost:5173 (Vite dev server)
_allowed_origins_raw = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:4173"
)
_allowed_origins = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=r"https://revguard.*\.vercel\.app",  # covers preview branches
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ── Health endpoint ────────────────────────────────────────────────────────────
@app.get("/health", tags=["ops"])
async def health_check():
    """Keep-alive endpoint for external pingers (cron-job.org)."""
    return JSONResponse({"status": "ok", "env": os.getenv("APP_ENV", "development")})

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(webhook_router)
app.include_router(stream_router)
app.include_router(simulate_router)
app.include_router(policy_router)
