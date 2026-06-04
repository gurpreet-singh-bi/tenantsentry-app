"""
mode.py
-------
TenantSentry.ai — Runtime Mode Management

Two modes, always developed in parallel:

  DEV  — Zero external dependencies. No API calls, no Supabase, no billing.
          Uses deterministic mock data from pipeline/dev_pipeline.py.
          Fast, free, reproducible. Default for local development.

  LIVE — Full production pipeline. Real Claude API, real Supabase, real VoyageAI.
          Requires all credentials in .env. Used in production and for real audits.

The mode is set at startup via DEV_MODE env var, and can be toggled at runtime
via POST /api/admin/mode/toggle (admin token required). The toggle survives for
the life of the server process — restart resets to the env var value.

────────────────────────────────────────────────────────────────────────────────
DUAL-MODE DEVELOPMENT CONTRACT
────────────────────────────────────────────────────────────────────────────────
EVERY feature must be implemented in BOTH modes simultaneously.
No feature ships with only a Live implementation.

Pattern for any new service/module:

    from api.mode import is_dev

    def my_feature(data):
        if is_dev():
            return _dev_my_feature(data)   # deterministic, zero deps
        return _live_my_feature(data)      # real API/DB call

Dev implementations must:
  - Return deterministic, realistic data (not empty dicts or None)
  - Cover the same code paths exercised by Live mode
  - Be fast (< 100ms per operation, excluding deliberate sleep for UX realism)
  - Never call external APIs, Supabase, or the filesystem beyond tmp

Live implementations must:
  - Handle failures gracefully (try/except, logged, never crash the request)
  - Always have a documented fallback if the external service is unavailable
────────────────────────────────────────────────────────────────────────────────
"""

import os
import threading
from enum import Enum

from loguru import logger


class Mode(str, Enum):
    DEV  = "dev"
    LIVE = "live"


# ── Module-level state ────────────────────────────────────────────────────────
_lock = threading.Lock()

# Startup value — read from env, never changed by this module directly
_startup_mode: Mode = (
    Mode.DEV if os.environ.get("DEV_MODE", "true").lower() == "true" else Mode.LIVE
)

# Runtime value — can be toggled via toggle()
_current_mode: Mode = _startup_mode


# ── Public API ────────────────────────────────────────────────────────────────

def current() -> Mode:
    """Return the current active mode."""
    with _lock:
        return _current_mode


def is_dev() -> bool:
    """True when running in Dev mode — use mock/dev implementations."""
    return current() == Mode.DEV


def is_live() -> bool:
    """True when running in Live mode — use real API/DB implementations."""
    return current() == Mode.LIVE


def toggle() -> Mode:
    """
    Switch between DEV and LIVE modes at runtime.
    Thread-safe. Returns the new mode.
    Called by POST /api/admin/mode/toggle.
    """
    global _current_mode
    with _lock:
        _current_mode = Mode.LIVE if _current_mode == Mode.DEV else Mode.DEV
        new_mode = _current_mode

    logger.info(f"Mode toggled → {new_mode.upper()}")
    return new_mode


def set_mode(mode: Mode) -> None:
    """Explicitly set the mode. Used in tests."""
    global _current_mode
    with _lock:
        _current_mode = mode
    logger.info(f"Mode set → {mode.upper()}")


def status() -> dict:
    """Return a status dict for the /api/mode endpoint."""
    mode = current()
    return {
        "mode":         mode.value,
        "is_dev":       mode == Mode.DEV,
        "is_live":      mode == Mode.LIVE,
        "startup_mode": _startup_mode.value,
        "description":  (
            "Dev mode — no API calls, deterministic mock data"
            if mode == Mode.DEV else
            "Live mode — real Claude API, Supabase, VoyageAI"
        ),
    }
