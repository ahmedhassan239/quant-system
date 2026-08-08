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
# Minimum 24h quote volume threshold in USDT ($30,000,000.0)
# ── ALPHA MODE: Lowered from $75M → $30M to broaden scanner radar
MIN_24H_VOLUME_USDT = float(os.environ.get("MIN_24H_VOLUME_USDT", "30000000.0"))

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
# ── ALPHA MODE: 5x leverage for all trend trades (was 1x)
FUTURES_LEVERAGE = int(os.environ.get("FUTURES_LEVERAGE", "5"))
FUTURES_MARGIN_TYPE = os.environ.get("FUTURES_MARGIN_TYPE", "ISOLATED")

# Stop-Limit Slippage Cap: Maximum allowed slippage from stopPrice → limit price (0.5%)
# Used by set_stop_loss_order() to calculate the limit execution price for STOP (Stop-Limit) orders.
# LONG close (SELL): limit price = stopPrice * (1 - cap)  → worst fill 0.5% below trigger
# SHORT close (BUY): limit price = stopPrice * (1 + cap)  → worst fill 0.5% above trigger
STOP_LIMIT_SLIPPAGE_CAP = float(os.environ.get("STOP_LIMIT_SLIPPAGE_CAP", "0.005"))

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
#  ALPHA MODE — MICRO-MANAGEMENT KILL SWITCHES
# ──────────────────────────────────────────────────────────────────────
# DISABLE_STAGNANT_EXIT: When True, trades are NOT closed just because
# they've been sideways for >2 hours. Give the setup time to play out.
DISABLE_STAGNANT_EXIT = True

# DISABLE_TREND_REVERSAL_EJECT: When True, positions are NOT force-closed
# when the 1h Macro trend flips. Instead, the Dynamic ATR Trailing Stop
# Loss handles the exit organically.
DISABLE_TREND_REVERSAL_EJECT = True

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

# ── Dynamic Regime-Based Risk Parameters ──
# ── ALPHA MODE: TP raised to 3.0%, TSL widened to 2.5x/2.0x ATR across all primary regimes
REGIME_RISK_PARAMS = {
    'RANGE': {
        'SL_ATR_MULT': 2.5,
        'PARTIAL_TP_PCT': 0.030,                # 3.0% (was 0.6%) — let winners run
        'TSL_ATR_ACTIVATION_MULT': 2.5,         # 2.5x ATR (was 2.0x) — room to breathe
        'TSL_ATR_TRAIL_MULT': 2.0,              # 2.0x ATR (was 1.5x) — wider trail
    },
    'TREND': {
        'SL_ATR_MULT': 1.5,
        'PARTIAL_TP_PCT': 0.030,                # 3.0% (was 2.0%) — let winners run
        'TSL_ATR_ACTIVATION_MULT': 2.5,         # 2.5x ATR (was 2.0x) — room to breathe
        'TSL_ATR_TRAIL_MULT': 2.0,              # 2.0x ATR (was 1.5x) — wider trail
    },
    'STORM': {
        'SL_ATR_MULT': 1.5,
        'PARTIAL_TP_PCT': 0.030,                # 3.0% (was 0.5%) — no more penny exits
        'TSL_ATR_ACTIVATION_MULT': 2.5,         # 2.5x ATR (was 0.5x) — match primary
        'TSL_ATR_TRAIL_MULT': 2.0,              # 2.0x ATR (was 0.3x) — match primary
    },
    'CRASH_CATCHER': {
        # SL is wick-based (not ATR-based) — SL_ATR_MULT intentionally 0
        'SL_ATR_MULT': 0.0,
        # Aggressive first TP: exit 50% at 0.5% profit to secure the bounce
        'PARTIAL_TP_PCT': 0.005,
        # Activate TSL very early (0.5x ATR) to lock in the rubber-band pop
        'TSL_ATR_ACTIVATION_MULT': 0.5,
        # Trail tightly (0.3x ATR) — this is a hit-and-run strategy
        'TSL_ATR_TRAIL_MULT': 0.3,
    }
}

TSL_ACTIVATION_PCT = float(os.environ.get("TSL_ACTIVATION_PCT", "0.008"))          # Legacy fallback
TSL_TRAIL_PCT = float(os.environ.get("TSL_TRAIL_PCT", "0.004"))                   # Legacy fallback
TRAILING_ACTIVATE_PCT = TSL_ACTIVATION_PCT                                        # Alias for backward compatibility
TRAILING_DISTANCE_PCT = TSL_TRAIL_PCT                                             # Alias for backward compatibility


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

# ──────────────────────────────────────────────────────────────────────
#  STRATEGY D — CRASH CATCHER (EXTREME MEAN REVERSION ENGINE)
# ──────────────────────────────────────────────────────────────────────
# Trigger thresholds (the anomaly gate)
CRASH_CATCHER_ZSCORE_LONG   = float(os.environ.get("CC_ZSCORE_LONG",  "-3.5"))  # 15m Z-Score < -3.5 for LONG reversion
CRASH_CATCHER_ZSCORE_SHORT  = float(os.environ.get("CC_ZSCORE_SHORT", "3.5"))   # 15m Z-Score > +3.5 for SHORT reversion
CRASH_CATCHER_RSI_LONG      = float(os.environ.get("CC_RSI_LONG",     "25.0"))  # RSI < 25 (extreme oversold)
CRASH_CATCHER_RSI_SHORT     = float(os.environ.get("CC_RSI_SHORT",    "75.0"))  # RSI > 75 (extreme overbought)

# Confirmation gates (catching the bounce, not the knife)
CRASH_CATCHER_VOL_MULT      = float(os.environ.get("CC_VOL_MULT",      "5.0"))  # Candle volume > 5x MA (whale absorption)
CRASH_CATCHER_VOL_MA_PERIOD = int(os.environ.get(  "CC_VOL_MA_PERIOD", "20"))   # Volume MA lookback period
CRASH_CATCHER_WICK_RATIO    = float(os.environ.get("CC_WICK_RATIO",    "0.6"))  # Pin Bar: wick > 60% of total candle range

# Risk management (tight SL at wick, aggressive TSL)
CRASH_CATCHER_SL_BUFFER_PCT = float(os.environ.get("CC_SL_BUFFER",     "0.001")) # 0.1% buffer beyond wick tip for SL
CRASH_CATCHER_TSL_ATR_MULT  = float(os.environ.get("CC_TSL_ATR_MULT",  "0.5"))  # TSL activates at 0.5x ATR (early lock-in)
CRASH_CATCHER_ALLOC_PCT     = float(os.environ.get("CC_ALLOC_PCT",     "0.05")) # 5% wallet allocation (fixed, counter-trend prudence)

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
