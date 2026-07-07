import os
import requests
import traceback
import pandas as pd
from datetime import datetime
from database import SessionLocal, MarketData, TradingSignal, PortfolioState, engine, init_db

DEFAULT_USDT_BALANCE = 1000.0

# ──────────────────────────────────────────────────────────────────────
#  RISK MANAGEMENT CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
MAX_BUYS = 4
DCA_WEIGHTS = [0.10, 0.20, 0.30, 0.40]
SAFETY_ORDER_DIP_PCT = 0.02
TRADING_FEE = 0.001
MIN_PROFIT_PCT = 0.01         # +1.0%
HARD_STOP_LOSS_PCT = 0.05     # -5.0%
TRAILING_ACTIVATE_PCT = 0.015
TRAILING_PULLBACK_PCT = 0.005


def load_portfolio(session, symbol):
    """
    Load the latest portfolio state from the database.
    Returns a dict with usdt_balance, paxg_balance, average_entry_price,
    dca_level, last_exec_price, total_cost, and highest_price_since_entry.
    """
    last_state = session.query(PortfolioState).filter(
        PortfolioState.symbol == symbol
    ).order_by(PortfolioState.id.desc()).first()

    if last_state:
        return {
            'usdt_balance': last_state.usdt_balance,
            'paxg_balance': last_state.paxg_balance,
            'average_entry_price': getattr(last_state, 'average_entry_price', None),
            'dca_level': getattr(last_state, 'dca_level', 0),
            'last_exec_price': getattr(last_state, 'last_exec_price', None),
            'total_cost': getattr(last_state, 'total_cost', 0.0),
            'highest_price_since_entry': last_state.highest_price_since_entry
        }
    # First run — return defaults
    return {
        'usdt_balance': DEFAULT_USDT_BALANCE,
        'paxg_balance': 0.0,
        'average_entry_price': None,
        'dca_level': 0,
        'last_exec_price': None,
        'total_cost': 0.0,
        'highest_price_since_entry': None
    }


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

def calculate_rsi(df, period=14):
    """
    Calculate 14-period RSI using pure Pandas and Wilder's Smoothing.
    """
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    
    # Apply standard Wilder's Smoothing (EMA with alpha=1/14)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    df.loc[avg_loss == 0, 'RSI'] = 100.0
    return df

def detect_order_blocks(df, lookback=15):
    """
    Detect basic SMC Order Blocks (Bullish and Bearish) in the recent lookback period.
    """
    if len(df) < lookback:
        return None, None
        
    recent_df = df.iloc[-lookback:]
    
    # -- Bullish OB --
    # Identify the lowest low in the lookback period
    min_idx = recent_df['low'].idxmin()
    min_loc = df.index.get_loc(min_idx)
    
    bullish_ob = None
    # Find the last bearish candle (close < open) before or at the lowest low
    for i in range(min_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] < row['open']:
            bullish_ob = {
                'high': row['high'],
                'low': row['low'],
                'timestamp': row['timestamp']
            }
            break

    # -- Bearish OB --
    # Identify the highest high in the lookback period
    max_idx = recent_df['high'].idxmax()
    max_loc = df.index.get_loc(max_idx)
    
    bearish_ob = None
    # Find the last bullish candle (close > open) before or at the highest high
    for i in range(max_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] > row['open']:
            bearish_ob = {
                'high': row['high'],
                'low': row['low'],
                'timestamp': row['timestamp']
            }
            break

    return bullish_ob, bearish_ob


# ──────────────────────────────────────────────────────────────────────
#  PORTFOLIO HELPERS
# ──────────────────────────────────────────────────────────────────────

def _execute_sell(portfolio, current_price, symbol, session, exit_reason,
                  bullish_ob=None, bearish_ob=None, current_rsi=None):
    """
    Execute a SELL: convert PAXG → USDT, compute PnL, save to DB,
    and send a Telegram alert.  Returns the updated portfolio dict.
    """
    sell_value = float(portfolio['paxg_balance']) * float(current_price) * (1 - TRADING_FEE)
    buy_price = portfolio.get('average_entry_price')
    total_cost = portfolio.get('total_cost', 0.0)
    pnl_pct_val = None
    pnl_usd_val = None
    pnl_section = ""

    if buy_price and float(buy_price) > 0:
        pnl_pct_val = float(((float(current_price) - float(buy_price)) / float(buy_price)) * 100)
        pnl_usd_val = float(sell_value - total_cost)
        sign = "+" if pnl_pct_val >= 0 else ""
        pnl_section = f"\\n- PnL (This Trade): {sign}{pnl_pct_val:.2f}% ({sign}${pnl_usd_val:.2f})"

    portfolio['usdt_balance'] += round(sell_value, 2)
    portfolio['paxg_balance'] = 0.0
    portfolio['average_entry_price'] = None
    portfolio['dca_level'] = 0
    portfolio['last_exec_price'] = None
    portfolio['total_cost'] = 0.0
    portfolio['highest_price_since_entry'] = None

    # Calculate total portfolio value
    total_value = float(portfolio['usdt_balance'])

    # Save portfolio snapshot to database
    portfolio_record = PortfolioState(
        timestamp=datetime.now(),
        symbol=symbol,
        decision='SELL',
        current_price=float(current_price),
        usdt_balance=float(portfolio['usdt_balance']),
        paxg_balance=float(portfolio['paxg_balance']),
        average_entry_price=None,
        dca_level=0,
        last_exec_price=None,
        total_cost=0.0,
        highest_price_since_entry=None,
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
        'SIGNAL': '📉 Technical Signal (RSI + Order Block)',
        'STOP_LOSS': '🛑 Hard Stop-Loss (-1.5%)',
        'TRAILING_STOP': '📐 Trailing Stop (pulled back from peak)',
    }
    reason_text = reason_labels.get(exit_reason, exit_reason)

    # Build indicator context
    if current_rsi is not None:
        rsi_info = f"- RSI: {float(current_rsi):.1f}\n"
    else:
        rsi_info = ""

    ob_info = ""
    if bearish_ob:
        ob_info = f"- Bearish OB: ${float(bearish_ob['low']):.2f} – ${float(bearish_ob['high']):.2f}\n"

    alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')

    alert_msg = (
        f"\U0001f6a8 *QUANT ALERT: SELL* \U0001f6a8\n"
        f"\n"
        f"*Symbol:* {symbol}\n"
        f"*Price:* ${float(current_price):.2f}\n"
        f"*Time:* {alert_time}\n"
        f"\n"
        f"\U0001f4a1 *Exit Reason:* {reason_text}\n"
        f"{rsi_info}"
        f"{ob_info}"
        f"\n"
        f"\U0001f4bc *Virtual Portfolio:*{pnl_section}\n"
        f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
        f"- PAXG Balance: {portfolio['paxg_balance']:.6f} PAXG\n"
        f"- Total Value: ${total_value:.2f}"
    )
    send_telegram_alert(alert_msg)

    return portfolio


def _save_tracking_update(portfolio, current_price, symbol, session):
    """
    Save a portfolio snapshot that only updates highest_price_since_entry
    (no BUY/SELL, just a state persistence for trailing stop tracking).
    """
    total_value = float(portfolio['usdt_balance']) + (float(portfolio['paxg_balance']) * float(current_price))

    portfolio_record = PortfolioState(
        timestamp=datetime.now(),
        symbol=symbol,
        decision='HOLD',
        current_price=float(current_price),
        usdt_balance=float(portfolio['usdt_balance']),
        paxg_balance=float(portfolio['paxg_balance']),
        average_entry_price=float(portfolio['average_entry_price']) if portfolio['average_entry_price'] is not None else None,
        dca_level=int(portfolio['dca_level']),
        last_exec_price=float(portfolio['last_exec_price']) if portfolio['last_exec_price'] is not None else None,
        total_cost=float(portfolio['total_cost']),
        highest_price_since_entry=float(portfolio['highest_price_since_entry']) if portfolio['highest_price_since_entry'] is not None else None,
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


def run_analyzer(symbol='PAXGUSDT', timeframe='15m'):
    """
    Query market data, calculate RSI, detect Order Blocks, check risk
    management exits, evaluate signals, and manage virtual portfolio.
    """
    print("--- Analyzer Started ---", flush=True)
    # Ensure tables exist (specifically for the new TradingSignal table)
    init_db()
    
    session = SessionLocal()
    try:
        # Query all records for the symbol and timeframe, ordered by timestamp ascending
        query = session.query(MarketData).filter(
            MarketData.symbol == symbol,
            MarketData.timeframe == timeframe
        ).order_by(MarketData.timestamp.asc())
        
        # Load the queried data into a Pandas DataFrame
        df = pd.read_sql(query.statement, engine)
        
        if df.empty:
            print("No data found in the database.", flush=True)
            return

        # 1. Calculate 14-period RSI
        df = calculate_rsi(df, period=14)
        df['SMA_200'] = df['close'].rolling(window=200).mean()
        
        # 2. Detect Order Blocks
        bullish_ob, bearish_ob = detect_order_blocks(df, lookback=15)
        
        # 3. Get current candle data
        last_row = df.iloc[-1]
        current_price = last_row['close']
        current_rsi = last_row['RSI']
        current_sma = float(last_row['SMA_200']) if pd.notna(last_row['SMA_200']) else None

        # 4. Load portfolio state (includes trailing stop watermark)
        portfolio = load_portfolio(session, symbol)
        in_position = portfolio['paxg_balance'] is not None and portfolio['paxg_balance'] > 0
        entry_price = portfolio.get('average_entry_price')
        highest_price = portfolio.get('highest_price_since_entry')

        # ──────────────────────────────────────────────────────────
        #  RISK MANAGEMENT EXITS (checked BEFORE signal logic)
        # ──────────────────────────────────────────────────────────
        risk_exit_triggered = False

        if in_position and entry_price and float(entry_price) > 0:
            cp = float(current_price)
            ep = float(entry_price)
            unrealized_pct = (cp - ep) / ep

            # Update highest price watermark
            if highest_price is None or cp > float(highest_price):
                portfolio['highest_price_since_entry'] = cp
                highest_price = cp

            # 1) Hard Stop-Loss: -1.5% from entry
            if unrealized_pct <= -HARD_STOP_LOSS_PCT:
                print(f"⛔ HARD STOP-LOSS triggered! Price ${cp:.2f} is "
                      f"{unrealized_pct*100:.2f}% below entry ${ep:.2f}", flush=True)
                portfolio = _execute_sell(
                    portfolio, current_price, symbol, session, 'STOP_LOSS',
                    bullish_ob, bearish_ob, current_rsi)
                risk_exit_triggered = True

            # 2) Trailing Stop: activate at +1.5%, trigger on -0.5% pullback
            elif unrealized_pct >= TRAILING_ACTIVATE_PCT and highest_price:
                hp = float(highest_price)
                pullback_pct = (hp - cp) / hp
                if pullback_pct >= TRAILING_PULLBACK_PCT:
                    print(f"📐 TRAILING STOP triggered! Price ${cp:.2f} pulled back "
                          f"{pullback_pct*100:.2f}% from peak ${hp:.2f}", flush=True)
                    portfolio = _execute_sell(
                        portfolio, current_price, symbol, session, 'TRAILING_STOP',
                        bullish_ob, bearish_ob, current_rsi)
                    risk_exit_triggered = True

        # ──────────────────────────────────────────────────────────
        #  SIGNAL LOGIC (only if no risk exit was triggered)
        # ──────────────────────────────────────────────────────────
        decision = 'WAIT'

        if not risk_exit_triggered:
            dca_level = portfolio.get('dca_level', 0)
            if current_rsi < 30:
                if dca_level == 0 and bullish_ob and current_price <= bullish_ob['high'] and current_sma and current_price > current_sma:
                    decision = 'BUY'
                elif 0 < dca_level < MAX_BUYS:
                    last_exec_price = portfolio.get('last_exec_price')
                    if last_exec_price and current_price <= last_exec_price * (1 - SAFETY_ORDER_DIP_PCT):
                        decision = 'BUY'

            if bearish_ob and current_price >= bearish_ob['low'] and current_rsi > 70:
                decision = 'SELL'

        # 5. Save signal to Database (always, even on WAIT)
        signal = TradingSignal(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=pd.Timestamp(last_row['timestamp']).to_pydatetime(),
            current_price=float(current_price),
            rsi=float(current_rsi) if pd.notna(current_rsi) else None,
            bullish_ob_low=float(bullish_ob['low']) if bullish_ob else None,
            bullish_ob_high=float(bullish_ob['high']) if bullish_ob else None,
            bearish_ob_low=float(bearish_ob['low']) if bearish_ob else None,
            bearish_ob_high=float(bearish_ob['high']) if bearish_ob else None,
            decision='SELL' if risk_exit_triggered else decision
        )
        
        session.add(signal)
        session.commit()

        # ──────────────────────────────────────────────────────────
        #  EXECUTE SIGNALS (BUY / SELL with min-profit gate)
        # ──────────────────────────────────────────────────────────
        if not risk_exit_triggered and decision in ('BUY', 'SELL'):
            # Reload portfolio state (in case risk exit changed it)
            portfolio = load_portfolio(session, symbol)
            in_position = portfolio['paxg_balance'] is not None and portfolio['paxg_balance'] > 0

            # Query the last non-WAIT decision from the database
            last_signal = session.query(TradingSignal).filter(
                TradingSignal.symbol == symbol,
                TradingSignal.timeframe == timeframe,
                TradingSignal.decision.in_(['BUY', 'SELL']),
                TradingSignal.id != signal.id  # Exclude the one we just inserted
            ).order_by(TradingSignal.id.desc()).first()

            last_decision = last_signal.decision if last_signal else None

            if decision != last_decision or decision == 'BUY':
                # ── Execute BUY ──
                dca_level = portfolio.get('dca_level', 0)
                if decision == 'BUY' and dca_level < MAX_BUYS:
                    spend = DEFAULT_USDT_BALANCE * DCA_WEIGHTS[dca_level]
                    if portfolio['usdt_balance'] >= spend:
                        effective_usdt = spend * (1 - TRADING_FEE)
                        paxg_bought = effective_usdt / float(current_price)
                        
                        # Update average entry price
                        old_paxg = float(portfolio.get('paxg_balance', 0) or 0)
                        old_avg = float(portfolio.get('average_entry_price', 0) or 0)
                        old_val = old_paxg * old_avg
                        new_val = paxg_bought * float(current_price)
                        
                        portfolio['paxg_balance'] = round(old_paxg + paxg_bought, 6)
                        portfolio['average_entry_price'] = (old_val + new_val) / portfolio['paxg_balance']
                        portfolio['dca_level'] = dca_level + 1
                        portfolio['last_exec_price'] = float(current_price)
                        portfolio['total_cost'] = float(portfolio.get('total_cost', 0)) + effective_usdt
                        portfolio['usdt_balance'] -= spend
                        if dca_level == 0:
                            portfolio['highest_price_since_entry'] = float(current_price)

                        # Calculate total portfolio value at current price
                        total_value = portfolio['usdt_balance'] + (float(portfolio['paxg_balance']) * float(current_price))

                        # Save portfolio snapshot to database
                        portfolio_record = PortfolioState(
                            timestamp=datetime.now(),
                            symbol=symbol,
                            decision='BUY',
                            current_price=float(current_price),
                            usdt_balance=float(portfolio['usdt_balance']),
                            paxg_balance=float(portfolio['paxg_balance']),
                            average_entry_price=float(portfolio['average_entry_price']),
                            dca_level=int(portfolio['dca_level']),
                            last_exec_price=float(portfolio['last_exec_price']),
                            total_cost=float(portfolio['total_cost']),
                            highest_price_since_entry=float(portfolio['highest_price_since_entry']),
                            pnl_pct=None,
                            pnl_usd=None,
                            total_portfolio_value=float(round(total_value, 2))
                        )
                    try:
                        session.add(portfolio_record)
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        print(f"Warning: Failed to save portfolio state to DB: {e}", flush=True)
                        traceback.print_exc()

                    # Telegram alert for BUY
                    rsi_label = "Oversold"
                    ob_type = "Bullish"
                    ob_low = bullish_ob['low'] if bullish_ob else 0
                    ob_high = bullish_ob['high'] if bullish_ob else 0
                    alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')

                    alert_msg = (
                        f"\U0001f6a8 *QUANT ALERT: BUY* \U0001f6a8\n"
                        f"\n"
                        f"*Symbol:* {symbol}\n"
                        f"*Price:* ${float(current_price):.2f}\n"
                        f"*Time:* {alert_time}\n"
                        f"\n"
                        f"\U0001f4a1 *Why this decision?*\n"
                        f"- RSI is at {float(current_rsi):.1f} (Indicates {rsi_label}).\n"
                        f"- Price entered {ob_type} Order Block between ${float(ob_low):.2f} and ${float(ob_high):.2f}.\n"
                        f"\n"
                        f"\U0001f6e1 *Risk Management Active:*\n"
                        f"- Stop-Loss: -1.5% (${float(current_price) * (1 - HARD_STOP_LOSS_PCT):.2f})\n"
                        f"- Trailing Stop activates at +1.5%\n"
                        f"\n"
                        f"\U0001f4bc *Virtual Portfolio:*\n"
                        f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
                        f"- PAXG Balance: {portfolio['paxg_balance']:.6f} PAXG\n"
                        f"- Total Value: ${total_value:.2f}"
                    )
                    send_telegram_alert(alert_msg)

                # ── Execute SELL (with min-profit gate) ──
                elif decision == 'SELL' and in_position and portfolio['paxg_balance'] > 0:
                    ep = float(portfolio['average_entry_price']) if portfolio['average_entry_price'] else 0
                    if ep > 0:
                        profit_pct = (float(current_price) - ep) / ep
                        if profit_pct < MIN_PROFIT_PCT:
                            print(f"⏸️ Signal SELL suppressed: profit {profit_pct*100:.2f}% "
                                  f"< min gate {MIN_PROFIT_PCT*100:.1f}%", flush=True)
                        else:
                            print(f"✅ Signal SELL executing: profit {profit_pct*100:.2f}% "
                                  f">= min gate {MIN_PROFIT_PCT*100:.1f}%", flush=True)
                            portfolio = _execute_sell(
                                portfolio, current_price, symbol, session, 'SIGNAL',
                                bullish_ob, bearish_ob, current_rsi)
                    else:
                        # No entry price on record — execute normally
                        portfolio = _execute_sell(
                            portfolio, current_price, symbol, session, 'SIGNAL',
                            bullish_ob, bearish_ob, current_rsi)
            else:
                print(f"Duplicate {decision} signal \u2014 Telegram alert suppressed.", flush=True)

        # ──────────────────────────────────────────────────────────
        #  UPDATE TRAILING STOP WATERMARK (if still in position)
        # ──────────────────────────────────────────────────────────
        if not risk_exit_triggered and decision == 'WAIT':
            portfolio = load_portfolio(session, symbol)
            in_position = portfolio['paxg_balance'] is not None and portfolio['paxg_balance'] > 0

            if in_position:
                old_highest = portfolio.get('highest_price_since_entry')
                cp = float(current_price)
                if old_highest is None or cp > float(old_highest):
                    portfolio['highest_price_since_entry'] = cp
                    old_val = f"${float(old_highest):.2f}" if old_highest else "$0.00"
                    print(f"📈 New high watermark: ${cp:.2f} (was {old_val})", flush=True)
                    _save_tracking_update(portfolio, current_price, symbol, session)

        # 6. Print summary output
        print("=== Quant Analyzer Summary ===", flush=True)
        print(f"Symbol: {symbol} | Timeframe: {timeframe}", flush=True)
        print(f"Current Price: {current_price:.2f}", flush=True)
        print(f"Current RSI (14): {current_rsi:.2f}", flush=True)
        
        print("\n--- Detected Order Blocks (15-period lookback) ---", flush=True)
        if bullish_ob:
            print(f"Bullish OB: {bullish_ob['low']:.2f} - {bullish_ob['high']:.2f} (from {bullish_ob['timestamp']})", flush=True)
        else:
            print("Bullish OB: Not found", flush=True)
            
        if bearish_ob:
            print(f"Bearish OB: {bearish_ob['low']:.2f} - {bearish_ob['high']:.2f} (from {bearish_ob['timestamp']})", flush=True)
        else:
            print("Bearish OB: Not found", flush=True)

        # Risk management status
        portfolio = load_portfolio(session, symbol)
        in_position = portfolio['paxg_balance'] is not None and portfolio['paxg_balance'] > 0
        if in_position and portfolio.get('average_entry_price'):
            ep = float(portfolio['average_entry_price'])
            cp = float(current_price)
            hp = float(portfolio['highest_price_since_entry']) if portfolio.get('highest_price_since_entry') else cp
            unrealized = ((cp - ep) / ep) * 100
            trailing_status = "ACTIVE" if (cp - ep) / ep >= TRAILING_ACTIVATE_PCT else "INACTIVE"
            print(f"\n--- Risk Management ---", flush=True)
            print(f"Entry: ${ep:.2f} | Unrealized: {unrealized:+.2f}%", flush=True)
            print(f"Stop-Loss @ ${ep * (1 - HARD_STOP_LOSS_PCT):.2f} | "
                  f"Trailing: {trailing_status} (Peak: ${hp:.2f})", flush=True)

        if risk_exit_triggered:
            print(f"\n⚠️ Risk exit was triggered this cycle.", flush=True)
        else:
            print(f"\nDecision {decision} saved to database successfully.", flush=True)

    except Exception as e:
        session.rollback()
        print(f"Error during analysis: {e}", flush=True)
        traceback.print_exc()
    finally:
        session.close()
        print("--- Analyzer Completed ---", flush=True)

if __name__ == "__main__":
    run_analyzer()
