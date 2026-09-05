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
from sqlalchemy import text

from app.bandit import flush_state as flush_bandit_state
from app.bandit import load_state as load_bandit_state
from app.db import close_db, get_engine, init_db
from app.logging_config import get_logger, setup_logging
from app.policy import get_policy, load_policy
from app.retry_queue import rehydrate as rehydrate_retries
from app.routers.issuers import router as issuers_router
from app.routers.policy_api import router as policy_router
from app.routers.retries import router as retries_router
from app.routers.simulate import router as simulate_router
from app.routers.stream import router as stream_router
from app.routers.traces import router as traces_router
from app.routers.twilio import router as twilio_router
from app.routers.webhook import router as webhook_router
from app.scheduler import start_scheduler, stop_scheduler
from app.schemas import HealthResponse

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
      3. Start the scheduler
      4. Reload persisted bandit weights
      5. Rehydrate the durable retry queue

    Steps 4 and 5 are what let the service survive the free tier's idle
    spin-down: without them every cold start would forget what the bandit had
    learned and silently drop every retry that was still pending.

    Shutdown:
      1. Stop the scheduler
      2. Flush bandit weights back to the database
      3. Dispose DB connection pool
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

    logger.info("bandit.state_loaded", extra=await load_bandit_state())
    logger.info("retry_queue.rehydrated", extra=await rehydrate_retries(policy))

    yield  # Application is running

    stop_scheduler()
    logger.info("scheduler.stopped")

    try:
        await flush_bandit_state()
    except Exception as exc:  # pragma: no cover — never block shutdown
        logger.warning("bandit.flush_failed", extra={"error": str(exc)})

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
# The single health route for the whole app.  Used by the Render keep-alive
# pinger, so it must stay cheap — one trivial round-trip to prove the DB pool
# is actually alive, rather than reporting "connected" unconditionally.
@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health_check():
    """Keep-alive endpoint for external pingers (cron-job.org)."""
    db_status = "connected"
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        db_status = "unavailable"
        logger.warning("health.db_unreachable", extra={"error": str(exc)})

    return HealthResponse(
        status="ok" if db_status == "connected" else "degraded",
        version=app.version,
        db=db_status,
        env=os.getenv("APP_ENV", "development"),
        policy_loaded=get_policy() is not None,
    )

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(webhook_router)
app.include_router(stream_router)
app.include_router(simulate_router)
app.include_router(policy_router)
app.include_router(traces_router)
app.include_router(issuers_router)
app.include_router(retries_router)
app.include_router(twilio_router)
