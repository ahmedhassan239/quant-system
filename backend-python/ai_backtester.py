import pandas as pd
import joblib
import numpy as np

def run_ai_backtest():
    print("--- Starting AI Trading Backtest ---", flush=True)
    
    # 1. Load the trained model
    model_path = 'models/quant_rf_model.pkl'
    try:
        model = joblib.load(model_path)
        print(f"Loaded model from {model_path}", flush=True)
    except FileNotFoundError:
        print(f"Model file {model_path} not found. Run model_trainer.py first.", flush=True)
        return
        
    # 2. Load the dataset
    data_path = 'data/processed/PAXGUSDT_features.csv'
    try:
        df = pd.read_csv(data_path)
        print(f"Loaded dataset from {data_path} with {len(df):,} rows.", flush=True)
    except FileNotFoundError:
        print(f"Dataset {data_path} not found. Ensure feature engineering is complete.", flush=True)
        return
        
    # 3. Define Features (X) matrix exactly as trained
    feature_cols = [
        'open', 'high', 'low', 'close', 'volume',
        'RSI_14', 'MACD', 'MACD_signal', 'BB_high', 'BB_low',
        'ATR_14', 'SMA_50', 'SMA_200'
    ]
    
    X = df[feature_cols]
    
    # 4. Generate AI Confidence Scores for the entire dataset
    print("Generating AI trading signals with confidence filtering...", flush=True)
    buy_confidence = model.predict_proba(X)[:, 1]
    df['Buy_Confidence'] = buy_confidence
    
    # Confidence threshold: only enter trades when model is >= 65% confident
    confidence_threshold = 0.65
    
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
    highest_price = 0.0  # Track highest price for trailing stop
    
    # Risk Management Parameters
    trailing_stop_pct = 0.015  # 1.5% Trailing distance
    stop_loss_pct = 0.01       # 1% Stop Loss
    
    print("Simulating trading execution through historical data...", flush=True)
    
    # 6. Simulation Loop using fast itertuples
    for row in df.itertuples(index=False):
        current_price = row.close
        confidence = row.Buy_Confidence
        
        # BUY Logic: Only enter if confidence >= threshold
        if confidence >= confidence_threshold and not in_position:
            # Deduct the 0.1% buy fee from capital before calculating position size
            capital_after_fee = capital * (1 - trading_fee)
            position_size = capital_after_fee / current_price
            capital = 0.0
            
            entry_price = current_price
            in_position = True
            highest_price = current_price  # Set initial highest price
            
        # SELL Logic: Only Trailing Stop and Hard Stop Loss
        elif in_position:
            highest_price = max(highest_price, current_price)
            
            # Check Exit Conditions: Trailing Stop OR Hard Stop Loss
            hit_ts = current_price <= highest_price * (1 - trailing_stop_pct)
            hit_sl = current_price <= entry_price * (1 - stop_loss_pct)
            
            if hit_ts or hit_sl:
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
                highest_price = 0.0
            
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
    
    print("\n" + "="*40, flush=True)
    print("           AI BACKTEST REPORT", flush=True)
    print("="*40, flush=True)
    print(f"Asset Tested:    PAXGUSDT", flush=True)
    print(f"Initial Capital: ${initial_capital:,.2f}", flush=True)
    print(f"Final Capital:   ${capital:,.2f}", flush=True)
    print(f"Total ROI:       {roi:,.2f}%", flush=True)
    print(f"Total Trades:    {trades}", flush=True)
    print(f"Winning Trades:  {winning_trades}", flush=True)
    print(f"Losing Trades:   {losing_trades}", flush=True)
    print(f"Win Rate:        {win_rate:,.2f}%", flush=True)
    print("="*40 + "\n", flush=True)

if __name__ == "__main__":
    run_ai_backtest()
