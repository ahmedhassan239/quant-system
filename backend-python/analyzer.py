import os
import logging
import requests
import traceback
import numpy as np
import pandas as pd

logger = logging.getLogger("Analyzer")
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
from datetime import datetime
from sqlalchemy import func
from database import (SessionLocal, MarketData, TradingSignal, PortfolioState, BotLog,
                      TradeHistory, engine, init_db, init_shared_db, count_active_positions,
                      save_macro_state, get_macro_trend,
                      SLOT_BUDGET, MAX_CONCURRENT_POSITIONS, TOTAL_CAPITAL)
from config import (TIMEFRAME, ALERT_PREFIX, ENGINE_ROLE,
                    MACRO_SMA_PERIOD, MACRO_SDC_MULTIPLIER,
                    ZSCORE_LONG_THRESHOLD, ZSCORE_SHORT_THRESHOLD,
                    ZSCORE_SMA_PERIOD, OB_VOLUME_MULTIPLIER, OB_VOLUME_MA_PERIOD,
                    BREAKOUT_VOLUME_MULTIPLIER, BREAKOUT_CONSOLIDATION_PERIOD,
                    WHALE_VOLUME_MULTIPLIER,
                    MAX_SCALE_INS, PYRAMID_TIER1_PNL, PYRAMID_TIER2_PNL,
                    PYRAMID_TIER1_SIZE_PCT, PYRAMID_TIER2_SIZE_PCT,
                    TESTNET_FORCE_TRADES, HARD_STOP_LOSS_PCT, STOP_LOSS_PCT,
                    MAX_GLOBAL_POSITIONS, TSL_ACTIVATION_PCT, TSL_TRAIL_PCT,
                    TRAILING_ACTIVATE_PCT, TRAILING_DISTANCE_PCT,
                    ATR_PERIOD, REGIME_RISK_PARAMS,
                    # Alpha Mode — Micro-Management Kill Switches
                    DISABLE_STAGNANT_EXIT, DISABLE_TREND_REVERSAL_EJECT,
                    # Strategy D — Crash Catcher (Extreme Mean Reversion Engine)
                    CRASH_CATCHER_ZSCORE_LONG, CRASH_CATCHER_ZSCORE_SHORT,
                    CRASH_CATCHER_RSI_LONG, CRASH_CATCHER_RSI_SHORT,
                    CRASH_CATCHER_VOL_MULT, CRASH_CATCHER_VOL_MA_PERIOD,
                    CRASH_CATCHER_WICK_RATIO, CRASH_CATCHER_SL_BUFFER_PCT,
                    CRASH_CATCHER_TSL_ATR_MULT, CRASH_CATCHER_ALLOC_PCT)
from futures_executor import (open_position, close_position, get_futures_balance,
                              get_position_info, count_all_open_positions, set_stop_loss_order,
                              update_stop_loss_price, execute_partial_tp_scaleout)
from market_regime import MarketRegime, strategy_router, RegimeDetector

# ──────────────────────────────────────────────────────────────────────
#  RISK MANAGEMENT CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
MAX_BUYS = 1
ENTRY_WEIGHT = 1.0
TRADING_FEE = 0.001                       # 0.1 % per side
MIN_PROFIT_PCT = 0.01                     # +1.0 %
TSL_ACTIVATION_PCT = TSL_ACTIVATION_PCT   # +0.8 % unrealized PnL to activate TSL
TSL_TRAIL_PCT = TSL_TRAIL_PCT             # 0.4 % trailing distance from peak/trough
TRAILING_ACTIVATE_PCT = TSL_ACTIVATION_PCT # +0.8 % alias
TRAILING_DISTANCE_PCT = TSL_TRAIL_PCT     # 0.4 % alias
ATR_PERIOD = ATR_PERIOD                           # 14-period ATR
TRAILING_PULLBACK_PCT = 0.005             # -0.5 % (legacy, kept for compat)
HARD_STOP_LOSS_PCT = 0.05                 # 5.0 % absolute stop loss
STOP_LOSS_PCT = 0.05                      # 5.0 % trailing/soft stop loss
MAX_GLOBAL_POSITIONS = MAX_GLOBAL_POSITIONS  # Hard limit: max open positions on Binance (10)


# ══════════════════════════════════════════════════════════════════════
#  DATABASE LOGGING
# ══════════════════════════════════════════════════════════════════════
def log_to_db(session, symbol, action, message):
    try:
        log_entry = BotLog(
            symbol=symbol,
            action=action,
            message=message
        )
        session.add(log_entry)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Warning: Failed to save bot log to DB: {e}", flush=True)

# ══════════════════════════════════════════════════════════════════════
#  STATISTICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════

def calculate_rsi(df, period=14):
    """
    Calculate 14-period RSI using pure Pandas and Wilder's Smoothing.
    """
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    df.loc[avg_loss == 0, 'RSI'] = 100.0
    return df


def calculate_zscore(df, sma_period=50):
    """
    Calculate Z-Score of price relative to a rolling SMA.

    Z = (close - SMA) / σ

    where σ is the rolling standard deviation over the same window.
    """
    df[f'SMA_{sma_period}'] = df['close'].rolling(window=sma_period).mean()
    df[f'STD_{sma_period}'] = df['close'].rolling(window=sma_period).std()

    # Avoid division by zero
    std_col = df[f'STD_{sma_period}'].replace(0, np.nan)
    df['Z_Score'] = (df['close'] - df[f'SMA_{sma_period}']) / std_col

    return df


def calculate_sdc(df, sma_period=50, multiplier=2.0):
    """
    Calculate Standard Deviation Channel (SDC).

    Upper = SMA + multiplier × σ
    Lower = SMA - multiplier × σ
    """
    sma_col = f'SMA_{sma_period}'
    std_col = f'STD_{sma_period}'

    # Ensure SMA and STD are already computed
    if sma_col not in df.columns:
        df[sma_col] = df['close'].rolling(window=sma_period).mean()
    if std_col not in df.columns:
        df[std_col] = df['close'].rolling(window=sma_period).std()

    df['SDC_upper'] = df[sma_col] + multiplier * df[std_col]
    df['SDC_lower'] = df[sma_col] - multiplier * df[std_col]

    return df


def calculate_atr(df, period=14):
    """
    Calculate 14-period Average True Range (ATR) using pure Pandas and Wilder's Smoothing.
    True Range = max(high - low, abs(high - prev_close), abs(low - prev_close))
    """
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['ATR'] = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return df


# ══════════════════════════════════════════════════════════════════════
#  ORDER BLOCK DETECTION (with Volume Filter)
# ══════════════════════════════════════════════════════════════════════

def detect_order_blocks(df, lookback=15, volume_multiplier=OB_VOLUME_MULTIPLIER,
                        volume_ma_period=OB_VOLUME_MA_PERIOD):
    """
    Detect SMC Order Blocks (Bullish and Bearish) with volume confirmation.

    An OB is only valid if the block-forming candle's volume is greater than
    `volume_multiplier` × the `volume_ma_period`-period moving average volume.

    Returns (bullish_ob, bearish_ob) — each is a dict or None.
    """
    if len(df) < lookback:
        return None, None

    # Pre-compute volume MA for the entire DataFrame
    df_vol_ma = df['volume'].rolling(window=volume_ma_period).mean()

    recent_df = df.iloc[-lookback:]

    # -- Bullish OB --
    min_idx = recent_df['low'].idxmin()
    min_loc = df.index.get_loc(min_idx)

    bullish_ob = None
    for i in range(min_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] < row['open']:
            # Volume filter: OB candle volume > 1.5x the 20-period avg
            vol_ma_at_i = df_vol_ma.iloc[i] if i < len(df_vol_ma) and pd.notna(df_vol_ma.iloc[i]) else 0
            if vol_ma_at_i > 0 and row['volume'] > volume_multiplier * vol_ma_at_i:
                bullish_ob = {
                    'high': row['high'],
                    'low': row['low'],
                    'timestamp': row['timestamp'],
                    'volume': row['volume'],
                    'vol_ma': vol_ma_at_i,
                    'vol_ratio': row['volume'] / vol_ma_at_i,
                }
            # If volume filter fails, keep searching for a valid OB
            else:
                continue
            break

    # -- Bearish OB --
    max_idx = recent_df['high'].idxmax()
    max_loc = df.index.get_loc(max_idx)

    bearish_ob = None
    for i in range(max_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] > row['open']:
            vol_ma_at_i = df_vol_ma.iloc[i] if i < len(df_vol_ma) and pd.notna(df_vol_ma.iloc[i]) else 0
            if vol_ma_at_i > 0 and row['volume'] > volume_multiplier * vol_ma_at_i:
                bearish_ob = {
                    'high': row['high'],
                    'low': row['low'],
                    'timestamp': row['timestamp'],
                    'volume': row['volume'],
                    'vol_ma': vol_ma_at_i,
                    'vol_ratio': row['volume'] / vol_ma_at_i,
                }
            else:
                continue
            break

    return bullish_ob, bearish_ob


# ══════════════════════════════════════════════════════════════════════
#  BREAKOUT DETECTION (with Volume Filter)
# ══════════════════════════════════════════════════════════════════════

def detect_consolidation_breakout(df, lookback=BREAKOUT_CONSOLIDATION_PERIOD, volume_multiplier=BREAKOUT_VOLUME_MULTIPLIER,
                                  volume_ma_period=OB_VOLUME_MA_PERIOD):
    if len(df) < lookback + 1:
        return None, None

    df_vol_ma = df['volume'].rolling(window=volume_ma_period).mean()
    recent_df = df.iloc[-(lookback+1):-1]
    current_candle = df.iloc[-1]
    
    consol_high = recent_df['high'].max()
    consol_low = recent_df['low'].min()
    
    vol_ma_at_current = df_vol_ma.iloc[-1]
    
    bullish_breakout = None
    bearish_breakout = None
    
    if vol_ma_at_current > 0 and current_candle['volume'] > volume_multiplier * vol_ma_at_current:
        if current_candle['close'] > consol_high:
            bullish_breakout = {
                'consolidation_high': consol_high,
                'consolidation_low': consol_low,
                'breakout_candle_low': current_candle['low'],
                'volume': current_candle['volume'],
                'vol_ratio': current_candle['volume'] / vol_ma_at_current,
            }
        elif current_candle['close'] < consol_low:
            bearish_breakout = {
                'consolidation_high': consol_high,
                'consolidation_low': consol_low,
                'breakout_candle_high': current_candle['high'],
                'volume': current_candle['volume'],
                'vol_ratio': current_candle['volume'] / vol_ma_at_current,
            }
            
    return bullish_breakout, bearish_breakout


# ══════════════════════════════════════════════════════════════════════
#  TELEGRAM HELPER
# ══════════════════════════════════════════════════════════════════════

def send_telegram_alert(message):
    """
    Send a notification message to Telegram.
    Fails silently to avoid crashing the trading bot.
    """
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')

    if not token or not chat_id:
        print("Telegram credentials not configured. Skipping alert.", flush=True)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    escaped_message = message.replace("_", "\\_")
    payload = {"chat_id": chat_id, "text": escaped_message, "parse_mode": "Markdown"}

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 400 and "can't parse entities" in response.text.lower():
            print(f"Telegram Markdown parse failed ({response.text}). Retrying without parse_mode...", flush=True)
            payload_plain = {"chat_id": chat_id, "text": message}
            response = requests.post(url, json=payload_plain, timeout=10)
        if response.status_code == 200:
            print("Telegram alert sent successfully.", flush=True)
        else:
            print(f"Telegram API error: {response.status_code} - {response.text}", flush=True)
    except requests.exceptions.RequestException as e:
        print(f"Failed to send Telegram alert: {e}", flush=True)
        traceback.print_exc()


def send_periodic_report(futures_client=None):
    """
    Generate and send an automated periodic PNL and portfolio status report via Telegram.
    Summarizes:
      - Total Realized PNL (sum of trade_history.pnl_usd)
      - Win Rate and Total Closed Trades Count
      - Active Positions count and Total Unrealized PNL
      - Available Wallet Balance
    """
    session = SessionLocal()
    try:
        # 1. Total Realized PNL from trade_history
        realized_pnl_result = session.query(func.sum(TradeHistory.pnl_usd)).scalar()
        realized_pnl = float(realized_pnl_result) if realized_pnl_result is not None else 0.0

        # 2. Win Rate & Trade Counts
        total_trades = session.query(TradeHistory).count()
        winning_trades = session.query(TradeHistory).filter(TradeHistory.outcome == 'WIN').count()
        win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

        # 3. Active Positions & Unrealized PNL
        active_db_positions = session.query(PortfolioState).filter(
            PortfolioState.asset_balance > 0.000001,
            PortfolioState.decision.in_(['LONG', 'SHORT'])
        ).all()
        active_count = len(active_db_positions)

        unrealized_pnl = 0.0
        wallet_balance = 0.0

        if futures_client:
            try:
                # Query live account balance & positions from Binance Testnet
                wallet_balance = get_futures_balance(futures_client)
                pos_risk = futures_client.futures_position_information()
                live_active = 0
                for p in pos_risk:
                    amt = float(p.get('positionAmt', 0))
                    if amt != 0:
                        mark_price = float(p.get('markPrice') or p.get('entryPrice') or 0)
                        if abs(amt) * mark_price >= 2.0:
                            live_active += 1
                            unrealized_pnl += float(p.get('unRealizedProfit', 0))
                if live_active > 0:
                    active_count = live_active
            except Exception as e:
                print(f"⚠️ Warning querying live Binance account for report: {e}", flush=True)

        if wallet_balance == 0.0 and active_db_positions:
            wallet_balance = float(active_db_positions[0].usdt_balance or 0.0)

        # 4. Format Telegram Report Message
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        realized_sign = "+" if realized_pnl >= 0 else ""
        unrealized_sign = "+" if unrealized_pnl >= 0 else ""

        report_msg = (
            f"📊 *{ALERT_PREFIX} Periodic PNL Report* 📊\n"
            f"──────────────────────────────\n"
            f"💰 *Realized PNL:* `${realized_sign}{realized_pnl:,.2f}`\n"
            f"📈 *Win Rate:* `{win_rate:.1f}%` ({winning_trades}/{total_trades} Trades)\n"
            f"🟢 *Active Positions:* `{active_count}`\n"
            f"🔄 *Unrealized PNL:* `${unrealized_sign}{unrealized_pnl:,.2f}`\n"
            f"💵 *Wallet Balance:* `${wallet_balance:,.2f}`\n"
            f"──────────────────────────────\n"
            f"⏰ *Generated:* `{now_str}`"
        )

        print("=" * 60, flush=True)
        print("📊 [PERIODIC REPORT] Sending Telegram Summary...", flush=True)
        print(report_msg, flush=True)
        print("=" * 60, flush=True)

        send_telegram_alert(report_msg)
        return report_msg

    except Exception as e:
        session.rollback()
        print(f"❌ Error generating periodic report: {e}", flush=True)
        traceback.print_exc()
        return None
    finally:
        session.close()



# ══════════════════════════════════════════════════════════════════════
#  STRATEGY C — WHALE HUNTER (VOLUME ANOMALY DETECTION)
# ══════════════════════════════════════════════════════════════════════

def detect_whale_strike(df, threshold=None):
    """
    Detect Strategy C: Whale Hunter (Volume Anomaly Spike).
    Monitors 5m candle volume for sudden, massive volume spikes > WHALE_VOLUME_MULTIPLIER
    (default 10.0x) of the 50-period volume SMA.

    Returns:
        dict or None: {
            'is_whale': True,
            'direction': 'LONG' | 'SHORT',
            'vol_ratio': float,
            'candle_high': float,
            'candle_low': float,
            'candle_close': float
        }
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
            return None

        return {
            'is_whale': True,
            'direction': direction,
            'vol_ratio': round(float(vol_ratio), 2),
            'candle_high': high_p,
            'candle_low': low_p,
            'candle_close': close_p
        }

    return None


# ══════════════════════════════════════════════════════════════════════
#  STRATEGY D — CRASH CATCHER (EXTREME MEAN REVERSION ENGINE)
# ══════════════════════════════════════════════════════════════════════

def evaluate_extreme_reversion(
    df: pd.DataFrame,
    current_price: float,
    current_rsi: float | None,
    current_zscore: float | None,
    current_atr: float,
    in_position: bool,
    active_count: int,
    max_positions: int,
) -> dict | None:
    """
    Strategy D: Crash Catcher — Extreme Mean Reversion Engine.

    Activates ONLY during statistically extreme anomalies — events so
    rare that the primary trend-following engine is explicitly frozen
    (STORM regime, Z-Score < -3.0). This module is the second, concurrent
    execution logic that capitalises on the capitulation wicks and short
    squeezes that *follow* those crashes.

    ── Trigger Conditions (The Anomaly) ──────────────────────────────
      LONG  reversion: Z-Score < -3.5  AND  RSI < 25
      SHORT reversion: Z-Score > +3.5  AND  RSI > 75

    ── Confirmation (Catching the Bounce, not the Knife) ─────────────
      Gate 1 — Volume Anomaly:
        Current candle volume > CRASH_CATCHER_VOL_MULT (5x) times the
        CRASH_CATCHER_VOL_MA_PERIOD (20-period) volume moving average.
        Indicates Whale absorption or forced capitulation prints.

      Gate 2 — Pin Bar / Reversal Wick:
        LONG  → lower wick ratio > CRASH_CATCHER_WICK_RATIO (60%)
                i.e.  (open/close_min − low) / (high − low) > 0.6
                      The bottom is being aggressively defended.
        SHORT → upper wick ratio > CRASH_CATCHER_WICK_RATIO (60%)
                i.e.  (high − open/close_max) / (high − low) > 0.6
                      The top is being aggressively rejected.

    ── Risk Management (Hit and Run) ─────────────────────────────────
      Stop-Loss : wick tip ± CRASH_CATCHER_SL_BUFFER_PCT (0.1% buffer).
                  LONG  SL = candle_low  * (1 - 0.001)
                  SHORT SL = candle_high * (1 + 0.001)
                  If price pierces the wick, the thesis is wrong. Exit.

      TSL        : CRASH_CATCHER_TSL_ATR_MULT (0.5x ATR) activation —
                  activates much earlier than the primary engine (2x ATR)
                  to lock in the rubber-band bounce aggressively.

    ── Bypass Permissions (ONLY for this module) ─────────────────────
      ✅ Bypasses the STORM Freeze restriction — by running BEFORE the
         STORM override check in run_analyzer().
      ✅ Bypasses the Macro Hard Filter — by never passing through
         StrategyRouter.route(), which is where the filter lives.

    ── Architecture note ─────────────────────────────────────────────
      Pure function — no side effects, no DB access, no Telegram calls.
      The caller (run_analyzer) is responsible for execution and logging.

    Args:
        df            : 15m DataFrame with 'open','high','low','close','volume'
                        already populated. Must have at least 21 rows.
        current_price : Latest close price.
        current_rsi   : Pre-computed 14-period RSI for the current candle.
        current_zscore: Pre-computed Z-Score (50-period SMA based).
        current_atr   : Pre-computed 14-period ATR.
        in_position   : True if this symbol already has an open position.
        active_count  : Number of currently active positions across all symbols.
        max_positions : Hard cap on concurrent open positions.

    Returns:
        dict | None:
          On success:  {
            'direction'    : 'LONG' | 'SHORT',
            'strategy_type': 'CRASH_CATCHER_LONG' | 'CRASH_CATCHER_SHORT',
            'stop_loss'    : float,    # wick-based SL with buffer
            'wick_ratio'   : float,    # diagnostic (for logs/alerts)
            'vol_ratio'    : float,    # diagnostic (for logs/alerts)
            'candle_low'   : float,
            'candle_high'  : float,
            'candle_open'  : float,
            'candle_close' : float,
          }
          On failure: None
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
    long_trigger  = (current_zscore <= CRASH_CATCHER_ZSCORE_LONG  and
                     current_rsi    <  CRASH_CATCHER_RSI_LONG)
    short_trigger = (current_zscore >= CRASH_CATCHER_ZSCORE_SHORT and
                     current_rsi    >  CRASH_CATCHER_RSI_SHORT)

    if not long_trigger and not short_trigger:
        return None  # No anomaly — primary engine handles this cycle

    direction = 'LONG' if long_trigger else 'SHORT'

    # ── Gate 1: Volume Anomaly — Whale Absorption / Capitulation Print ─
    # Use prior candles (shift(1)) as the baseline to avoid look-ahead
    # bias on the current candle being evaluated.
    vol_ma_series = df['volume'].shift(1).rolling(CRASH_CATCHER_VOL_MA_PERIOD).mean()
    baseline_vol  = vol_ma_series.iloc[-1]
    current_vol   = float(df['volume'].iloc[-1])

    if pd.isna(baseline_vol) or baseline_vol <= 0:
        logger.debug(
            f"[CRASH CATCHER] Volume MA not yet available "
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
    last_candle  = df.iloc[-1]
    candle_open  = float(last_candle['open'])
    candle_close = float(last_candle['close'])
    candle_high  = float(last_candle['high'])
    candle_low   = float(last_candle['low'])

    candle_range = candle_high - candle_low

    if candle_range <= 0:
        # Doji with zero range — cannot compute wick ratios reliably
        logger.debug(f"[CRASH CATCHER] Zero candle range detected for {direction}. Skipping.")
        return None

    if direction == 'LONG':
        # Lower wick = distance from candle_low to the body bottom
        body_bottom = min(candle_open, candle_close)
        lower_wick  = body_bottom - candle_low
        wick_ratio  = lower_wick / candle_range

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
        body_top   = max(candle_open, candle_close)
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
        f"± {CRASH_CATCHER_SL_BUFFER_PCT*100:.1f}%)"
    )
    print(
        f"🚨 [CRASH CATCHER] {direction} triggered via Extreme Mean Reversion | "
        f"Z: {current_zscore:+.3f} | RSI: {current_rsi:.1f} | "
        f"Vol: {vol_ratio:.1f}x | Wick: {wick_ratio:.2f} | "
        f"SL: ${stop_loss:.4f}",
        flush=True,
    )

    return {
        'direction'    : direction,
        'strategy_type': strategy_type,
        'stop_loss'    : stop_loss,
        'wick_ratio'   : round(wick_ratio, 3),
        'vol_ratio'    : round(vol_ratio, 2),
        'candle_low'   : candle_low,
        'candle_high'  : candle_high,
        'candle_open'  : candle_open,
        'candle_close' : candle_close,
    }




def evaluate_pyramid_scale_in(portfolio, current_price, symbol, session, futures_client=None):
    """
    Evaluate and execute Pyramiding (Scaling Into Winners) for an active position.
    
    Tiers:
      - Tier 1 (dca_level == 0): Unrealized PnL >= +2.0%. Add 50% of initial size.
      - Tier 2 (dca_level == 1): Unrealized PnL >= +4.0%. Add 25% of initial size.

    Strict Risk Rule (Break-Even Mandate):
      - Calculate hypothetical new Average Entry Price.
      - Move Trailing Stop Loss to Break-Even (or better) for the combined position.
      - Abort scale-in if SL cannot be set safely.
    """
    if not portfolio or not portfolio.get('asset_balance') or portfolio['asset_balance'] <= 0:
        return False, portfolio

    dca_level = int(portfolio.get('dca_level', 0) or 0)
    if dca_level >= MAX_SCALE_INS:
        return False, portfolio

    direction = portfolio.get('position_direction')
    if not direction or direction not in ('LONG', 'SHORT'):
        return False, portfolio

    ep = float(portfolio.get('average_entry_price') or 0.0)
    cp = float(current_price)
    if ep <= 0 or cp <= 0:
        return False, portfolio

    # Calculate current unrealized PnL %
    if direction == 'LONG':
        unrealized_pnl = (cp - ep) / ep
    else:
        unrealized_pnl = (ep - cp) / ep

    # Check Tier Eligibility
    tier_num = 0
    scale_size_pct = 0.0

    if dca_level == 0 and unrealized_pnl >= PYRAMID_TIER1_PNL:
        tier_num = 1
        scale_size_pct = PYRAMID_TIER1_SIZE_PCT
    elif dca_level == 1 and unrealized_pnl >= PYRAMID_TIER2_PNL:
        tier_num = 2
        scale_size_pct = PYRAMID_TIER2_SIZE_PCT
    else:
        return False, portfolio

    # Quantities and Costs
    existing_qty = float(portfolio['asset_balance'])
    if dca_level == 0:
        initial_qty = existing_qty
    else:
        initial_qty = existing_qty / 1.50

    add_qty = initial_qty * scale_size_pct
    if add_qty <= 0:
        return False, portfolio

    existing_cost = existing_qty * ep
    add_cost = add_qty * cp
    new_qty = existing_qty + add_qty
    new_avg_entry_price = (existing_cost + add_cost) / new_qty

    # ── Strict Risk Rule: Break-Even Mandate ──
    # The new SL MUST be at or better than new_avg_entry_price
    current_sl = float(portfolio.get('stop_loss_price', 0) or 0)
    
    if direction == 'LONG':
        new_stop_loss = max(current_sl, new_avg_entry_price)
        if new_stop_loss >= cp:
            print(f"🛑 [PYRAMID ABORT] {symbol} LONG Tier {tier_num}: Proposed Break-Even SL (${new_stop_loss:.2f}) >= Current Price (${cp:.2f}). Aborting scale-in.", flush=True)
            return False, portfolio
    else:  # SHORT
        if current_sl > 0:
            new_stop_loss = min(current_sl, new_avg_entry_price)
        else:
            new_stop_loss = new_avg_entry_price
        if new_stop_loss <= cp:
            print(f"🛑 [PYRAMID ABORT] {symbol} SHORT Tier {tier_num}: Proposed Break-Even SL (${new_stop_loss:.2f}) <= Current Price (${cp:.2f}). Aborting scale-in.", flush=True)
            return False, portfolio

    # ── Execute Scale-In ──
    if futures_client:
        try:
            info = futures_client.futures_exchange_info()
            step_size = 0.001
            for s in info.get('symbols', []):
                if s['symbol'] == symbol:
                    for flt in s.get('filters', []):
                        if flt['filterType'] == 'LOT_SIZE':
                            step_size = float(flt['stepSize'])
                            break
                    break
            precision = len(str(step_size).rstrip('0').split('.')[-1]) if '.' in str(step_size) else 0
            order_qty = round(add_qty - (add_qty % step_size), precision)
            if order_qty <= 0:
                print(f"⚠️ [PYRAMID SKIP] {symbol}: Calculated scale-in qty too small after rounding.", flush=True)
                return False, portfolio

            side = 'BUY' if direction == 'LONG' else 'SELL'
            futures_client.futures_create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=order_qty
            )
            set_stop_loss_order(futures_client, symbol, direction, new_stop_loss)
        except Exception as exc:
            print(f"❌ [PYRAMID ERROR] Failed Binance scale-in order for {symbol}: {exc}", flush=True)
            return False, portfolio

    # Update Portfolio Dictionary & DB
    portfolio['dca_level'] = dca_level + 1
    portfolio['asset_balance'] = new_qty
    portfolio['average_entry_price'] = new_avg_entry_price
    portfolio['stop_loss_price'] = new_stop_loss
    portfolio['stop_loss'] = new_stop_loss
    portfolio['trailing_active'] = True

    pyramid_msg = (
        f"🔼 [PYRAMID] Scaled into winning position {symbol} ({direction}) | Tier {tier_num} ({unrealized_pnl*100:+.2f}% PnL) | "
        f"New Avg Price: ${new_avg_entry_price:.2f} | Added Qty: {add_qty:.4f} | SL moved to Break-Even: ${new_stop_loss:.2f}"
    )
    print(pyramid_msg, flush=True)
    log_to_db(session, symbol, "PYRAMID", pyramid_msg)
    send_telegram_alert(pyramid_msg)

    try:
        new_state = PortfolioState(
            timestamp=datetime.now(),
            symbol=symbol,
            decision=portfolio.get('decision', direction),
            current_price=cp,
            usdt_balance=portfolio.get('usdt_balance', 1000.0),
            asset_balance=new_qty,
            position_direction=direction,
            average_entry_price=new_avg_entry_price,
            highest_price_since_entry=portfolio.get('highest_price_since_entry', cp),
            lowest_price_since_entry=portfolio.get('lowest_price_since_entry', cp),
            stop_loss_price=new_stop_loss,
            stop_loss=new_stop_loss,
            trailing_active=True,
            partial_tp_hit=portfolio.get('partial_tp_hit', False),
            dca_level=portfolio['dca_level'],
            total_portfolio_value=portfolio.get('total_portfolio_value', 1000.0)
        )
        session.add(new_state)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Warning: Failed to persist pyramid scale-in to DB: {e}", flush=True)

    return True, portfolio


# ══════════════════════════════════════════════════════════════════════
#  TRADE UPGRADING (POSITION ROTATION) HELPERS
# ══════════════════════════════════════════════════════════════════════

def calculate_strength_score(z_score, vol_ratio=1.0):
    """
    Calculate a quantitative Strength Score for an entry signal or active position.
    Primary factors:
      - Absolute Z-Score (momentum / deviation extreme)
      - Volume Multiplier (liquidity / breakout confirmation / whale spikes)
    Formula:
      Strength Score = |Z-Score| * (1.0 + 0.1 * min(max(vol_ratio, 1.0), 10.0))
    """
    abs_z = abs(z_score) if z_score is not None else 0.0
    vr = float(vol_ratio) if vol_ratio is not None and float(vol_ratio) > 0 else 1.0
    # For Whale Strike volume spikes (vr >= 10x), give an effective Z baseline of 1.5 if Z is small
    if vr >= 8.0 and abs_z < 1.5:
        abs_z = 1.5

    vol_bonus = 0.1 * min(max(vr, 1.0), 10.0)
    score = abs_z * (1.0 + vol_bonus)
    return round(score, 3)


def find_weakest_active_position(session, futures_client=None):
    """
    Scan all active open positions from live Binance Futures API (or local DB).
    Evaluates each position's real-time PnL, duration, and Strength Score.
    Flags positions open >= 2 hours with minimal PnL (< 0.5%) as STAGNANT (Score = 0.0).

    Returns:
      (weakest_symbol, weakest_score, weakest_portfolio, is_stagnant)
      or (None, 0.0, None, False) if no active positions exist.
    """
    active_items = []

    # 1. Fetch live open positions from Binance Futures API if client is available
    if futures_client:
        try:
            info = futures_client.futures_position_information()
            for p in info:
                amt = float(p.get('positionAmt', 0))
                if amt != 0:
                    sym = p['symbol']
                    ep = float(p.get('entryPrice', 0))
                    cp = float(p.get('markPrice', 0) or ep)
                    direction = 'LONG' if amt > 0 else 'SHORT'
                    active_items.append({
                        'symbol': sym,
                        'amount': abs(amt),
                        'entry_price': ep,
                        'current_price': cp,
                        'direction': direction
                    })
        except Exception as e:
            print(f"Warning: Failed to fetch live Binance positions in weakest scanner: {e}", flush=True)

    # 2. Fallback to DB if futures_client not available or returned no items
    if not active_items:
        latest_ids = (
            session.query(func.max(PortfolioState.id).label('max_id'))
            .group_by(PortfolioState.symbol)
            .subquery()
        )

        db_rows = (
            session.query(PortfolioState)
            .filter(
                PortfolioState.id.in_(session.query(latest_ids.c.max_id)),
                func.abs(PortfolioState.asset_balance) > 0
            )
            .all()
        )

        for row in db_rows:
            active_items.append({
                'symbol': row.symbol,
                'amount': float(abs(row.asset_balance or 0)),
                'entry_price': float(row.average_entry_price or 0),
                'current_price': float(row.current_price or 0),
                'direction': row.position_direction or 'LONG'
            })

    if not active_items:
        return None, 0.0, None, False

    weakest_symbol = None
    weakest_score = float('inf')
    weakest_portfolio = None
    weakest_is_stagnant = False

    for item in active_items:
        sym = item['symbol']
        port = load_portfolio(session, sym)

        ep = item['entry_price'] or float(port.get('average_entry_price') or 0)
        cp = item['current_price'] or float(port.get('current_price') or ep)
        direction = item['direction'] or port.get('position_direction') or 'LONG'

        if not port.get('average_entry_price') or port.get('average_entry_price') == 0:
            port['average_entry_price'] = ep
            port['position_direction'] = direction
            port['asset_balance'] = item['amount']

        # Calculate position duration (in hours)
        first_entry = (
            session.query(PortfolioState.timestamp)
            .filter(PortfolioState.symbol == sym, PortfolioState.asset_balance != 0)
            .order_by(PortfolioState.id.asc())
            .first()
        )
        entry_time = first_entry[0] if first_entry else datetime.now()
        hours_open = (datetime.now() - entry_time).total_seconds() / 3600.0 if entry_time else 0.0

        # Calculate current unrealized PnL %
        pnl_pct = 0.0
        if ep > 0 and cp > 0:
            if direction == 'LONG':
                pnl_pct = ((cp - ep) / ep) * 100.0
            else:
                pnl_pct = ((ep - cp) / ep) * 100.0

        # Flag stagnant trades: open >= 2.0 hours with PnL < 0.5%
        is_stag = (hours_open >= 2.0 and pnl_pct < 0.5)

        # Fetch current signal Z-score for the position
        latest_sig = (
            session.query(TradingSignal)
            .filter(TradingSignal.symbol == sym)
            .order_by(TradingSignal.id.desc())
            .first()
        )
        z_val = latest_sig.z_score if (latest_sig and latest_sig.z_score is not None) else 0.0
        v_ratio = getattr(latest_sig, 'bullish_ob_vol_ratio', 1.0) or 1.0

        pos_score = 0.0 if is_stag else calculate_strength_score(z_val, v_ratio)

        if pos_score < weakest_score:
            weakest_score = pos_score
            weakest_symbol = sym
            weakest_portfolio = port
            weakest_is_stagnant = is_stag

    if weakest_score == float('inf'):
        weakest_score = 0.0

    return weakest_symbol, float(weakest_score or 0.0), weakest_portfolio, weakest_is_stagnant


# ══════════════════════════════════════════════════════════════════════
#  PORTFOLIO HELPERS
# ══════════════════════════════════════════════════════════════════════

def load_portfolio(session, symbol):
    """
    Load the latest portfolio state for a given symbol from the database.
    """
    last_state = session.query(PortfolioState).filter(
        PortfolioState.symbol == symbol
    ).order_by(PortfolioState.id.desc()).first()

    if last_state:
        sl_val = getattr(last_state, 'stop_loss_price', None)
        if sl_val is None:
            sl_val = getattr(last_state, 'stop_loss', None)

        # If stop_loss_price is None on the latest row but position is active, look up latest non-null local SL
        if sl_val is None and getattr(last_state, 'asset_balance', 0) and float(last_state.asset_balance) > 0:
            prev_sl = session.query(PortfolioState.stop_loss_price, PortfolioState.stop_loss).filter(
                PortfolioState.symbol == symbol,
                PortfolioState.asset_balance > 0,
                (PortfolioState.stop_loss_price.isnot(None) | PortfolioState.stop_loss.isnot(None))
            ).order_by(PortfolioState.id.desc()).first()
            if prev_sl:
                sl_val = prev_sl.stop_loss_price if prev_sl.stop_loss_price is not None else prev_sl.stop_loss

        return {
            'usdt_balance': last_state.usdt_balance,
            'asset_balance': last_state.asset_balance,
            'average_entry_price': getattr(last_state, 'average_entry_price', None),
            'dca_level': getattr(last_state, 'dca_level', 0),
            'last_exec_price': getattr(last_state, 'last_exec_price', None),
            'total_cost': getattr(last_state, 'total_cost', 0.0),
            'highest_price_since_entry': last_state.highest_price_since_entry,
            'lowest_price_since_entry': getattr(last_state, 'lowest_price_since_entry', None),
            'position_direction': getattr(last_state, 'position_direction', None),
            'stop_loss_price': float(sl_val) if sl_val is not None else None,
            'stop_loss': float(sl_val) if sl_val is not None else None,
            'trailing_active': getattr(last_state, 'trailing_active', False),
            'partial_tp_hit': getattr(last_state, 'partial_tp_hit', False),
            'strategy': getattr(last_state, 'strategy', None),
        }
    return {
        'usdt_balance': SLOT_BUDGET,
        'asset_balance': 0.0,
        'average_entry_price': None,
        'dca_level': 0,
        'last_exec_price': None,
        'total_cost': 0.0,
        'highest_price_since_entry': None,
        'lowest_price_since_entry': None,
        'position_direction': None,
        'stop_loss_price': None,
        'stop_loss': None,
        'trailing_active': False,
        'partial_tp_hit': False,
        'strategy': None,
    }


def _close_position_handler(portfolio, current_price, symbol, session, exit_reason,
                            futures_client=None, bullish_ob=None, bearish_ob=None,
                            current_rsi=None, current_zscore=None, macro_info=None):
    """
    Close an open position (LONG or SHORT): convert asset → USDT, compute PnL,
    optionally execute on Futures exchange, save to DB, and send a Telegram alert.
    Returns the updated portfolio dict.
    """
    # ── 1. Pull accurately from active position / live Binance position ──
    live_pos = None
    if futures_client:
        try:
            live_pos = get_position_info(futures_client, symbol)
        except Exception as e:
            print(f"⚠️ [{symbol}] Error checking live position in close handler: {e}", flush=True)

    direction = portfolio.get('position_direction') or portfolio.get('direction')
    asset_balance = float(portfolio.get('asset_balance') or 0.0)
    buy_price = portfolio.get('average_entry_price') or portfolio.get('entry_price')

    if live_pos and live_pos.get('size', 0) > 0:
        if not direction or direction == 'None':
            direction = live_pos.get('direction')
        if asset_balance <= 0:
            asset_balance = float(live_pos.get('size', 0.0))
        if not buy_price or float(buy_price) <= 0:
            buy_price = float(live_pos.get('entry_price', 0.0))

    # ── 2. Fallback: NEVER pass None to a non-null column ──
    if not direction or direction == 'None':
        direction = 'LONG'
    if not buy_price or float(buy_price) <= 0:
        buy_price = float(current_price)
    if not asset_balance or asset_balance < 0:
        asset_balance = 0.0

    total_cost = float(portfolio.get('total_cost') or (asset_balance * float(buy_price)))
    pnl_pct_val = None
    pnl_usd_val = None
    pnl_section = ""

    # ── Calculate PnL ──
    if buy_price and float(buy_price) > 0:
        ep = float(buy_price)
        cp = float(current_price)
        if direction == 'LONG':
            pnl_pct_val = ((cp - ep) / ep) * 100
            pnl_usd_val = (cp - ep) * asset_balance
            sell_value = asset_balance * cp
        else:  # SHORT
            pnl_pct_val = ((ep - cp) / ep) * 100
            pnl_usd_val = (ep - cp) * asset_balance
            sell_value = asset_balance * (2 * ep - cp)
        sign = "+" if pnl_usd_val >= 0 else ""
        pnl_section = f"\n- PnL (This Trade): {sign}{pnl_pct_val:.2f}% ({sign}${pnl_usd_val:.2f})"
    else:
        sell_value = asset_balance * float(current_price)

    # ── Execute Futures close order (use LIVE Binance position size) ──
    if futures_client and asset_balance > 0:
        import logging as _close_logging
        _close_logger = _close_logging.getLogger("FuturesExecutor")
        # Query the REAL position size from Binance to avoid quantity mismatches
        close_qty = live_pos['size'] if (live_pos and live_pos.get('size', 0) > 0) else asset_balance
        _close_logger.warning(
            f"🚨 SL TRIGGERED & EXECUTED: {symbol} at {current_price} "
            f"(exit_reason={exit_reason}, closing qty={close_qty})"
        )
        order = close_position(futures_client, symbol, direction, close_qty)
        if order:
            order_id = order.get('orderId', 'ALREADY_CLOSED') if isinstance(order, dict) else 'ALREADY_CLOSED'
            print(f"✅ [{symbol}] Futures CLOSE {direction} executed | OrderID: {order_id} | Qty: {close_qty}", flush=True)
        else:
            print(f"⚠️ [{symbol}] Futures CLOSE {direction} order failed. Aborting local DB update so it can retry.", flush=True)
            return portfolio

    # ── Reset portfolio ──
    close_label = f"CLOSE_{direction}"
    portfolio['usdt_balance'] += round(sell_value, 2)
    portfolio['asset_balance'] = 0.0
    portfolio['average_entry_price'] = None
    portfolio['dca_level'] = 0
    portfolio['last_exec_price'] = None
    portfolio['total_cost'] = 0.0
    portfolio['highest_price_since_entry'] = None
    portfolio['lowest_price_since_entry'] = None
    portfolio['position_direction'] = None

    total_value = float(portfolio['usdt_balance'])

    # 1. Archive to TradeHistory
    outcome = 'WIN' if pnl_usd_val and pnl_usd_val > 0 else 'LOSS'
    history_record = TradeHistory(
        symbol=symbol,
        direction=direction,
        entry_price=float(buy_price) if buy_price else 0.0,
        exit_price=float(current_price),
        quantity=float(asset_balance),
        pnl_usd=float(pnl_usd_val) if pnl_usd_val is not None else 0.0,
        pnl_pct=float(pnl_pct_val) if pnl_pct_val is not None else 0.0,
        outcome=outcome,
        exit_reason=exit_reason,
        closed_at=datetime.utcnow()
    )

    try:
        session.add(history_record)
        # 2. Delete active position from PortfolioState and save CLOSED record
        session.query(PortfolioState).filter(PortfolioState.symbol == symbol).delete(synchronize_session=False)
        closed_rec = PortfolioState(
            timestamp=datetime.now(),
            symbol=symbol,
            decision='CLOSED',
            current_price=float(current_price),
            usdt_balance=float(portfolio['usdt_balance']),
            asset_balance=0.0,
            position_direction=None,
            average_entry_price=None,
            dca_level=0,
            last_exec_price=None,
            total_cost=0.0,
            highest_price_since_entry=None,
            lowest_price_since_entry=None,
            stop_loss_price=None,
            stop_loss=None,
            strategy=portfolio.get('strategy'),
            trailing_active=False,
            pnl_pct=float(pnl_pct_val) if pnl_pct_val is not None else 0.0,
            pnl_usd=float(pnl_usd_val) if pnl_usd_val is not None else 0.0,
            total_portfolio_value=float(portfolio['usdt_balance'])
        )
        session.add(closed_rec)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Warning: Failed to archive trade and delete active position: {e}", flush=True)
        traceback.print_exc()

    # Build Telegram alert
    reason_labels = {
        'SIGNAL': '📉 Technical Signal (MTF Confluence)',
        'STOP_LOSS': f'🛑 Hard Stop-Loss (-{0:.1f}%)',
        'TRAILING_STOP': '📐 Trailing Stop (pulled back from extreme)',
        'TREND_REVERSAL_EJECT': '🚨 TREND REVERSAL EJECT — Macro thesis broken',
    }
    reason_text = reason_labels.get(exit_reason, exit_reason)

    rsi_info = f"- RSI: {float(current_rsi):.1f}\n" if current_rsi is not None else ""
    zscore_info = f"- Z-Score: {float(current_zscore):+.2f}\n" if current_zscore is not None else ""
    macro_str = ""
    if macro_info:
        macro_str = f"- Macro Trend: {macro_info.get('macro_trend', 'N/A')}\n"

    ob_info = ""
    if direction == 'LONG' and bearish_ob:
        ob_info = f"- Bearish OB: ${float(bearish_ob['low']):.2f} – ${float(bearish_ob['high']):.2f}\n"
    elif direction == 'SHORT' and bullish_ob:
        ob_info = f"- Bullish OB: ${float(bullish_ob['low']):.2f} – ${float(bullish_ob['high']):.2f}\n"

    alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')

    alert_msg = (
        f"{ALERT_PREFIX} \U0001f6a8 *QUANT ALERT: CLOSE {direction}* \U0001f6a8\n"
        f"\n"
        f"*Symbol:* {symbol}\n"
        f"*Price:* ${float(current_price):.2f}\n"
        f"*Time:* {alert_time}\n"
        f"\n"
        f"\U0001f4a1 *Exit Reason:* {reason_text}\n"
        f"{macro_str}"
        f"{rsi_info}"
        f"{zscore_info}"
        f"{ob_info}"
        f"\n"
        f"\U0001f4bc *Virtual Portfolio:*{pnl_section}\n"
        f"- Slot Budget: ${SLOT_BUDGET:,.0f}\n"
        f"- Global Total Capital: ${TOTAL_CAPITAL:,.0f}\n"
        f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
        f"- Asset Balance: {portfolio['asset_balance']:.6f}\n"
        f"- Total Value: ${total_value:.2f}"
    )
    send_telegram_alert(alert_msg)

    return portfolio


def _save_tracking_update(portfolio, current_price, symbol, session, futures_client=None):
    """
    Save a portfolio snapshot that only updates highest/lowest_price_since_entry.
    """
    total_value = float(portfolio['usdt_balance']) + (float(portfolio['asset_balance']) * float(current_price))

    db_decision = 'HOLD'
    db_position_direction = portfolio.get('position_direction')
    db_entry_price = float(portfolio['average_entry_price']) if portfolio['average_entry_price'] is not None else None
    db_pnl_usd = None

    if futures_client:
        pos_info = get_position_info(futures_client, symbol)
        if pos_info and pos_info['size'] > 0:
            db_decision = pos_info['direction']  # Force 'LONG' or 'SHORT'
            db_position_direction = pos_info['direction']
            db_entry_price = pos_info['entry_price']
            db_pnl_usd = pos_info['unrealized_pnl']

    portfolio_record = PortfolioState(
        timestamp=datetime.now(),
        symbol=symbol,
        decision=db_decision,
        current_price=float(current_price),
        usdt_balance=float(portfolio['usdt_balance']),
        asset_balance=float(portfolio['asset_balance']),
        position_direction=db_position_direction,
        average_entry_price=db_entry_price,
        dca_level=int(portfolio['dca_level']),
        last_exec_price=float(portfolio['last_exec_price']) if portfolio['last_exec_price'] is not None else None,
        total_cost=float(portfolio['total_cost']),
        highest_price_since_entry=float(portfolio['highest_price_since_entry']) if portfolio['highest_price_since_entry'] is not None else None,
        lowest_price_since_entry=float(portfolio['lowest_price_since_entry']) if portfolio['lowest_price_since_entry'] is not None else None,
        stop_loss_price=float(portfolio['stop_loss_price']) if portfolio.get('stop_loss_price') is not None else None,
        stop_loss=float(portfolio['stop_loss_price']) if portfolio.get('stop_loss_price') is not None else None,
        strategy=portfolio.get('strategy'),
        trailing_active=portfolio.get('trailing_active', False),
        partial_tp_hit=portfolio.get('partial_tp_hit', False),
        pnl_pct=None,
        pnl_usd=db_pnl_usd,
        total_portfolio_value=float(round(total_value, 2))
    )
    for attempt in range(3):
        try:
            session.add(portfolio_record)
            session.commit()
            break
        except Exception as e:
            session.rollback()
            if attempt == 2:
                print(f"Warning: Failed to save tracking update to DB after 3 attempts: {e}", flush=True)
                traceback.print_exc()
            else:
                import time; time.sleep(1)


# ══════════════════════════════════════════════════════════════════════
#  ENGINE A: MACRO TREND ANALYZER (1h)
# ══════════════════════════════════════════════════════════════════════

def run_macro_analyzer(symbol='BTCUSDT', timeframe=TIMEFRAME):
    """
    1h Macro Trend Engine.

    Computes:
      • SMA-50 (50-period Simple Moving Average)
      • Z-Score = (close - SMA) / σ
      • SDC (Standard Deviation Channel): SMA ± 2σ

    Decision:
      • If close > SMA-50 AND Z-Score > 0 → UPTREND
      • Otherwise → DOWNTREND

    Writes result to the shared MacroState DB table.
    """
    print(f"\n{'='*60}", flush=True)
    print(f"--- Macro Analyzer Started [{symbol}] (1h Trend Engine) ---", flush=True)
    print(f"{'='*60}", flush=True)

    init_db()
    init_shared_db()

    session = SessionLocal()
    try:
        # Query 1h market data
        query = session.query(MarketData).filter(
            MarketData.symbol == symbol,
            MarketData.timeframe == timeframe
        ).order_by(MarketData.timestamp.asc())

        df = pd.read_sql(query.statement, engine)

        if df.empty:
            print(f"No 1h data found for {symbol}. Skipping.", flush=True)
            return

        if len(df) < MACRO_SMA_PERIOD:
            print(f"Insufficient data for {symbol}: {len(df)} candles "
                  f"(need {MACRO_SMA_PERIOD}). Skipping.", flush=True)
            return

        # 1. Compute statistical indicators
        df = calculate_zscore(df, sma_period=MACRO_SMA_PERIOD)
        df = calculate_sdc(df, sma_period=MACRO_SMA_PERIOD, multiplier=MACRO_SDC_MULTIPLIER)

        # 2. Get latest values
        last_row = df.iloc[-1]
        current_price = float(last_row['close'])
        sma_50 = float(last_row[f'SMA_{MACRO_SMA_PERIOD}'])
        z_score = float(last_row['Z_Score']) if pd.notna(last_row['Z_Score']) else 0.0
        std_dev = float(last_row[f'STD_{MACRO_SMA_PERIOD}']) if pd.notna(last_row[f'STD_{MACRO_SMA_PERIOD}']) else 0.0
        sdc_upper = float(last_row['SDC_upper']) if pd.notna(last_row['SDC_upper']) else None
        sdc_lower = float(last_row['SDC_lower']) if pd.notna(last_row['SDC_lower']) else None

        # 3. Determine macro trend
        if current_price > sma_50 and z_score > 0:
            macro_trend = 'UPTREND'
        else:
            macro_trend = 'DOWNTREND'

        # 4. Check for trend change (for alert purposes)
        old_state = get_macro_trend(symbol)
        old_trend = old_state['macro_trend'] if old_state else None
        trend_changed = (old_trend is not None and old_trend != macro_trend)

        # 5. Save to shared DB
        save_macro_state(
            symbol=symbol,
            macro_trend=macro_trend,
            sma_50=sma_50,
            z_score=z_score,
            std_dev=std_dev,
            sdc_upper=sdc_upper,
            sdc_lower=sdc_lower,
            current_price=current_price,
        )

        # 6. Log summary
        trend_emoji = "🟢" if macro_trend == 'UPTREND' else "🔴"
        print(f"\n=== Macro Summary [{symbol}] ===", flush=True)
        print(f"  Price:     ${current_price:,.2f}", flush=True)
        print(f"  SMA-50:    ${sma_50:,.2f}", flush=True)
        print(f"  Z-Score:   {z_score:+.3f}", flush=True)
        print(f"  σ (STD):   ${std_dev:,.2f}", flush=True)
        if sdc_upper and sdc_lower:
            print(f"  SDC Upper: ${sdc_upper:,.2f}", flush=True)
            print(f"  SDC Lower: ${sdc_lower:,.2f}", flush=True)
        print(f"  Trend:     {trend_emoji} {macro_trend}", flush=True)
        if trend_changed:
            print(f"  ⚡ TREND CHANGED: {old_trend} → {macro_trend}", flush=True)

        # 7. Send Telegram alert on trend change
        if trend_changed:
            alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
            sdc_str = ""
            if sdc_upper and sdc_lower:
                sdc_str = f"- SDC Channel: ${sdc_lower:,.2f} — ${sdc_upper:,.2f}\n"

            alert_msg = (
                f"{ALERT_PREFIX} {trend_emoji} *MACRO TREND CHANGE: {macro_trend}*\n"
                f"\n"
                f"*Symbol:* {symbol}\n"
                f"*Price:* ${current_price:,.2f}\n"
                f"*Time:* {alert_time}\n"
                f"\n"
                f"\U0001f4ca *Statistical Analysis (1h):*\n"
                f"- SMA-50: ${sma_50:,.2f}\n"
                f"- Z-Score: {z_score:+.3f}\n"
                f"- Std Dev: ${std_dev:,.2f}\n"
                f"{sdc_str}"
                f"\n"
                f"Previous: {old_trend} → New: *{macro_trend}*"
            )
            send_telegram_alert(alert_msg)

    except Exception as e:
        session.rollback()
        print(f"Error during macro analysis of {symbol}: {e}", flush=True)
        traceback.print_exc()
    finally:
        session.close()
        print(f"--- Macro Analyzer Completed [{symbol}] ---", flush=True)


# ══════════════════════════════════════════════════════════════════════
#  ENGINE B: EXECUTION ANALYZER (15m)
# ══════════════════════════════════════════════════════════════════════

def run_analyzer(symbol='PAXGUSDT', timeframe=TIMEFRAME, futures_client=None):
    """
    15m Execution Engine with MTF Confluence.

    Before evaluating signals, fetches the macro_trend from the shared DB.

    Entry conditions (replaces old RSI-based logic):
      LONG:  macro_trend == 'UPTREND'  AND price touches Bullish OB
             AND 15m Z-Score < -1.5 (dynamic oversold)
      SHORT: macro_trend == 'DOWNTREND' AND price touches Bearish OB
             AND 15m Z-Score > +1.5 (dynamic overbought)

    All risk management (stop-loss, trailing stop, DCA) is preserved.
    """
    print(f"\n--- Execution Analyzer Started [{symbol}] (15m MTF Confluence) ---", flush=True)

    session = SessionLocal()
    try:
        # ── 0. Fetch macro trend from shared DB (Strict Requirement — No Bypass) ──
        macro_info = get_macro_trend(symbol)
        if (
            macro_info is None
            or not macro_info.get('macro_trend')
            or macro_info.get('sma_50') is None
            or float(macro_info.get('sma_50', 0)) <= 0
        ):
            msg = f"🛑 [SKIP] {symbol}: Insufficient 1h Macro history or invalid SMA-50."
            print(msg, flush=True)
            log_to_db(session, symbol, "SKIP", msg)
            return False

        macro_trend = macro_info['macro_trend']
        macro_zscore = float(macro_info['z_score']) if macro_info.get('z_score') is not None else 0.0
        macro_sma = float(macro_info['sma_50'])
        macro_emoji = "🟢" if macro_trend == 'UPTREND' else "🔴"

        print(f"  {macro_emoji} [{symbol}] Macro: {macro_trend} | "
              f"1h Z: {macro_zscore:+.2f} | SMA-50: ${macro_sma:,.2f}", flush=True)

        # ── 1. Query 15m market data ──
        query = session.query(MarketData).filter(
            MarketData.symbol == symbol,
            MarketData.timeframe == timeframe
        ).order_by(MarketData.timestamp.asc())

        df = pd.read_sql(query.statement, engine)

        if df.empty:
            print(f"No data found for {symbol}. Skipping.", flush=True)
            return

        # ── 2. Compute indicators ──
        df = calculate_rsi(df, period=14)
        df = calculate_zscore(df, sma_period=ZSCORE_SMA_PERIOD)
        df = calculate_atr(df, period=ATR_PERIOD)

        # ── 3. Detect volume-filtered Order Blocks ──
        bullish_ob, bearish_ob = detect_order_blocks(df, lookback=15)

        # ── 4. Get current candle data ──
        last_row = df.iloc[-1]
        current_price = float(last_row['close'])
        current_rsi = float(last_row['RSI']) if pd.notna(last_row['RSI']) else None
        current_zscore = float(last_row['Z_Score']) if pd.notna(last_row['Z_Score']) else None
        current_sma = float(last_row[f'SMA_{ZSCORE_SMA_PERIOD}']) if pd.notna(last_row[f'SMA_{ZSCORE_SMA_PERIOD}']) else None
        current_atr = float(last_row['ATR']) if pd.notna(last_row['ATR']) and float(last_row['ATR']) > 0 else (float(current_price) * 0.005)

        print(f"  📊 [{symbol}] 15m Z-Score: {current_zscore:+.3f} | "
              f"RSI: {current_rsi:.1f}" if current_zscore and current_rsi else
              f"  📊 [{symbol}] Indicators computing...", flush=True)

        # ── Strategy C Heartbeat: diagnose trend alignment + data starvation ──
        candle_count = len(df)
        sma_label = f"${current_sma:,.2f}" if current_sma else "None ⚠️ DATA STARVATION"
        if current_sma is None:
            trend_action = f"SKIP (SMA-50 is None — only {candle_count} candles, need ≥{ZSCORE_SMA_PERIOD})"
        elif macro_trend == 'UPTREND' and current_price > current_sma:
            trend_action = "✅ LONG eligible (Price > SMA)"
        elif macro_trend == 'DOWNTREND' and current_price < current_sma:
            trend_action = "✅ SHORT eligible (Price < SMA)"
        else:
            trend_action = f"⏸️ SKIP ({macro_trend} but price {'<' if current_price < current_sma else '>'} SMA)"
        print(f"  🔍 [{symbol}] Candles: {candle_count} | Macro: {macro_trend} | "
              f"Price: ${current_price:,.2f} | SMA-50: {sma_label} | {trend_action}", flush=True)

        # ── 5. Load portfolio state ──
        portfolio = load_portfolio(session, symbol)
        if futures_client:
            live_pos_start = get_position_info(futures_client, symbol)
            if live_pos_start and live_pos_start.get('size', 0) > 0:
                portfolio['asset_balance'] = live_pos_start['size']
                portfolio['average_entry_price'] = live_pos_start['entry_price']
                portfolio['position_direction'] = live_pos_start['direction']

        in_position = portfolio['asset_balance'] is not None and float(portfolio['asset_balance']) > 0
        entry_price = portfolio.get('average_entry_price')
        highest_price = portfolio.get('highest_price_since_entry')
        lowest_price = portfolio.get('lowest_price_since_entry')
        pos_direction = portfolio.get('position_direction')

        # ──────────────────────────────────────────────────────────
        #  RISK MANAGEMENT EXITS (checked BEFORE signal logic)
        # ──────────────────────────────────────────────────────────
        
        # ── Pre-compute Regime for Dynamic Risk Parameters ──
        active_regime, precomputed_meta = RegimeDetector.detect(df)
        active_mode_value = active_regime.value
        
        # ── Fetch Regime-Specific Risk Parameters ──
        rp = REGIME_RISK_PARAMS.get(active_mode_value, REGIME_RISK_PARAMS['RANGE'])
        sl_atr_mult = rp['SL_ATR_MULT']
        partial_tp_pct = rp['PARTIAL_TP_PCT']
        tsl_atr_activation_mult = rp['TSL_ATR_ACTIVATION_MULT']
        tsl_atr_trail_mult = rp['TSL_ATR_TRAIL_MULT']

        risk_exit_triggered = False

        if in_position and entry_price and float(entry_price) > 0:
            cp = float(current_price)
            ep = float(entry_price)
            stop_loss = float(portfolio.get('stop_loss_price', 0) or 0)
            trailing_active = portfolio.get('trailing_active', False)

            # ── 1. Trend Invalidation / Emergency Eject ──
            # ALPHA MODE: When DISABLE_TREND_REVERSAL_EJECT is True, the immediate
            # macro-flip eject is bypassed. The Dynamic ATR TSL handles exits organically.
            if not DISABLE_TREND_REVERSAL_EJECT:
                if pos_direction == 'LONG' and macro_trend == 'DOWNTREND':
                    msg = (f"🚨🔴 [{symbol}] TREND REVERSAL EJECT — LONG position vs "
                           f"DOWNTREND macro. Thesis invalidated. CLOSING IMMEDIATELY "
                           f"at ${cp:.2f} (Entry: ${ep:.2f})")
                    print(msg, flush=True)
                    log_to_db(session, symbol, "EXIT", msg)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session,
                        'TREND_REVERSAL_EJECT',
                        futures_client, bullish_ob, bearish_ob,
                        current_rsi, current_zscore, macro_info)
                    risk_exit_triggered = True

                elif pos_direction == 'SHORT' and macro_trend == 'UPTREND':
                    msg = (f"🚨🟢 [{symbol}] TREND REVERSAL EJECT — SHORT position vs "
                           f"UPTREND macro. Thesis invalidated. CLOSING IMMEDIATELY "
                           f"at ${cp:.2f} (Entry: ${ep:.2f})")
                    print(msg, flush=True)
                    log_to_db(session, symbol, "EXIT", msg)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session,
                        'TREND_REVERSAL_EJECT',
                        futures_client, bullish_ob, bearish_ob,
                        current_rsi, current_zscore, macro_info)
                    risk_exit_triggered = True
            else:
                if pos_direction == 'LONG' and macro_trend == 'DOWNTREND':
                    print(f"⏸️ [{symbol}] TREND REVERSAL EJECT DISABLED (Alpha Mode) — "
                          f"LONG vs DOWNTREND. ATR TSL will protect.", flush=True)
                elif pos_direction == 'SHORT' and macro_trend == 'UPTREND':
                    print(f"⏸️ [{symbol}] TREND REVERSAL EJECT DISABLED (Alpha Mode) — "
                          f"SHORT vs UPTREND. ATR TSL will protect.", flush=True)

            if pos_direction == 'LONG' and not risk_exit_triggered:
                position_size = float(portfolio.get('asset_balance', 0) or 0)
                if position_size <= 0 and futures_client:
                    try:
                        live_pos = get_position_info(futures_client, symbol)
                        if live_pos and live_pos.get('size', 0) > 0:
                            position_size = float(live_pos['size'])
                    except Exception:
                        pass
                position_value = abs(ep * position_size)
                unrealized_pnl = (cp - ep) * position_size
                unrealized_pct = (abs(unrealized_pnl) / position_value) if (position_value > 0 and unrealized_pnl > 0) else (unrealized_pnl / position_value if position_value > 0 else 0.0)

                # Track new peak price
                if highest_price is None or cp > float(highest_price):
                    portfolio['highest_price_since_entry'] = cp
                    highest_price = cp

                # ── Aggressive Debug Logging ──
                if unrealized_pnl > 0:
                    logger.info(f"🔎 [TP-MATH] {symbol} | uPnL: ${unrealized_pnl:.2f} | Value: ${position_value:.2f} | ROE: {unrealized_pct*100:.2f}% | Target: {partial_tp_pct*100:.2f}%")

                # ── Partial Take Profit (Scale-Out 50%) & Auto Break-Even ──
                if not risk_exit_triggered and unrealized_pct >= partial_tp_pct and not portfolio.get('partial_tp_hit', False):
                    total_qty = float(portfolio.get('asset_balance', 0) or 0)
                    if total_qty > 0 and futures_client:
                        tsl_trailing_dist = tsl_atr_trail_mult * current_atr if current_atr else None
                        tp_order, rem_qty = execute_partial_tp_scaleout(
                            futures_client, symbol, 'LONG', total_qty, ep,
                            trailing_distance=tsl_trailing_dist, atr_val=current_atr
                        )
                        if tp_order and rem_qty > 0:
                            portfolio['partial_tp_hit'] = True
                            closed_qty = total_qty - rem_qty
                            portfolio['asset_balance'] = rem_qty
                            if portfolio.get('total_cost'):
                                portfolio['total_cost'] = float(portfolio['total_cost']) * (rem_qty / total_qty)
                            portfolio['usdt_balance'] = float(portfolio.get('usdt_balance', 0)) + (closed_qty * cp)
                            if stop_loss < ep:
                                stop_loss = ep
                                portfolio['stop_loss_price'] = ep
                                portfolio['stop_loss'] = ep
                            
                            pnl_realized_est = (cp - ep) * closed_qty
                            msg = (f"🎯 [{symbol}] PARTIAL TAKE PROFIT EXECUTED (LONG) | "
                                   f"Closed 50% size ({closed_qty} units) at ${cp:.4f} (+{unrealized_pct*100:.2f}%) | "
                                   f"Est. Profit: +${pnl_realized_est:.2f} | Remaining Qty: {rem_qty} | Stop Loss locked at Entry ${ep:.4f}")
                            print(msg, flush=True)
                            log_to_db(session, symbol, "ENTRY", msg)

                            history_record = TradeHistory(
                                symbol=symbol,
                                direction='LONG',
                                entry_price=ep,
                                exit_price=cp,
                                quantity=closed_qty,
                                pnl_usd=pnl_realized_est,
                                pnl_pct=unrealized_pct * 100,
                                outcome='WIN',
                                exit_reason='PARTIAL_TAKE_PROFIT',
                                closed_at=datetime.utcnow()
                            )
                            session.add(history_record)

                            try:
                                latest_record = session.query(PortfolioState).filter(
                                    PortfolioState.symbol == symbol,
                                    PortfolioState.position_direction == 'LONG'
                                ).order_by(PortfolioState.id.desc()).first()
                                if latest_record:
                                    latest_record.partial_tp_hit = True
                                    latest_record.asset_balance = rem_qty
                                    latest_record.total_cost = portfolio['total_cost']
                                    latest_record.usdt_balance = portfolio['usdt_balance']
                                    latest_record.stop_loss_price = stop_loss
                                    latest_record.stop_loss = stop_loss
                                session.commit()
                            except Exception as e:
                                session.rollback()
                                print(f"Warning: Failed to persist partial TP state to DB: {e}", flush=True)

                # ── Break-Even Trigger (+1.0%) ──
                if not risk_exit_triggered and unrealized_pct >= 0.01:
                    if stop_loss < ep:
                        old_sl = stop_loss
                        stop_loss = ep
                        portfolio['stop_loss_price'] = stop_loss
                        portfolio['stop_loss'] = stop_loss
                        msg = (f"🛡️ BREAK-EVEN TRIGGERED: {symbol} LONG at {unrealized_pct*100:+.2f}% PnL. "
                               f"Moved SL from ${old_sl:.4f} to Entry ${ep:.4f}")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)
                        
                        if futures_client:
                            set_stop_loss_order(futures_client, symbol, 'LONG', stop_loss)
                            
                        try:
                            latest_record = session.query(PortfolioState).filter(
                                PortfolioState.symbol == symbol,
                                PortfolioState.position_direction == 'LONG'
                            ).order_by(PortfolioState.id.desc()).first()
                            if latest_record:
                                latest_record.stop_loss_price = stop_loss
                                latest_record.stop_loss = stop_loss
                                session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"Warning: Failed to persist break-even SL to DB: {e}", flush=True)

                # ── Trailing Stop Loss (Profit-Locking) ──
                # Dynamic Volatility-Based TSL using ATR: activates when profit >= 2.0 * ATR
                tsl_activation_dist = tsl_atr_activation_mult * current_atr
                tsl_trailing_dist = tsl_atr_trail_mult * current_atr
                price_move_fav = cp - ep

                if not risk_exit_triggered and price_move_fav >= tsl_activation_dist:
                    if not trailing_active:
                        trailing_active = True
                        portfolio['trailing_active'] = True
                        msg = (f"📈 [{symbol}] DYNAMIC ATR TRAILING STOP ACTIVATED for LONG | "
                               f"Move: +${price_move_fav:.4f} (+{unrealized_pct*100:.2f}%) | "
                               f"Threshold: +${tsl_activation_dist:.4f} ({tsl_atr_activation_mult}x ATR: ${current_atr:.4f})")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)

                    # Trail strictly by 1.5 * ATR from the PEAK price
                    peak = float(highest_price) if highest_price else cp
                    new_sl = peak - tsl_trailing_dist

                    if new_sl > stop_loss:
                        old_sl = stop_loss
                        portfolio['stop_loss_price'] = new_sl
                        portfolio['stop_loss'] = new_sl
                        stop_loss = new_sl
                        locked_pnl = ((new_sl - ep) / ep) * 100
                        msg = (f"📈 DYNAMIC ATR TRAILING STOP UPDATED: {symbol} | "
                               f"New SL: ${new_sl:.4f} (was ${old_sl:.4f}) | "
                               f"Peak: ${peak:.2f} | Trailing Dist: ${tsl_trailing_dist:.4f} ({tsl_atr_trail_mult}x ATR) | Locked Profit: {locked_pnl:+.2f}%")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)

                        if futures_client:
                            set_stop_loss_order(futures_client, symbol, 'LONG', stop_loss, trailing_distance=tsl_trailing_dist, atr_val=current_atr)

                    # Persist trailing state to DB for frontend
                    try:
                        latest_record = session.query(PortfolioState).filter(
                            PortfolioState.symbol == symbol,
                            PortfolioState.position_direction == 'LONG'
                        ).order_by(PortfolioState.id.desc()).first()
                        if latest_record:
                            latest_record.stop_loss_price = stop_loss
                            latest_record.stop_loss = stop_loss
                            latest_record.trailing_active = True
                            latest_record.highest_price_since_entry = float(highest_price) if highest_price else cp
                            session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"Warning: Failed to persist trailing SL to DB: {e}", flush=True)

                # ── Check Stop Loss hit ──
                if not risk_exit_triggered and stop_loss > 0 and cp <= stop_loss:
                    sl_type = 'TRAILING_STOP' if trailing_active else 'STOP_LOSS'
                    msg = (f"🚨 [{symbol}] LONG {sl_type} HIT at ${cp:.2f} "
                           f"(SL: ${stop_loss:.2f}) — EXECUTING CLOSE ON BINANCE")
                    print(msg, flush=True)
                    log_to_db(session, symbol, "EXIT", msg)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, sl_type,
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True

                # ── Stagnant Trade Closer (>2h open, flat PnL) ──
                # ALPHA MODE: When DISABLE_STAGNANT_EXIT is True, trades are NOT
                # closed for being sideways. Give the setup time to play out.
                if not risk_exit_triggered and in_position and not DISABLE_STAGNANT_EXIT:
                    first_entry_row = (
                        session.query(PortfolioState)
                        .filter(
                            PortfolioState.symbol == symbol,
                            PortfolioState.position_direction == 'LONG',
                        )
                        .order_by(PortfolioState.id.asc())
                        .first()
                    )
                    if first_entry_row:
                        open_seconds = (datetime.now() - first_entry_row.timestamp).total_seconds()
                        if open_seconds > 7200 and -0.005 <= unrealized_pct <= 0.005:
                            open_hours = open_seconds / 3600
                            msg = (f"⏰ STAGNANT POSITION CLOSED: {symbol} LONG open for "
                                   f"{open_hours:.1f}h with flat PnL ({unrealized_pct*100:+.2f}%). "
                                   f"Freeing slot for fresh opportunities.")
                            print(msg, flush=True)
                            log_to_db(session, symbol, "EXIT", msg)
                            portfolio = _close_position_handler(
                                portfolio, current_price, symbol, session, 'STAGNANT',
                                futures_client, bullish_ob, bearish_ob, current_rsi,
                                current_zscore, macro_info)
                            risk_exit_triggered = True


            elif pos_direction == 'SHORT' and not risk_exit_triggered:
                position_size = float(portfolio.get('asset_balance', 0) or 0)
                if position_size <= 0 and futures_client:
                    try:
                        live_pos = get_position_info(futures_client, symbol)
                        if live_pos and live_pos.get('size', 0) > 0:
                            position_size = float(live_pos['size'])
                    except Exception:
                        pass
                position_value = abs(ep * position_size)
                unrealized_pnl = (ep - cp) * position_size
                unrealized_pct = (abs(unrealized_pnl) / position_value) if (position_value > 0 and unrealized_pnl > 0) else (unrealized_pnl / position_value if position_value > 0 else 0.0)

                # Track new trough price
                if lowest_price is None or cp < float(lowest_price):
                    portfolio['lowest_price_since_entry'] = cp
                    lowest_price = cp

                # ── Aggressive Debug Logging ──
                if unrealized_pnl > 0:
                    logger.info(f"🔎 [TP-MATH] {symbol} | uPnL: ${unrealized_pnl:.2f} | Value: ${position_value:.2f} | ROE: {unrealized_pct*100:.2f}% | Target: {partial_tp_pct*100:.2f}%")

                # ── Partial Take Profit (Scale-Out 50%) & Auto Break-Even ──
                if not risk_exit_triggered and unrealized_pct >= partial_tp_pct and not portfolio.get('partial_tp_hit', False):
                    total_qty = float(portfolio.get('asset_balance', 0) or 0)
                    if total_qty > 0 and futures_client:
                        tsl_trailing_dist = tsl_atr_trail_mult * current_atr if current_atr else None
                        tp_order, rem_qty = execute_partial_tp_scaleout(
                            futures_client, symbol, 'SHORT', total_qty, ep,
                            trailing_distance=tsl_trailing_dist, atr_val=current_atr
                        )
                        if tp_order and rem_qty > 0:
                            portfolio['partial_tp_hit'] = True
                            closed_qty = total_qty - rem_qty
                            portfolio['asset_balance'] = rem_qty
                            if portfolio.get('total_cost'):
                                portfolio['total_cost'] = float(portfolio['total_cost']) * (rem_qty / total_qty)
                            portfolio['usdt_balance'] = float(portfolio.get('usdt_balance', 0)) + (closed_qty * (2 * ep - cp))
                            if stop_loss > ep or stop_loss == 0:
                                stop_loss = ep
                                portfolio['stop_loss_price'] = ep
                                portfolio['stop_loss'] = ep
                            
                            pnl_realized_est = (ep - cp) * closed_qty
                            msg = (f"🎯 [{symbol}] PARTIAL TAKE PROFIT EXECUTED (SHORT) | "
                                   f"Closed 50% size ({closed_qty} units) at ${cp:.4f} (+{unrealized_pct*100:.2f}%) | "
                                   f"Est. Profit: +${pnl_realized_est:.2f} | Remaining Qty: {rem_qty} | Stop Loss locked at Entry ${ep:.4f}")
                            print(msg, flush=True)
                            log_to_db(session, symbol, "ENTRY", msg)

                            history_record = TradeHistory(
                                symbol=symbol,
                                direction='SHORT',
                                entry_price=ep,
                                exit_price=cp,
                                quantity=closed_qty,
                                pnl_usd=pnl_realized_est,
                                pnl_pct=unrealized_pct * 100,
                                outcome='WIN',
                                exit_reason='PARTIAL_TAKE_PROFIT',
                                closed_at=datetime.utcnow()
                            )
                            session.add(history_record)

                            try:
                                latest_record = session.query(PortfolioState).filter(
                                    PortfolioState.symbol == symbol,
                                    PortfolioState.position_direction == 'SHORT'
                                ).order_by(PortfolioState.id.desc()).first()
                                if latest_record:
                                    latest_record.partial_tp_hit = True
                                    latest_record.asset_balance = rem_qty
                                    latest_record.total_cost = portfolio['total_cost']
                                    latest_record.usdt_balance = portfolio['usdt_balance']
                                    latest_record.stop_loss_price = stop_loss
                                    latest_record.stop_loss = stop_loss
                                session.commit()
                            except Exception as e:
                                session.rollback()
                                print(f"Warning: Failed to persist partial TP state to DB: {e}", flush=True)

                # ── Break-Even Trigger (+1.0%) ──
                if not risk_exit_triggered and unrealized_pct >= 0.01:
                    if stop_loss > ep or stop_loss == 0:
                        old_sl = stop_loss
                        stop_loss = ep
                        portfolio['stop_loss_price'] = stop_loss
                        portfolio['stop_loss'] = stop_loss
                        msg = (f"🛡️ BREAK-EVEN TRIGGERED: {symbol} SHORT at {unrealized_pct*100:+.2f}% PnL. "
                               f"Moved SL from ${old_sl:.4f} to Entry ${ep:.4f}")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)
                        
                        if futures_client:
                            set_stop_loss_order(futures_client, symbol, 'SHORT', stop_loss)
                            
                        try:
                            latest_record = session.query(PortfolioState).filter(
                                PortfolioState.symbol == symbol,
                                PortfolioState.position_direction == 'SHORT'
                            ).order_by(PortfolioState.id.desc()).first()
                            if latest_record:
                                latest_record.stop_loss_price = stop_loss
                                latest_record.stop_loss = stop_loss
                                session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"Warning: Failed to persist break-even SL to DB: {e}", flush=True)

                # ── Trailing Stop Loss (Profit-Locking) ──
                # Dynamic Volatility-Based TSL using ATR: activates when profit >= 2.0 * ATR
                tsl_activation_dist = tsl_atr_activation_mult * current_atr
                tsl_trailing_dist = tsl_atr_trail_mult * current_atr
                price_move_fav = ep - cp

                if not risk_exit_triggered and price_move_fav >= tsl_activation_dist:
                    if not trailing_active:
                        trailing_active = True
                        portfolio['trailing_active'] = True
                        msg = (f"📈 [{symbol}] DYNAMIC ATR TRAILING STOP ACTIVATED for SHORT | "
                               f"Move: +${price_move_fav:.4f} (+{unrealized_pct*100:.2f}%) | "
                               f"Threshold: +${tsl_activation_dist:.4f} ({tsl_atr_activation_mult}x ATR: ${current_atr:.4f})")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)

                    # Trail strictly by 1.5 * ATR from the TROUGH price
                    trough = float(lowest_price) if lowest_price else cp
                    new_sl = trough + tsl_trailing_dist

                    if stop_loss == 0 or new_sl < stop_loss:
                        old_sl = stop_loss
                        portfolio['stop_loss_price'] = new_sl
                        portfolio['stop_loss'] = new_sl
                        stop_loss = new_sl
                        locked_pnl = ((ep - new_sl) / ep) * 100
                        msg = (f"📈 DYNAMIC ATR TRAILING STOP UPDATED: {symbol} | "
                               f"New SL: ${new_sl:.4f} (was ${old_sl:.4f}) | "
                               f"Trough: ${trough:.2f} | Trailing Dist: ${tsl_trailing_dist:.4f} ({tsl_atr_trail_mult}x ATR) | Locked Profit: {locked_pnl:+.2f}%")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)

                        if futures_client:
                            set_stop_loss_order(futures_client, symbol, 'SHORT', stop_loss, trailing_distance=tsl_trailing_dist, atr_val=current_atr)

                    # Persist trailing state to DB for frontend
                    try:
                        latest_record = session.query(PortfolioState).filter(
                            PortfolioState.symbol == symbol,
                            PortfolioState.position_direction == 'SHORT'
                        ).order_by(PortfolioState.id.desc()).first()
                        if latest_record:
                            latest_record.stop_loss_price = stop_loss
                            latest_record.stop_loss = stop_loss
                            latest_record.trailing_active = True
                            latest_record.lowest_price_since_entry = float(lowest_price) if lowest_price else cp
                            session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"Warning: Failed to persist trailing SL to DB: {e}", flush=True)

                # ── Check Stop Loss hit ──
                if not risk_exit_triggered and stop_loss > 0 and cp >= stop_loss:
                    sl_type = 'TRAILING_STOP' if trailing_active else 'STOP_LOSS'
                    msg = (f"🚨 [{symbol}] SHORT {sl_type} HIT at ${cp:.2f} "
                           f"(SL: ${stop_loss:.2f}) — EXECUTING CLOSE ON BINANCE")
                    print(msg, flush=True)
                    log_to_db(session, symbol, "EXIT", msg)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, sl_type,
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True

                # ── Stagnant Trade Closer (>2h open, flat PnL) ──
                # ALPHA MODE: When DISABLE_STAGNANT_EXIT is True, trades are NOT
                # closed for being sideways. Give the setup time to play out.
                if not risk_exit_triggered and in_position and not DISABLE_STAGNANT_EXIT:
                    first_entry_row = (
                        session.query(PortfolioState)
                        .filter(
                            PortfolioState.symbol == symbol,
                            PortfolioState.position_direction == 'SHORT',
                        )
                        .order_by(PortfolioState.id.asc())
                        .first()
                    )
                    if first_entry_row:
                        open_seconds = (datetime.now() - first_entry_row.timestamp).total_seconds()
                        if open_seconds > 7200 and -0.005 <= unrealized_pct <= 0.005:
                            open_hours = open_seconds / 3600
                            msg = (f"⏰ STAGNANT POSITION CLOSED: {symbol} SHORT open for "
                                   f"{open_hours:.1f}h with flat PnL ({unrealized_pct*100:+.2f}%). "
                                   f"Freeing slot for fresh opportunities.")
                            print(msg, flush=True)
                            log_to_db(session, symbol, "EXIT", msg)
                            portfolio = _close_position_handler(
                                portfolio, current_price, symbol, session, 'STAGNANT',
                                futures_client, bullish_ob, bearish_ob, current_rsi,
                                current_zscore, macro_info)
                            risk_exit_triggered = True


        # ── Pyramiding (Scaling Into Winners) Check ──
        if in_position and not risk_exit_triggered:
            scaled_in, portfolio = evaluate_pyramid_scale_in(portfolio, current_price, symbol, session, futures_client)

        bullish_breakout, bearish_breakout = detect_consolidation_breakout(df)
        whale_strike = detect_whale_strike(df)

        # ──────────────────────────────────────────────────────────
        #  REGIME DETECTION & STRATEGY ROUTING (Middleware / Brain)
        #  ─ Detects TREND / RANGE / STORM using ADX + ATR + Z-Score
        #  ─ Routes to the correct TradingStrategy implementation
        #  ─ Risk management exits above run BEFORE this block
        # ──────────────────────────────────────────────────────────
        decision = 'WAIT'
        strategy_type = None
        new_stop_loss = 0.0
        active_mode_value = MarketRegime.RANGE.value  # conservative default

        if not risk_exit_triggered:
            active_count = count_active_positions(session)

            # ── STRATEGY C: Whale Hunter (Volume Anomaly Spike > 10x) ──
            # Bypasses regime detection — volume creates its own regime.
            if whale_strike and not in_position:
                w_direction = whale_strike['direction']
                w_vol_ratio = whale_strike['vol_ratio']

                is_macro_aligned = False
                if w_direction == 'LONG' and macro_trend in ('UPTREND', 'NEUTRAL'):
                    is_macro_aligned = True
                elif w_direction == 'SHORT' and macro_trend in ('DOWNTREND', 'NEUTRAL'):
                    is_macro_aligned = True

                if is_macro_aligned:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = w_direction
                        strategy_type = 'WHALE_STRIKE'
                        active_mode_value = MarketRegime.TREND.value  # Whale = momentum = TREND
                        if w_direction == 'LONG':
                            new_stop_loss = whale_strike['candle_low'] * 0.999
                        else:
                            new_stop_loss = whale_strike['candle_high'] * 1.001

                        msg = (f"🐋 [WHALE STRIKE DETECTED] {symbol} {decision}! "
                               f"Vol: {w_vol_ratio:.1f}x avg | Price: ${current_price:.2f} | Macro: {macro_trend}")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)
                        send_telegram_alert(msg)

            # ── STRATEGY D: Crash Catcher (Extreme Mean Reversion Engine) ──────
            # ⚡ BYPASS PERMISSIONS: This block runs BEFORE the STORM freeze and
            #    BEFORE the StrategyRouter (which contains the Macro Hard Filter).
            #    It is the ONLY module authorised to place market orders during a
            #    STORM regime and to take counter-trend positions.
            # ────────────────────────────────────────────────────────────────────
            if decision == 'WAIT':
                cc_signal = evaluate_extreme_reversion(
                    df=df,
                    current_price=current_price,
                    current_rsi=current_rsi,
                    current_zscore=current_zscore,
                    current_atr=current_atr,
                    in_position=in_position,
                    active_count=active_count,
                    max_positions=MAX_CONCURRENT_POSITIONS,
                )

                if cc_signal:
                    decision          = cc_signal['direction']
                    strategy_type     = cc_signal['strategy_type']
                    new_stop_loss     = cc_signal['stop_loss']
                    active_mode_value = 'CRASH_CATCHER'

                    # Override TSL parameters with Crash Catcher's aggressive profile
                    # (early activation at 0.5x ATR, tight trail at 0.3x ATR)
                    tsl_atr_activation_mult = CRASH_CATCHER_TSL_ATR_MULT
                    tsl_atr_trail_mult      = REGIME_RISK_PARAMS['CRASH_CATCHER']['TSL_ATR_TRAIL_MULT']
                    partial_tp_pct          = REGIME_RISK_PARAMS['CRASH_CATCHER']['PARTIAL_TP_PCT']

                    storm_bypass_msg = (
                        f"⚡ [CRASH CATCHER] STORM Freeze BYPASSED for {symbol} {decision} "
                        f"(counter-trend override authorised) | "
                        f"Z: {current_zscore:+.3f} | RSI: {current_rsi:.1f} | "
                        f"Vol: {cc_signal['vol_ratio']:.1f}x | Wick: {cc_signal['wick_ratio']:.2f} | "
                        f"SL: ${new_stop_loss:.4f} | Regime: {active_regime.value}"
                    )
                    print(storm_bypass_msg, flush=True)
                    log_to_db(session, symbol, "ENTRY", storm_bypass_msg)

            # ── STORM OVERRIDE: Freeze all new entries (primary engine only) ──
            # NOTE: Crash Catcher (Strategy D) already set decision above if it
            # fired. If decision is still 'WAIT', the STORM freeze applies.
            if active_regime.value == 'STORM' and decision == 'WAIT':
                print(f"⏸️ [{symbol}] STORM Regime Active — Freezing New Entries (primary engine)", flush=True)

            # ── STRATEGY ROUTER: Regime-Aware Entry Logic ──
            if decision == 'WAIT' and active_regime.value != 'STORM':
                regime, routed_decision, routed_strategy_type, routed_stop_loss, regime_meta = strategy_router.route(
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
                    max_positions=MAX_CONCURRENT_POSITIONS,
                    futures_client=futures_client,
                    session=session,
                    precomputed_regime=active_regime,
                    precomputed_meta=precomputed_meta,
                )
                active_mode_value = regime.value

                if routed_decision in ('LONG', 'SHORT'):
                    decision      = routed_decision
                    strategy_type = routed_strategy_type
                    new_stop_loss = routed_stop_loss
                    # Log per spec: [MODE: X] Symbol: Y | ADX: ... | RSI: ...
                    adx_str    = f"{regime_meta['adx']:.1f}" if regime_meta.get('adx') is not None else "N/A"
                    rsi_log    = f"{current_rsi:.1f}" if current_rsi is not None else "N/A"
                    zscore_log = f"{current_zscore:+.3f}" if current_zscore is not None else "N/A"
                    print(
                        f"[MODE: {regime.value}] Symbol: {symbol} | ADX: {adx_str} | "
                        f"RSI: {rsi_log} | Z: {zscore_log} | "
                        f"Strategy: {strategy_type}",
                        flush=True,
                    )
                elif routed_strategy_type == 'STORM_LIMIT_PENDING':
                    # Storm strategy placed a limit order async — log only
                    adx_str = f"{regime_meta['adx']:.1f}" if regime_meta.get('adx') is not None else "N/A"
                    print(
                        f"[🌪️ STORM PENDING] {symbol} | ADX: {adx_str} | "
                        f"Limit order placed — awaiting fill (TTL: 15 min)",
                        flush=True,
                    )

            # ── TESTNET FORCE TRADES (override — kept for backward compat) ──
            if decision == 'WAIT' and TESTNET_FORCE_TRADES and current_zscore is not None and current_rsi is not None:
                current_sma_val = float(last_row[f'SMA_{ZSCORE_SMA_PERIOD}']) if pd.notna(last_row.get(f'SMA_{ZSCORE_SMA_PERIOD}')) else None
                if current_sma_val and macro_trend == 'UPTREND' and current_price > current_sma_val and current_rsi > 55.0 and current_zscore > 1.2:
                    if active_count < MAX_CONCURRENT_POSITIONS and not in_position:
                        decision = 'LONG'
                        strategy_type = 'TREND_ALIGN'
                        active_mode_value = MarketRegime.TREND.value
                        new_stop_loss = current_sma_val * 0.995
                        msg = f"🧪 [{symbol}] LONG Strategy C (TREND_ALIGN): Macro={macro_trend} + Price > SMA-50"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)
                elif current_sma_val and macro_trend == 'DOWNTREND' and current_price < current_sma_val and current_rsi < 45.0 and current_zscore < -1.2:
                    if active_count < MAX_CONCURRENT_POSITIONS and not in_position:
                        decision = 'SHORT'
                        strategy_type = 'TREND_ALIGN'
                        active_mode_value = MarketRegime.TREND.value
                        new_stop_loss = current_sma_val * 1.005
                        msg = f"🧪 [{symbol}] SHORT Strategy C (TREND_ALIGN): Macro={macro_trend} + Price < SMA-50"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)

            # ── Stop-Loss Integrity Guard ──
            if decision in ('LONG', 'SHORT'):
                if new_stop_loss <= 0.0 or pd.isna(new_stop_loss):
                    if decision == 'LONG':
                        new_stop_loss = current_price * (1.0 - STOP_LOSS_PCT)
                    else:  # SHORT
                        new_stop_loss = current_price * (1.0 + STOP_LOSS_PCT)

                is_invalid = False
                if new_stop_loss <= 0.0 or pd.isna(new_stop_loss):
                    is_invalid = True
                elif decision == 'LONG' and new_stop_loss >= current_price:
                    is_invalid = True
                elif decision == 'SHORT' and new_stop_loss <= current_price:
                    is_invalid = True

                if is_invalid:
                    msg = f"🛑 [SKIP] {symbol}: Unable to calculate valid Stop-Loss price for {decision} (SL: ${new_stop_loss}, Price: ${current_price})."
                    print(msg, flush=True)
                    log_to_db(session, symbol, "SKIP", msg)
                    decision = 'WAIT'

            # ── DEBUG LOGGER: Why is the bot skipping? ──
            if decision == 'WAIT' and not risk_exit_triggered and not in_position:
                z_str   = f"{current_zscore:+.3f}" if current_zscore is not None else "N/A"
                rsi_str = f"{current_rsi:.1f}"    if current_rsi  is not None else "N/A"
                print(f"  [DEBUG] {symbol} | [MODE: {active_mode_value}] Z: {z_str} | RSI: {rsi_str} | No entry signal.", flush=True)

        # ── 6. Save signal to Database ──
        # Build db_decision from live Binance state WITHOUT mutating `decision`.
        # This keeps execution logic untouched (no duplicate orders) while
        # ensuring the DB row reflects the true position for Laravel.
        db_decision = decision  # default: whatever the signal logic decided

        if decision == 'WAIT' and not risk_exit_triggered:
            # Check live Binance position (source of truth)
            if futures_client:
                pos_info = get_position_info(futures_client, symbol)
                if pos_info and pos_info['size'] > 0:
                    db_decision = pos_info['direction']  # 'LONG' or 'SHORT'
                    print(f"  🔒 [{symbol}] DB decision synced to '{db_decision}' "
                          f"(live Binance position, size={pos_info['size']})", flush=True)

            # Fallback: local portfolio state
            if db_decision == 'WAIT' and in_position and pos_direction in ('LONG', 'SHORT'):
                db_decision = pos_direction
                print(f"  🔒 [{symbol}] DB decision synced to '{db_decision}' "
                      f"(local portfolio has open position)", flush=True)

        if risk_exit_triggered:
            db_decision = f"CLOSE_{pos_direction}" if pos_direction in ('LONG', 'SHORT') else 'WAIT'

        signal = TradingSignal(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=pd.Timestamp(last_row['timestamp']).to_pydatetime(),
            current_price=float(current_price),
            rsi=float(current_rsi) if current_rsi is not None else None,
            z_score=float(current_zscore) if current_zscore is not None else None,
            macro_trend=macro_trend,
            bullish_ob_low=float(bullish_ob['low']) if bullish_ob else None,
            bullish_ob_high=float(bullish_ob['high']) if bullish_ob else None,
            bearish_ob_low=float(bearish_ob['low']) if bearish_ob else None,
            bearish_ob_high=float(bearish_ob['high']) if bearish_ob else None,
            decision=db_decision
        )

        session.add(signal)
        session.commit()


        # ──────────────────────────────────────────────────────────
        #  EXECUTE SIGNALS (LONG / SHORT with position management)
        # ──────────────────────────────────────────────────────────
        if not risk_exit_triggered and decision in ('LONG', 'SHORT'):
            portfolio = load_portfolio(session, symbol)
            in_position = portfolio['asset_balance'] is not None and portfolio['asset_balance'] > 0
            pos_direction = portfolio.get('position_direction')

            last_signal = session.query(TradingSignal).filter(
                TradingSignal.symbol == symbol,
                TradingSignal.timeframe == timeframe,
                TradingSignal.decision.in_(['LONG', 'SHORT', 'CLOSE_LONG', 'CLOSE_SHORT']),
                TradingSignal.id != signal.id
            ).order_by(TradingSignal.id.desc()).first()

            last_decision = last_signal.decision if last_signal else None

            if decision != last_decision or decision == 'LONG':

                # ── If we're in an opposite position, close it first ──
                if in_position and pos_direction and pos_direction != decision:
                    print(f"🔄 [{symbol}] Reversing: closing {pos_direction} before opening {decision}", flush=True)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'SIGNAL',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    in_position = False

                # ── Scale-Up (Pyramiding) Safety Checks ──
                dca_level = portfolio.get('dca_level', 0)
                if decision == 'LONG' and in_position and pos_direction == 'LONG':
                    if dca_level >= 3:
                        print(f"🛑 [{symbol}] LONG Scale-Up Skipped: Max iterations (2) reached.", flush=True)
                        decision = 'WAIT'
                    else:
                        ep = float(portfolio.get('average_entry_price', 0))
                        sl = float(portfolio.get('stop_loss', 0))
                        if sl < ep or sl == 0:
                            print(f"🛑 [{symbol}] LONG Scale-Up Skipped: Stop Loss (${sl:.4f}) not at/past Break-Even (${ep:.4f}).", flush=True)
                            decision = 'WAIT'

                if decision == 'SHORT' and in_position and pos_direction == 'SHORT':
                    if dca_level >= 3:
                        print(f"🛑 [{symbol}] SHORT Scale-Up Skipped: Max iterations (2) reached.", flush=True)
                        decision = 'WAIT'
                    else:
                        ep = float(portfolio.get('average_entry_price', 0))
                        sl = float(portfolio.get('stop_loss', 0))
                        if sl > ep or sl == 0:
                            print(f"🛑 [{symbol}] SHORT Scale-Up Skipped: Stop Loss (${sl:.4f}) not at/past Break-Even (${ep:.4f}).", flush=True)
                            decision = 'WAIT'

                # ── Execute LONG (open or DCA) ──
                if decision == 'LONG':
                    # Determine Conviction Tier
                    vol_ratio = 0.0
                    if strategy_type == 'PULLBACK' and bullish_ob and 'vol_ratio' in bullish_ob:
                        vol_ratio = bullish_ob['vol_ratio']
                    elif strategy_type == 'BREAKOUT' and bullish_breakout and 'vol_ratio' in bullish_breakout:
                        vol_ratio = bullish_breakout['vol_ratio']

                    abs_z = abs(current_zscore) if current_zscore else 0.0

                    allocation_pct = 0.05
                    tier_str = "Tier 3"
                    if abs_z >= 2.0 and vol_ratio >= 4.0:
                        allocation_pct = 0.20
                        tier_str = "Tier 1"
                    elif abs_z >= 1.0 and vol_ratio >= 2.0:
                        allocation_pct = 0.10
                        tier_str = "Tier 2"

                    if futures_client:
                        actual_balance = get_futures_balance(futures_client)
                    else:
                        actual_balance = portfolio.get('usdt_balance', 0.0)
                    
                    spend = actual_balance * allocation_pct

                    if actual_balance < spend or actual_balance <= 0:
                        print(f"⚠️ Skipping execution: Insufficient USDT balance ({actual_balance:.2f} USDT available)", flush=True)
                        return

                    if portfolio['usdt_balance'] >= spend:
                        effective_usdt = spend * (1 - TRADING_FEE)
                        asset_bought = effective_usdt / float(current_price)

                        old_asset = float(portfolio.get('asset_balance', 0) or 0)
                        old_avg = float(portfolio.get('average_entry_price', 0) or 0)
                        old_val = old_asset * old_avg
                        new_val = asset_bought * float(current_price)

                        portfolio['asset_balance'] = round(old_asset + asset_bought, 6)
                        portfolio['average_entry_price'] = (old_val + new_val) / portfolio['asset_balance']
                        portfolio['dca_level'] = dca_level + 1
                        portfolio['last_exec_price'] = float(current_price)
                        portfolio['total_cost'] = float(portfolio.get('total_cost', 0)) + effective_usdt
                        portfolio['usdt_balance'] -= spend
                        portfolio['position_direction'] = 'LONG'
                        
                        # Set SL to max(old SL, new average entry price) to cover the scale-up cost
                        if dca_level == 0:
                            if not new_stop_loss or float(new_stop_loss) <= 0.0:
                                new_stop_loss = float(current_price) * 0.985
                        elif dca_level > 0:
                            new_stop_loss = max(float(new_stop_loss), portfolio['average_entry_price'])

                        portfolio['stop_loss_price'] = float(new_stop_loss)
                        portfolio['stop_loss'] = float(new_stop_loss)
                        if dca_level == 0:
                            portfolio['highest_price_since_entry'] = float(current_price)
                            portfolio['lowest_price_since_entry'] = None

                        order = None

                        total_value = portfolio['usdt_balance'] + (float(portfolio['asset_balance']) * float(current_price))

                        # Generate reason_msg and strategy_name before DB insertion
                        if strategy_type == 'PULLBACK':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}, OB:{vol_ratio:.1f}x] - Pullback"
                            ob_low = bullish_ob['low'] if bullish_ob else 0
                            ob_high = bullish_ob['high'] if bullish_ob else 0
                            reason_msg = (f"{strategy_name} | Macro: {macro_trend} | "
                                          f"OB: ${float(ob_low):.2f}-${float(ob_high):.2f}")
                        elif strategy_type == 'BREAKOUT':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}, OB:{vol_ratio:.1f}x] - Breakout"
                            reason_msg = (f"{strategy_name} | Macro: {macro_trend} | Consolidation High Cleared")
                        elif strategy_type == 'TREND_ALIGN':
                            strategy_name = f"{tier_str} - Trend Align"
                            reason_msg = (f"{strategy_name} | Macro: {macro_trend} | "
                                          f"Price ${current_price:.2f} > SMA-50 ${current_sma:.2f} | 🧪 TESTNET ONLY")
                        elif strategy_type in ('CRASH_CATCHER_LONG', 'CRASH_CATCHER_SHORT'):
                            _cc = cc_signal if cc_signal else {}
                            strategy_name = f"CRASH_CATCHER [Z:{abs_z:.1f}, Vol:{_cc.get('vol_ratio',0):.1f}x, Wick:{_cc.get('wick_ratio',0):.2f}] - Extreme MR"
                            reason_msg = (f"{strategy_name} | SL: wick_low=${new_stop_loss:.4f} | "
                                          f"TSL: {CRASH_CATCHER_TSL_ATR_MULT}x ATR | Counter-trend bypass active")
                        else:
                            strategy_name = f"{tier_str} - Unknown"
                            reason_msg = f"Strategy: Unknown"

                        portfolio['strategy'] = strategy_name

                        highest_price = portfolio.get('highest_price_since_entry')
                        hp_since_entry = float(highest_price) if highest_price is not None else float(current_price)

                        portfolio_record = PortfolioState(
                            timestamp=datetime.now(),
                            symbol=symbol,
                            decision='LONG',
                            current_price=float(current_price),
                            usdt_balance=float(portfolio['usdt_balance']),
                            asset_balance=float(portfolio['asset_balance']),
                            position_direction='LONG',
                            average_entry_price=float(portfolio['average_entry_price']),
                            dca_level=int(portfolio['dca_level']),
                            last_exec_price=float(portfolio['last_exec_price']),
                            total_cost=float(portfolio['total_cost']),
                            highest_price_since_entry=hp_since_entry,
                            stop_loss_price=float(new_stop_loss),
                            stop_loss=float(new_stop_loss),
                            strategy=strategy_name,
                            trailing_active=False,
                            lowest_price_since_entry=None,
                            pnl_pct=None,
                            pnl_usd=None,
                            total_portfolio_value=float(round(total_value, 2)),
                            entry_reason=reason_msg,
                            active_mode=active_mode_value,
                        )
                        for attempt in range(3):
                            try:
                                session.add(portfolio_record)
                                session.commit()
                                break
                            except Exception as e:
                                session.rollback()
                                if attempt == 2:
                                    print(f"Warning: Failed to save portfolio state to DB after 3 attempts: {e}", flush=True)
                                    traceback.print_exc()
                                else:
                                    import time; time.sleep(1)



                        # ── Telegram alert with Active Mode badge ──
                        alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
                        _valid_regimes = [r.value for r in MarketRegime]
                        mode_label = MarketRegime(active_mode_value).telegram_label if active_mode_value in _valid_regimes else '[CRASH CATCHER 🚨]'

                        if strategy_type == 'PULLBACK':
                            alert_reason = (f"- Strategy: A (Pullback)\n"
                                          f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                          f"- 15m Z-Score: {current_zscore:+.2f} (threshold: {ZSCORE_LONG_THRESHOLD})\n"
                                          f"- Bullish OB: ${float(ob_low):.2f} - ${float(ob_high):.2f}\n"
                                          f"- OB Volume: {vol_ratio} avg")
                        elif strategy_type == 'BREAKOUT':
                            alert_reason = (f"- Strategy: B (Momentum Breakout)\n"
                                          f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                          f"- Breakout Volume: {vol_ratio} avg\n"
                                          f"- Consolidation High Cleared!")
                        elif strategy_type == 'TREND_ALIGN':
                            alert_reason = (f"- Strategy: C (Trend Alignment) 🧪\n"
                                          f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                          f"- Price: ${current_price:.2f} > SMA-50: ${current_sma:.2f}\n"
                                          f"- ⚠️ TESTNET ONLY — No OB/Volume confirmation")
                        elif strategy_type == 'RANGE_MEAN_REVERSION':
                            alert_reason = (f"- Strategy: Mean Reversion (Range)\n"
                                          f"- RSI: {current_rsi:.1f} (oversold <35)\n"
                                          f"- Bullish OB touch: ${bullish_ob['low']:.2f}–${bullish_ob['high']:.2f}")
                        elif strategy_type in ('CRASH_CATCHER_LONG', 'CRASH_CATCHER_SHORT'):
                            _cc = cc_signal if cc_signal else {}
                            alert_reason = (
                                f"- Strategy: 🚨 CRASH CATCHER (Extreme Mean Reversion)\n"
                                f"- Trigger: Z-Score {current_zscore:+.3f} (≤{CRASH_CATCHER_ZSCORE_LONG}) + RSI {current_rsi:.1f} (<{CRASH_CATCHER_RSI_LONG})\n"
                                f"- Volume Anomaly: {_cc.get('vol_ratio', 0):.1f}x MA ({CRASH_CATCHER_VOL_MULT}x threshold) — Whale absorption detected\n"
                                f"- Pin Bar: Lower wick ratio {_cc.get('wick_ratio', 0):.2f} (>{CRASH_CATCHER_WICK_RATIO}) — Bottom defended\n"
                                f"- SL: ${new_stop_loss:.4f} (wick low −0.1% buffer) — WICK BREACH = EXIT\n"
                                f"- TSL: Activates at {CRASH_CATCHER_TSL_ATR_MULT}x ATR (rubber-band lock-in)\n"
                                f"- ⚡ STORM Freeze BYPASSED — Counter-trend override active"
                            )
                        else:
                            alert_reason = f"- Strategy: Unknown"

                        alert_msg = (
                            f"{ALERT_PREFIX} 🚨 *QUANT ALERT: {'SCALE-UP LONG' if dca_level > 0 else 'OPEN LONG'}* 🚨\n"
                            f"\n"
                            f"🤖 Active Mode: {mode_label}\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Price:* ${float(current_price):.2f}\n"
                            f"*Time:* {alert_time}\n"
                            f"\n"
                            f"💡 *MTF Confluence:*\n"
                            f"{alert_reason}\n"
                            f"\n"
                            f"🛡 *Risk Management:*\n"
                            f"- Entry Price: ${float(portfolio['average_entry_price']):.2f}\n"
                            f"- Stop-Loss: ${new_stop_loss:.2f} (Dynamic)\n"
                            f"- Trailing Stop: {tsl_atr_activation_mult}x ATR act / {tsl_atr_trail_mult}x ATR trail\n"
                            f"\n"
                            f"💼 *Virtual Portfolio:*\n"
                            f"- Slot Budget: ${SLOT_BUDGET:,.0f}\n"
                            f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
                            f"- Asset Balance: {portfolio['asset_balance']:.6f}\n"
                            f"- Total Value: ${total_value:.2f}\n"
                            f"- DCA Iteration: {dca_level + 1}/3"
                        )
                        if not futures_client or order:
                            send_telegram_alert(alert_msg)
                        # Generate reason_msg and strategy_name before DB insertion
                        if strategy_type == 'PULLBACK':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}, OB:{vol_ratio:.1f}x] - Pullback"
                            ob_low = bearish_ob['low'] if bearish_ob else 0
                            ob_high = bearish_ob['high'] if bearish_ob else 0
                            reason_msg = (f"{strategy_name} | Macro: {macro_trend} | "
                                          f"OB: ${float(ob_low):.2f}-${float(ob_high):.2f}")
                        elif strategy_type == 'BREAKOUT':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}, OB:{vol_ratio:.1f}x] - Breakout"
                            reason_msg = (f"{strategy_name} | Macro: {macro_trend} | Consolidation Low Broken")
                        elif strategy_type == 'TREND_ALIGN':
                            strategy_name = f"{tier_str} - Trend Align"
                            reason_msg = (f"{strategy_name} | Macro: {macro_trend} | "
                                          f"Price ${current_price:.2f} < SMA-50 ${current_sma:.2f} | 🧪 TESTNET ONLY")
                        elif strategy_type in ('CRASH_CATCHER_LONG', 'CRASH_CATCHER_SHORT'):
                            _cc = cc_signal if cc_signal else {}
                            strategy_name = f"CRASH_CATCHER [Z:{abs_z:.1f}, Vol:{_cc.get('vol_ratio',0):.1f}x, Wick:{_cc.get('wick_ratio',0):.2f}] - Extreme MR"
                            reason_msg = (f"{strategy_name} | SL: wick_high=${new_stop_loss:.4f} | "
                                          f"TSL: {CRASH_CATCHER_TSL_ATR_MULT}x ATR | Counter-trend bypass active")
                        else:
                            strategy_name = f"{tier_str} - Unknown"
                            reason_msg = f"Strategy: Unknown"
                        
                        portfolio['strategy'] = strategy_name

                        lowest_price = portfolio.get('lowest_price_since_entry')
                        lp_since_entry = float(lowest_price) if lowest_price is not None else float(current_price)

                        portfolio_record = PortfolioState(
                            timestamp=datetime.now(),
                            symbol=symbol,
                            decision='SHORT',
                            current_price=float(current_price),
                            usdt_balance=float(portfolio['usdt_balance']),
                            asset_balance=float(portfolio['asset_balance']),
                            position_direction='SHORT',
                            average_entry_price=float(portfolio['average_entry_price']),
                            dca_level=int(portfolio['dca_level']),
                            last_exec_price=float(portfolio['last_exec_price']),
                            total_cost=float(portfolio['total_cost']),
                            highest_price_since_entry=None,
                            lowest_price_since_entry=lp_since_entry,
                            stop_loss_price=float(new_stop_loss),
                            stop_loss=float(new_stop_loss),
                            strategy=strategy_name,
                            trailing_active=False,
                            pnl_pct=None,
                            pnl_usd=None,
                            total_portfolio_value=float(round(total_value, 2)),
                            entry_reason=reason_msg,
                            active_mode=active_mode_value,
                        )
                        for attempt in range(3):
                            try:
                                session.add(portfolio_record)
                                session.commit()
                                break
                            except Exception as e:
                                session.rollback()
                                if attempt == 2:
                                    print(f"Warning: Failed to save portfolio state to DB after 3 attempts: {e}", flush=True)
                                    traceback.print_exc()
                                else:
                                    import time; time.sleep(1)



                        # ── Telegram alert with Active Mode badge ──
                        alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
                        mode_label = MarketRegime(active_mode_value).telegram_label if active_mode_value else '[TREND 🚀]'

                        if strategy_type == 'PULLBACK':
                            alert_reason = (f"- Strategy: A (Pullback)\n"
                                          f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                          f"- 15m Z-Score: {current_zscore:+.2f} (threshold: +{ZSCORE_SHORT_THRESHOLD})\n"
                                          f"- Bearish OB: ${float(ob_low):.2f} - ${float(ob_high):.2f}\n"
                                          f"- OB Volume: {vol_ratio} avg")
                        elif strategy_type == 'BREAKOUT':
                            alert_reason = (f"- Strategy: B (Momentum Breakout)\n"
                                          f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                          f"- Breakout Volume: {vol_ratio} avg\n"
                                          f"- Consolidation Low Broken!")
                        elif strategy_type == 'TREND_ALIGN':
                            alert_reason = (f"- Strategy: C (Trend Alignment) 🧪\n"
                                          f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                          f"- Price: ${current_price:.2f} < SMA-50: ${current_sma:.2f}\n"
                                          f"- ⚠️ TESTNET ONLY — No OB/Volume confirmation")
                        elif strategy_type == 'RANGE_MEAN_REVERSION':
                            alert_reason = (f"- Strategy: Mean Reversion (Range)\n"
                                          f"- RSI: {current_rsi:.1f} (overbought >65)\n"
                                          f"- Bearish OB touch: ${bearish_ob['low']:.2f}–${bearish_ob['high']:.2f}")
                        elif strategy_type in ('CRASH_CATCHER_LONG', 'CRASH_CATCHER_SHORT'):
                            _cc = cc_signal if cc_signal else {}
                            alert_reason = (
                                f"- Strategy: \U0001f6a8 CRASH CATCHER (Extreme Mean Reversion)\n"
                                f"- Trigger: Z-Score {current_zscore:+.3f} (\u2265+{CRASH_CATCHER_ZSCORE_SHORT}) + RSI {current_rsi:.1f} (>{CRASH_CATCHER_RSI_SHORT})\n"
                                f"- Volume Anomaly: {_cc.get('vol_ratio', 0):.1f}x MA ({CRASH_CATCHER_VOL_MULT}x threshold) \u2014 Forced short squeeze detected\n"
                                f"- Pin Bar: Upper wick ratio {_cc.get('wick_ratio', 0):.2f} (>{CRASH_CATCHER_WICK_RATIO}) \u2014 Top actively rejected\n"
                                f"- SL: ${new_stop_loss:.4f} (wick high +0.1% buffer) \u2014 WICK BREACH = EXIT\n"
                                f"- TSL: Activates at {CRASH_CATCHER_TSL_ATR_MULT}x ATR (rubber-band lock-in)\n"
                                f"- \u26a1 Macro Hard Filter BYPASSED \u2014 Counter-trend override active"
                            )
                        else:
                            alert_reason = f"- Strategy: Unknown"

                        alert_msg = (
                            f"{ALERT_PREFIX} \U0001f6a8 *QUANT ALERT: {'SCALE-UP SHORT' if dca_level > 0 else 'OPEN SHORT'}* \U0001f6a8\n"
                            f"\n"
                            f"🤖 Active Mode: {mode_label}\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Price:* ${float(current_price):.2f}\n"
                            f"*Time:* {alert_time}\n"
                            f"\n"
                            f"\U0001f4a1 *MTF Confluence:*\n"
                            f"{alert_reason}\n"
                            f"\n"
                            f"\U0001f6e1 *Risk Management:*\n"
                            f"- Entry Price: ${float(portfolio['average_entry_price']):.2f}\n"
                            f"- Stop-Loss: ${new_stop_loss:.2f} (Dynamic)\n"
                            f"- Trailing Stop: {tsl_atr_activation_mult}x ATR act / {tsl_atr_trail_mult}x ATR trail\n"
                            f"\n"
                            f"\U0001f4bc *Virtual Portfolio:*\n"
                            f"- Slot Budget: ${SLOT_BUDGET:,.0f}\n"
                            f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
                            f"- Asset Balance: {portfolio['asset_balance']:.6f}\n"
                            f"- Total Value: ${total_value:.2f}\n"
                            f"- DCA Iteration: {dca_level + 1}/3"
                        )
                        if not futures_client or order:
                            send_telegram_alert(alert_msg)

                # ── Close LONG via SHORT signal (if holding LONG) ──
                elif decision == 'SHORT' and in_position and pos_direction == 'LONG':
                    ep = float(portfolio['average_entry_price']) if portfolio['average_entry_price'] else 0
                    if ep > 0:
                        profit_pct = (float(current_price) - ep) / ep
                        if profit_pct < MIN_PROFIT_PCT:
                            print(f"⏸️ [{symbol}] Signal CLOSE LONG suppressed: profit {profit_pct*100:.2f}% "
                                  f"< min gate {MIN_PROFIT_PCT*100:.1f}%", flush=True)
                        else:
                            print(f"✅ [{symbol}] Signal CLOSE LONG executing: profit {profit_pct*100:.2f}% "
                                  f">= min gate {MIN_PROFIT_PCT*100:.1f}%", flush=True)
                            portfolio = _close_position_handler(
                                portfolio, current_price, symbol, session, 'SIGNAL',
                                futures_client, bullish_ob, bearish_ob, current_rsi,
                                current_zscore, macro_info)
                    else:
                        portfolio = _close_position_handler(
                            portfolio, current_price, symbol, session, 'SIGNAL',
                            futures_client, bullish_ob, bearish_ob, current_rsi,
                            current_zscore, macro_info)
            else:
                print(f"[{symbol}] Duplicate {decision} signal — Telegram alert suppressed.", flush=True)

        # ──────────────────────────────────────────────────────────
        #  SYNC POSITION STATE TO DB (for Laravel dashboard)
        #  When decision=='WAIT' but a live position exists, we MUST
        #  write a PortfolioState row with Binance-synced fields so
        #  the dashboard always shows accurate active positions.
        # ──────────────────────────────────────────────────────────
        if not risk_exit_triggered and decision == 'WAIT':
            portfolio = load_portfolio(session, symbol)
            in_position = portfolio['asset_balance'] is not None and portfolio['asset_balance'] > 0

            # ── Fetch live Binance position (source of truth) ──
            # NOTE: db_decision carries forward from the TradingSignal block above.
            # Do NOT reset it here — it already holds 'LONG'/'SHORT' if Binance
            # confirmed a live position during signal save.
            db_pos_direction = portfolio.get('position_direction')
            db_entry_price = float(portfolio['average_entry_price']) if portfolio.get('average_entry_price') else None
            db_unrealized_pnl = None

            if futures_client:
                pos_info = get_position_info(futures_client, symbol)
                if pos_info and pos_info['size'] > 0:
                    db_decision = pos_info['direction']           # 'LONG' or 'SHORT'
                    db_pos_direction = pos_info['direction']
                    db_entry_price = pos_info['entry_price']
                    db_unrealized_pnl = pos_info['unrealized_pnl']
                    in_position = True  # Binance confirms position is open
                    print(f"  🔒 [{symbol}] Binance sync: {db_decision} | "
                          f"Entry=${db_entry_price:.2f} | "
                          f"uPnL=${db_unrealized_pnl:.2f}", flush=True)

                    # ── Active Position Sync: Unconditional TP-MATH Log & Partial Scale-Out ──
                    u_pnl = float(pos_info.get('unRealizedProfit') if 'unRealizedProfit' in pos_info else pos_info.get('unrealized_pnl', 0.0))
                    pos_amt = abs(float(pos_info.get('positionAmt') if 'positionAmt' in pos_info else pos_info.get('size', 0.0)))
                    entry_price = float(pos_info.get('entryPrice') if 'entryPrice' in pos_info else pos_info.get('entry_price', 0.0))

                    position_value = pos_amt * entry_price
                    unrealized_pct = abs(u_pnl) / position_value if position_value > 0 else 0.0

                    logger.info(f"🔎 [TP-MATH] {symbol} | uPnL: ${u_pnl:.2f} | Value: ${position_value:.2f} | ROE: {unrealized_pct*100:.2f}% | Target: {partial_tp_pct*100:.2f}% | Hit: {portfolio.get('partial_tp_hit', False)}")

                    if not risk_exit_triggered and u_pnl > 0 and unrealized_pct >= partial_tp_pct and not portfolio.get('partial_tp_hit', False):
                        total_qty = pos_amt
                        if total_qty > 0 and futures_client:
                            tsl_trailing_dist = tsl_atr_trail_mult * current_atr if current_atr else None
                            tp_order, rem_qty = execute_partial_tp_scaleout(
                                futures_client, symbol, db_pos_direction, total_qty, entry_price,
                                trailing_distance=tsl_trailing_dist, atr_val=current_atr
                            )
                            if tp_order and rem_qty > 0:
                                portfolio['partial_tp_hit'] = True
                                closed_qty = total_qty - rem_qty
                                portfolio['asset_balance'] = rem_qty
                                if portfolio.get('total_cost'):
                                    portfolio['total_cost'] = float(portfolio['total_cost']) * (rem_qty / total_qty)
                                cp = float(current_price)
                                if db_pos_direction == 'LONG':
                                    portfolio['usdt_balance'] = float(portfolio.get('usdt_balance', 0)) + (closed_qty * cp)
                                    pnl_realized_est = (cp - entry_price) * closed_qty
                                else:
                                    portfolio['usdt_balance'] = float(portfolio.get('usdt_balance', 0)) + (closed_qty * (2 * entry_price - cp))
                                    pnl_realized_est = (entry_price - cp) * closed_qty

                                msg = (f"🎯 [{symbol}] PARTIAL TAKE PROFIT EXECUTED ({db_pos_direction}) | "
                                       f"Closed 50% size ({closed_qty} units) at ${cp:.4f} (+{unrealized_pct*100:.2f}%) | "
                                       f"Est. Profit: +${pnl_realized_est:.2f} | Remaining Qty: {rem_qty} | Stop Loss locked at Entry ${entry_price:.4f}")
                                print(msg, flush=True)
                                logger.info(msg)
                                log_to_db(session, symbol, "ENTRY", msg)

                                history_record = TradeHistory(
                                    symbol=symbol,
                                    direction=db_pos_direction,
                                    entry_price=entry_price,
                                    exit_price=cp,
                                    quantity=closed_qty,
                                    pnl_usd=pnl_realized_est,
                                    pnl_pct=unrealized_pct * 100,
                                    outcome='WIN',
                                    exit_reason='PARTIAL_TAKE_PROFIT',
                                    closed_at=datetime.utcnow()
                                )
                                session.add(history_record)

                                try:
                                    latest_record = session.query(PortfolioState).filter(
                                        PortfolioState.symbol == symbol,
                                        PortfolioState.position_direction == db_pos_direction
                                    ).order_by(PortfolioState.id.desc()).first()
                                    if latest_record:
                                        latest_record.partial_tp_hit = True
                                        latest_record.asset_balance = rem_qty
                                        latest_record.total_cost = portfolio['total_cost']
                                        latest_record.usdt_balance = portfolio['usdt_balance']
                                        latest_record.stop_loss_price = entry_price
                                        latest_record.stop_loss = entry_price
                                    session.commit()
                                except Exception as e:
                                    session.rollback()
                                    print(f"Warning: Failed to persist partial TP state to DB: {e}", flush=True)


                    # Preserve existing local SL, or only calculate new SL if it's a completely new execution
                    sl_val = portfolio.get('stop_loss_price')
                    if sl_val is None or float(sl_val) <= 0:
                        existing_sl_row = session.query(PortfolioState.stop_loss_price, PortfolioState.stop_loss).filter(
                            PortfolioState.symbol == symbol,
                            PortfolioState.asset_balance > 0,
                            (PortfolioState.stop_loss_price.isnot(None) | PortfolioState.stop_loss.isnot(None))
                        ).order_by(PortfolioState.id.desc()).first()

                        if existing_sl_row and (existing_sl_row.stop_loss_price or existing_sl_row.stop_loss):
                            sl_val = float(existing_sl_row.stop_loss_price or existing_sl_row.stop_loss)
                            print(f"🔄 [{symbol}] Preserved existing local Stop Loss during sync: ${sl_val:.4f}", flush=True)
                        else:
                            if db_pos_direction == 'LONG':
                                sl_val = float(db_entry_price) - (sl_atr_mult * current_atr)
                            elif db_pos_direction == 'SHORT':
                                sl_val = float(db_entry_price) + (sl_atr_mult * current_atr)
                            print(f"🔄 [{symbol}] Calculated missing Stop Loss for new position execution during sync: ${sl_val:.4f}", flush=True)

                    portfolio['stop_loss_price'] = sl_val
                    portfolio['stop_loss'] = sl_val

                    if sl_val and float(sl_val) > 0 and futures_client:
                        try:
                            open_ords = futures_client.futures_get_open_orders(symbol=symbol)
                            algo_ords = futures_client.futures_get_open_algo_orders(symbol=symbol) if hasattr(futures_client, 'futures_get_open_algo_orders') else []
                            has_sl = any(o.get('type') == 'STOP_MARKET' or o.get('orderType') == 'STOP_MARKET' for o in (open_ords + algo_ords))
                            if not has_sl:
                                print(f"🛡️ [{symbol}] Missing live SL on Binance! Placing emergency hard Stop Loss at ${float(sl_val):.4f}", flush=True)
                                set_stop_loss_order(futures_client, symbol, db_pos_direction, float(sl_val))
                        except Exception as sl_check_err:
                            print(f"⚠️ [{symbol}] Could not check/set emergency SL on Binance: {sl_check_err}", flush=True)

                elif pos_info and pos_info['size'] == 0.0:
                    if in_position and db_pos_direction in ('LONG', 'SHORT'):
                        print(f"🧹 [{symbol}] Binance reports no position. Fetching true realized PNL and archiving (MANUAL_CLOSE)...", flush=True)
                        
                        realized_pnl_usd = 0.0
                        if futures_client:
                            try:
                                income_hist = futures_client.futures_income_history(symbol=symbol, incomeType="REALIZED_PNL", limit=10)
                                if income_hist:
                                    # income_hist is ascending by time; last element is the newest
                                    latest_time = income_hist[-1]['time']
                                    # Sum income from events within 60 seconds of the most recent closure
                                    recent_income = [float(x['income']) for x in income_hist if latest_time - x['time'] <= 60000]
                                    realized_pnl_usd = sum(recent_income)
                                    print(f"  💸 Fetched Realized PNL from Binance: ${realized_pnl_usd:.4f}", flush=True)
                            except Exception as e:
                                print(f"  ⚠️ Could not fetch realized PNL for {symbol}: {e}", flush=True)

                        ep = float(db_entry_price) if db_entry_price and float(db_entry_price) > 0 else float(current_price)
                        asset_bal = float(portfolio.get('asset_balance') or 0.0)
                        
                        pnl_pct_val = 0.0
                        if ep > 0 and asset_bal > 0:
                            pnl_pct_val = (realized_pnl_usd / (asset_bal * ep)) * 100

                        outcome = 'WIN' if realized_pnl_usd > 0 else 'LOSS'
                        history_record = TradeHistory(
                            symbol=symbol,
                            direction=db_pos_direction,
                            entry_price=ep,
                            exit_price=float(current_price),
                            quantity=asset_bal,
                            pnl_usd=realized_pnl_usd,
                            pnl_pct=pnl_pct_val,
                            outcome=outcome,
                            exit_reason='MANUAL_CLOSE',
                            closed_at=datetime.utcnow()
                        )
                        
                        try:
                            session.add(history_record)
                            session.query(PortfolioState).filter(PortfolioState.symbol == symbol).delete(synchronize_session=False)
                            
                            new_usdt_bal = float(portfolio.get('usdt_balance') or 0.0) + (asset_bal * float(current_price)) + realized_pnl_usd
                            closed_rec = PortfolioState(
                                timestamp=datetime.now(),
                                symbol=symbol,
                                decision='CLOSED',
                                current_price=float(current_price),
                                usdt_balance=new_usdt_bal,
                                asset_balance=0.0,
                                position_direction=None,
                                average_entry_price=None,
                                dca_level=0,
                                last_exec_price=None,
                                total_cost=0.0,
                                highest_price_since_entry=None,
                                lowest_price_since_entry=None,
                                stop_loss_price=None,
                                stop_loss=None,
                                strategy=portfolio.get('strategy'),
                                trailing_active=False,
                                pnl_pct=pnl_pct_val,
                                pnl_usd=realized_pnl_usd,
                                total_portfolio_value=new_usdt_bal,
                                active_mode=portfolio.get('active_mode')
                            )
                            session.add(closed_rec)
                            session.commit()
                            print(f"  ✅ [{symbol}] Successfully archived ghost position with actual PNL.", flush=True)
                        except Exception as e:
                            session.rollback()
                            print(f"  ⚠️ Error archiving ghost position for {symbol}: {e}", flush=True)

                        db_decision = 'MANUAL_CLOSE'
                        portfolio['asset_balance'] = 0.0
                        in_position = False

            # Fallback: if db_decision is still WAIT but local portfolio has a position
            if db_decision == 'WAIT' and in_position and db_pos_direction in ('LONG', 'SHORT'):
                db_decision = db_pos_direction

            if in_position:
                pos_direction = db_pos_direction or portfolio.get('position_direction')
                cp = float(current_price)

                # ── Update watermarks ──
                if pos_direction == 'LONG':
                    old_highest = portfolio.get('highest_price_since_entry')
                    if old_highest is None or cp > float(old_highest):
                        portfolio['highest_price_since_entry'] = cp
                        old_val = f"${float(old_highest):.2f}" if old_highest else "$0.00"
                        print(f"📈 [{symbol}] LONG new high watermark: ${cp:.2f} (was {old_val})", flush=True)

                elif pos_direction == 'SHORT':
                    old_lowest = portfolio.get('lowest_price_since_entry')
                    if old_lowest is None or cp < float(old_lowest):
                        portfolio['lowest_price_since_entry'] = cp
                        old_val = f"${float(old_lowest):.2f}" if old_lowest else "$0.00"
                        print(f"📉 [{symbol}] SHORT new low watermark: ${cp:.2f} (was {old_val})", flush=True)

                # ── Always save synced PortfolioState row ──
                total_value = float(portfolio['usdt_balance']) + (float(portfolio['asset_balance']) * cp)
                sl_val = portfolio.get('stop_loss_price') if portfolio.get('stop_loss_price') is not None else portfolio.get('stop_loss')

                portfolio_record = PortfolioState(
                    timestamp=datetime.now(),
                    symbol=symbol,
                    decision=db_decision,
                    current_price=cp,
                    usdt_balance=float(portfolio['usdt_balance']),
                    asset_balance=float(portfolio['asset_balance']),
                    position_direction=db_pos_direction,
                    average_entry_price=float(db_entry_price) if db_entry_price else None,
                    dca_level=int(portfolio['dca_level']),
                    last_exec_price=float(portfolio['last_exec_price']) if portfolio['last_exec_price'] is not None else None,
                    total_cost=float(portfolio['total_cost']),
                    highest_price_since_entry=float(portfolio['highest_price_since_entry']) if portfolio['highest_price_since_entry'] is not None else None,
                    lowest_price_since_entry=float(portfolio['lowest_price_since_entry']) if portfolio['lowest_price_since_entry'] is not None else None,
                    stop_loss_price=float(sl_val) if sl_val is not None and float(sl_val) > 0 else None,
                    stop_loss=float(sl_val) if sl_val is not None and float(sl_val) > 0 else None,
                    strategy=portfolio.get('strategy'),
                    trailing_active=portfolio.get('trailing_active', False),
                    pnl_pct=None,
                    pnl_usd=None,
                    total_portfolio_value=float(round(total_value, 2)),
                    active_mode=active_mode_value,
                )
                for attempt in range(3):
                    try:
                        session.add(portfolio_record)
                        session.commit()
                        msg = f"✅ [{symbol}] Position state synced to DB: decision='{db_decision}', direction='{db_pos_direction}', entry=${db_entry_price or 0:.2f}"
                        print(f"  {msg}", flush=True)
                        log_to_db(session, symbol, "INFO", msg)
                        break
                    except Exception as e:
                        session.rollback()
                        if attempt == 2:
                            print(f"Warning: Failed to save synced portfolio state to DB after 3 attempts: {e}", flush=True)
                            traceback.print_exc()
                        else:
                            import time; time.sleep(1)

        # ── Back-patch TradingSignal if sync changed db_decision ──
        # The TradingSignal was committed early (before execution & sync).
        # If the sync block upgraded db_decision (e.g. WAIT → LONG),
        # we must update the already-committed row so the dashboard reads
        # the correct state.
        if signal.decision != db_decision:
            print(f"  🔄 [{symbol}] Patching TradingSignal: "
                  f"'{signal.decision}' → '{db_decision}'", flush=True)
            signal.decision = db_decision
            try:
                session.commit()
            except Exception as e:
                session.rollback()
                print(f"Warning: Failed to patch TradingSignal decision: {e}", flush=True)

        # ── 7. Print summary ──
        print(f"\n=== Execution Summary [{symbol}] (15m MTF) ===", flush=True)
        print(f"Symbol: {symbol} | Timeframe: {timeframe}", flush=True)
        print(f"Price: ${current_price:,.2f} | RSI: {current_rsi:.1f}" if current_rsi else
              f"Price: ${current_price:,.2f}", flush=True)
        print(f"15m Z-Score: {current_zscore:+.3f}" if current_zscore else
              "15m Z-Score: N/A", flush=True)
        print(f"Macro Trend: {macro_emoji} {macro_trend} | 1h Z: {macro_zscore:+.2f}", flush=True)

        print("\n--- Order Blocks (Volume-Filtered) ---", flush=True)
        if bullish_ob:
            print(f"Bullish OB: ${bullish_ob['low']:.2f} - ${bullish_ob['high']:.2f} "
                  f"(Vol: {bullish_ob.get('vol_ratio', 0):.1f}x avg)", flush=True)
        else:
            print("Bullish OB: Not found (or volume too low)", flush=True)

        if bearish_ob:
            print(f"Bearish OB: ${bearish_ob['low']:.2f} - ${bearish_ob['high']:.2f} "
                  f"(Vol: {bearish_ob.get('vol_ratio', 0):.1f}x avg)", flush=True)
        else:
            print("Bearish OB: Not found (or volume too low)", flush=True)

        # Risk management status
        portfolio = load_portfolio(session, symbol)
        in_position = portfolio['asset_balance'] is not None and portfolio['asset_balance'] > 0
        pos_direction = portfolio.get('position_direction')
        if in_position and portfolio.get('average_entry_price'):
            ep = float(portfolio['average_entry_price'])
            cp = float(current_price)
            if pos_direction == 'LONG':
                hp = float(portfolio['highest_price_since_entry']) if portfolio.get('highest_price_since_entry') else cp
                current_sl = float(portfolio.get('stop_loss_price', 0) or 0)
                unrealized = ((cp - ep) / ep) * 100
                is_trailing = portfolio.get('trailing_active', False)
                trailing_status = "🟢 ACTIVE" if is_trailing else "⚪ INACTIVE"
                print(f"\n--- Risk Management (LONG) ---", flush=True)
                print(f"Entry: ${ep:.2f} | Unrealized: {unrealized:+.2f}% | Peak: ${hp:.2f}", flush=True)
                tsl_act_str = f"+${tsl_atr_activation_mult * current_atr:.4f} ({tsl_atr_activation_mult}x ATR)" if current_atr else f"+{TRAILING_ACTIVATE_PCT*100:.1f}%"
                tsl_trl_str = f"${tsl_atr_trail_mult * current_atr:.4f} ({tsl_atr_trail_mult}x ATR)" if current_atr else f"{TRAILING_DISTANCE_PCT*100:.1f}%"
                tp_status = "✅ HIT (50% Closed)" if portfolio.get('partial_tp_hit') else f"⚪ WAITING (+{partial_tp_pct*100:.1f}%)"
                print(f"Stop-Loss: ${current_sl:.2f} | TSL: {trailing_status} | Partial TP: {tp_status} "
                      f"(activates at {tsl_act_str}, trails {tsl_trl_str})", flush=True)
            elif pos_direction == 'SHORT':
                lp = float(portfolio['lowest_price_since_entry']) if portfolio.get('lowest_price_since_entry') else cp
                current_sl = float(portfolio.get('stop_loss_price', 0) or 0)
                unrealized = ((ep - cp) / ep) * 100
                is_trailing = portfolio.get('trailing_active', False)
                trailing_status = "🟢 ACTIVE" if is_trailing else "⚪ INACTIVE"
                tp_status = "✅ HIT (50% Closed)" if portfolio.get('partial_tp_hit') else f"⚪ WAITING (+{partial_tp_pct*100:.1f}%)"
                print(f"\n--- Risk Management (SHORT) ---", flush=True)
                print(f"Entry: ${ep:.2f} | Unrealized: {unrealized:+.2f}% | Trough: ${lp:.2f}", flush=True)
                tsl_act_str = f"+${tsl_atr_activation_mult * current_atr:.4f} ({tsl_atr_activation_mult}x ATR)" if current_atr else f"+{TRAILING_ACTIVATE_PCT*100:.1f}%"
                tsl_trl_str = f"${tsl_atr_trail_mult * current_atr:.4f} ({tsl_atr_trail_mult}x ATR)" if current_atr else f"{TRAILING_DISTANCE_PCT*100:.1f}%"
                print(f"Stop-Loss: ${current_sl:.2f} | TSL: {trailing_status} | Partial TP: {tp_status} "
                      f"(activates at {tsl_act_str}, trails {tsl_trl_str})", flush=True)

        if risk_exit_triggered:
            print(f"\n⚠️ [{symbol}] Risk exit was triggered this cycle.", flush=True)
        else:
            print(f"\n[{symbol}] Decision '{db_decision}' saved to database successfully.", flush=True)

        # ════════════════════════════════════════════════════════════════════
        # 🚀 EXECUTION ENGINE: BINANCE API PLACEMENT (Production Guards)
        # ════════════════════════════════════════════════════════════════════
        MAX_POSITION_USDT = 250.0        # Hard cap: never allocate more than $250
        MAX_WALLET_PCT    = 0.05         # Hard cap: never allocate more than 5% of wallet

        if futures_client and db_decision in ('LONG', 'SHORT'):
            import logging
            _exec_logger = logging.getLogger("FuturesExecutor")
            direction = db_decision

            # ── GUARD 0: Global Max Positions & Trade Upgrading (Position Rotation) ──
            current_open_count = count_all_open_positions(futures_client)
            can_proceed_with_entry = True

            # Check if candidate symbol is ALREADY in an active position
            live_pos = get_position_info(futures_client, symbol)
            if live_pos and live_pos['size'] > 0:
                _exec_logger.info(
                    f"🔒 [SKIP ENTRY] {symbol} already has an active Binance "
                    f"{live_pos['direction']} position (size={live_pos['size']}). Skipping."
                )
                can_proceed_with_entry = False

            if can_proceed_with_entry and current_open_count >= MAX_GLOBAL_POSITIONS:
                # Calculate candidate signal Strength Score
                cand_vol = 1.0
                if whale_strike:
                    cand_vol = whale_strike['vol_ratio']
                elif bullish_ob and 'vol_ratio' in bullish_ob:
                    cand_vol = bullish_ob['vol_ratio']
                elif bearish_ob and 'vol_ratio' in bearish_ob:
                    cand_vol = bearish_ob['vol_ratio']
                elif bullish_breakout and 'vol_ratio' in bullish_breakout:
                    cand_vol = bullish_breakout['vol_ratio']
                elif bearish_breakout and 'vol_ratio' in bearish_breakout:
                    cand_vol = bearish_breakout['vol_ratio']

                cand_score = calculate_strength_score(current_zscore, cand_vol)

                # NEW RULE: Strictly forbid upgrading/replacing an active position with Strategy A or Strategy B signals.
                # The ONLY condition allowed to trigger a POSITION_UPGRADE is if the incoming signal is a Whale Strike (Strategy C).
                if not whale_strike:
                    _exec_logger.warning(
                        f"🛑 [SKIP UPGRADE] Max global positions ({MAX_GLOBAL_POSITIONS}) reached. "
                        f"Signal {symbol} is a standard Strategy A/B signal. Position upgrades are STRICTLY FORBIDDEN "
                        f"unless triggered by a Whale Strike (Strategy C)."
                    )
                    can_proceed_with_entry = False
                else:
                    # Scan active open positions to find the weakest link
                    weak_sym, weak_score, weak_port, weak_stagnant = find_weakest_active_position(session, futures_client)
                    weak_score_val = float(weak_score) if weak_score is not None else 0.0

                    # Anti-churn upgrade threshold: candidate score >= weakest score + 1.5 (or weakest is stagnant)
                    if weak_sym and weak_sym != symbol and (weak_stagnant or (cand_score >= weak_score_val + 1.5)):
                        upgrade_msg = (
                            f"🔄 [POSITION UPGRADE] Max slots ({MAX_GLOBAL_POSITIONS}) reached! "
                            f"Closing weak position '{weak_sym}' (Score={weak_score_val:.2f}, Stagnant={weak_stagnant}) "
                            f"to enter Whale Strike signal '{symbol}' {direction} (Score={cand_score:.2f}, Z={current_zscore:+.2f})."
                        )
                        print(upgrade_msg, flush=True)
                        log_to_db(session, symbol, "UPGRADE", upgrade_msg)
                        send_telegram_alert(upgrade_msg)

                        # Gracefully close weakest position
                        weak_price = float(weak_port.get('average_entry_price') or current_price) if weak_port else current_price
                        _close_position_handler(
                            weak_port, weak_price, weak_sym, session, 'POSITION_UPGRADE',
                            futures_client, bullish_ob, bearish_ob, current_rsi,
                            current_zscore, macro_info
                        )
                        # ── ROTATION SYNC: Verify closure on Binance and DB before proceeding ──
                        closed_confirmed = False
                        if futures_client:
                            for _attempt in range(5):
                                import time
                                time.sleep(0.5)
                                check_pos = get_position_info(futures_client, weak_sym)
                                if not check_pos or check_pos.get('size', 0.0) == 0.0:
                                    closed_confirmed = True
                                    break
                        else:
                            closed_confirmed = True

                        if closed_confirmed:
                            current_open_count = count_all_open_positions(futures_client)
                            if current_open_count < MAX_GLOBAL_POSITIONS:
                                can_proceed_with_entry = True
                            else:
                                _exec_logger.error(f"❌ [ROTATION SYNC] After closing {weak_sym}, open count ({current_open_count}) >= limit ({MAX_GLOBAL_POSITIONS}). Blocking new entry.")
                                can_proceed_with_entry = False
                        else:
                            _exec_logger.error(f"❌ [ROTATION SYNC FAILED] Could not confirm closure of {weak_sym} on Binance after retries. Aborting upgrade entry for {symbol}.")
                            can_proceed_with_entry = False
                    else:
                        _exec_logger.warning(
                            f"🛑 [SKIP UPGRADE] Max global positions ({MAX_GLOBAL_POSITIONS}) reached. "
                            f"Whale Strike candidate {symbol} (Score={cand_score:.2f}, Z={current_zscore:+.2f}) "
                            f"is not strong enough to replace weakest position '{weak_sym or 'N/A'}' (Score={weak_score_val:.2f}). "
                            f"Required gap: +1.5."
                        )
                        can_proceed_with_entry = False

            if can_proceed_with_entry:
                    # ── GUARD 2: Live Free Margin Check ──
                    available_balance = get_futures_balance(futures_client)

                    # ── Position Sizing: Conviction Tier (capped) ──
                    _alloc_pct = 0.05  # Tier 3 default
                    _abs_z = abs(current_zscore) if current_zscore else 0.0
                    _vol_ratio = 0.0
                    if bullish_ob and 'vol_ratio' in bullish_ob:
                        _vol_ratio = bullish_ob['vol_ratio']
                    elif bearish_ob and 'vol_ratio' in bearish_ob:
                        _vol_ratio = bearish_ob['vol_ratio']
                    if _abs_z >= 2.0 and _vol_ratio >= 4.0:
                        _alloc_pct = 0.10   # Tier 1 (was 0.20 — capped for safety)
                    elif _abs_z >= 1.0 and _vol_ratio >= 2.0:
                        _alloc_pct = 0.05   # Tier 2 (was 0.10 — capped for safety)

                    # ── GUARD 3: Position Size Hard Cap ──
                    calculated_size = available_balance * _alloc_pct
                    wallet_cap      = available_balance * MAX_WALLET_PCT
                    allocated_usdt  = min(calculated_size, wallet_cap, MAX_POSITION_USDT)

                    _exec_logger.info(
                        f"[{symbol}] Sizing: calculated=${calculated_size:.2f}, "
                        f"wallet_cap=${wallet_cap:.2f}, hard_cap=${MAX_POSITION_USDT}, "
                        f"final_allocated=${allocated_usdt:.2f}"
                    )

                    # ── GUARD 4: Safety Bypass ──
                    if available_balance < 10.0:
                        _exec_logger.warning(
                            f"⚠️ [SKIP EXECUTION] Insufficient Free Margin: "
                            f"${available_balance:.2f} available (Required: ${allocated_usdt:.2f}) "
                            f"for {symbol} {direction}."
                        )
                    elif allocated_usdt > available_balance:
                        _exec_logger.warning(
                            f"⚠️ [SKIP EXECUTION] Insufficient Free Margin: "
                            f"${available_balance:.2f} available (Required: ${allocated_usdt:.2f}) "
                            f"for {symbol} {direction}."
                        )
                    elif allocated_usdt < 5.0:
                        _exec_logger.warning(
                            f"⚠️ [SKIP EXECUTION] Allocated amount too small: "
                            f"${allocated_usdt:.2f} for {symbol} {direction}."
                        )
                    elif new_stop_loss <= 0.0 or pd.isna(new_stop_loss) or (direction == 'LONG' and new_stop_loss >= current_price) or (direction == 'SHORT' and new_stop_loss <= current_price):
                        _exec_logger.error(
                            f"🛑 [SKIP EXECUTION] {symbol} {direction}: Invalid or missing Stop-Loss price (${new_stop_loss} vs Price ${current_price}). "
                            f"Binance order placement blocked."
                        )
                    else:
                        try:
                            order = open_position(futures_client, symbol, direction, allocated_usdt)
                            if order:
                                _exec_logger.info(
                                    f"✅ EXECUTED ON BINANCE: {direction} | "
                                    f"Symbol: {symbol} | "
                                    f"OrderID: {order.get('orderId')} | "
                                    f"Allocated: ${allocated_usdt:.2f}"
                                )
                                if new_stop_loss and float(new_stop_loss) > 0:
                                    set_stop_loss_order(futures_client, symbol, direction, float(new_stop_loss))
                                return True
                            else:
                                _exec_logger.warning(
                                    f"⚠️ [{symbol}] open_position returned None. Order blocked or restricted."
                                )
                                return False

                        except BinanceAPIException as api_err:
                            from futures_executor import _check_api_exception_for_blacklist
                            _check_api_exception_for_blacklist(symbol, api_err)
                            _exec_logger.warning(
                                f"⚠️ BINANCE API EXCEPTION: {symbol} {direction} | "
                                f"[{api_err.code}] {api_err.message}"
                            )
                        except Exception as e:
                            _exec_logger.error(
                                f"❌ BINANCE REJECTED ORDER: {symbol} {direction} | "
                                f"Allocated: ${allocated_usdt:.2f} | "
                                f"Error: {str(e)}"
                            )
                            traceback.print_exc()
        # ════════════════════════════════════════════════════════════════════


    except Exception as e:
        session.rollback()
        print(f"Error during analysis of {symbol}: {e}", flush=True)
        traceback.print_exc()
    finally:
        session.close()
        print(f"--- Execution Analyzer Completed [{symbol}] ---", flush=True)

    return False


if __name__ == "__main__":
    if ENGINE_ROLE.upper() == "MACRO":
        run_macro_analyzer()
    else:
        run_analyzer()
