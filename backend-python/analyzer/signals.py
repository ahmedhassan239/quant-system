"""
analyzer/signals.py
───────────────────
Stateless strategy evaluation functions for the Execution Analyzer.

All functions are **pure** (no DB access, no Telegram, no side effects).
They accept pre-computed indicator values and return a signal dict or None.

Strategy implementations:
    evaluate_extreme_reversion  – Strategy D: Crash Catcher
    calculate_strength_score    – Position / signal quality scorer
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pandas as pd

from config import (
    CRASH_CATCHER_ZSCORE_LONG,
    CRASH_CATCHER_ZSCORE_SHORT,
    CRASH_CATCHER_RSI_LONG,
    CRASH_CATCHER_RSI_SHORT,
    CRASH_CATCHER_VOL_MULT,
    CRASH_CATCHER_VOL_MA_PERIOD,
    CRASH_CATCHER_WICK_RATIO,
    CRASH_CATCHER_SL_BUFFER_PCT,
)

logger = logging.getLogger("Analyzer.signals")


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY D — CRASH CATCHER (EXTREME MEAN REVERSION ENGINE)
# ══════════════════════════════════════════════════════════════════════

def evaluate_extreme_reversion(
    df: pd.DataFrame,
    current_price: float,
    current_rsi: Optional[float],
    current_zscore: Optional[float],
    current_atr: float,
    in_position: bool,
    active_count: int,
    max_positions: int,
) -> Optional[Dict[str, Any]]:
    """Strategy D: Crash Catcher — Extreme Mean Reversion Engine.

    Activates ONLY during statistically extreme anomalies — events so
    rare that the primary trend-following engine is explicitly frozen
    (STORM regime, Z-Score < -3.0). This module capitalises on the
    capitulation wicks and short squeezes that *follow* those crashes.

    ── Trigger Conditions (The Anomaly) ──────────────────────────────
      LONG  reversion: Z-Score ≤ CRASH_CATCHER_ZSCORE_LONG  AND  RSI < CRASH_CATCHER_RSI_LONG
      SHORT reversion: Z-Score ≥ CRASH_CATCHER_ZSCORE_SHORT AND  RSI > CRASH_CATCHER_RSI_SHORT

    ── Confirmation (Catching the Bounce, not the Knife) ─────────────
      Gate 1 — Volume Anomaly:
        Current candle volume > CRASH_CATCHER_VOL_MULT times the
        CRASH_CATCHER_VOL_MA_PERIOD-period volume MA (using prior
        candles as baseline to avoid look-ahead bias).

      Gate 2 — Pin Bar / Reversal Wick:
        LONG  → lower wick ratio > CRASH_CATCHER_WICK_RATIO
                i.e.  (body_bottom − low) / (high − low) > ratio
                      The bottom is being aggressively defended.
        SHORT → upper wick ratio > CRASH_CATCHER_WICK_RATIO
                i.e.  (high − body_top)  / (high − low) > ratio
                      The top is being aggressively rejected.

    ── Risk Management (Hit and Run) ─────────────────────────────────
      Stop-Loss : wick tip ± CRASH_CATCHER_SL_BUFFER_PCT (0.1% buffer).
                  LONG  SL = candle_low  * (1 - buffer)
                  SHORT SL = candle_high * (1 + buffer)

    ── Bypass Permissions (ONLY for this module) ─────────────────────
      ✅ Bypasses the STORM Freeze — evaluated BEFORE the STORM override
         check in run_analyzer().
      ✅ Bypasses the Macro Hard Filter — never passes through
         StrategyRouter.route(), which is where that filter lives.

    ── Architecture Note ─────────────────────────────────────────────
      Pure function — no side effects, no DB access, no Telegram calls.
      The caller (core.run_analyzer) handles execution and logging.

    Args:
        df:            15m OHLCV DataFrame (must have ≥ CRASH_CATCHER_VOL_MA_PERIOD + 1 rows).
        current_price: Latest close price.
        current_rsi:   Pre-computed 14-period RSI (None if not yet warm).
        current_zscore:Pre-computed Z-Score (None if not yet warm).
        current_atr:   Pre-computed 14-period ATR.
        in_position:   True if this symbol already has an open position.
        active_count:  Number of currently active positions across all symbols.
        max_positions: Hard cap on concurrent open positions.

    Returns:
        On success: dict with keys
            ``direction``, ``strategy_type``, ``stop_loss``,
            ``wick_ratio``, ``vol_ratio``,
            ``candle_low``, ``candle_high``, ``candle_open``, ``candle_close``.
        On failure: ``None``.

    Edge cases:
        - ``in_position=True`` → always returns ``None`` (no double entry).
        - ``active_count >= max_positions`` → ``None`` (no free slots).
        - Either indicator is ``None`` → ``None`` (indicator not warmed).
        - Zero candle range (doji) → ``None`` (wick ratio undefined).
    """
    # ── Pre-flight checks ──────────────────────────────────────────────
    if in_position:
        return None  # Never enter on a symbol that already has an open slot

    if active_count >= max_positions:
        return None  # No free slots

    if current_rsi is None or current_zscore is None:
        return None  # Indicators not yet warm (insufficient history)

    min_rows = CRASH_CATCHER_VOL_MA_PERIOD + 1
    if len(df) < min_rows:
        return None  # Not enough candles to compute volume MA

    # ── Gate 0: Z-Score + RSI Anomaly (The Trigger) ───────────────────
    long_trigger = (
        current_zscore <= CRASH_CATCHER_ZSCORE_LONG
        and current_rsi < CRASH_CATCHER_RSI_LONG
    )
    short_trigger = (
        current_zscore >= CRASH_CATCHER_ZSCORE_SHORT
        and current_rsi > CRASH_CATCHER_RSI_SHORT
    )

    if not long_trigger and not short_trigger:
        return None  # No anomaly — primary engine handles this cycle

    direction = 'LONG' if long_trigger else 'SHORT'

    # ── Gate 1: Volume Anomaly — Whale Absorption / Capitulation Print ─
    # Use prior candles (shift(1)) as the baseline to avoid look-ahead
    # bias on the current candle being evaluated.
    vol_ma_series = df['volume'].shift(1).rolling(CRASH_CATCHER_VOL_MA_PERIOD).mean()
    baseline_vol = vol_ma_series.iloc[-1]
    current_vol = float(df['volume'].iloc[-1])

    if pd.isna(baseline_vol) or baseline_vol <= 0:
        logger.debug(
            "[CRASH CATCHER] Volume MA not yet available "
            f"(need {CRASH_CATCHER_VOL_MA_PERIOD} prior candles). Skipping."
        )
        return None

    vol_ratio = current_vol / baseline_vol

    if vol_ratio < CRASH_CATCHER_VOL_MULT:
        logger.debug(
            f"[CRASH CATCHER] {direction} anomaly detected "
            f"(Z={current_zscore:+.2f}, RSI={current_rsi:.1f}) but "
            f"volume gate FAILED: {vol_ratio:.2f}x < {CRASH_CATCHER_VOL_MULT}x required."
        )
        return None

    # ── Gate 2: Reversal Candlestick Pattern (Pin Bar / Capitulation Wick) ─
    last_candle = df.iloc[-1]
    candle_open = float(last_candle['open'])
    candle_close = float(last_candle['close'])
    candle_high = float(last_candle['high'])
    candle_low = float(last_candle['low'])

    candle_range = candle_high - candle_low

    if candle_range <= 0:
        # Doji with zero range — cannot compute wick ratios reliably
        logger.debug(f"[CRASH CATCHER] Zero candle range detected for {direction}. Skipping.")
        return None

    if direction == 'LONG':
        # Lower wick = distance from candle_low to the body bottom
        body_bottom = min(candle_open, candle_close)
        lower_wick = body_bottom - candle_low
        wick_ratio = lower_wick / candle_range

        if wick_ratio < CRASH_CATCHER_WICK_RATIO:
            logger.debug(
                f"[CRASH CATCHER] LONG anomaly + volume gate PASSED "
                f"(Z={current_zscore:+.2f}, Vol={vol_ratio:.1f}x) but "
                f"Pin Bar gate FAILED: lower wick ratio {wick_ratio:.2f} "
                f"< {CRASH_CATCHER_WICK_RATIO} required. Bottom not yet defended."
            )
            return None

        # SL: just below the wick low with a 0.1% buffer — if we breach this, thesis is dead
        stop_loss = candle_low * (1.0 - CRASH_CATCHER_SL_BUFFER_PCT)

    else:  # direction == 'SHORT'
        # Upper wick = distance from the body top to candle_high
        body_top = max(candle_open, candle_close)
        upper_wick = candle_high - body_top
        wick_ratio = upper_wick / candle_range

        if wick_ratio < CRASH_CATCHER_WICK_RATIO:
            logger.debug(
                f"[CRASH CATCHER] SHORT anomaly + volume gate PASSED "
                f"(Z={current_zscore:+.2f}, Vol={vol_ratio:.1f}x) but "
                f"Pin Bar gate FAILED: upper wick ratio {wick_ratio:.2f} "
                f"< {CRASH_CATCHER_WICK_RATIO} required. Top not yet rejected."
            )
            return None

        # SL: just above the wick high with a 0.1% buffer
        stop_loss = candle_high * (1.0 + CRASH_CATCHER_SL_BUFFER_PCT)

    # ── All three gates passed — signal confirmed ──────────────────────
    strategy_type = f'CRASH_CATCHER_{direction}'

    logger.warning(
        f"🚨 [CRASH CATCHER] {direction} triggered via Extreme Mean Reversion | "
        f"Z-Score: {current_zscore:+.3f} | RSI: {current_rsi:.1f} | "
        f"Volume: {vol_ratio:.1f}x MA | Wick ratio: {wick_ratio:.2f} | "
        f"SL: ${stop_loss:.4f} (wick {'low' if direction == 'LONG' else 'high'} "
        f"± {CRASH_CATCHER_SL_BUFFER_PCT * 100:.1f}%)"
    )
    print(
        f"🚨 [CRASH CATCHER] {direction} triggered via Extreme Mean Reversion | "
        f"Z: {current_zscore:+.3f} | RSI: {current_rsi:.1f} | "
        f"Vol: {vol_ratio:.1f}x | Wick: {wick_ratio:.2f} | "
        f"SL: ${stop_loss:.4f}",
        flush=True,
    )

    return {
        'direction':     direction,
        'strategy_type': strategy_type,
        'stop_loss':     stop_loss,
        'wick_ratio':    round(wick_ratio, 3),
        'vol_ratio':     round(vol_ratio, 2),
        'candle_low':    candle_low,
        'candle_high':   candle_high,
        'candle_open':   candle_open,
        'candle_close':  candle_close,
    }


# ══════════════════════════════════════════════════════════════════════
#  POSITION / SIGNAL STRENGTH SCORER
# ══════════════════════════════════════════════════════════════════════

def calculate_strength_score(
    z_score: Optional[float],
    vol_ratio: float = 1.0,
) -> float:
    """Calculate a quantitative Strength Score for an entry signal or active position.

    The score is used by the Position Rotation (Trade Upgrade) system to
    compare the incoming signal against the weakest currently held position.

    Formula::

        abs_z = |z_score|   (with a floor of 1.5 if vol_ratio >= 8.0)
        score = abs_z × (1.0 + 0.1 × clamp(vol_ratio, 1.0, 10.0))

    Primary factors:
        - Absolute Z-Score: captures momentum / statistical deviation.
        - Volume multiplier: captures liquidity / breakout confirmation.

    Special case:
        For Whale Strike volume spikes (vol_ratio ≥ 8.0), if the
        absolute Z-Score is unusually small (< 1.5), it is floored to
        1.5 so that a high-volume anomaly always scores meaningfully.

    Args:
        z_score:   Latest 15m Z-Score for the symbol (``None`` → treated as 0).
        vol_ratio: Volume/SMA ratio from the triggering signal (default: 1.0).

    Returns:
        Strength score as a non-negative float rounded to 3 decimal places.
    """
    abs_z = abs(z_score) if z_score is not None else 0.0
    vr = float(vol_ratio) if vol_ratio is not None and float(vol_ratio) > 0 else 1.0

    # For Whale Strike volume spikes (vr >= 8.0x), give an effective Z baseline
    # of 1.5 if Z is small — prevents a zero-Z whale from scoring as 0.
    if vr >= 8.0 and abs_z < 1.5:
        abs_z = 1.5

    vol_bonus = 0.1 * min(max(vr, 1.0), 10.0)
    score = abs_z * (1.0 + vol_bonus)
    return round(score, 3)
