"""
RevGuard — Structured JSON logging setup

Sets up a single root logger that emits one JSON line per log record.
Import `get_logger` in any module and use it like stdlib `logging`.

Every guardrail decision is logged at INFO level with a structured payload
so the audit trail is machine-readable from day one.
"""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger to emit structured JSON to stdout."""
    root = logging.getLogger()
    if root.handlers:
        # Already configured (e.g., called twice in tests) — don't duplicate handlers
        return

    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.  Callers should pass ``__name__``."""
    return logging.getLogger(name)
