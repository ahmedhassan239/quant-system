"""
analyzer/indicators.py
──────────────────────
Pure mathematical indicator functions for the Execution Analyzer.

All functions in this module are **side-effect free**:
  • No database reads or writes.
  • No Telegram or logging calls.
  • No external API calls.

Each function accepts a pandas DataFrame and returns either a
mutated copy of that DataFrame (with new columns added) or a
plain Python data structure.

Indicator functions:
    calculate_rsi             – 14-period Wilder RSI
    calculate_zscore          – Z-Score relative to a rolling SMA
    calculate_sdc             – Standard Deviation Channel (upper/lower bands)
    calculate_atr             – 14-period Wilder ATR
    detect_order_blocks       – SMC Order Blocks with volume confirmation
    detect_consolidation_breakout – Breakout from a consolidation range
    detect_whale_strike       – Volume anomaly spike (Strategy C trigger)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from config import (
    OB_VOLUME_MULTIPLIER,
    OB_VOLUME_MA_PERIOD,
    BREAKOUT_CONSOLIDATION_PERIOD,
    BREAKOUT_VOLUME_MULTIPLIER,
    WHALE_VOLUME_MULTIPLIER,
)


# ══════════════════════════════════════════════════════════════════════
#  STATISTICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════

def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calculate RSI using Wilder's Smoothing (EWM alpha = 1/period).

    The RSI is appended as a new column ``'RSI'`` on the input DataFrame.
    A value of 100.0 is assigned to rows where the average loss is zero
    (i.e. a window of only gains), avoiding division-by-zero.

    Args:
        df:     OHLCV DataFrame containing a ``'close'`` column.
        period: Look-back window for the smoothed averages (default: 14).

    Returns:
        The same DataFrame with an added ``'RSI'`` column.

    Edge cases:
        - Fewer rows than ``period``: RSI values are ``NaN`` until the
          EWM accumulates sufficient history (``min_periods=period``).
        - Zero average loss: RSI is set to 100.0 (not ``inf``).
    """
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    df.loc[avg_loss == 0, 'RSI'] = 100.0
    return df


def calculate_zscore(df: pd.DataFrame, sma_period: int = 50) -> pd.DataFrame:
    """Calculate Z-Score of price relative to a rolling SMA.

    Formula::

        Z = (close - SMA_n) / σ_n

    where ``σ_n`` is the rolling standard deviation over the same window.
    Division by zero is prevented by replacing zero-std values with NaN.

    Columns added to ``df``:
        - ``f'SMA_{sma_period}'``: Rolling simple moving average.
        - ``f'STD_{sma_period}'``: Rolling standard deviation.
        - ``'Z_Score'``:           Standardised deviation from SMA.

    Args:
        df:         OHLCV DataFrame with a ``'close'`` column.
        sma_period: Look-back window (default: 50).

    Returns:
        The same DataFrame with three new columns.
    """
    df[f'SMA_{sma_period}'] = df['close'].rolling(window=sma_period).mean()
    df[f'STD_{sma_period}'] = df['close'].rolling(window=sma_period).std()

    std_col = df[f'STD_{sma_period}'].replace(0, np.nan)
    df['Z_Score'] = (df['close'] - df[f'SMA_{sma_period}']) / std_col

    return df


def calculate_sdc(
    df: pd.DataFrame,
    sma_period: int = 50,
    multiplier: float = 2.0,
) -> pd.DataFrame:
    """Calculate the Standard Deviation Channel (SDC) around a SMA.

    Bands::

        SDC_upper = SMA + multiplier × σ
        SDC_lower = SMA - multiplier × σ

    If ``SMA_{sma_period}`` and ``STD_{sma_period}`` columns already
    exist (e.g. from a prior ``calculate_zscore`` call), they are reused.

    Args:
        df:         OHLCV DataFrame with a ``'close'`` column.
        sma_period: Look-back window used for both SMA and σ (default: 50).
        multiplier: Sigma multiplier for band width (default: 2.0).

    Returns:
        The same DataFrame with ``'SDC_upper'`` and ``'SDC_lower'`` columns.
    """
    sma_col = f'SMA_{sma_period}'
    std_col = f'STD_{sma_period}'

    if sma_col not in df.columns:
        df[sma_col] = df['close'].rolling(window=sma_period).mean()
    if std_col not in df.columns:
        df[std_col] = df['close'].rolling(window=sma_period).std()

    df['SDC_upper'] = df[sma_col] + multiplier * df[std_col]
    df['SDC_lower'] = df[sma_col] - multiplier * df[std_col]

    return df


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calculate Average True Range (ATR) using Wilder's Smoothing.

    True Range is the maximum of:
        - High − Low
        - |High − Previous Close|
        - |Low  − Previous Close|

    ATR is the EWM (alpha = 1/period) of True Range.

    Args:
        df:     OHLCV DataFrame with ``'high'``, ``'low'``, ``'close'`` columns.
        period: Look-back window (default: 14).

    Returns:
        The same DataFrame with an added ``'ATR'`` column.
    """
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['ATR'] = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return df


# ══════════════════════════════════════════════════════════════════════
#  ORDER BLOCK DETECTION (with Volume Filter)
# ══════════════════════════════════════════════════════════════════════

def detect_order_blocks(
    df: pd.DataFrame,
    lookback: int = 15,
    volume_multiplier: float = OB_VOLUME_MULTIPLIER,
    volume_ma_period: int = OB_VOLUME_MA_PERIOD,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Detect SMC Order Blocks (Bullish and Bearish) with volume confirmation.

    An Order Block is only valid if the block-forming candle's volume is
    greater than ``volume_multiplier × volume_ma_period``-period volume MA.

    Algorithm:
        Bullish OB — look left from the recent swing low for the last
        bearish candle (close < open) whose volume passes the filter.

        Bearish OB — look left from the recent swing high for the last
        bullish candle (close > open) whose volume passes the filter.

    Args:
        df:                OHLCV DataFrame with at least ``lookback`` rows.
        lookback:          Number of recent candles to search for swing
                           highs/lows (default: 15).
        volume_multiplier: Minimum ratio of OB candle volume to MA volume
                           for a valid block (default: ``OB_VOLUME_MULTIPLIER``).
        volume_ma_period:  Period for the volume moving average baseline
                           (default: ``OB_VOLUME_MA_PERIOD``).

    Returns:
        A ``(bullish_ob, bearish_ob)`` tuple where each element is either:
            - ``None`` — no valid block found.
            - ``dict`` with keys: ``high``, ``low``, ``timestamp``,
              ``volume``, ``vol_ma``, ``vol_ratio``.

    Edge cases:
        - ``len(df) < lookback`` → returns ``(None, None)`` immediately.
        - If no candle in the search range passes the volume filter, the
          corresponding OB is ``None``.
    """
    if len(df) < lookback:
        return None, None

    df_vol_ma = df['volume'].rolling(window=volume_ma_period).mean()
    recent_df = df.iloc[-lookback:]

    # ── Bullish OB: search left from the recent swing low ──
    min_idx = recent_df['low'].idxmin()
    min_loc = df.index.get_loc(min_idx)

    bullish_ob: Optional[Dict[str, Any]] = None
    for i in range(min_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] < row['open']:
            vol_ma_at_i = (
                df_vol_ma.iloc[i]
                if i < len(df_vol_ma) and pd.notna(df_vol_ma.iloc[i])
                else 0
            )
            if vol_ma_at_i > 0 and row['volume'] > volume_multiplier * vol_ma_at_i:
                bullish_ob = {
                    'high':      row['high'],
                    'low':       row['low'],
                    'timestamp': row['timestamp'],
                    'volume':    row['volume'],
                    'vol_ma':    vol_ma_at_i,
                    'vol_ratio': row['volume'] / vol_ma_at_i,
                }
            else:
                continue
            break

    # ── Bearish OB: search left from the recent swing high ──
    max_idx = recent_df['high'].idxmax()
    max_loc = df.index.get_loc(max_idx)

    bearish_ob: Optional[Dict[str, Any]] = None
    for i in range(max_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] > row['open']:
            vol_ma_at_i = (
                df_vol_ma.iloc[i]
                if i < len(df_vol_ma) and pd.notna(df_vol_ma.iloc[i])
                else 0
            )
            if vol_ma_at_i > 0 and row['volume'] > volume_multiplier * vol_ma_at_i:
                bearish_ob = {
                    'high':      row['high'],
                    'low':       row['low'],
                    'timestamp': row['timestamp'],
                    'volume':    row['volume'],
                    'vol_ma':    vol_ma_at_i,
                    'vol_ratio': row['volume'] / vol_ma_at_i,
                }
            else:
                continue
            break

    return bullish_ob, bearish_ob


# ══════════════════════════════════════════════════════════════════════
#  BREAKOUT DETECTION (with Volume Filter)
# ══════════════════════════════════════════════════════════════════════

def detect_consolidation_breakout(
    df: pd.DataFrame,
    lookback: int = BREAKOUT_CONSOLIDATION_PERIOD,
    volume_multiplier: float = BREAKOUT_VOLUME_MULTIPLIER,
    volume_ma_period: int = OB_VOLUME_MA_PERIOD,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Detect a volume-confirmed breakout from a consolidation range.

    A breakout is confirmed only when the **current** candle:
        1. Closes above the consolidation high (bullish) or below the
           consolidation low (bearish).
        2. Has a volume > ``volume_multiplier × volume_MA``.

    The consolidation range is defined by the ``lookback`` candles
    *before* the current candle (the current candle itself is excluded
    from the range calculation to avoid look-ahead bias).

    Args:
        df:                OHLCV DataFrame.
        lookback:          Number of candles forming the consolidation
                           range (default: ``BREAKOUT_CONSOLIDATION_PERIOD``).
        volume_multiplier: Minimum ratio of breakout candle volume to MA
                           volume (default: ``BREAKOUT_VOLUME_MULTIPLIER``).
        volume_ma_period:  Period for the volume MA baseline
                           (default: ``OB_VOLUME_MA_PERIOD``).

    Returns:
        ``(bullish_breakout, bearish_breakout)`` tuple. Each is either
        ``None`` or a ``dict`` with keys: ``consolidation_high``,
        ``consolidation_low``, ``breakout_candle_low`` / ``_high``,
        ``volume``, ``vol_ratio``.

    Edge cases:
        - ``len(df) < lookback + 1`` → returns ``(None, None)``.
        - If volume filter fails → both returns are ``None``.
    """
    if len(df) < lookback + 1:
        return None, None

    df_vol_ma = df['volume'].rolling(window=volume_ma_period).mean()
    recent_df = df.iloc[-(lookback + 1):-1]
    current_candle = df.iloc[-1]

    consol_high = recent_df['high'].max()
    consol_low = recent_df['low'].min()

    vol_ma_at_current = df_vol_ma.iloc[-1]

    bullish_breakout: Optional[Dict[str, Any]] = None
    bearish_breakout: Optional[Dict[str, Any]] = None

    if vol_ma_at_current > 0 and current_candle['volume'] > volume_multiplier * vol_ma_at_current:
        if current_candle['close'] > consol_high:
            bullish_breakout = {
                'consolidation_high':  consol_high,
                'consolidation_low':   consol_low,
                'breakout_candle_low': current_candle['low'],
                'volume':              current_candle['volume'],
                'vol_ratio':           current_candle['volume'] / vol_ma_at_current,
            }
        elif current_candle['close'] < consol_low:
            bearish_breakout = {
                'consolidation_high':   consol_high,
                'consolidation_low':    consol_low,
                'breakout_candle_high': current_candle['high'],
                'volume':               current_candle['volume'],
                'vol_ratio':            current_candle['volume'] / vol_ma_at_current,
            }

    return bullish_breakout, bearish_breakout


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY C — WHALE HUNTER (VOLUME ANOMALY DETECTION)
# ══════════════════════════════════════════════════════════════════════

def detect_whale_strike(
    df: pd.DataFrame,
    threshold: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Detect Strategy C: Whale Hunter — Volume Anomaly Spike.

    Monitors the most recent 5m candle for sudden, massive volume spikes
    greater than ``threshold`` times the 50-period volume SMA.

    The prior 50 candles are used as the baseline (``shift(1)``) to avoid
    look-ahead bias on the current candle being evaluated.

    Args:
        df:        OHLCV DataFrame with at least 51 rows.
        threshold: Minimum vol/SMA ratio to trigger a whale alert.
                   Defaults to ``WHALE_VOLUME_MULTIPLIER`` from config.

    Returns:
        ``None`` if no anomaly is detected, otherwise a ``dict`` with:
            - ``is_whale``    : True
            - ``direction``   : ``'LONG'`` or ``'SHORT'``
            - ``vol_ratio``   : float — ratio of current to baseline volume
            - ``candle_high`` : float
            - ``candle_low``  : float
            - ``candle_close``: float

    Edge cases:
        - Fewer than 50 rows → returns ``None``.
        - Doji candle (close == open) → returns ``None`` (no direction).
        - NaN or zero baseline volume → returns ``None``.
    """
    if threshold is None:
        threshold = WHALE_VOLUME_MULTIPLIER

    if len(df) < 50:
        return None

    vol_sma50_prior = df['volume'].shift(1).rolling(50).mean()
    baseline_vol_sma = vol_sma50_prior.iloc[-1]
    current_vol = df['volume'].iloc[-1]

    if pd.isna(baseline_vol_sma) or baseline_vol_sma <= 0:
        return None

    vol_ratio = current_vol / baseline_vol_sma

    if vol_ratio >= threshold:
        open_p = float(df['open'].iloc[-1])
        close_p = float(df['close'].iloc[-1])
        high_p = float(df['high'].iloc[-1])
        low_p = float(df['low'].iloc[-1])

        if close_p > open_p:
            direction = 'LONG'
        elif close_p < open_p:
            direction = 'SHORT'
        else:
            return None  # Doji — no directional conviction

        return {
            'is_whale':    True,
            'direction':   direction,
            'vol_ratio':   round(float(vol_ratio), 2),
            'candle_high': high_p,
            'candle_low':  low_p,
            'candle_close': close_p,
        }

    return None
