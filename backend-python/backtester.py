import requests
import pandas as pd

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
    min_idx = recent_df['low'].idxmin()
    min_loc = df.index.get_loc(min_idx)
    
    bullish_ob = None
    for i in range(min_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] < row['open']:
            bullish_ob = {
                'high': row['high'],
                'low': row['low']
            }
            break

    # -- Bearish OB --
    max_idx = recent_df['high'].idxmax()
    max_loc = df.index.get_loc(max_idx)
    
    bearish_ob = None
    for i in range(max_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] > row['open']:
            bearish_ob = {
                'high': row['high'],
                'low': row['low']
            }
            break

    return bullish_ob, bearish_ob

def run_backtest():
    print("Fetching historical data from Binance...")
    url = "https://api.binance.com/api/v3/klines"
    params = {'symbol': 'BTCUSDT', 'interval': '15m', 'limit': 1000}
    
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    
    # Load into Pandas DataFrame
    df = pd.DataFrame(data, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume', 
        'close_time', 'quote_asset_volume', 'number_of_trades', 
        'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
    ])
    
    df = df[['open_time', 'open', 'high', 'low', 'close', 'volume']]
    df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
    
    # Ensure numerical columns are floats
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
        
    print(f"Loaded {len(df)} candles. Starting backtest simulation...")
    
    # Initial Portfolio Setup
    initial_balance = 10000.0
    balance = initial_balance
    position_size = 0.0
    entry_price = 0.0
    
    winning_trades = 0
    losing_trades = 0
    total_trades = 0
    
    # Loop through the DataFrame starting from index 15
    for i in range(15, len(df)):
        # Slice previous 15 candles + current candle (16 rows total)
        slice_df = df.iloc[i-15:i+1].copy()
        slice_df.reset_index(drop=True, inplace=True)
        
        # Re-implement RSI and OB calculation logic
        slice_df = calculate_rsi(slice_df, period=14)
        bullish_ob, bearish_ob = detect_order_blocks(slice_df, lookback=15)
        
        current_row = slice_df.iloc[-1]
        current_price = current_row['close']
        current_rsi = current_row['RSI']
        
        # BUY CONDITION
        if position_size == 0:
            if pd.notna(current_rsi) and current_rsi < 30 and bullish_ob and current_price <= bullish_ob['high']:
                # Buy using full balance
                position_size = balance / current_price
                entry_price = current_price
                balance = 0.0
                
        # SELL CONDITION
        elif position_size > 0:
            tp_condition = (bearish_ob is not None and current_price >= bearish_ob['low']) or (pd.notna(current_rsi) and current_rsi > 70)
            sl_condition = (bullish_ob is not None and current_price < bullish_ob['low'])
            
            if tp_condition or sl_condition:
                # Sell all position
                balance = position_size * current_price
                total_trades += 1
                
                if current_price > entry_price:
                    winning_trades += 1
                else:
                    losing_trades += 1
                    
                position_size = 0.0
                entry_price = 0.0
                
    # Close out any remaining position at the final price to calculate final balance
    if position_size > 0:
        final_price = df.iloc[-1]['close']
        balance = position_size * final_price
        total_trades += 1
        if final_price > entry_price:
            winning_trades += 1
        else:
            losing_trades += 1
            
    # Calculate performance metrics
    roi = ((balance - initial_balance) / initial_balance) * 100
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

    # Formatted Backtest Report
    print("\n" + "="*40)
    print("           BACKTEST REPORT")
    print("="*40)
    print(f"Initial Capital: ${initial_balance:,.2f}")
    print(f"Final Capital:   ${balance:,.2f}")
    print(f"Total ROI:       {roi:,.2f}%")
    print(f"Total Trades:    {total_trades}")
    print(f"Winning Trades:  {winning_trades}")
    print(f"Losing Trades:   {losing_trades}")
    print(f"Win Rate:        {win_rate:,.2f}%")
    print("="*40)

if __name__ == "__main__":
    run_backtest()
