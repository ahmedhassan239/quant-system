"""
analyzer/utils.py
─────────────────
Shared utility helpers used by multiple analyzer sub-modules.

Placing ``log_to_db`` here (rather than in ``portfolio.py``) prevents
circular imports: ``portfolio.py`` → ``risk.py`` → (potentially) back,
with both needing the logging helper.
"""

from __future__ import annotations

from database import BotLog, SessionLocal


# ══════════════════════════════════════════════════════════════════════
#  DATABASE LOGGING
# ══════════════════════════════════════════════════════════════════════

def log_to_db(
    session: object,
    symbol: str,
    action: str,
    message: str,
) -> None:
    """Write a structured log entry to the ``bot_log`` database table.

    Fails silently: if the DB write fails, a warning is printed but the
    exception is not re-raised so that a logging failure never interrupts
    the trading loop.

    Args:
        session: An active SQLAlchemy ``Session`` object.
        symbol:  Trading pair symbol (e.g. ``'BTCUSDT'``).
        action:  Short action tag (e.g. ``'ENTRY'``, ``'EXIT'``, ``'INFO'``).
        message: Full human-readable log message.
    """
    try:
        log_entry = BotLog(
            symbol=symbol,
            action=action,
            message=message,
        )
        session.add(log_entry)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Warning: Failed to save bot log to DB: {e}", flush=True)
