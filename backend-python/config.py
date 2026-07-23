"""
config.py — Central Environment-Driven Configuration
=====================================================
Single source of truth for ALL environment-variable-driven settings.

Super Bot MTF Architecture:
  Engine A (MACRO):     15m timeframe — trend detection via Z-Score + SMA-50
  Engine B (EXECUTION): 5m timeframe — entry execution with MTF confluence
"""

import os

# ──────────────────────────────────────────────────────────────────────
#  ENVIRONMENT TYPE (LIVE / TESTNET)
# ──────────────────────────────────────────────────────────────────────
ENV_TYPE = os.environ.get("ENV_TYPE", "LIVE")          # "LIVE" or "TESTNET"

# ──────────────────────────────────────────────────────────────────────
#  ENGINE ROLE (MACRO / EXECUTION)
# ──────────────────────────────────────────────────────────────────────
ENGINE_ROLE = os.environ.get("ENGINE_ROLE", "EXECUTION")  # "MACRO" or "EXECUTION"

# ──────────────────────────────────────────────────────────────────────
#  BINANCE API
# ──────────────────────────────────────────────────────────────────────
# Legacy Spot URL (kept for backward compatibility)
BINANCE_BASE_URL = os.environ.get(
    "BINANCE_BASE_URL",
    "https://testnet.binancefuture.com"                 # Futures Testnet default
)

# Futures REST API base (used by data_fetcher, scanner, etc.)
BINANCE_FUTURES_BASE_URL = os.environ.get(
    "BINANCE_FUTURES_BASE_URL",
    "https://testnet.binancefuture.com"
)

# API credentials (read by futures_executor.py for python-binance client)
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# ──────────────────────────────────────────────────────────────────────
#  FUTURES POSITION DEFAULTS
# ──────────────────────────────────────────────────────────────────────
FUTURES_LEVERAGE = int(os.environ.get("FUTURES_LEVERAGE", "1"))
FUTURES_MARGIN_TYPE = os.environ.get("FUTURES_MARGIN_TYPE", "ISOLATED")

# ──────────────────────────────────────────────────────────────────────
#  TIMEFRAME & SCHEDULING
# ──────────────────────────────────────────────────────────────────────
TIMEFRAME = os.environ.get("TIMEFRAME", "5m")          # "15m" macro, "5m" execution

# Derive the scheduling interval in minutes from the timeframe string
# e.g. "15m" → 15, "5m" → 5, "1h" → 60
def _parse_interval_minutes(tf: str) -> int:
    """Convert a Binance-style timeframe string to minutes."""
    tf = tf.strip().lower()
    if tf.endswith('m'):
        return int(tf[:-1])
    if tf.endswith('h'):
        return int(tf[:-1]) * 60
    if tf.endswith('d'):
        return int(tf[:-1]) * 1440
    return 15  # fallback

SCHEDULE_INTERVAL_MINUTES = _parse_interval_minutes(TIMEFRAME)

# ──────────────────────────────────────────────────────────────────────
#  SHARED DATABASE (MTF Communication)
# ──────────────────────────────────────────────────────────────────────
MACRO_DB_NAME = os.environ.get("MACRO_DB_NAME", "quant_shared_db")

# ──────────────────────────────────────────────────────────────────────
#  TESTNET TESTING MODE
# ──────────────────────────────────────────────────────────────────────
# ⚠️ Force trades on pure trend alignment (bypasses OB + Z-Score gates).
#    Set to False for production!
TESTNET_FORCE_TRADES = False

# ──────────────────────────────────────────────────────────────────────
#  STATISTICAL ANALYSIS CONSTANTS
# ──────────────────────────────────────────────────────────────────────
# Macro Engine (1h)
MACRO_SMA_PERIOD = 50              # SMA window for macro trend
MACRO_SDC_MULTIPLIER = 2.0         # ±2σ Standard Deviation Channel

# Execution Engine (5m) — Risk thresholds
HARD_STOP_LOSS_PCT = 0.05          # 5% absolute stop loss
STOP_LOSS_PCT = 0.05               # 5% trailing/soft stop loss

# Execution Engine (5m) — Z-Score thresholds for entry (⚠️ TEMPORARILY RELAXED FOR TESTING)
ZSCORE_LONG_THRESHOLD = -0.15      # Z < -0.15 → oversold (LONG entry)   [PROD: -0.8]
ZSCORE_SHORT_THRESHOLD = 0.15      # Z > +0.15 → overbought (SHORT entry) [PROD: +0.8]
ZSCORE_SMA_PERIOD = 50             # SMA window for execution Z-Score

# Order Block volume filter (⚠️ TEMPORARILY RELAXED FOR TESTING)
OB_VOLUME_MULTIPLIER = 1.1         # OB candle volume must be > 1.1x 20-period avg  [PROD: 1.5]
OB_VOLUME_MA_PERIOD = 20           # Moving average window for volume baseline

# Breakout volume filter — Strategy B (⚠️ TEMPORARILY RELAXED FOR TESTING)
BREAKOUT_VOLUME_MULTIPLIER = 1.1   # Breakout candle volume > 1.1x 20-period avg  [PROD: 2.5]
BREAKOUT_CONSOLIDATION_PERIOD = 20 # Lookback for consolidation zone

# ──────────────────────────────────────────────────────────────────────
#  TELEGRAM ALERT PREFIX
# ──────────────────────────────────────────────────────────────────────
# Dynamically build a prefix like "🧪 [TESTNET - 1h MACRO]"
_role_label = "MACRO" if ENGINE_ROLE.upper() == "MACRO" else "EXEC"
if ENV_TYPE.upper() == "TESTNET":
    ALERT_PREFIX = f"🧪 [TESTNET - {TIMEFRAME} {_role_label}]"
    ALERT_EMOJI = "🧪"
else:
    ALERT_PREFIX = f"🚀 [LIVE - {TIMEFRAME} {_role_label}]"
    ALERT_EMOJI = "🚀"
