import pandas as pd
import joblib

def run_ai_backtest():
    print("--- Starting AI Trading Backtest ---")
    
    # 1. Load the trained model
    model_path = 'models/quant_rf_model.pkl'
    try:
        model = joblib.load(model_path)
        print(f"Loaded model from {model_path}")
    except FileNotFoundError:
        print(f"Model file {model_path} not found. Run model_trainer.py first.")
        return
        
    # 2. Load the dataset
    data_path = 'data/processed/BTCUSDT_features.csv'
    try:
        df = pd.read_csv(data_path)
        print(f"Loaded dataset from {data_path} with {len(df):,} rows.")
    except FileNotFoundError:
        print(f"Dataset {data_path} not found. Ensure feature engineering is complete.")
        return
        
    # 3. Define Features (X) matrix exactly as trained
    feature_cols = [
        'open', 'high', 'low', 'close', 'volume',
        'RSI_14', 'MACD', 'MACD_signal', 'BB_high', 'BB_low',
        'ATR_14', 'SMA_50', 'SMA_200'
    ]
    
    X = df[feature_cols]
    
    # 4. Generate AI Predictions for the entire dataset
    print("Generating AI trading signals...")
    df['AI_Signal'] = model.predict(X)
    
    # 5. Initialize Trading Variables
    initial_capital = 10000.0
    capital = initial_capital
    in_position = False
    entry_price = 0.0
    position_size = 0.0
    trades = 0
    winning_trades = 0
    losing_trades = 0
    trading_fee = 0.001  # 0.1% Binance Spot fee
    candles_held = 0     # Track duration of current trade
    
    # Risk Management Parameters
    take_profit_pct = 0.02  # 2% Take Profit
    stop_loss_pct = 0.01    # 1% Stop Loss
    
    print("Simulating trading execution through historical data...")
    
    # 6. Simulation Loop using fast iterrows/itertuples
    for row in df.itertuples(index=False):
        current_price = row.close
        ai_signal = row.AI_Signal
        
        # BUY Logic
        if ai_signal == 1 and not in_position:
            # Deduct the 0.1% buy fee from capital before calculating position size
            capital_after_fee = capital * (1 - trading_fee)
            position_size = capital_after_fee / current_price
            capital = 0.0
            
            entry_price = current_price
            in_position = True
            candles_held = 0  # Initialize candle counter
            
        # SELL Logic
        elif in_position:
            candles_held += 1  # Increment candle counter
            
            # Check Exit Conditions: Take Profit, Stop Loss, OR Time Exit (4 periods)
            hit_tp = current_price >= entry_price * (1 + take_profit_pct)
            hit_sl = current_price <= entry_price * (1 - stop_loss_pct)
            hit_time = candles_held >= 4
            
            if hit_tp or hit_sl or hit_time:
                # Calculate gross revenue from selling the asset, then deduct the 0.1% sell fee
                gross_revenue = position_size * current_price
                capital = gross_revenue * (1 - trading_fee)
                position_size = 0.0
                
                # Calculate Win/Loss statistics
                # Note: A profitable trade requires covering both the entry and exit fees
                if current_price > entry_price:
                    winning_trades += 1
                else:
                    losing_trades += 1
                    
                trades += 1
                in_position = False
                entry_price = 0.0
                candles_held = 0
            
    # Close out any remaining open position on the very last candle
    if in_position:
        final_price = df.iloc[-1]['close']
        gross_revenue = position_size * final_price
        capital = gross_revenue * (1 - trading_fee)
        
        if final_price > entry_price:
            winning_trades += 1
        else:
            losing_trades += 1
        trades += 1
        
    # 7. Print AI Backtest Report
    roi = ((capital - initial_capital) / initial_capital) * 100
    win_rate = (winning_trades / trades * 100) if trades > 0 else 0.0
    
    print("\n" + "="*40)
    print("           AI BACKTEST REPORT")
    print("="*40)
    print(f"Asset Tested:    BTCUSDT")
    print(f"Initial Capital: ${initial_capital:,.2f}")
    print(f"Final Capital:   ${capital:,.2f}")
    print(f"Total ROI:       {roi:,.2f}%")
    print(f"Total Trades:    {trades}")
    print(f"Winning Trades:  {winning_trades}")
    print(f"Losing Trades:   {losing_trades}")
    print(f"Win Rate:        {win_rate:,.2f}%")
    print("="*40 + "\n")

if __name__ == "__main__":
    run_ai_backtest()
