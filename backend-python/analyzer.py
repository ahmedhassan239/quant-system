import pandas as pd
from database import SessionLocal, MarketData, TradingSignal, engine, init_db

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
