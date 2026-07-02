import os
import glob
import pandas as pd
import ta

def engineer_features():
    print("--- Starting Feature Engineering Pipeline ---")
    
    # Use glob to find all files ending with _5years_15m.csv in the data/raw directory
    file_pattern = "data/raw/*_5years_15m.csv"
    csv_files = glob.glob(file_pattern)
    
    if not csv_files:
        print(f"No files found matching the pattern '{file_pattern}'.")
        print("Please ensure the data scraper has finished running and the files exist.")
        return
        
    print(f"Found {len(csv_files)} raw data files to process.\n")
    
    for file in csv_files:
        # Extract symbol name (e.g., BTCUSDT from BTCUSDT_5years_15m.csv)
        filename_base = os.path.basename(file)
        symbol = filename_base.split('_')[0]
        
        print(f"Processing {symbol}...")
        
        # Load the CSV into a DataFrame
        df = pd.read_csv(file)
        
        # Ensure timestamp is sorted chronologically
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df.sort_values(by='timestamp').reset_index(drop=True)
            
        initial_rows = len(df)
        cols_before = len(df.columns)
        
        # 1. RSI (14)
        df['RSI_14'] = ta.momentum.RSIIndicator(close=df['close'], window=14).rsi()
        
        # 2. MACD (standard: fast=12, slow=26, signal=9)
        macd_indicator = ta.trend.MACD(close=df['close'])
        df['MACD'] = macd_indicator.macd()
        df['MACD_signal'] = macd_indicator.macd_signal()
            
        # 3. Bollinger Bands (20)
        bb_indicator = ta.volatility.BollingerBands(close=df['close'], window=20)
        df['BB_high'] = bb_indicator.bollinger_hband()
        df['BB_low'] = bb_indicator.bollinger_lband()
            
        # 4. ATR (14)
        df['ATR_14'] = ta.volatility.AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
        
        # 5. SMA (50)
        df['SMA_50'] = df['close'].rolling(window=50).mean()
        
        # 6. SMA (200)
        df['SMA_200'] = df['close'].rolling(window=200).mean()
        
        # 7. Create Target Variable: Target_1h_Return
        # Percentage change of the 'close' price shifted 4 periods into the future
        # (future close - current close) / current close
        df['Target_1h_Return'] = (df['close'].shift(-4) - df['close']) / df['close']
        
        # Calculate how many features we added
        cols_after = len(df.columns)
        features_generated = cols_after - cols_before
        
        # Drop all rows containing NaN values 
        # (This cleans moving average warmups at the top and future shifts at the bottom)
        df = df.dropna().reset_index(drop=True)
        
        final_rows = len(df)
        dropped_rows = initial_rows - final_rows
        
        # Save the cleaned, enriched DataFrame
        os.makedirs('data/processed', exist_ok=True)
        output_filename = f"data/processed/{symbol}_features.csv"
        df.to_csv(output_filename, index=False)
        
        print(f"  -> Generated {features_generated} features.")
        print(f"  -> Dropped {dropped_rows} rows with NaN values.")
        print(f"  -> Saved {output_filename} successfully ({final_rows} rows remaining).\n")

    print("--- Feature Engineering Pipeline Completed Successfully! ---")

if __name__ == "__main__":
    engineer_features()
