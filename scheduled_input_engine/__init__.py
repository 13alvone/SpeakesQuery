"""
SpeakesQuery Scheduled Input Engine
─────────────────────────────────
Background engine that manages cron-scheduled data ingestion tasks.

Usage:
    from scheduled_input_engine import start_engine, get_engine, shutdown_engine

    start_engine()          # call once at app startup
    engine = get_engine()   # access from anywhere
    shutdown_engine()       # call at app shutdown
"""

import logging
import threading

logger = logging.getLogger(__name__)

_engine = None
_lock = threading.Lock()


def start_engine():
    """Initialise and start the scheduled input engine (idempotent)."""
    global _engine
    with _lock:
        if _engine is not None:
            return _engine
        from .engine import ScheduledInputEngine
        _engine = ScheduledInputEngine()
        _engine.start()
        logger.info("[i] Scheduled input engine started")
        return _engine


def get_engine():
    """Return the running engine instance, or raise if not started."""
    if _engine is None:
        raise RuntimeError("Engine not started. Call start_engine() first.")
    return _engine


def shutdown_engine():
    """Gracefully shut down the engine."""
    global _engine
    with _lock:
        if _engine is not None:
            _engine.shutdown()
            _engine = None
            logger.info("[i] Scheduled input engine shut down")
