"""
market_regime.py — Market Regime Detection & Strategy Pattern
=============================================================
Implements a dynamic "Market Regime Detection" system that classifies the
current market condition and routes execution to the appropriate strategy.

Regime Priority (evaluated in order — highest risk wins):
  1. STORM  — Extreme volatility → Capital Preservation (limit orders only)
  2. RANGE  — Chop zone / mean reversion → Tight TP/SL, no trailing
  3. TREND  — Trending market → Existing Breakout/Pullback logic

Architecture (SOLID):
  • RegimeDetector   — Single Responsibility: pure regime classification.
  • TradingStrategy  — Open/Closed: abstract interface, open for extension.
  • TrendStrategy    — Delegates to existing run_analyzer() entry logic.
  • RangeStrategy    — Mean reversion entries with tight risk management.
  • StormStrategy    — Deep limit orders only; auto-cancels after 15 min.
  • StrategyRouter   — Dependency Inversion: routes via regime → strategy.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from binance.exceptions import BinanceAPIException
from config import REGIME_RISK_PARAMS

if TYPE_CHECKING:
    from binance.client import Client

logger = logging.getLogger("MarketRegime")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    import sys
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
        datefmt='%H:%M:%S',
    ))
    logger.addHandler(_ch)


# ══════════════════════════════════════════════════════════════════════
#  ENUM: Market Regime
# ══════════════════════════════════════════════════════════════════════

class MarketRegime(str, Enum):
    """
    The three mutually-exclusive market states the bot can operate in.
    Inherits from str so values serialize cleanly to JSON / DB strings.
    """
    TREND = "TREND"   # 🚀 Trending market — ride the move
    RANGE = "RANGE"   # ⚖️  Chop zone — mean reversion harvest
    STORM = "STORM"   # 🌪️  Extreme volatility — capital preservation only

    @property
    def emoji(self) -> str:
        return {"TREND": "🚀", "RANGE": "⚖️", "STORM": "🌪️"}[self.value]

    @property
    def telegram_label(self) -> str:
        return f"[{self.value} {self.emoji}]"


# ══════════════════════════════════════════════════════════════════════
#  REGIME DETECTOR — Pure Function, No Side Effects
# ══════════════════════════════════════════════════════════════════════

class RegimeDetector:
    """
    Classifies the current market into one of three regimes using:
      • ADX(14)      — trend strength
      • ATR(14)      — recent volatility vs. 50-period rolling mean
      • Z-Score(50)  — already computed in run_analyzer()

    Call detect() with a DataFrame that already contains:
      'ATR'     (14-period ATR, computed by calculate_atr)
      'Z_Score' (50-period Z-Score, computed by calculate_zscore)

    ADX is computed internally (requires 'high', 'low', 'close').

    Priority order (STORM is checked first — capital preservation always wins):
      STORM : ATR > 3x its 50-period MA  OR  |Z-Score| > 3.0
      RANGE : ADX < 25 AND |Z-Score| <= 1.0
      TREND : ADX >= 25 AND |Z-Score| >= 1.2
      (fallback → RANGE, the most conservative non-storm state)
    """

    # Regime thresholds — all tunable via constants
    # ── BEAST MODE: ADX thresholds lowered from 25 → 12 to allow entries on weak trends
    STORM_ATR_MULT   = 3.0    # ATR spike threshold (x its 50-period MA)
    STORM_ZSCORE     = 3.0    # |Z-Score| hard limit before STORM
    RANGE_ADX_MAX    = 12.0   # ADX below 12 = true chop (BEAST MODE: was 25)
    RANGE_ZSCORE_MAX = 1.0    # |Z-Score| must be inside ±1.0 for RANGE
    TREND_ADX_MIN    = 12.0   # ADX at/above 12 = trending enough (BEAST MODE: was 25)
    TREND_ZSCORE_MIN = 1.2    # |Z-Score| must be ≥ 1.2 for TREND
    ATR_MA_PERIOD    = 50     # Window for ATR moving average (STORM detection)
    ADX_PERIOD       = 14     # ADX smoothing period

    @classmethod
    def _calculate_adx(cls, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        Compute ADX(14) using Wilder's smoothing.
        Returns a Series aligned with df's index (NaN for warm-up rows).
        """
        high = df['high']
        low  = df['low']
        close = df['close']

        # True Range
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs()
        ], axis=1).max(axis=1)

        # Directional movement
        up_move   = high - high.shift(1)
        down_move = low.shift(1) - low

        dm_plus  = np.where((up_move > down_move) & (up_move > 0),  up_move,  0.0)
        dm_minus = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        # Wilder's smoothing (equivalent to EMA with alpha=1/period)
        alpha = 1.0 / period
        atr_s  = tr.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        di_p   = pd.Series(dm_plus,  index=df.index).ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        di_m   = pd.Series(dm_minus, index=df.index).ewm(alpha=alpha, min_periods=period, adjust=False).mean()

        # Avoid division by zero
        safe_atr = atr_s.replace(0, np.nan)
        di_plus  = 100.0 * di_p / safe_atr
        di_minus = 100.0 * di_m / safe_atr

        dx_denom = di_plus + di_minus
        dx_denom = dx_denom.replace(0, np.nan)
        dx = 100.0 * (di_plus - di_minus).abs() / dx_denom

        adx = dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        return adx

    @classmethod
    def detect(cls, df: pd.DataFrame) -> tuple[MarketRegime, dict]:
        """
        Classify the current market regime.

        Args:
            df: DataFrame with columns: high, low, close, ATR, Z_Score.
                Must have at least (ADX_PERIOD * 2 + ATR_MA_PERIOD) rows
                for reliable readings (~78 rows minimum at 14-period ADX).

        Returns:
            (MarketRegime, meta_dict) where meta_dict contains:
              adx, z_score, atr, atr_ma, atr_ratio, regime_reason
        """
        fallback_meta = {
            'adx': None, 'z_score': None,
            'atr': None, 'atr_ma': None, 'atr_ratio': None,
            'regime_reason': 'Insufficient data — defaulting to RANGE (conservative)',
        }

        min_rows = cls.ADX_PERIOD * 2 + cls.ATR_MA_PERIOD
        if len(df) < min_rows:
            logger.warning(f"RegimeDetector: only {len(df)} rows (need {min_rows}). Defaulting to RANGE.")
            return MarketRegime.RANGE, fallback_meta

        # ── Read current values ──
        last = df.iloc[-1]
        z_score = float(last['Z_Score']) if pd.notna(last.get('Z_Score')) else 0.0
        current_atr = float(last['ATR']) if pd.notna(last.get('ATR')) and float(last.get('ATR', 0)) > 0 else None

        if current_atr is None:
            logger.warning("RegimeDetector: ATR is None/NaN. Defaulting to RANGE.")
            return MarketRegime.RANGE, {**fallback_meta, 'z_score': z_score}

        # ── Compute ATR 50-period MA (exclude current candle for cleaner baseline) ──
        atr_series = df['ATR'].dropna()
        atr_ma = float(atr_series.iloc[-cls.ATR_MA_PERIOD:-1].mean()) if len(atr_series) >= cls.ATR_MA_PERIOD + 1 else None
        atr_ratio = (current_atr / atr_ma) if (atr_ma and atr_ma > 0) else None

        # ── Compute ADX ──
        adx_series = cls._calculate_adx(df, period=cls.ADX_PERIOD)
        adx = float(adx_series.iloc[-1]) if pd.notna(adx_series.iloc[-1]) else None

        meta = {
            'adx':          round(adx, 2) if adx is not None else None,
            'z_score':      round(z_score, 3),
            'atr':          round(current_atr, 6),
            'atr_ma':       round(atr_ma, 6) if atr_ma else None,
            'atr_ratio':    round(atr_ratio, 2) if atr_ratio else None,
            'regime_reason': '',
        }

        abs_z = abs(z_score)

        # ── Priority 1: STORM (Capital Preservation) ──
        storm_atr_trigger   = (atr_ratio is not None and atr_ratio > cls.STORM_ATR_MULT)
        storm_zscore_trigger = (abs_z > cls.STORM_ZSCORE)

        if storm_atr_trigger or storm_zscore_trigger:
            reason_parts = []
            if storm_atr_trigger:
                reason_parts.append(f"ATR spike {atr_ratio:.1f}x MA (>{cls.STORM_ATR_MULT}x)")
            if storm_zscore_trigger:
                reason_parts.append(f"|Z-Score| {abs_z:.2f} (>{cls.STORM_ZSCORE})")
            meta['regime_reason'] = f"STORM: {' + '.join(reason_parts)}"

            # ── Storm Direction: enables conditional freeze in core.py ──
            # If Z-Score drove the STORM trigger, record which direction
            # the parabolic move is heading so the engine can allow
            # trend-aligned entries while still blocking counter-trend.
            if storm_zscore_trigger:
                meta['storm_direction'] = 'UP' if z_score > 0 else 'DOWN'
            else:
                meta['storm_direction'] = None  # ATR-only spike — no directional bias

            return MarketRegime.STORM, meta

        # ── Priority 2: RANGE (Mean Reversion) ──
        adx_low    = (adx is not None and adx < cls.RANGE_ADX_MAX)
        z_in_range = (abs_z <= cls.RANGE_ZSCORE_MAX)

        if adx_low and z_in_range:
            meta['regime_reason'] = (
                f"RANGE: ADX {adx:.1f} (<{cls.RANGE_ADX_MAX}) + "
                f"|Z-Score| {abs_z:.2f} (≤{cls.RANGE_ZSCORE_MAX})"
            )
            return MarketRegime.RANGE, meta

        # ── Priority 3: TREND (Breakout/Pullback) ──
        adx_strong  = (adx is not None and adx >= cls.TREND_ADX_MIN)
        z_stretched = (abs_z >= cls.TREND_ZSCORE_MIN)

        if adx_strong and z_stretched:
            meta['regime_reason'] = (
                f"TREND: ADX {adx:.1f} (≥{cls.TREND_ADX_MIN}) + "
                f"|Z-Score| {abs_z:.2f} (≥{cls.TREND_ZSCORE_MIN})"
            )
            return MarketRegime.TREND, meta

        # ── Fallback: RANGE (conservative, no trade in ambiguous zones) ──
        # Pre-compute adx_str so the format specifier always receives a plain float,
        # never an inline conditional (which raises ValueError in Python's formatter).
        adx_str = f"{adx:.1f}" if (adx is not None and pd.notna(adx) and adx > 0) else "N/A"
        meta['regime_reason'] = (
            f"RANGE (fallback): ADX={adx_str} / "
            f"|Z|={abs_z:.2f} — no regime rule matched"
        )
        return MarketRegime.RANGE, meta


# ══════════════════════════════════════════════════════════════════════
#  ABSTRACT BASE: TradingStrategy
# ══════════════════════════════════════════════════════════════════════

class TradingStrategy(ABC):
    """
    Abstract Strategy interface.
    Each concrete strategy handles one MarketRegime and implements execute().
    The strategy receives all required context via parameters — no hidden state.
    """

    @property
    @abstractmethod
    def regime(self) -> MarketRegime:
        """The regime this strategy is responsible for."""
        ...

    @abstractmethod
    def execute(
        self,
        symbol: str,
        df: pd.DataFrame,
        portfolio: dict,
        current_price: float,
        current_rsi: float | None,
        current_zscore: float | None,
        current_atr: float,
        bullish_ob: dict | None,
        bearish_ob: dict | None,
        bullish_breakout: dict | None,
        bearish_breakout: dict | None,
        macro_trend: str,
        in_position: bool,
        active_count: int,
        max_positions: int,
        futures_client,
        session,
        regime_meta: dict,
    ) -> tuple[str, str | None, float]:
        """
        Evaluate entry conditions and return the trading decision.

        Returns:
            (decision, strategy_type, new_stop_loss)
            decision       : 'LONG', 'SHORT', or 'WAIT'
            strategy_type  : human-readable label (e.g. 'RANGE_LONG')
            new_stop_loss  : calculated stop-loss price (0.0 if WAIT)
        """
        ...


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY A: TrendStrategy — Breakout / Pullback (existing logic)
# ══════════════════════════════════════════════════════════════════════

class TrendStrategy(TradingStrategy):
    """
    Mode A — TREND regime.
    Delegates to the original Pullback + Breakout entry logic.
    Wide ATR-based Trailing Stops are managed by run_analyzer() risk section.
    """

    @property
    def regime(self) -> MarketRegime:
        return MarketRegime.TREND

    def execute(
        self,
        symbol, df, portfolio, current_price, current_rsi,
        current_zscore, current_atr, bullish_ob, bearish_ob,
        bullish_breakout, bearish_breakout, macro_trend,
        in_position, active_count, max_positions,
        futures_client, session, regime_meta,
    ) -> tuple[str, str | None, float]:

        decision = 'WAIT'
        strategy_type = None
        new_stop_loss = 0.0

        if current_zscore is None or current_rsi is None:
            return decision, strategy_type, new_stop_loss

        if in_position:
            return decision, strategy_type, new_stop_loss

        if active_count >= max_positions:
            logger.info(f"[{symbol}] [TREND] Max slots reached ({active_count}/{max_positions})")
            return 'WAIT', None, 0.0

        # ── Precompute EMAs for Beast Mode micro-breakout triggers ──
        ema_9  = df['close'].ewm(span=9, adjust=False).mean()
        ema_20 = df['close'].ewm(span=20, adjust=False).mean()
        current_ema9  = float(ema_9.iloc[-1])  if len(ema_9)  >= 9  else None
        current_ema20 = float(ema_20.iloc[-1]) if len(ema_20) >= 20 else None

        # ── LONG Confluence (UPTREND) ── BEAST MODE: RSI gate widened to > 35 ──
        if macro_trend == 'UPTREND' and current_rsi > 35.0:
            # ── PRIMARY: EMA-9/EMA-20 Micro-Breakout (fast entry, no OB dependency) ──
            if (current_ema9 and current_ema20
                    and current_price > current_ema9
                    and current_ema9 > current_ema20):
                decision = 'LONG'
                strategy_type = 'PULLBACK'
                # ATR-based stop: 1.0x ATR below entry
                new_stop_loss = current_price - (1.0 * current_atr) if current_atr else current_price * 0.985
                logger.info(
                    f"[TREND] {symbol} LONG Micro-Breakout — Price ${current_price:.2f} > "
                    f"EMA9 ${current_ema9:.2f} > EMA20 ${current_ema20:.2f} | "
                    f"RSI {current_rsi:.1f} | Z={current_zscore:+.2f}"
                )

            # ── FALLBACK: OB touch (legacy path, still valid) ──
            elif bullish_ob and current_price >= bullish_ob['low'] and current_zscore < -1.2:
                decision = 'LONG'
                strategy_type = 'PULLBACK'
                new_stop_loss = bullish_ob['low'] * 0.999
                logger.info(f"[TREND] {symbol} LONG Pullback — OB touch + Z={current_zscore:+.2f}")

            # Strategy B: Momentum Breakout
            elif bullish_breakout and current_zscore > 1.2:
                decision = 'LONG'
                strategy_type = 'BREAKOUT'
                new_stop_loss = bullish_breakout['breakout_candle_low'] * 0.999
                logger.info(f"[TREND] {symbol} LONG Breakout — Vol {bullish_breakout['vol_ratio']:.1f}x + Z={current_zscore:+.2f}")

            # Strategy E: Trend Momentum — catches smooth trends with no OB/breakout
            # BEAST MODE: RSI band widened to 35-75
            elif current_ema20 and current_price > current_ema20 and 35.0 <= current_rsi <= 75.0:
                if len(df) >= 4:
                    prior_3_high = float(df['high'].iloc[-4:-1].max())
                    candle_close = float(df['close'].iloc[-1])
                    if candle_close > prior_3_high:
                        decision = 'LONG'
                        strategy_type = 'TREND_MOMENTUM'
                        # ATR-based stop: 1.0x ATR below entry
                        new_stop_loss = current_price - (1.0 * current_atr) if current_atr else current_price * 0.985
                        logger.info(
                            f"[TREND] {symbol} LONG Momentum — Price ${current_price:.2f} > EMA20 ${current_ema20:.2f} | "
                            f"RSI {current_rsi:.1f} | Close ${candle_close:.2f} > Prior3H ${prior_3_high:.2f} | "
                            f"Z={current_zscore:+.2f}"
                        )

        # ── SHORT Confluence (DOWNTREND) ── BEAST MODE: RSI gate widened to < 65 ──
        elif macro_trend == 'DOWNTREND' and current_rsi < 65.0:
            # ── PRIMARY: EMA-9/EMA-20 Micro-Breakdown (fast entry, no OB dependency) ──
            if (current_ema9 and current_ema20
                    and current_price < current_ema9
                    and current_ema9 < current_ema20):
                decision = 'SHORT'
                strategy_type = 'PULLBACK'
                # ATR-based stop: 1.0x ATR above entry
                new_stop_loss = current_price + (1.0 * current_atr) if current_atr else current_price * 1.015
                logger.info(
                    f"[TREND] {symbol} SHORT Micro-Breakdown — Price ${current_price:.2f} < "
                    f"EMA9 ${current_ema9:.2f} < EMA20 ${current_ema20:.2f} | "
                    f"RSI {current_rsi:.1f} | Z={current_zscore:+.2f}"
                )

            # ── FALLBACK: OB touch (legacy path, still valid) ──
            elif bearish_ob and current_price <= bearish_ob['high'] and current_zscore > 1.2:
                decision = 'SHORT'
                strategy_type = 'PULLBACK'
                new_stop_loss = bearish_ob['high'] * 1.001
                logger.info(f"[TREND] {symbol} SHORT Pullback — OB touch + Z={current_zscore:+.2f}")

            # Strategy B: Momentum Breakout
            elif bearish_breakout and current_zscore < -1.2:
                decision = 'SHORT'
                strategy_type = 'BREAKOUT'
                new_stop_loss = bearish_breakout['breakout_candle_high'] * 1.001
                logger.info(f"[TREND] {symbol} SHORT Breakout — Vol {bearish_breakout['vol_ratio']:.1f}x + Z={current_zscore:+.2f}")

            # Strategy E: Trend Momentum — catches smooth downtrends
            # BEAST MODE: RSI band widened to 25-65
            elif current_ema20 and current_price < current_ema20 and 25.0 <= current_rsi <= 65.0:
                if len(df) >= 4:
                    prior_3_low = float(df['low'].iloc[-4:-1].min())
                    candle_close = float(df['close'].iloc[-1])
                    if candle_close < prior_3_low:
                        decision = 'SHORT'
                        strategy_type = 'TREND_MOMENTUM'
                        # ATR-based stop: 1.0x ATR above entry
                        new_stop_loss = current_price + (1.0 * current_atr) if current_atr else current_price * 1.015
                        logger.info(
                            f"[TREND] {symbol} SHORT Momentum — Price ${current_price:.2f} < EMA20 ${current_ema20:.2f} | "
                            f"RSI {current_rsi:.1f} | Close ${candle_close:.2f} < Prior3L ${prior_3_low:.2f} | "
                            f"Z={current_zscore:+.2f}"
                        )

        return decision, strategy_type, new_stop_loss


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY B: RangeStrategy — Chop Harvester (Mean Reversion)
# ══════════════════════════════════════════════════════════════════════

class RangeStrategy(TradingStrategy):
    """
    Mode B — RANGE regime (Chop Harvester).
    Mean Reversion entries using RSI extremes + Order Block confluence.

    Risk Parameters:
      • Stop-Loss  : ±0.5% from entry (tight, range-aware)
      • Take-Profit: +1.0% to +1.5% (managed in run_analyzer via PARTIAL_TP_PCT)
      • No Trailing Stop (range-bound price will oscillate)
    """

    # BEAST MODE: Wider RSI bands for more entries
    RSI_LONG_THRESHOLD  = 45.0   # Mid-range — look for longs (BEAST MODE: was 35)
    RSI_SHORT_THRESHOLD = 55.0   # Mid-range — look for shorts (BEAST MODE: was 65)

    @property
    def regime(self) -> MarketRegime:
        return MarketRegime.RANGE

    def execute(
        self,
        symbol, df, portfolio, current_price, current_rsi,
        current_zscore, current_atr, bullish_ob, bearish_ob,
        bullish_breakout, bearish_breakout, macro_trend,
        in_position, active_count, max_positions,
        futures_client, session, regime_meta,
    ) -> tuple[str, str | None, float]:

        if in_position:
            return 'WAIT', None, 0.0

        if active_count >= max_positions:
            logger.info(f"[{symbol}] [RANGE] Max slots reached ({active_count}/{max_positions})")
            return 'WAIT', None, 0.0

        if current_rsi is None:
            return 'WAIT', None, 0.0

        decision = 'WAIT'
        strategy_type = None
        new_stop_loss = 0.0
        
        sl_dist = REGIME_RISK_PARAMS['RANGE']['SL_ATR_MULT'] * (current_atr if current_atr else current_price * 0.005)

        # ── BEAST MODE: LONG on RSI condition alone (OB touch optional) ──
        if current_rsi < self.RSI_LONG_THRESHOLD:
            decision = 'LONG'
            strategy_type = 'RANGE_MEAN_REVERSION'
            new_stop_loss = current_price - sl_dist
            ob_info = ""
            if bullish_ob and bullish_ob['low'] <= current_price <= bullish_ob['high']:
                ob_info = f" + Bullish OB touch ${bullish_ob['low']:.2f}–${bullish_ob['high']:.2f}"
            logger.info(
                f"[RANGE] {symbol} LONG MR — RSI {current_rsi:.1f} "
                f"(< {self.RSI_LONG_THRESHOLD}){ob_info}"
            )

        # ── BEAST MODE: SHORT on RSI condition alone (OB touch optional) ──
        elif current_rsi > self.RSI_SHORT_THRESHOLD:
            decision = 'SHORT'
            strategy_type = 'RANGE_MEAN_REVERSION'
            new_stop_loss = current_price + sl_dist
            ob_info = ""
            if bearish_ob and bearish_ob['low'] <= current_price <= bearish_ob['high']:
                ob_info = f" + Bearish OB touch ${bearish_ob['low']:.2f}–${bearish_ob['high']:.2f}"
            logger.info(
                f"[RANGE] {symbol} SHORT MR — RSI {current_rsi:.1f} "
                f"(> {self.RSI_SHORT_THRESHOLD}){ob_info}"
            )

        return decision, strategy_type, new_stop_loss


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY C: StormStrategy — Knife Catcher (Capital Preservation)
# ══════════════════════════════════════════════════════════════════════

class StormStrategy(TradingStrategy):
    """
    Mode C — STORM regime (Capital Preservation).

    STRICT CONSTRAINTS:
      • ZERO market orders. Never chase price.
      • Deep LIMIT ORDERS only — placed far from current price.
      • Hard Stop-Loss: ±0.2% from the limit order entry price.
      • Auto-cancellation: any unfilled limit order is cancelled after 15 min.
      • Entire limit placement wrapped in try/except for Binance -4411/-2027.

    Entry Points:
      • LONG limit : 3~5% BELOW current price (catches panic lows)
      • SHORT limit : 3~5% ABOVE current price (catches euphoric highs)

    Memory Safety:
      threading.Timer fires once → cancels order → garbage collected.
      No persistent state, no accumulating threads.
    """

    LIMIT_OFFSET_PCT   = 0.04   # 4% offset from current price (mid of 3–5% range)
    HARD_SL_PCT        = 0.002  # 0.2% stop from limit fill price
    ORDER_TTL_SECONDS  = 900    # 15 minutes

    @property
    def regime(self) -> MarketRegime:
        return MarketRegime.STORM

    def execute(
        self,
        symbol, df, portfolio, current_price, current_rsi,
        current_zscore, current_atr, bullish_ob, bearish_ob,
        bullish_breakout, bearish_breakout, macro_trend,
        in_position, active_count, max_positions,
        futures_client, session, regime_meta,
    ) -> tuple[str, str | None, float]:
        """
        In STORM mode we never return a 'LONG' or 'SHORT' decision
        directly into the normal run_analyzer() execution path, because
        that path would issue MARKET orders.

        Instead, we place LIMIT orders here directly via the futures_client,
        then return 'WAIT' to prevent the normal market-order path from firing.
        The placed limit order acts as an independent async position opener.
        """
        if in_position:
            logger.info(f"[{symbol}] [STORM] Already in position — skipping limit order placement.")
            return 'WAIT', None, 0.0

        if active_count >= max_positions:
            logger.info(f"[{symbol}] [STORM] Max slots reached ({active_count}/{max_positions})")
            return 'WAIT', None, 0.0

        if not futures_client:
            logger.info(f"[{symbol}] [STORM] No futures client (virtual mode) — skipping limit orders.")
            return 'WAIT', None, 0.0

        # ── Determine limit direction from macro trend bias ──
        if macro_trend == 'UPTREND':
            self._place_storm_limit(symbol, 'LONG', current_price, futures_client, regime_meta)
        elif macro_trend == 'DOWNTREND':
            self._place_storm_limit(symbol, 'SHORT', current_price, futures_client, regime_meta)
        else:
            logger.info(
                f"[{symbol}] [STORM] Macro trend is {macro_trend} — "
                "no directional bias available. Sitting out."
            )

        # Always return WAIT — limit orders are placed async; market orders must never fire
        return 'WAIT', 'STORM_LIMIT_PENDING', 0.0

    def _place_storm_limit(
        self,
        symbol: str,
        direction: str,
        current_price: float,
        futures_client,
        regime_meta: dict,
    ) -> None:
        """
        Place a deep limit order and schedule its auto-cancellation.
        Wrapped in try/except — Binance -4411 and -2027 are silenced.
        """
        try:
            from futures_executor import _round_quantity, _round_price, setup_symbol

            # ── Calculate limit price ──
            if direction == 'LONG':
                limit_price = current_price * (1.0 - self.LIMIT_OFFSET_PCT)
                sl_price    = limit_price * (1.0 - self.HARD_SL_PCT)
                side = 'BUY'
            else:  # SHORT
                limit_price = current_price * (1.0 + self.LIMIT_OFFSET_PCT)
                sl_price    = limit_price * (1.0 + self.HARD_SL_PCT)
                side = 'SELL'

            limit_price = _round_price(futures_client, symbol, limit_price)
            sl_price    = _round_price(futures_client, symbol, sl_price)

            # ── Estimate quantity (use 5% of available balance) ──
            try:
                balances = futures_client.futures_account_balance()
                usdt_bal = next(
                    (float(b['availableBalance']) for b in balances if b['asset'] == 'USDT'),
                    0.0
                )
            except Exception:
                usdt_bal = 0.0

            if usdt_bal <= 0:
                logger.warning(f"[{symbol}] [STORM] Zero USDT balance — cannot place limit order.")
                return

            spend    = usdt_bal * 0.05  # 5% of available balance
            raw_qty  = spend / limit_price
            quantity = _round_quantity(futures_client, symbol, raw_qty)

            if quantity <= 0:
                logger.warning(f"[{symbol}] [STORM] Quantity rounded to 0 — skipping limit order.")
                return

            setup_symbol(futures_client, symbol)

            logger.warning(
                f"🌪️  [STORM] [{symbol}] Placing {direction} LIMIT @ ${limit_price:,.4f} "
                f"({self.LIMIT_OFFSET_PCT*100:.0f}% below current ${current_price:,.4f}) | "
                f"Qty: {quantity} | Hard SL: ${sl_price:,.4f} ({self.HARD_SL_PCT*100:.1f}%) | "
                f"ADX: {regime_meta.get('adx')} | ATR ratio: {regime_meta.get('atr_ratio')}x"
            )

            order = futures_client.futures_create_order(
                symbol=symbol,
                side=side,
                type='LIMIT',
                timeInForce='GTC',   # Good Till Cancelled (we cancel via TTL timer)
                price=limit_price,
                quantity=quantity,
            )

            order_id = order.get('orderId')
            if not order_id:
                logger.error(f"[{symbol}] [STORM] Limit order placed but no orderId returned: {order}")
                return

            logger.info(
                f"[{symbol}] [STORM] ✅ Limit order placed | OrderID: {order_id} | "
                f"Will auto-cancel in {self.ORDER_TTL_SECONDS // 60} min."
            )

            # ── Schedule auto-cancellation (memory-safe: one-shot Timer) ──
            def _cancel_if_unfilled(sym: str, oid: int, fc) -> None:
                """Daemon timer callback — cancels limit order if still open."""
                try:
                    open_orders = fc.futures_get_open_orders(symbol=sym)
                    still_open  = any(str(o.get('orderId')) == str(oid) for o in open_orders)
                    if still_open:
                        fc.futures_cancel_order(symbol=sym, orderId=oid)
                        logger.warning(
                            f"🗑️  [STORM] [{sym}] Limit order {oid} expired unfilled "
                            f"after {self.ORDER_TTL_SECONDS // 60} min — CANCELLED."
                        )
                    else:
                        logger.info(f"[{sym}] [STORM] Limit order {oid} already filled/closed.")
                except BinanceAPIException as e:
                    # -2011: Unknown order — already filled or cancelled by exchange
                    if e.code != -2011:
                        logger.warning(f"[{sym}] [STORM] Cancel TTL error [{e.code}]: {e.message}")
                except Exception as exc:
                    logger.debug(f"[{sym}] [STORM] Cancel TTL generic error: {exc}")

            timer = threading.Timer(
                self.ORDER_TTL_SECONDS,
                _cancel_if_unfilled,
                args=(symbol, order_id, futures_client),
            )
            timer.daemon = True   # Dies with the main process — no memory leak
            timer.start()

        except BinanceAPIException as e:
            # Silent handling for known restriction codes per spec
            SILENT_CODES = (-4411, -2027)
            if e.code in SILENT_CODES:
                logger.debug(
                    f"[{symbol}] [STORM] Limit order silently rejected "
                    f"[{e.code}] {e.message} — skipping without halting engine."
                )
            else:
                logger.error(
                    f"[{symbol}] [STORM] ❌ Limit order Binance API error "
                    f"[{e.code}]: {e.message}"
                )
        except Exception as exc:
            logger.error(f"[{symbol}] [STORM] ❌ Unexpected error placing limit order: {exc}")


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY ROUTER
# ══════════════════════════════════════════════════════════════════════

class StrategyRouter:
    """
    Routes execution to the correct TradingStrategy based on the detected regime.

    Implements the Strategy Pattern's Context role:
      1. Calls RegimeDetector.detect() to classify the market.
      2. Selects the matching strategy from an internal registry.
      3. Delegates execute() call to that strategy.
      4. Returns (regime, decision, strategy_type, new_stop_loss).
    """

    def __init__(self) -> None:
        self._strategies: dict[MarketRegime, TradingStrategy] = {
            MarketRegime.TREND: TrendStrategy(),
            MarketRegime.RANGE: RangeStrategy(),
            MarketRegime.STORM: StormStrategy(),
        }

    def route(
        self,
        symbol: str,
        df: pd.DataFrame,
        portfolio: dict,
        current_price: float,
        current_rsi: float | None,
        current_zscore: float | None,
        current_atr: float,
        bullish_ob: dict | None,
        bearish_ob: dict | None,
        bullish_breakout: dict | None,
        bearish_breakout: dict | None,
        macro_trend: str,
        in_position: bool,
        active_count: int,
        max_positions: int,
        futures_client,
        session,
        precomputed_regime: MarketRegime | None = None,
        precomputed_meta: dict | None = None,
        btc_macro_trend: str | None = None,
    ) -> tuple[MarketRegime, str, str | None, float, dict]:
        """
        Detect regime → select strategy → execute.

        Returns:
            (regime, decision, strategy_type, new_stop_loss, regime_meta)
        """
        # 1. Detect regime (or use precomputed)
        if precomputed_regime is not None and precomputed_meta is not None:
            regime, regime_meta = precomputed_regime, precomputed_meta
        else:
            regime, regime_meta = RegimeDetector.detect(df)

        # 2. Build the prominent log line required by spec
        adx_str     = f"{regime_meta['adx']:.1f}"    if regime_meta.get('adx')     is not None else "N/A"
        atr_str     = f"{regime_meta['atr']:.4f}"    if regime_meta.get('atr')     is not None else "N/A"
        rsi_str     = f"{current_rsi:.1f}"           if current_rsi  is not None else "N/A"
        zscore_str  = f"{current_zscore:+.3f}"        if current_zscore is not None else "N/A"
        ratio_str   = f"{regime_meta['atr_ratio']:.2f}x" if regime_meta.get('atr_ratio') is not None else "N/A"

        logger.info(
            f"[MODE: {regime.value}] Symbol: {symbol} | "
            f"ADX: {adx_str} | RSI: {rsi_str} | Z: {zscore_str} | "
            f"ATR: {atr_str} | ATR/MA: {ratio_str} | "
            f"Reason: {regime_meta.get('regime_reason', '')}"
        )
        # Also flush to stdout for Docker log visibility
        print(
            f"[MODE: {regime.value} {regime.emoji}] {symbol} | "
            f"ADX: {adx_str} | RSI: {rsi_str} | Z: {zscore_str} | "
            f"ATR: {atr_str} | Ratio: {ratio_str}",
            flush=True,
        )

        # 3. Select and execute strategy
        strategy = self._strategies[regime]
        decision, strategy_type, new_stop_loss = strategy.execute(
            symbol=symbol,
            df=df,
            portfolio=portfolio,
            current_price=current_price,
            current_rsi=current_rsi,
            current_zscore=current_zscore,
            current_atr=current_atr,
            bullish_ob=bullish_ob,
            bearish_ob=bearish_ob,
            bullish_breakout=bullish_breakout,
            bearish_breakout=bearish_breakout,
            macro_trend=macro_trend,
            in_position=in_position,
            active_count=active_count,
            max_positions=max_positions,
            futures_client=futures_client,
            session=session,
            regime_meta=regime_meta,
        )

        # 4. HARD FILTER: Strict Macro-Trend Alignment
        #    BEAST MODE: Removed for LONGs — macro alignment is still checked
        #    in TrendStrategy. Keep only the counter-trend LONG block.
        if decision in ('LONG', 'SHORT'):
            if macro_trend == 'DOWNTREND' and decision == 'LONG':
                logger.info(f"[{symbol}] HARD FILTER: Dropping LONG signal (Macro Trend is DOWNTREND)")
                decision, strategy_type, new_stop_loss = 'WAIT', None, 0.0

        # 5. BEAST MODE: Bi-Directional Shorts — allow SHORT momentum scalps
        #    when intraday 5m price is below EMA-20 (downward momentum).
        #    OLD BEHAVIOR: Blocked ALL shorts when BTC/asset macro was UPTREND.
        #    NEW BEHAVIOR: Allow shorts if price < EMA-20 (intraday momentum check).
        if decision == 'SHORT':
            # Compute intraday EMA-20 from the DataFrame passed through route()
            if len(df) >= 20:
                ema_20_series = df['close'].ewm(span=20, adjust=False).mean()
                ema_20_val = float(ema_20_series.iloc[-1])
                current_close = float(df['close'].iloc[-1])
                if current_close >= ema_20_val:
                    logger.info(
                        f"[{symbol}] BEAST MODE SHORT FILTER: Dropping SHORT — "
                        f"Price ${current_close:.2f} >= EMA20 ${ema_20_val:.2f} "
                        f"(no intraday downward momentum)"
                    )
                    decision, strategy_type, new_stop_loss = 'WAIT', None, 0.0
                else:
                    logger.info(
                        f"[{symbol}] BEAST MODE: SHORT ALLOWED — "
                        f"Price ${current_close:.2f} < EMA20 ${ema_20_val:.2f} "
                        f"(intraday downward momentum confirmed)"
                    )

        return regime, decision, strategy_type, new_stop_loss, regime_meta


# ── Module-level singleton for convenient import ──
strategy_router = StrategyRouter()
