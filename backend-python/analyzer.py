import os
import requests
import pandas as pd
from datetime import datetime
from database import SessionLocal, MarketData, TradingSignal, PortfolioState, engine, init_db

DEFAULT_USDT_BALANCE = 1000.0


def load_portfolio(session, symbol):
    """
    Load the latest portfolio state from the database.
    Returns a dict with usdt_balance, paxg_balance, last_buy_price.
    """
    last_state = session.query(PortfolioState).filter(
        PortfolioState.symbol == symbol
    ).order_by(PortfolioState.id.desc()).first()

    if last_state:
        return {
            'usdt_balance': last_state.usdt_balance,
            'paxg_balance': last_state.paxg_balance,
            'last_buy_price': last_state.last_buy_price
        }
    # First run — return defaults
    return {
        'usdt_balance': DEFAULT_USDT_BALANCE,
        'paxg_balance': 0.0,
        'last_buy_price': None
    }


def send_telegram_alert(message):
    """
    Send a notification message to Telegram.
    Fails silently to avoid crashing the trading bot.
    """
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')

    if not token or not chat_id:
        print("Telegram credentials not configured. Skipping alert.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("Telegram alert sent successfully.")
        else:
            print(f"Telegram API error: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to send Telegram alert: {e}")

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

def run_analyzer(symbol='PAXGUSDT', timeframe='15m'):
    """
    Query market data, calculate RSI, detect Order Blocks, and save trading decisions.
    """
    print("--- Analyzer Started ---")
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
            print("No data found in the database.")
            return

        # 1. Calculate 14-period RSI
        df = calculate_rsi(df, period=14)
        
        # 2. Detect Order Blocks
        bullish_ob, bearish_ob = detect_order_blocks(df, lookback=15)
        
        # 3. Decision Logic
        last_row = df.iloc[-1]
        current_price = last_row['close']
        current_rsi = last_row['RSI']
        
        decision = 'WAIT'
        
        if bullish_ob and current_price <= bullish_ob['high'] and current_rsi < 30:
            decision = 'BUY'
            
        if bearish_ob and current_price >= bearish_ob['low'] and current_rsi > 70:
            decision = 'SELL'
            
        # 4. Save to Database
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
            decision=decision
        )
        
        session.add(signal)
        session.commit()

        # 4b. Signal State Management + Virtual Portfolio + Telegram Alert
        if decision in ('BUY', 'SELL'):
            # Query the last non-WAIT decision from the database
            last_signal = session.query(TradingSignal).filter(
                TradingSignal.symbol == symbol,
                TradingSignal.timeframe == timeframe,
                TradingSignal.decision.in_(['BUY', 'SELL']),
                TradingSignal.id != signal.id  # Exclude the one we just inserted
            ).order_by(TradingSignal.id.desc()).first()

            last_decision = last_signal.decision if last_signal else None

            if decision != last_decision:
                # --- Update Virtual Portfolio ---
                portfolio = load_portfolio(session, symbol)
                pnl_pct_val = None
                pnl_usd_val = None
                pnl_section = ""

                if decision == 'BUY' and portfolio['usdt_balance'] > 0:
                    # Convert all USDT to PAXG
                    paxg_bought = portfolio['usdt_balance'] / current_price
                    portfolio['paxg_balance'] = round(paxg_bought, 6)
                    portfolio['last_buy_price'] = float(current_price)
                    portfolio['usdt_balance'] = 0.0

                elif decision == 'SELL' and portfolio['paxg_balance'] > 0:
                    # Convert all PAXG back to USDT
                    sell_value = portfolio['paxg_balance'] * current_price
                    buy_price = portfolio.get('last_buy_price')
                    if buy_price and buy_price > 0:
                        pnl_pct_val = ((current_price - buy_price) / buy_price) * 100
                        pnl_usd_val = sell_value - (portfolio['paxg_balance'] * buy_price)
                        sign = "+" if pnl_pct_val >= 0 else ""
                        pnl_section = f"\n- PnL (This Trade): {sign}{pnl_pct_val:.2f}% ({sign}${pnl_usd_val:.2f})"
                    portfolio['usdt_balance'] = round(sell_value, 2)
                    portfolio['paxg_balance'] = 0.0
                    portfolio['last_buy_price'] = None

                # Calculate total portfolio value at current price
                total_value = portfolio['usdt_balance'] + (portfolio['paxg_balance'] * current_price)

                # Save portfolio snapshot to database
                portfolio_record = PortfolioState(
                    timestamp=datetime.now(),
                    symbol=symbol,
                    decision=decision,
                    current_price=float(current_price),
                    usdt_balance=portfolio['usdt_balance'],
                    paxg_balance=portfolio['paxg_balance'],
                    last_buy_price=portfolio['last_buy_price'],
                    pnl_pct=pnl_pct_val,
                    pnl_usd=pnl_usd_val,
                    total_portfolio_value=round(total_value, 2)
                )
                session.add(portfolio_record)
                session.commit()

                # --- Build Reason Section ---
                if decision == 'BUY':
                    rsi_label = "Oversold"
                    ob_type = "Bullish"
                    ob_low = bullish_ob['low'] if bullish_ob else 0
                    ob_high = bullish_ob['high'] if bullish_ob else 0
                else:
                    rsi_label = "Overbought"
                    ob_type = "Bearish"
                    ob_low = bearish_ob['low'] if bearish_ob else 0
                    ob_high = bearish_ob['high'] if bearish_ob else 0

                alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')

                alert_msg = (
                    f"\U0001f6a8 *QUANT ALERT: {decision}* \U0001f6a8\n"
                    f"\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Price:* ${current_price:.2f}\n"
                    f"*Time:* {alert_time}\n"
                    f"\n"
                    f"\U0001f4a1 *Why this decision?*\n"
                    f"- RSI is at {current_rsi:.1f} (Indicates {rsi_label}).\n"
                    f"- Price entered {ob_type} Order Block between ${ob_low:.2f} and ${ob_high:.2f}.\n"
                    f"\n"
                    f"\U0001f4bc *Virtual Portfolio:*{pnl_section}\n"
                    f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
                    f"- PAXG Balance: {portfolio['paxg_balance']:.6f} PAXG\n"
                    f"- Total Value: ${total_value:.2f}"
                )
                send_telegram_alert(alert_msg)
            else:
                print(f"Duplicate {decision} signal \u2014 Telegram alert suppressed.")

        # 5. Print summary output
        print("=== Quant Analyzer Summary ===")
        print(f"Symbol: {symbol} | Timeframe: {timeframe}")
        print(f"Current Price: {current_price:.2f}")
        print(f"Current RSI (14): {current_rsi:.2f}")
        
        print("\n--- Detected Order Blocks (15-period lookback) ---")
        if bullish_ob:
            print(f"Bullish OB: {bullish_ob['low']:.2f} - {bullish_ob['high']:.2f} (from {bullish_ob['timestamp']})")
        else:
            print("Bullish OB: Not found")
            
        if bearish_ob:
            print(f"Bearish OB: {bearish_ob['low']:.2f} - {bearish_ob['high']:.2f} (from {bearish_ob['timestamp']})")
        else:
            print("Bearish OB: Not found")
            
        print(f"\nDecision {decision} saved to database successfully.")

    except Exception as e:
        session.rollback()
        print(f"Error during analysis: {e}")
    finally:
        session.close()
        print("--- Analyzer Completed ---")

if __name__ == "__main__":
    run_analyzer()
