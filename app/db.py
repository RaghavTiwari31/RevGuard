"""
RevGuard — SQLAlchemy 2.0 async database layer

Models:
  - Event          → raw webhook payload + processing state
  - Trace          → per-pipeline-run audit record (one per event attempt)
  - Customer       → lightweight customer profile for cooldown / retry tracking
  - IdempotencyLock → distributed lock table (row = exactly-once guarantee)

The engine is built from DATABASE_URL env var:
  - Empty / SQLite  → aiosqlite (local dev)
  - postgresql+asyncpg://... → asyncpg (Render Postgres)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


# ── Base ─────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Models ────────────────────────────────────────────────────────────────────

class Event(Base):
    """Raw Razorpay webhook event, stored before any processing begins."""

    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(128), nullable=False, unique=True, index=True)
    payment_id = Column(String(128), nullable=True, index=True)
    customer_id = Column(String(128), nullable=True, index=True)

    # Financial fields
    amount_paise = Column(Integer, nullable=False)           # in paise (₹1 = 100 paise)
    currency = Column(String(8), default="INR", nullable=False)

    # Failure metadata
    error_code = Column(String(128), nullable=True)
    error_reason = Column(String(256), nullable=True)
    error_description = Column(String(512), nullable=True)

    # Bank / issuer info
    bank = Column(String(64), nullable=True)
    issuer_bin = Column(String(16), nullable=True)           # First 6 digits of card

    # Raw payload (JSON string) for auditability
    raw_payload = Column(Text, nullable=False)

    # Lifecycle
    retry_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class Trace(Base):
    """
    One row per pipeline execution for a given event.
    Immutable after creation — never UPDATE a Trace row; append new ones.
    """

    __tablename__ = "traces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String(64), nullable=False, unique=True, index=True)  # trc_<uuid>
    event_id = Column(String(128), nullable=False, index=True)

    # Triage result
    category = Column(String(64), nullable=True)           # e.g. TEMPORARY_CASHFLOW
    action_type = Column(String(64), nullable=True)        # e.g. GENERATE_PAYMENT_LINK
    outcome_status = Column(String(64), nullable=True)     # e.g. AWAITING_CUSTOMER_SETTLEMENT

    # Guardrail results (JSON string)
    guardrail_checks = Column(Text, nullable=True)

    # LLM outputs
    rationale = Column(Text, nullable=True)
    confidence_score = Column(Float, nullable=True)
    hinglish_message = Column(Text, nullable=True)
    llm_provider = Column(String(32), nullable=True)    # groq | anthropic | canned_fallback

    # Financials
    amount_inr = Column(Float, nullable=True)

    # Dispatch / action
    dispatch_channel = Column(String(32), nullable=True)     # sms | whatsapp | voice | none
    dispatch_cost_inr = Column(Float, nullable=True)
    razorpay_link_id = Column(String(128), nullable=True)
    razorpay_link_url = Column(String(512), nullable=True)
    retry_scheduled_at = Column(DateTime(timezone=True), nullable=True)

    # Triage metadata (JSON string)
    triage_metadata = Column(Text, nullable=True)

    # Classification
    classification_rule = Column(String(128), nullable=True)

    # Pre-flight outcome
    pre_flight_passed = Column(Boolean, default=False, nullable=False)
    pre_flight_rejection_reason = Column(String(256), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class Customer(Base):
    """
    Lightweight customer record — tracks outreach history for cooldown checks
    and retry counts across the lifecycle.
    """

    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(String(128), nullable=False, unique=True, index=True)
    email = Column(String(256), nullable=True)
    phone = Column(String(32), nullable=True)
    name = Column(String(256), nullable=True)

    # Retry / outreach tracking
    total_attempts = Column(Integer, default=0, nullable=False)
    last_contacted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class IdempotencyLock(Base):
    """
    One row per (event_id) — insertion is atomic; a second INSERT for the same
    event_id raises an IntegrityError which we catch to implement exactly-once
    processing.
    """

    __tablename__ = "idempotency_locks"
    __table_args__ = (UniqueConstraint("event_id", name="uq_idempotency_event_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(128), nullable=False, index=True)
    trace_id = Column(String(64), nullable=False)           # The trace that claimed this lock
    locked_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


# ── Engine / Session factory ──────────────────────────────────────────────────

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def _build_url(raw_url: str | None) -> str:
    """
    Resolve the DATABASE_URL to an async-driver URL.
    - Empty / None → SQLite via aiosqlite (local dev)
    - postgresql://... → rewrite to postgresql+asyncpg://...
    - postgresql+asyncpg://... → use as-is
    """
    if not raw_url:
        return "sqlite+aiosqlite:///./revguard_dev.db"
    if raw_url.startswith("postgresql://"):
        return raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw_url


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        raise RuntimeError("Database engine not initialised. Call init_db() first.")
    return _engine


def get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        raise RuntimeError("Session factory not initialised. Call init_db() first.")
    return _session_factory


async def init_db(database_url: str | None = None) -> None:
    """
    Create the engine, session factory, and all tables.
    Call once at application startup (FastAPI lifespan).
    """
    global _engine, _session_factory

    url = _build_url(database_url or os.getenv("DATABASE_URL"))

    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_async_engine(
        url,
        echo=False,
        connect_args=connect_args,
    )

    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async with _engine.begin() as conn:
        # Import Phase 2 models so they are registered with Base.metadata
        import app.issuer_radar  # noqa: F401 — registers IssuerBinStats
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Dispose the engine cleanly (called in FastAPI shutdown lifespan)."""
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None
