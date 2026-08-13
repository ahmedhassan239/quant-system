"""
analyzer/__init__.py
────────────────────
Backward-compatible public facade for the ``analyzer`` package.

This module re-exports the exact same 5 symbols that were previously
accessible as top-level attributes of the monolithic ``analyzer.py``
module, so that all existing callers (e.g. ``main.py``) continue to
work without any changes::

    from analyzer import run_analyzer
    from analyzer import run_macro_analyzer
    from analyzer import send_telegram_alert
    from analyzer import send_periodic_report
    from analyzer import MAX_GLOBAL_POSITIONS

No business logic lives here.  All implementations are in their
respective sub-modules.
"""

from analyzer.core import run_macro_analyzer, run_analyzer
from analyzer.notifications import send_telegram_alert, send_periodic_report
from config import MAX_GLOBAL_POSITIONS  # re-exported constant

__all__ = [
    "run_analyzer",
    "run_macro_analyzer",
    "send_telegram_alert",
    "send_periodic_report",
    "MAX_GLOBAL_POSITIONS",
]
