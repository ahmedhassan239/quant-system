"""
analyzer/constants.py
─────────────────────
Domain-level constants for the Execution Analyzer package.

These are values that were previously hard-coded as magic numbers
inside ``analyzer.py``. Centralising them here ensures:
  • A single source of truth for every numeric threshold.
  • Zero-regression: values are identical to the originals.
  • Easy tuning without touching business logic.

All constants that come from ``config.py`` or ``database.py`` are
*not* duplicated here — they are imported directly by the modules
that need them.
"""

# ──────────────────────────────────────────────────────────────────────
#  TRADING FEES
# ──────────────────────────────────────────────────────────────────────

TRADING_FEE: float = 0.001
"""Maker/taker fee rate (0.1% per side) applied on virtual portfolio fills.

Used in the virtual execution block:
    effective_usdt = spend * (1 - TRADING_FEE)
    asset_bought   = effective_usdt / current_price
"""

# ──────────────────────────────────────────────────────────────────────
#  SIGNAL EXIT GATE
# ──────────────────────────────────────────────────────────────────────

MIN_PROFIT_PCT: float = 0.01
"""Minimum unrealised profit (+1.0 %) required before a counter-signal
can force-close an existing LONG position.

Gate: if profit_pct < MIN_PROFIT_PCT → suppress CLOSE LONG signal.
"""

# ──────────────────────────────────────────────────────────────────────
#  BINANCE ORDER PLACEMENT HARD CAPS
# ──────────────────────────────────────────────────────────────────────

MAX_POSITION_USDT: float = 250.0
"""Hard ceiling on any single Binance Futures order in USD.

Regardless of balance or conviction tier, no order may exceed this
value. Acts as a last-resort capital-protection guard.
"""

MAX_WALLET_PCT: float = 0.05
"""Maximum fraction of available Futures wallet balance per position (5 %).

Calculated size is capped at: min(tier_size, wallet × MAX_WALLET_PCT, MAX_POSITION_USDT)
"""

# ──────────────────────────────────────────────────────────────────────
#  CONVICTION TIER THRESHOLDS (position sizing)
# ──────────────────────────────────────────────────────────────────────

CONVICTION_TIER1_Z: float = 2.0
CONVICTION_TIER1_VOL: float = 4.0
CONVICTION_TIER1_ALLOC: float = 0.20
"""Tier 1 (highest conviction): |Z| >= 2.0 AND vol_ratio >= 4.0 → 20% of slot budget."""

CONVICTION_TIER2_Z: float = 1.0
CONVICTION_TIER2_VOL: float = 2.0
CONVICTION_TIER2_ALLOC: float = 0.10
"""Tier 2 (moderate conviction): |Z| >= 1.0 AND vol_ratio >= 2.0 → 10% of slot budget."""

CONVICTION_TIER3_ALLOC: float = 0.05
"""Tier 3 (default / low conviction): 5% of slot budget."""

# Binance-side caps override the virtual tier allocations (production guard)
BINANCE_TIER1_ALLOC: float = 0.10   # was 0.20, capped for live safety
BINANCE_TIER2_ALLOC: float = 0.05   # was 0.10, capped for live safety

# ──────────────────────────────────────────────────────────────────────
#  RISK MANAGEMENT THRESHOLDS
# ──────────────────────────────────────────────────────────────────────

BREAK_EVEN_TRIGGER_PCT: float = 0.01
"""Unrealised PnL threshold (+1.0 %) at which Stop-Loss is moved to entry price."""

TRAILING_PULLBACK_PCT: float = 0.005
"""Legacy trailing-stop pullback constant (−0.5 %). Kept for backward compatibility.
The system now uses Dynamic ATR-based trailing stops; this constant is no longer
used in any calculation but is preserved so external code referencing it does not break.
"""

# ──────────────────────────────────────────────────────────────────────
#  STAGNANT POSITION DETECTION
# ──────────────────────────────────────────────────────────────────────

STAGNANT_HOURS_THRESHOLD: float = 2.0
"""A position open for at least this many hours with a flat PnL is
classified as stagnant and eligible for forced closure (when
DISABLE_STAGNANT_EXIT is False in config.py).
"""

STAGNANT_PNL_BAND: float = 0.005
"""PnL band (±0.5 %) within which a position is considered 'flat' for
the stagnant detection check.

    is_stagnant = (hours_open >= STAGNANT_HOURS_THRESHOLD
                   and -STAGNANT_PNL_BAND <= unrealised_pct <= STAGNANT_PNL_BAND)
"""

# ──────────────────────────────────────────────────────────────────────
#  POSITION ROTATION (TRADE UPGRADE)
# ──────────────────────────────────────────────────────────────────────

POSITION_UPGRADE_SCORE_GAP: float = 1.5
"""Minimum strength-score advantage the incoming Whale Strike candidate
must have over the weakest current position before a rotation is authorised.

    can_upgrade = (cand_score >= weak_score + POSITION_UPGRADE_SCORE_GAP)
                  OR weakest position is stagnant.
"""

ROTATION_CONFIRM_RETRIES: int = 5
"""Number of Binance API poll attempts to confirm a position has been
closed before proceeding with the replacement entry.
"""

ROTATION_CONFIRM_SLEEP_S: float = 0.5
"""Seconds to sleep between each rotation-confirmation poll attempt."""

# ──────────────────────────────────────────────────────────────────────
#  BINANCE income_history PNL FETCH (Ghost Position Archiver)
# ──────────────────────────────────────────────────────────────────────

NET_PNL_INCOME_TYPES: frozenset = frozenset({
    "REALIZED_PNL",
    "COMMISSION",
    "FUNDING_FEE",
})
"""Income types fetched from futures_income_history to compute the true
NET realised PnL for a closed position.

    net_pnl = gross_pnl + commissions + funding_fees
             (commissions and funding_fees carry negative signs from Binance)

CRITICAL: Do NOT add or remove entries from this set. Querying only
REALIZED_PNL causes wallet-balance drift because commissions and funding
fees drain the account without being reflected in the local DB.
"""

INCOME_HISTORY_MAX_RETRIES: int = 3
"""Maximum retry attempts for futures_income_history before falling back
to futures_account_trades. Binance has a latency of a few seconds after
position closure before income entries appear.
"""

INCOME_HISTORY_RETRY_SLEEP_S: int = 3
"""Seconds to sleep between income_history retry attempts."""
