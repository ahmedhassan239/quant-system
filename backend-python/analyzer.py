import os
import requests
import traceback
import numpy as np
import pandas as pd
from datetime import datetime
from database import (SessionLocal, MarketData, TradingSignal, PortfolioState, BotLog,
                      TradeHistory, engine, init_db, init_shared_db, count_active_positions,
                      save_macro_state, get_macro_trend,
                      SLOT_BUDGET, MAX_CONCURRENT_POSITIONS, TOTAL_CAPITAL)
from config import (TIMEFRAME, ALERT_PREFIX, ENGINE_ROLE,
                    MACRO_SMA_PERIOD, MACRO_SDC_MULTIPLIER,
                    ZSCORE_LONG_THRESHOLD, ZSCORE_SHORT_THRESHOLD,
                    ZSCORE_SMA_PERIOD, OB_VOLUME_MULTIPLIER, OB_VOLUME_MA_PERIOD,
                    BREAKOUT_VOLUME_MULTIPLIER, BREAKOUT_CONSOLIDATION_PERIOD,
                    TESTNET_FORCE_TRADES, HARD_STOP_LOSS_PCT, STOP_LOSS_PCT)
from futures_executor import (open_position, close_position, get_futures_balance,
                              get_position_info, count_all_open_positions)

# ──────────────────────────────────────────────────────────────────────
#  RISK MANAGEMENT CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
MAX_BUYS = 1
ENTRY_WEIGHT = 1.0
TRADING_FEE = 0.001                       # 0.1 % per side
MIN_PROFIT_PCT = 0.01                     # +1.0 %
TRAILING_ACTIVATE_PCT = 0.015             # +1.5 % unrealized PnL to activate TSL
TRAILING_DISTANCE_PCT = 0.01              # 1.0 % trailing distance from peak/trough
TRAILING_PULLBACK_PCT = 0.005             # -0.5 % (legacy, kept for compat)
HARD_STOP_LOSS_PCT = 0.05                 # 5.0 % absolute stop loss
STOP_LOSS_PCT = 0.05                      # 5.0 % trailing/soft stop loss
MAX_GLOBAL_POSITIONS = 6                  # Hard limit: max open positions on Binance


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
        sl_val = getattr(last_state, 'stop_loss_price', None)
        if sl_val is None:
            sl_val = getattr(last_state, 'stop_loss', None)

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
        'strategy': None,
    }


def sync_and_purge_all_positions(futures_client, session):
    """
    Sync live Binance positions with DB and purge ghost positions.
    Ensures real live positions (ETH, XRP, BNB, BROCCOLI, BANK, ON, etc.)
    always have active PortfolioState DB records with asset_balance > 0
    and decision in ('LONG', 'SHORT').
    """
    if not futures_client:
        return

    try:
        from futures_executor import fetch_all_positions
        from database import get_open_position_symbols
        live_positions = fetch_all_positions(futures_client)
        if not live_positions:
            return

        open_db_symbols = get_open_position_symbols(session)

        # 1. Sync live Binance positions into DB
        for sym, pos in live_positions.items():
            size = pos.get('size', 0.0)
            if size > 0:
                direction = pos['direction']
                entry_price = pos['entry_price']
                u_pnl = pos['unrealized_pnl']

                latest_rec = session.query(PortfolioState).filter(
                    PortfolioState.symbol == sym
                ).order_by(PortfolioState.id.desc()).first()

                sl_val = 0.0
                if latest_rec:
                    sl_val = getattr(latest_rec, 'stop_loss', None) or getattr(latest_rec, 'stop_loss_price', None) or 0.0

                if not latest_rec or latest_rec.asset_balance <= 0 or latest_rec.decision not in ('LONG', 'SHORT'):
                    new_rec = PortfolioState(
                        timestamp=datetime.now(),
                        symbol=sym,
                        decision=direction,
                        current_price=entry_price,
                        usdt_balance=1000.0,
                        asset_balance=size,
                        position_direction=direction,
                        average_entry_price=entry_price,
                        dca_level=0,
                        total_cost=size * entry_price,
                        stop_loss_price=float(sl_val) if sl_val > 0 else 0.0,
                        stop_loss=float(sl_val) if sl_val > 0 else 0.0,
                        pnl_usd=u_pnl,
                        total_portfolio_value=1000.0
                    )
                    session.add(new_rec)
                    print(f"🔄 [SYNC] Live Binance position for {sym} ({direction}, Qty={size}) synced to DB", flush=True)

        # 2. Purge ghost positions (symbols with DB asset_balance > 0 but size == 0 on Binance)
        for sym in open_db_symbols:
            live_info = live_positions.get(sym)
            if live_info and live_info.get('size', 0.0) == 0.0:
                print(f"🧹 [PURGE] Clearing ghost position in DB for {sym} (Binance size = 0)", flush=True)
                latest_rec = session.query(PortfolioState).filter(
                    PortfolioState.symbol == sym
                ).order_by(PortfolioState.id.desc()).first()
                if latest_rec:
                    latest_rec.asset_balance = 0.0
                    latest_rec.decision = 'MANUAL_CLOSE'

        session.commit()
    except Exception as e:
        session.rollback()
        print(f"⚠️ Error in sync_and_purge_all_positions: {e}", flush=True)


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

    # ── Execute Futures close order (use LIVE Binance position size) ──
    if futures_client and asset_balance > 0:
        import logging as _close_logging
        _close_logger = _close_logging.getLogger("FuturesExecutor")
        # Query the REAL position size from Binance to avoid quantity mismatches
        live_pos = get_position_info(futures_client, symbol)
        close_qty = live_pos['size'] if (live_pos and live_pos['size'] > 0) else asset_balance
        _close_logger.warning(
            f"🚨 SL TRIGGERED & EXECUTED: {symbol} at {current_price} "
            f"(exit_reason={exit_reason}, closing qty={close_qty})"
        )
        order = close_position(futures_client, symbol, direction, close_qty)
        if order:
            print(f"✅ [{symbol}] Futures CLOSE {direction} executed | OrderID: {order['orderId']} | Qty: {close_qty}", flush=True)
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
        # 2. Delete the active position from PortfolioState
        session.query(PortfolioState).filter(PortfolioState.symbol == symbol).delete(synchronize_session=False)
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
        try:
            pos_info = get_position_info(futures_client, symbol)
            if pos_info and pos_info['size'] > 0:
                db_decision = pos_info['direction']  # Force 'LONG' or 'SHORT'
                db_position_direction = pos_info['direction']
                db_entry_price = pos_info['entry_price']
                db_pnl_usd = pos_info['unrealized_pnl']
                portfolio['asset_balance'] = pos_info['size']
                portfolio['position_direction'] = pos_info['direction']
        except (BinanceAPIException, requests.exceptions.HTTPError, Exception) as exc:
            if is_rate_limit_error(exc):
                print(f"⚠️ API Rate Limit hit in _save_tracking_update for {symbol}. Preserving DB state.", flush=True)
            else:
                print(f"⚠️ API error in _save_tracking_update for {symbol} ({exc}). Preserving DB state.", flush=True)

    if portfolio.get('asset_balance', 0) > 0 and db_decision not in ('LONG', 'SHORT'):
        db_decision = db_position_direction or 'LONG'

    sl_val = portfolio.get('stop_loss_price') if portfolio.get('stop_loss_price') is not None else portfolio.get('stop_loss')
    if sl_val is None or (isinstance(sl_val, (int, float)) and sl_val <= 0):
        last_rec = session.query(PortfolioState).filter(
            PortfolioState.symbol == symbol
        ).order_by(PortfolioState.id.desc()).first()
        if last_rec:
            sl_val = getattr(last_rec, 'stop_loss_price', None) or getattr(last_rec, 'stop_loss', None)

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
        stop_loss_price=float(sl_val) if sl_val is not None and float(sl_val) > 0 else 0.0,
        stop_loss=float(sl_val) if sl_val is not None and float(sl_val) > 0 else 0.0,
        strategy=portfolio.get('strategy'),
        trailing_active=portfolio.get('trailing_active', False),
        pnl_pct=None,
        pnl_usd=db_pnl_usd,
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

                # Track new peak price
                if highest_price is None or cp > float(highest_price):
                    portfolio['highest_price_since_entry'] = cp
                    highest_price = cp

                # ── Fast Take-Profit (+1.0%) ──
                # Capture quick gains immediately before TSL or SL logic.
                if not risk_exit_triggered and unrealized_pct >= 0.01:
                    msg = (f"🎯 QUICK TP HIT: {symbol} LONG at {unrealized_pct*100:+.2f}% PnL "
                           f"(entry=${ep:.4f}, current=${cp:.4f})")
                    print(msg, flush=True)
                    log_to_db(session, symbol, "EXIT", msg)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'QUICK_TP',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True

                # ── Trailing Stop Loss (Profit-Locking) ──
                # Only activates once unrealized PnL >= TRAILING_ACTIVATE_PCT
                if not risk_exit_triggered and unrealized_pct >= TRAILING_ACTIVATE_PCT:
                    if not trailing_active:
                        trailing_active = True
                        portfolio['trailing_active'] = True
                        msg = (f"📈 [{symbol}] TRAILING STOP ACTIVATED for LONG | "
                               f"Unrealized: {unrealized_pct*100:+.2f}% (threshold: {TRAILING_ACTIVATE_PCT*100:.1f}%)")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)

                    # Trail from the PEAK price, not current price
                    peak = float(highest_price) if highest_price else cp
                    new_sl = peak * (1 - TRAILING_DISTANCE_PCT)

                    if new_sl > stop_loss:
                        old_sl = stop_loss
                        portfolio['stop_loss_price'] = new_sl
                        portfolio['stop_loss'] = new_sl
                        stop_loss = new_sl
                        locked_pnl = ((new_sl - ep) / ep) * 100
                        msg = (f"📈 TRAILING STOP UPDATED: {symbol} | "
                               f"New SL: ${new_sl:.4f} (was ${old_sl:.4f}) | "
                               f"Peak: ${peak:.2f} | Locked Profit: {locked_pnl:+.2f}%")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)

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
                # Frees frozen capital slots when a position goes sideways.
                if not risk_exit_triggered and in_position:
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


            elif pos_direction == 'SHORT':
                unrealized_pct = (ep - cp) / ep

                # Track new trough price
                if lowest_price is None or cp < float(lowest_price):
                    portfolio['lowest_price_since_entry'] = cp
                    lowest_price = cp

                # ── Fast Take-Profit (+1.0%) ──
                # Capture quick gains immediately before TSL or SL logic.
                if not risk_exit_triggered and unrealized_pct >= 0.01:
                    msg = (f"🎯 QUICK TP HIT: {symbol} SHORT at {unrealized_pct*100:+.2f}% PnL "
                           f"(entry=${ep:.4f}, current=${cp:.4f})")
                    print(msg, flush=True)
                    log_to_db(session, symbol, "EXIT", msg)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'QUICK_TP',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True

                # ── Trailing Stop Loss (Profit-Locking) ──
                # Only activates once unrealized PnL >= TRAILING_ACTIVATE_PCT
                if not risk_exit_triggered and unrealized_pct >= TRAILING_ACTIVATE_PCT:
                    if not trailing_active:
                        trailing_active = True
                        portfolio['trailing_active'] = True
                        msg = (f"📈 [{symbol}] TRAILING STOP ACTIVATED for SHORT | "
                               f"Unrealized: {unrealized_pct*100:+.2f}% (threshold: {TRAILING_ACTIVATE_PCT*100:.1f}%)")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)

                    # Trail from the TROUGH price, not current price
                    trough = float(lowest_price) if lowest_price else cp
                    new_sl = trough * (1 + TRAILING_DISTANCE_PCT)

                    if stop_loss == 0 or new_sl < stop_loss:
                        old_sl = stop_loss
                        portfolio['stop_loss_price'] = new_sl
                        portfolio['stop_loss'] = new_sl
                        stop_loss = new_sl
                        locked_pnl = ((ep - new_sl) / ep) * 100
                        msg = (f"📈 TRAILING STOP UPDATED: {symbol} | "
                               f"New SL: ${new_sl:.4f} (was ${old_sl:.4f}) | "
                               f"Trough: ${trough:.2f} | Locked Profit: {locked_pnl:+.2f}%")
                        print(msg, flush=True)
                        log_to_db(session, symbol, "INFO", msg)

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
                # Frees frozen capital slots when a position goes sideways.
                if not risk_exit_triggered and in_position:
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
            if macro_trend == 'UPTREND' and not in_position:
                # Strategy A: Aggressive Pullback
                if (bullish_ob and current_price >= bullish_ob['low'] and current_zscore < -1.0):
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'LONG'
                        strategy_type = 'PULLBACK'
                        new_stop_loss = bullish_ob['low'] * 0.999 # Strictly below OB
                        msg = f"✨ [{symbol}] LONG Strategy A (PULLBACK): Macro={macro_trend} + Bullish OB + Z={current_zscore:+.2f}"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)
                
                # Strategy B: Momentum Breakout
                elif bullish_breakout:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'LONG'
                        strategy_type = 'BREAKOUT'
                        new_stop_loss = bullish_breakout['breakout_candle_low'] * 0.999 # Below breakout candle
                        msg = f"⚡ [{symbol}] LONG Strategy B (BREAKOUT): Macro={macro_trend} + Breakout Confirmed (Vol {bullish_breakout['vol_ratio']:.1f}x)"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)

                # Strategy C: Testnet — Pure Trend Alignment (force trades)
                elif TESTNET_FORCE_TRADES and current_sma and current_price > current_sma and current_rsi < 65 and current_zscore < 1.5:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'LONG'
                        strategy_type = 'TREND_ALIGN'
                        new_stop_loss = current_sma * 0.995  # SL just below the SMA
                        msg = f"🧪 [{symbol}] LONG Strategy C (TREND_ALIGN): Macro={macro_trend} + Price > SMA-50"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)

            # ── SHORT Confluence ──
            if decision == 'WAIT' and macro_trend == 'DOWNTREND' and not in_position:
                # Strategy A: Aggressive Pullback
                if (bearish_ob and current_price <= bearish_ob['high'] and current_zscore > 1.0):
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'SHORT'
                        strategy_type = 'PULLBACK'
                        new_stop_loss = bearish_ob['high'] * 1.001 # Strictly above OB
                        msg = f"✨ [{symbol}] SHORT Strategy A (PULLBACK): Macro={macro_trend} + Bearish OB + Z={current_zscore:+.2f}"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)
                
                # Strategy B: Momentum Breakout
                elif bearish_breakout:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'SHORT'
                        strategy_type = 'BREAKOUT'
                        new_stop_loss = bearish_breakout['breakout_candle_high'] * 1.001 # Above breakout candle
                        msg = f"⚡ [{symbol}] SHORT Strategy B (BREAKOUT): Macro={macro_trend} + Breakout Confirmed (Vol {bearish_breakout['vol_ratio']:.1f}x)"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)

                # Strategy C: Testnet — Pure Trend Alignment (force trades)
                elif TESTNET_FORCE_TRADES and current_sma and current_price < current_sma and current_rsi > 35 and current_zscore > -1.5:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'SHORT'
                        strategy_type = 'TREND_ALIGN'
                        new_stop_loss = current_sma * 1.005  # SL just above the SMA
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
        # Build db_decision from live Binance state WITHOUT mutating `decision`.
        # This keeps execution logic untouched (no duplicate orders) while
        # ensuring the DB row reflects the true position for Laravel.
        db_decision = decision  # default: whatever the signal logic decided

        if decision == 'WAIT' and not risk_exit_triggered:
            # Check live Binance position (source of truth)
            if futures_client:
                try:
                    pos_info = get_position_info(futures_client, symbol)
                    if pos_info and pos_info['size'] > 0:
                        db_decision = pos_info['direction']  # 'LONG' or 'SHORT'
                        print(f"  🔒 [{symbol}] DB decision synced to '{db_decision}' "
                              f"(live Binance position, size={pos_info['size']})", flush=True)
                except Exception as exc:
                    print(f"⚠️ [{symbol}] Signal position sync check skipped ({exc})", flush=True)
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

                # ── Execute LONG (open or DCA) ──
                dca_level = portfolio.get('dca_level', 0)
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
                        portfolio['stop_loss_price'] = float(new_stop_loss)
                        portfolio['stop_loss'] = float(new_stop_loss)
                        if dca_level == 0:
                            portfolio['highest_price_since_entry'] = float(current_price)
                            portfolio['lowest_price_since_entry'] = None

                        # [REMOVED] Order execution was previously here before DB save.


                        total_value = portfolio['usdt_balance'] + (float(portfolio['asset_balance']) * float(current_price))

                        # Generate reason_msg before DB insertion
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
                        else:
                            strategy_name = f"{tier_str} - Unknown"
                            reason_msg = f"Strategy: Unknown"
                        
                        portfolio['strategy'] = strategy_name

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
                            stop_loss=float(new_stop_loss),
                            strategy=strategy_name,
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
                        if not futures_client or order:
                            send_telegram_alert(alert_msg)

                # ── Execute SHORT (open new short position) ──
                elif decision == 'SHORT' and not in_position:
                    # Determine Conviction Tier
                    vol_ratio = 0.0
                    if strategy_type == 'PULLBACK' and bearish_ob and 'vol_ratio' in bearish_ob:
                        vol_ratio = bearish_ob['vol_ratio']
                    elif strategy_type == 'BREAKOUT' and bearish_breakout and 'vol_ratio' in bearish_breakout:
                        vol_ratio = bearish_breakout['vol_ratio']

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
                        asset_shorted = effective_usdt / float(current_price)

                        portfolio['asset_balance'] = round(asset_shorted, 6)
                        portfolio['average_entry_price'] = float(current_price)
                        portfolio['dca_level'] = 1
                        portfolio['last_exec_price'] = float(current_price)
                        portfolio['total_cost'] = effective_usdt
                        portfolio['usdt_balance'] -= spend
                        portfolio['position_direction'] = 'SHORT'
                        portfolio['stop_loss_price'] = float(new_stop_loss)
                        portfolio['stop_loss'] = float(new_stop_loss)
                        portfolio['lowest_price_since_entry'] = float(current_price)
                        portfolio['highest_price_since_entry'] = None

                        # [REMOVED] Order execution was previously here before DB save.

                        total_value = portfolio['usdt_balance'] + (float(portfolio['asset_balance']) * float(current_price))

                        # Generate reason_msg before DB insertion
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
                        else:
                            strategy_name = f"{tier_str} - Unknown"
                            reason_msg = f"Strategy: Unknown"
                        
                        portfolio['strategy'] = strategy_name

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
                            stop_loss=float(new_stop_loss),
                            strategy=strategy_name,
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
                try:
                    pos_info = get_position_info(futures_client, symbol)
                    if pos_info is not None:
                        if pos_info['size'] > 0:
                            db_decision = pos_info['direction']           # 'LONG' or 'SHORT'
                            db_pos_direction = pos_info['direction']
                            db_entry_price = pos_info['entry_price']
                            db_unrealized_pnl = pos_info['unrealized_pnl']
                            in_position = True  # Binance confirms position is open
                            print(f"  🔒 [{symbol}] Binance sync: {db_decision} | "
                                  f"Entry=${db_entry_price:.2f} | "
                                  f"uPnL=${db_unrealized_pnl:.2f}", flush=True)
                        elif pos_info['size'] == 0.0:
                            # ONLY clear DB slot if API successfully responded and confirmed size is 0
                            if in_position and db_pos_direction in ('LONG', 'SHORT'):
                                print(f"🧹 [{symbol}] Binance confirmed 0 position. Clearing DB slot (MANUAL_CLOSE).", flush=True)
                                db_decision = 'MANUAL_CLOSE'
                                portfolio['asset_balance'] = 0.0
                                in_position = False
                    else:
                        print(f"⚠️ [{symbol}] API error or Rate Limit during position sync. PRESERVING DB state.", flush=True)
                except Exception as exc:
                    print(f"⚠️ [{symbol}] Position sync error ({exc}). PRESERVING DB state.", flush=True)

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
                if sl_val is None or (isinstance(sl_val, (int, float)) and sl_val <= 0):
                    last_rec = session.query(PortfolioState).filter(
                        PortfolioState.symbol == symbol,
                        PortfolioState.position_direction == db_pos_direction
                    ).order_by(PortfolioState.id.desc()).first()
                    if last_rec:
                        sl_val = getattr(last_rec, 'stop_loss_price', None) or getattr(last_rec, 'stop_loss', None)
                        if sl_val and float(sl_val) > 0:
                            portfolio['stop_loss_price'] = float(sl_val)
                            portfolio['stop_loss'] = float(sl_val)

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
                    total_portfolio_value=float(round(total_value, 2))
                )
                try:
                    session.add(portfolio_record)
                    session.commit()
                    msg = f"✅ [{symbol}] Position state synced to DB: decision='{db_decision}', direction='{db_pos_direction}', entry=${db_entry_price or 0:.2f}, sl=${float(sl_val) if sl_val else 0:.4f}"
                    print(f"  {msg}", flush=True)
                    log_to_db(session, symbol, "INFO", msg)
                except Exception as e:
                    session.rollback()
                    print(f"Warning: Failed to save synced portfolio state to DB: {e}", flush=True)
                    traceback.print_exc()

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
                print(f"Stop-Loss: ${current_sl:.2f} | TSL: {trailing_status} "
                      f"(activates at +{TRAILING_ACTIVATE_PCT*100:.1f}%, trails {TRAILING_DISTANCE_PCT*100:.1f}%)", flush=True)
            elif pos_direction == 'SHORT':
                lp = float(portfolio['lowest_price_since_entry']) if portfolio.get('lowest_price_since_entry') else cp
                current_sl = float(portfolio.get('stop_loss_price', 0) or 0)
                unrealized = ((ep - cp) / ep) * 100
                is_trailing = portfolio.get('trailing_active', False)
                trailing_status = "🟢 ACTIVE" if is_trailing else "⚪ INACTIVE"
                print(f"\n--- Risk Management (SHORT) ---", flush=True)
                print(f"Entry: ${ep:.2f} | Unrealized: {unrealized:+.2f}% | Trough: ${lp:.2f}", flush=True)
                print(f"Stop-Loss: ${current_sl:.2f} | TSL: {trailing_status} "
                      f"(activates at +{TRAILING_ACTIVATE_PCT*100:.1f}%, trails {TRAILING_DISTANCE_PCT*100:.1f}%)", flush=True)

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

            # ── GUARD 0: Global Max Positions (Live Binance Count) ──
            current_open_count = count_all_open_positions(futures_client)
            if current_open_count >= MAX_GLOBAL_POSITIONS:
                _exec_logger.warning(
                    f"🛑 [SKIP ENTRY] Max global positions ({MAX_GLOBAL_POSITIONS}) reached. "
                    f"Currently open: {current_open_count}. "
                    f"Skipping {symbol} {direction} this cycle."
                )
            else:
                # ── GUARD 1: No Duplicate Entries ──
                live_pos = get_position_info(futures_client, symbol)
                if live_pos and live_pos['size'] > 0:
                    _exec_logger.info(
                        f"🔒 [SKIP ENTRY] {symbol} already has an active Binance "
                        f"{live_pos['direction']} position (size={live_pos['size']}). Skipping."
                    )
                else:
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
                            # ── Fetch stepSize precision for this symbol ──
                            info = futures_client.futures_exchange_info()
                            step_size = 0.001  # Safe fallback
                            for s in info['symbols']:
                                if s['symbol'] == symbol:
                                    for flt in s['filters']:
                                        if flt['filterType'] == 'LOT_SIZE':
                                            step_size = float(flt['stepSize'])
                                            break
                                    break

                            precision = (
                                len(str(step_size).rstrip('0').split('.')[-1])
                                if '.' in str(step_size) else 0
                            )

                            # ── Calculate quantity with strict precision ──
                            raw_qty = allocated_usdt / float(current_price)
                            qty = round(raw_qty - (raw_qty % step_size), precision)

                            if qty <= 0:
                                _exec_logger.error(
                                    f"❌ [{symbol}] Calculated qty is 0 after rounding "
                                    f"(allocated=${allocated_usdt:.2f}, price=${current_price:.2f}, "
                                    f"step={step_size}, precision={precision})"
                                )
                            else:
                                side = 'BUY' if direction == 'LONG' else 'SELL'

                                _exec_logger.info(
                                    f"Placing {direction} order for {symbol} | "
                                    f"Qty: {qty} | Allocated: ${allocated_usdt:.2f} | "
                                    f"Balance: ${available_balance:.2f} | "
                                    f"Tier: {_alloc_pct*100:.0f}%"
                                )

                                order = futures_client.futures_create_order(
                                    symbol=symbol,
                                    side=side,
                                    type='MARKET',
                                    quantity=qty,
                                )

                                _exec_logger.info(
                                    f"✅ EXECUTED ON BINANCE: {direction} | "
                                    f"Symbol: {symbol} | Qty: {qty} | "
                                    f"OrderID: {order['orderId']} | "
                                    f"Allocated: ${allocated_usdt:.2f}"
                                )
                                return True

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
