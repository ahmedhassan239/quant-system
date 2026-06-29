import os
import requests
import pandas as pd
import time
from datetime import datetime, timedelta

def fetch_5_years_data():
    # Expanded diverse list of assets for the ML dataset
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT', 'SUIUSDT', 'HBARUSDT', 'ONTUSDT', 'POLUSDT', 'ARBUSDT', 'LINEAUSDT', 'PUMPUSDT', 'XPLUSDT']
    
    # Calculate timestamps (5 years ago to now)
    end_time_dt = datetime.utcnow()
    start_time_dt = end_time_dt - timedelta(days=5*365)
    
    # Binance API uses milliseconds for timestamps
    global_start_time = int(start_time_dt.timestamp() * 1000)
    global_end_time = int(end_time_dt.timestamp() * 1000)
    
    url = "https://api.binance.com/api/v3/klines"
    
    for symbol in symbols:
        print(f"\n--- Starting data extraction for {symbol} ---")
        
        all_data = []
        current_start = global_start_time
        
        # Robust error handling for the entire symbol extraction process
        try:
            while current_start < global_end_time:
                params = {
                    'symbol': symbol,
                    'interval': '15m',
                    'limit': 1000,
                    'startTime': current_start,
                    'endTime': global_end_time
                }
                
                response = requests.get(url, params=params)
                
                # If the symbol is invalid or another HTTP error occurs, raise it here
                response.raise_for_status()
                data = response.json()
                
                if not data:
                    print(f"No more data returned for {symbol}. Finishing pagination.")
                    break
                    
                all_data.extend(data)
                
                # Update current_start to the close_time of the last candle + 1ms to fetch the next batch
                last_candle_close = data[-1][6]
                current_start = last_candle_close + 1
                
                # STRICT RATE LIMITING: Pause between individual pagination requests
                time.sleep(0.5)
                
        except Exception as e:
            print(f"WARNING: Skipping {symbol} due to error (e.g. newly listed or unavailable): {e}")
            # Ensure we pause before moving to the next symbol even if it failed
            print("Pausing for 5 seconds before moving to the next symbol to respect rate limits...")
            time.sleep(5)
            continue
            
        if not all_data:
            print(f"Failed to extract any data for {symbol}. Skipping.")
            continue
            
        print(f"Finished extracting {len(all_data)} candles for {symbol}. Processing DataFrame...")
        
        # Load into Pandas DataFrame
        df = pd.DataFrame(all_data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume', 
            'close_time', 'quote_asset_volume', 'number_of_trades', 
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        
        # Keep only essential columns and convert timestamps
        df = df[['open_time', 'open', 'high', 'low', 'close', 'volume']]
        df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
        
        # Ensure numerical columns are floats
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        # Reorder columns for final output
        df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
        
        # Save to CSV
        os.makedirs('data/raw', exist_ok=True)
        csv_filename = f"data/raw/{symbol}_5years_15m.csv"
        df.to_csv(csv_filename, index=False)
        print(f"Saved {csv_filename} successfully.")
        
        # STRICT RATE LIMITING: Pause between different symbols to ensure IP safety
        print("Pausing for 5 seconds before moving to the next symbol to respect rate limits...")
        time.sleep(5)
        
    print("\n--- All historical data extraction completed! ---")

if __name__ == "__main__":
    fetch_5_years_data()
