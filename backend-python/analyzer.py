import os
import requests
import traceback
import numpy as np
import pandas as pd
from datetime import datetime
from database import (SessionLocal, MarketData, TradingSignal, PortfolioState,
                      engine, init_db, init_shared_db, count_active_positions,
                      save_macro_state, get_macro_trend,
                      SLOT_BUDGET, MAX_CONCURRENT_POSITIONS, TOTAL_CAPITAL)
from config import (TIMEFRAME, ALERT_PREFIX, ENGINE_ROLE,
                    MACRO_SMA_PERIOD, MACRO_SDC_MULTIPLIER,
                    ZSCORE_LONG_THRESHOLD, ZSCORE_SHORT_THRESHOLD,
                    ZSCORE_SMA_PERIOD, OB_VOLUME_MULTIPLIER, OB_VOLUME_MA_PERIOD,
                    BREAKOUT_VOLUME_MULTIPLIER, BREAKOUT_CONSOLIDATION_PERIOD,
                    TESTNET_FORCE_TRADES, HARD_STOP_LOSS_PCT, STOP_LOSS_PCT)
from futures_executor import open_position, close_position

# ──────────────────────────────────────────────────────────────────────
#  RISK MANAGEMENT CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
MAX_BUYS = 1
ENTRY_WEIGHT = 1.0
TRADING_FEE = 0.001                       # 0.1 % per side
MIN_PROFIT_PCT = 0.01                     # +1.0 %
TRAILING_ACTIVATE_PCT = 0.02              # +2.0 %
TRAILING_PULLBACK_PCT = 0.005             # -0.5 %


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
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("Telegram alert sent successfully.", flush=True)
        else:
            print(f"Telegram API error: {response.status_code} - {response.text}", flush=True)
    except requests.exceptions.RequestException as e:
        print(f"Failed to send Telegram alert: {e}", flush=True)
        traceback.print_exc()


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
    }


def _close_position_handler(portfolio, current_price, symbol, session, exit_reason,
                            futures_client=None, bullish_ob=None, bearish_ob=None,
                            current_rsi=None, current_zscore=None, macro_info=None):
    """
    Close an open position (LONG or SHORT): convert asset → USDT, compute PnL,
    optionally execute on Futures exchange, save to DB, and send a Telegram alert.
    Returns the updated portfolio dict.
    """
    direction = portfolio.get('position_direction', 'LONG')
    asset_balance = float(portfolio['asset_balance'])
    buy_price = portfolio.get('average_entry_price')
    total_cost = portfolio.get('total_cost', 0.0)
    pnl_pct_val = None
    pnl_usd_val = None
    pnl_section = ""

    # ── Calculate PnL ──
    if buy_price and float(buy_price) > 0:
        ep = float(buy_price)
        cp = float(current_price)
        if direction == 'LONG':
            pnl_pct_val = ((cp - ep) / ep) * 100
            sell_value = asset_balance * cp * (1 - TRADING_FEE)
        else:  # SHORT
            pnl_pct_val = ((ep - cp) / ep) * 100
            sell_value = asset_balance * ep + (asset_balance * (ep - cp)) - (asset_balance * cp * TRADING_FEE)
        pnl_usd_val = float(sell_value - total_cost)
        sign = "+" if pnl_pct_val >= 0 else ""
        pnl_section = f"\n- PnL (This Trade): {sign}{pnl_pct_val:.2f}% ({sign}${pnl_usd_val:.2f})"
    else:
        sell_value = asset_balance * float(current_price) * (1 - TRADING_FEE)

    # ── Execute Futures close order ──
    if futures_client and asset_balance > 0:
        order = close_position(futures_client, symbol, direction, asset_balance)
        if order:
            print(f"✅ [{symbol}] Futures CLOSE {direction} executed | OrderID: {order['orderId']}", flush=True)
        else:
            print(f"⚠️ [{symbol}] Futures CLOSE {direction} order failed — portfolio updated virtually", flush=True)

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

    # Save portfolio snapshot to database
    portfolio_record = PortfolioState(
        timestamp=datetime.now(),
        symbol=symbol,
        decision=close_label,
        current_price=float(current_price),
        usdt_balance=float(portfolio['usdt_balance']),
        asset_balance=float(portfolio['asset_balance']),
        position_direction=None,
        average_entry_price=None,
        dca_level=0,
        last_exec_price=None,
        total_cost=0.0,
        highest_price_since_entry=None,
        lowest_price_since_entry=None,
        stop_loss_price=None,
        trailing_active=False,
        pnl_pct=float(pnl_pct_val) if pnl_pct_val is not None else None,
        pnl_usd=float(pnl_usd_val) if pnl_usd_val is not None else None,
        total_portfolio_value=float(round(total_value, 2))
    )
    try:
        session.add(portfolio_record)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Warning: Failed to save portfolio state to DB: {e}", flush=True)
        traceback.print_exc()

    # Build Telegram alert
    reason_labels = {
        'SIGNAL': '📉 Technical Signal (MTF Confluence)',
        'STOP_LOSS': f'🛑 Hard Stop-Loss (-{0:.1f}%)',
        'TRAILING_STOP': '📐 Trailing Stop (pulled back from extreme)',
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


def _save_tracking_update(portfolio, current_price, symbol, session):
    """
    Save a portfolio snapshot that only updates highest/lowest_price_since_entry.
    """
    total_value = float(portfolio['usdt_balance']) + (float(portfolio['asset_balance']) * float(current_price))

    portfolio_record = PortfolioState(
        timestamp=datetime.now(),
        symbol=symbol,
        decision='HOLD',
        current_price=float(current_price),
        usdt_balance=float(portfolio['usdt_balance']),
        asset_balance=float(portfolio['asset_balance']),
        position_direction=portfolio.get('position_direction'),
        average_entry_price=float(portfolio['average_entry_price']) if portfolio['average_entry_price'] is not None else None,
        dca_level=int(portfolio['dca_level']),
        last_exec_price=float(portfolio['last_exec_price']) if portfolio['last_exec_price'] is not None else None,
        total_cost=float(portfolio['total_cost']),
        highest_price_since_entry=float(portfolio['highest_price_since_entry']) if portfolio['highest_price_since_entry'] is not None else None,
        lowest_price_since_entry=float(portfolio['lowest_price_since_entry']) if portfolio['lowest_price_since_entry'] is not None else None,
        stop_loss_price=float(portfolio['stop_loss_price']) if portfolio.get('stop_loss_price') is not None else None,
        trailing_active=portfolio.get('trailing_active', False),
        pnl_pct=None,
        pnl_usd=None,
        total_portfolio_value=float(round(total_value, 2))
    )
    try:
        session.add(portfolio_record)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Warning: Failed to save tracking update to DB: {e}", flush=True)
        traceback.print_exc()


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
    init_db()
    init_shared_db()

    session = SessionLocal()
    try:
        # ── 0. Fetch macro trend from shared DB ──
        macro_info = get_macro_trend(symbol)
        # ⚠️ TESTING BYPASS: Allow trades even without macro trend data
        if macro_info is None:
            print(f"⚠️ [{symbol}] No macro trend — bypassed for testing (MTF disabled).", flush=True)
            macro_info = {'macro_trend': 'UPTREND', 'z_score': 0.0, 'sma_50': 0.0}

        macro_trend = macro_info['macro_trend']
        macro_zscore = macro_info['z_score']
        macro_sma = macro_info['sma_50']
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

        # ── 3. Detect volume-filtered Order Blocks ──
        bullish_ob, bearish_ob = detect_order_blocks(df, lookback=15)

        # ── 4. Get current candle data ──
        last_row = df.iloc[-1]
        current_price = float(last_row['close'])
        current_rsi = float(last_row['RSI']) if pd.notna(last_row['RSI']) else None
        current_zscore = float(last_row['Z_Score']) if pd.notna(last_row['Z_Score']) else None
        current_sma = float(last_row[f'SMA_{ZSCORE_SMA_PERIOD}']) if pd.notna(last_row[f'SMA_{ZSCORE_SMA_PERIOD}']) else None

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
        in_position = portfolio['asset_balance'] is not None and portfolio['asset_balance'] > 0
        entry_price = portfolio.get('average_entry_price')
        highest_price = portfolio.get('highest_price_since_entry')
        lowest_price = portfolio.get('lowest_price_since_entry')
        pos_direction = portfolio.get('position_direction')

        # ──────────────────────────────────────────────────────────
        #  RISK MANAGEMENT EXITS (checked BEFORE signal logic)
        # ──────────────────────────────────────────────────────────
        risk_exit_triggered = False

        if in_position and entry_price and float(entry_price) > 0:
            cp = float(current_price)
            ep = float(entry_price)
            stop_loss = float(portfolio.get('stop_loss_price', 0) or 0)
            trailing_active = portfolio.get('trailing_active', False)

            if pos_direction == 'LONG':
                unrealized_pct = (cp - ep) / ep

                if highest_price is None or cp > float(highest_price):
                    portfolio['highest_price_since_entry'] = cp
                    highest_price = cp

                # Activate trailing stop (move SL to breakeven + trail)
                if not trailing_active and unrealized_pct >= TRAILING_ACTIVATE_PCT:
                    portfolio['trailing_active'] = True
                    portfolio['stop_loss_price'] = max(stop_loss, ep) # Move to breakeven
                    stop_loss = portfolio['stop_loss_price']
                    trailing_active = True
                    print(f"✅ [{symbol}] LONG Trailing Stop ACTIVATED! SL moved to Breakeven ${stop_loss:.2f}", flush=True)

                if trailing_active and highest_price:
                    hp = float(highest_price)
                    trail_sl = hp * (1 - TRAILING_PULLBACK_PCT)
                    if trail_sl > stop_loss:
                        portfolio['stop_loss_price'] = trail_sl
                        stop_loss = trail_sl
                        
                # Check Stop Loss hit
                if stop_loss > 0 and cp <= stop_loss:
                    print(f"⛔ [{symbol}] LONG STOP-LOSS hit at ${cp:.2f} (SL: ${stop_loss:.2f})", flush=True)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'STOP_LOSS' if not trailing_active else 'TRAILING_STOP',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True


            elif pos_direction == 'SHORT':
                unrealized_pct = (ep - cp) / ep

                if lowest_price is None or cp < float(lowest_price):
                    portfolio['lowest_price_since_entry'] = cp
                    lowest_price = cp

                # Activate trailing stop
                if not trailing_active and unrealized_pct >= TRAILING_ACTIVATE_PCT:
                    portfolio['trailing_active'] = True
                    portfolio['stop_loss_price'] = min(stop_loss, ep) if stop_loss > 0 else ep # Move to breakeven
                    stop_loss = portfolio['stop_loss_price']
                    trailing_active = True
                    print(f"✅ [{symbol}] SHORT Trailing Stop ACTIVATED! SL moved to Breakeven ${stop_loss:.2f}", flush=True)

                if trailing_active and lowest_price:
                    lp = float(lowest_price)
                    trail_sl = lp * (1 + TRAILING_PULLBACK_PCT)
                    if stop_loss == 0 or trail_sl < stop_loss:
                        portfolio['stop_loss_price'] = trail_sl
                        stop_loss = trail_sl
                        
                # Check Stop Loss hit
                if stop_loss > 0 and cp >= stop_loss:
                    print(f"⛔ [{symbol}] SHORT STOP-LOSS hit at ${cp:.2f} (SL: ${stop_loss:.2f})", flush=True)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'STOP_LOSS' if not trailing_active else 'TRAILING_STOP',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True

        # ── 3.5 Detect Breakouts ──
        bullish_breakout, bearish_breakout = detect_consolidation_breakout(df)

        # ──────────────────────────────────────────────────────────
        #  SIGNAL LOGIC — MTF Confluence (Dual-Strategy)
        # ──────────────────────────────────────────────────────────
        decision = 'WAIT'
        strategy_type = None
        new_stop_loss = 0.0

        if not risk_exit_triggered and current_zscore is not None:
            active_count = count_active_positions(session)

            # ── LONG Confluence ──
            # ⚠️ TESTING BYPASS: macro_trend gate disabled — accept LONGs regardless of trend
            if not in_position:  # [PROD: if macro_trend == 'UPTREND' and not in_position:]
                # Strategy A: Aggressive Pullback
                if (bullish_ob and current_price <= bullish_ob['high'] and current_zscore < ZSCORE_LONG_THRESHOLD):
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'LONG'
                        strategy_type = 'PULLBACK'
                        new_stop_loss = bullish_ob['low'] * 0.999 # Strictly below OB
                        print(f"✨ [{symbol}] LONG Strategy A (PULLBACK): Macro=UPTREND + Bullish OB + Z={current_zscore:+.2f}", flush=True)
                
                # Strategy B: Momentum Breakout
                elif bullish_breakout:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'LONG'
                        strategy_type = 'BREAKOUT'
                        new_stop_loss = bullish_breakout['breakout_candle_low'] * 0.999 # Below breakout candle
                        print(f"⚡ [{symbol}] LONG Strategy B (BREAKOUT): Macro=UPTREND + Breakout Confirmed (Vol {bullish_breakout['vol_ratio']:.1f}x)", flush=True)

                # Strategy C: Testnet — Pure Trend Alignment (force trades)
                elif TESTNET_FORCE_TRADES and current_sma and current_price > current_sma:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'LONG'
                        strategy_type = 'TREND_ALIGN'
                        new_stop_loss = current_sma * 0.995  # SL just below the SMA
                        print(f"🧪 [{symbol}] LONG Strategy C (TREND_ALIGN): Macro=UPTREND + Price > SMA-50", flush=True)

            # ── SHORT Confluence ──
            # ⚠️ TESTING BYPASS: macro_trend gate disabled — accept SHORTs regardless of trend
            if decision == 'WAIT' and not in_position:  # [PROD: if decision == 'WAIT' and macro_trend == 'DOWNTREND' and not in_position:]
                # Strategy A: Aggressive Pullback
                if (bearish_ob and current_price >= bearish_ob['low'] and current_zscore > ZSCORE_SHORT_THRESHOLD):
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'SHORT'
                        strategy_type = 'PULLBACK'
                        new_stop_loss = bearish_ob['high'] * 1.001 # Strictly above OB
                        print(f"✨ [{symbol}] SHORT Strategy A (PULLBACK): Macro=DOWNTREND + Bearish OB + Z={current_zscore:+.2f}", flush=True)
                
                # Strategy B: Momentum Breakout
                elif bearish_breakout:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'SHORT'
                        strategy_type = 'BREAKOUT'
                        new_stop_loss = bearish_breakout['breakout_candle_high'] * 1.001 # Above breakout candle
                        print(f"⚡ [{symbol}] SHORT Strategy B (BREAKOUT): Macro=DOWNTREND + Breakout Confirmed (Vol {bearish_breakout['vol_ratio']:.1f}x)", flush=True)

                # Strategy C: Testnet — Pure Trend Alignment (force trades)
                elif TESTNET_FORCE_TRADES and current_sma and current_price < current_sma:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'SHORT'
                        strategy_type = 'TREND_ALIGN'
                        new_stop_loss = current_sma * 1.005  # SL just above the SMA
                        print(f"🧪 [{symbol}] SHORT Strategy C (TREND_ALIGN): Macro=DOWNTREND + Price < SMA-50", flush=True)

            # ── 🚨 DEBUG LOGGER: Why is the bot skipping? ──
            if decision == 'WAIT' and not risk_exit_triggered:
                if in_position:
                    print(f"  [DEBUG] {symbol} | Skipping entry: Already in position.", flush=True)
                else:
                    print(f"  [DEBUG] {symbol} | Z: {current_zscore:+.3f} (Req: <{ZSCORE_LONG_THRESHOLD} or >{ZSCORE_SHORT_THRESHOLD})", flush=True)
                    
                    if bullish_ob:
                        print(f"  [DEBUG] {symbol} | Bullish OB detected. High=${bullish_ob['high']:.2f}, Price=${current_price:.2f} (Req: Price <= OB High)", flush=True)
                    if bearish_ob:
                        print(f"  [DEBUG] {symbol} | Bearish OB detected. Low=${bearish_ob['low']:.2f}, Price=${current_price:.2f} (Req: Price >= OB Low)", flush=True)
                    
                    if not bullish_ob and not bearish_ob and not bullish_breakout and not bearish_breakout:
                        print(f"  [DEBUG] {symbol} | Skipping trade: No Order Block or Breakout detected on 5m chart.", flush=True)
                    elif active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"  [DEBUG] {symbol} | Skipping trade: Max concurrent slots reached ({active_count}/{MAX_CONCURRENT_POSITIONS}).", flush=True)
                    else:
                        print(f"  [DEBUG] {symbol} | Skipping trade: Signal does not match all criteria (Z-score not extreme enough OR Price not inside OB).", flush=True)

        # ── 6. Save signal to Database ──
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
            decision='CLOSE_LONG' if risk_exit_triggered and pos_direction == 'LONG'
                     else 'CLOSE_SHORT' if risk_exit_triggered and pos_direction == 'SHORT'
                     else decision
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

                # ── Execute LONG (open or DCA) ──
                dca_level = portfolio.get('dca_level', 0)
                if decision == 'LONG':
                    spend = SLOT_BUDGET * ENTRY_WEIGHT
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
                        if dca_level == 0:
                            portfolio['highest_price_since_entry'] = float(current_price)
                            portfolio['lowest_price_since_entry'] = None

                        if futures_client:
                            order = open_position(futures_client, symbol, 'LONG', spend)
                            if order:
                                print(f"✅ [{symbol}] Futures OPEN LONG executed | OrderID: {order['orderId']}", flush=True)
                            else:
                                print(f"⚠️ [{symbol}] Futures OPEN LONG order failed — portfolio updated virtually", flush=True)

                        total_value = portfolio['usdt_balance'] + (float(portfolio['asset_balance']) * float(current_price))

                        # Generate reason_msg before DB insertion
                        if strategy_type == 'PULLBACK':
                            ob_low = bullish_ob['low'] if bullish_ob else 0
                            ob_high = bullish_ob['high'] if bullish_ob else 0
                            vol_ratio = f"{bullish_ob['vol_ratio']:.1f}x" if bullish_ob and 'vol_ratio' in bullish_ob else "N/A"
                            reason_msg = (f"Strategy A (Pullback) | Macro: {macro_trend} | Z: {current_zscore:+.2f} | "
                                          f"OB: ${float(ob_low):.2f}-${float(ob_high):.2f} (Vol {vol_ratio})")
                        elif strategy_type == 'BREAKOUT':
                            vol_ratio = f"{bullish_breakout['vol_ratio']:.1f}x" if bullish_breakout and 'vol_ratio' in bullish_breakout else "N/A"
                            reason_msg = (f"Strategy B (Breakout) | Macro: {macro_trend} | Vol {vol_ratio} avg | Consolidation High Cleared")
                        elif strategy_type == 'TREND_ALIGN':
                            reason_msg = (f"Strategy C (Trend Align) | Macro: {macro_trend} | "
                                          f"Price ${current_price:.2f} > SMA-50 ${current_sma:.2f} | 🧪 TESTNET ONLY")
                        else:
                            reason_msg = f"Strategy: Unknown"

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
                            highest_price_since_entry=float(portfolio['highest_price_since_entry']),
                            stop_loss_price=float(new_stop_loss),
                            trailing_active=False,
                            lowest_price_since_entry=None,
                            pnl_pct=None,
                            pnl_usd=None,
                            total_portfolio_value=float(round(total_value, 2)),
                            entry_reason=reason_msg
                        )
                        try:
                            session.add(portfolio_record)
                            session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"Warning: Failed to save portfolio state to DB: {e}", flush=True)
                            traceback.print_exc()

                        # Telegram alert
                        alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
                        
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
                        else:
                            alert_reason = f"- Strategy: Unknown"

                        alert_msg = (
                            f"{ALERT_PREFIX} \U0001f6a8 *QUANT ALERT: OPEN LONG* \U0001f6a8\n"
                            f"\n"
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
                            f"- Trailing Stop activates at: +{TRAILING_ACTIVATE_PCT*100:.1f}%\n"
                            f"\n"
                            f"\U0001f4bc *Virtual Portfolio:*\n"
                            f"- Slot Budget: ${SLOT_BUDGET:,.0f}\n"
                            f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
                            f"- Asset Balance: {portfolio['asset_balance']:.6f}\n"
                            f"- Total Value: ${total_value:.2f}"
                        )
                        send_telegram_alert(alert_msg)

                # ── Execute SHORT (open new short position) ──
                elif decision == 'SHORT' and not in_position:
                    spend = SLOT_BUDGET * ENTRY_WEIGHT
                    if portfolio['usdt_balance'] >= spend:
                        effective_usdt = spend * (1 - TRADING_FEE)
                        asset_shorted = effective_usdt / float(current_price)

                        portfolio['asset_balance'] = round(asset_shorted, 6)
                        portfolio['average_entry_price'] = float(current_price)
                        portfolio['dca_level'] = 1
                        portfolio['last_exec_price'] = float(current_price)
                        portfolio['total_cost'] = effective_usdt
                        portfolio['usdt_balance'] -= spend
                        portfolio['position_direction'] = 'SHORT'
                        portfolio['lowest_price_since_entry'] = float(current_price)
                        portfolio['highest_price_since_entry'] = None

                        if futures_client:
                            order = open_position(futures_client, symbol, 'SHORT', spend)
                            if order:
                                print(f"✅ [{symbol}] Futures OPEN SHORT executed | OrderID: {order['orderId']}", flush=True)
                            else:
                                print(f"⚠️ [{symbol}] Futures OPEN SHORT order failed — portfolio updated virtually", flush=True)

                        total_value = portfolio['usdt_balance'] + (float(portfolio['asset_balance']) * float(current_price))

                        # Generate reason_msg before DB insertion
                        if strategy_type == 'PULLBACK':
                            ob_low = bearish_ob['low'] if bearish_ob else 0
                            ob_high = bearish_ob['high'] if bearish_ob else 0
                            vol_ratio = f"{bearish_ob['vol_ratio']:.1f}x" if bearish_ob and 'vol_ratio' in bearish_ob else "N/A"
                            reason_msg = (f"Strategy A (Pullback) | Macro: {macro_trend} | Z: {current_zscore:+.2f} | "
                                          f"OB: ${float(ob_low):.2f}-${float(ob_high):.2f} (Vol {vol_ratio})")
                        elif strategy_type == 'BREAKOUT':
                            vol_ratio = f"{bearish_breakout['vol_ratio']:.1f}x" if bearish_breakout and 'vol_ratio' in bearish_breakout else "N/A"
                            reason_msg = (f"Strategy B (Breakout) | Macro: {macro_trend} | Vol {vol_ratio} avg | Consolidation Low Broken")
                        elif strategy_type == 'TREND_ALIGN':
                            reason_msg = (f"Strategy C (Trend Align) | Macro: {macro_trend} | "
                                          f"Price ${current_price:.2f} < SMA-50 ${current_sma:.2f} | 🧪 TESTNET ONLY")
                        else:
                            reason_msg = f"Strategy: Unknown"

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
                            lowest_price_since_entry=float(portfolio['lowest_price_since_entry']),
                            stop_loss_price=float(new_stop_loss),
                            trailing_active=False,
                            pnl_pct=None,
                            pnl_usd=None,
                            total_portfolio_value=float(round(total_value, 2)),
                            entry_reason=reason_msg
                        )
                        try:
                            session.add(portfolio_record)
                            session.commit()
                        except Exception as e:
                            session.rollback()
                            print(f"Warning: Failed to save portfolio state to DB: {e}", flush=True)
                            traceback.print_exc()

                        # Telegram alert
                        alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
                        
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
                        else:
                            alert_reason = f"- Strategy: Unknown"

                        alert_msg = (
                            f"{ALERT_PREFIX} \U0001f6a8 *QUANT ALERT: OPEN SHORT* \U0001f6a8\n"
                            f"\n"
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
                            f"- Trailing Stop activates at: +{TRAILING_ACTIVATE_PCT*100:.1f}%\n"
                            f"\n"
                            f"\U0001f4bc *Virtual Portfolio:*\n"
                            f"- Slot Budget: ${SLOT_BUDGET:,.0f}\n"
                            f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
                            f"- Asset Balance: {portfolio['asset_balance']:.6f}\n"
                            f"- Total Value: ${total_value:.2f}"
                        )
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
        #  UPDATE TRAILING STOP WATERMARK
        # ──────────────────────────────────────────────────────────
        if not risk_exit_triggered and decision == 'WAIT':
            portfolio = load_portfolio(session, symbol)
            in_position = portfolio['asset_balance'] is not None and portfolio['asset_balance'] > 0

            if in_position:
                pos_direction = portfolio.get('position_direction')
                cp = float(current_price)
                watermark_updated = False

                if pos_direction == 'LONG':
                    old_highest = portfolio.get('highest_price_since_entry')
                    if old_highest is None or cp > float(old_highest):
                        portfolio['highest_price_since_entry'] = cp
                        old_val = f"${float(old_highest):.2f}" if old_highest else "$0.00"
                        print(f"📈 [{symbol}] LONG new high watermark: ${cp:.2f} (was {old_val})", flush=True)
                        watermark_updated = True

                elif pos_direction == 'SHORT':
                    old_lowest = portfolio.get('lowest_price_since_entry')
                    if old_lowest is None or cp < float(old_lowest):
                        portfolio['lowest_price_since_entry'] = cp
                        old_val = f"${float(old_lowest):.2f}" if old_lowest else "$0.00"
                        print(f"📉 [{symbol}] SHORT new low watermark: ${cp:.2f} (was {old_val})", flush=True)
                        watermark_updated = True

                if watermark_updated:
                    _save_tracking_update(portfolio, current_price, symbol, session)

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
                unrealized = ((cp - ep) / ep) * 100
                trailing_status = "ACTIVE" if (cp - ep) / ep >= TRAILING_ACTIVATE_PCT else "INACTIVE"
                print(f"\n--- Risk Management (LONG) ---", flush=True)
                print(f"Entry: ${ep:.2f} | Unrealized: {unrealized:+.2f}%", flush=True)
                print(f"Stop-Loss @ ${ep * (1 - HARD_STOP_LOSS_PCT):.2f} | "
                      f"Trailing: {trailing_status} (Peak: ${hp:.2f})", flush=True)
            elif pos_direction == 'SHORT':
                lp = float(portfolio['lowest_price_since_entry']) if portfolio.get('lowest_price_since_entry') else cp
                unrealized = ((ep - cp) / ep) * 100
                trailing_status = "ACTIVE" if (ep - cp) / ep >= TRAILING_ACTIVATE_PCT else "INACTIVE"
                print(f"\n--- Risk Management (SHORT) ---", flush=True)
                print(f"Entry: ${ep:.2f} | Unrealized: {unrealized:+.2f}%", flush=True)
                print(f"Stop-Loss @ ${ep * (1 + STOP_LOSS_PCT):.2f} | "
                      f"Trailing: {trailing_status} (Trough: ${lp:.2f})", flush=True)

        if risk_exit_triggered:
            print(f"\n⚠️ [{symbol}] Risk exit was triggered this cycle.", flush=True)
        else:
            print(f"\n[{symbol}] Decision {decision} saved to database successfully.", flush=True)

    except Exception as e:
        session.rollback()
        print(f"Error during analysis of {symbol}: {e}", flush=True)
        traceback.print_exc()
    finally:
        session.close()
        print(f"--- Execution Analyzer Completed [{symbol}] ---", flush=True)


if __name__ == "__main__":
    if ENGINE_ROLE.upper() == "MACRO":
        run_macro_analyzer()
    else:
        run_analyzer()
