"""
Shared pytest fixtures for RevGuard tests.

Provides:
  - async_session   — in-memory SQLite async session with all tables created
  - default_policy  — Policy instance loaded from policy.yaml
  - test_client     — FastAPI TestClient for integration tests
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base, init_db, _session_factory
from app.logging_config import setup_logging
from app.policy import Policy, load_policy, _policy  # noqa: F401 (reset in fixture)


# ── Logging ────────────────────────────────────────────────────────────────────
setup_logging()


# ── In-memory SQLite session ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def async_session() -> AsyncSession:
    """
    Yields a fresh in-memory SQLite session with all tables.
    Each test gets its own isolated DB.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session

    await engine.dispose()


# ── Policy fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
def default_policy() -> Policy:
    """Load policy from the repo-root policy.yaml (or defaults if absent)."""
    repo_root = Path(__file__).parent.parent
    return load_policy(repo_root / "policy.yaml")


# ── FastAPI test client ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def test_client():
    """
    Async HTTPX client wired to the FastAPI app via ASGITransport.
    Uses a temp in-memory DB so the test never touches disk.
    """
    import app.db as db_module

    # Override DB to in-memory SQLite for integration tests
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_module._engine = engine
    db_module._session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    await engine.dispose()
