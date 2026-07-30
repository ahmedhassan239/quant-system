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
#  DYNAMIC SCANNER RADAR SETTINGS
# ──────────────────────────────────────────────────────────────────────
# Minimum 24h quote volume threshold in USDT ($75,000,000.0)
MIN_24H_VOLUME_USDT = float(os.environ.get("MIN_24H_VOLUME_USDT", "75000000.0"))

# Max number of top volume-ranked symbols to scan dynamically (e.g. 45)
TOP_N = int(os.environ.get("TOP_N", "45"))

# Stablecoin / fiat-pegged quote pairs to exclude from trading
STABLECOIN_BLACKLIST = {
    'USDCUSDT', 'BUSDUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'DAIUSDT',
    'USDPUSDT', 'EURUSDT', 'GBPUSDT', 'AUDUSDT', 'JPYUSDT',
}

# VIP / Whitelisted priority symbols that ALWAYS bypass MIN_24H_VOLUME_USDT volume filter
VIP_SYMBOLS = {'PAXGUSDT','BZUSDT'}

# TradFi or restricted contracts that trigger Binance API errors (e.g. XAUUSDT -4411)
RESTRICTED_TOKENS_BLACKLIST = {
    'XAUUSDT', 'SKHYNIXUSDT', 'CLUSDT', 'SNDKUSDT', 'SOXLUSDT',
    'XAGUSDT', 'MUUSDT', 'SPCXUSDT', 'SKHYUSDT',
}

# Strict blacklist for known mock/testnet tokens + restricted contracts
MOCK_TOKENS_BLACKLIST = {
    'HANAUSDT', 'GWEIUSDT', 'ESPORTSUSDT', 'VELVETUSDT', 'PROMUSDT',
    'DEXEUSDT', 'AKEUSDT', 'ONUSDT', 'EULUSDT', 'FXSUSDT', 'BANKUSDT',
    'RIFUSDT',
}.union(RESTRICTED_TOKENS_BLACKLIST)


# Default fallback list if Binance API fails during initial startup
DEFAULT_SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
    'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'SUIUSDT',
]


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

# Execution Engine (5m) — Risk & Position Limits
HARD_STOP_LOSS_PCT = 0.07          # 7% absolute stop loss
STOP_LOSS_PCT = 0.07               # 7% trailing/soft stop loss
MAX_GLOBAL_POSITIONS = int(os.environ.get("MAX_GLOBAL_POSITIONS", "10"))  # Max open positions limit

# Execution Engine (5m) — Dynamic ATR Trailing Stop Loss (TSL) Settings
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))                               # 14-period ATR
SL_ATR_MULT = float(os.environ.get("SL_ATR_MULT", "1.5"))                          # Initial Stop Loss at 1.5 * ATR distance
TSL_ATR_ACTIVATION_MULT = float(os.environ.get("TSL_ATR_ACTIVATION_MULT", "1.0"))  # Activate TSL at 1.0 * ATR profit distance
TSL_ATR_TRAIL_MULT = float(os.environ.get("TSL_ATR_TRAIL_MULT", "0.5"))            # Trail price strictly by 0.5 * ATR distance
TSL_ACTIVATION_PCT = float(os.environ.get("TSL_ACTIVATION_PCT", "0.008"))          # Legacy fallback
TSL_TRAIL_PCT = float(os.environ.get("TSL_TRAIL_PCT", "0.004"))                   # Legacy fallback
TRAILING_ACTIVATE_PCT = TSL_ACTIVATION_PCT                                        # Alias for backward compatibility
TRAILING_DISTANCE_PCT = TSL_TRAIL_PCT                                             # Alias for backward compatibility
PARTIAL_TP_PCT = float(os.environ.get("PARTIAL_TP_PCT", "0.006"))                   # +0.6% ROE threshold for 50% scale-out


# Execution Engine (5m) — Z-Score thresholds for entry
ZSCORE_LONG_THRESHOLD = -1.2       # Z < -1.2 for Pullback LONG / Z > +1.2 for Breakout LONG
ZSCORE_SHORT_THRESHOLD = 1.2       # Z > +1.2 for Pullback SHORT / Z < -1.2 for Breakout SHORT
ZSCORE_SMA_PERIOD = 50             # SMA window for execution Z-Score

# Order Block volume filter (⚠️ TEMPORARILY RELAXED FOR TESTING)
OB_VOLUME_MULTIPLIER = 1.1         # OB candle volume must be > 1.1x 20-period avg  [PROD: 1.5]
OB_VOLUME_MA_PERIOD = 20           # Moving average window for volume baseline

# Breakout volume filter — Strategy B (⚠️ TEMPORARILY RELAXED FOR TESTING)
BREAKOUT_VOLUME_MULTIPLIER = 1.1   # Breakout candle volume > 1.1x 20-period avg  [PROD: 2.5]
BREAKOUT_CONSOLIDATION_PERIOD = 20 # Lookback for consolidation zone

# Strategy C — Whale Hunter (Volume Anomaly Detection)
WHALE_VOLUME_MULTIPLIER = float(os.environ.get("WHALE_VOLUME_MULTIPLIER", "10.0")) # Volume spike > 10.0x 50-period volume SMA

# Pyramiding Strategy (Scaling Into Winners)
MAX_SCALE_INS = 2                      # Max 2 scale-ins per winning position
PYRAMID_TIER1_PNL = 0.02               # Tier 1 trigger: +2.0% unrealized PnL
PYRAMID_TIER2_PNL = 0.04               # Tier 2 trigger: +4.0% unrealized PnL
PYRAMID_TIER1_SIZE_PCT = 0.50          # Add 50% of initial position size on Tier 1
PYRAMID_TIER2_SIZE_PCT = 0.25          # Add 25% of initial position size on Tier 2

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
