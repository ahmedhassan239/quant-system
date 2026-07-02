import requests
import pandas as pd
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from database import SessionLocal, MarketData, engine, init_db

def fetch_binance_klines(symbol='BTCUSDT', interval='15m', limit=100):
    """
    Fetch klines/candlestick data from Binance Public API.
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    
    df = pd.DataFrame(data, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume', 
        'close_time', 'quote_asset_volume', 'number_of_trades', 
        'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
    ])
    
    # Keep only the necessary columns
    df = df[['open_time', 'open', 'high', 'low', 'close', 'volume']]
    
    # Convert timestamps to datetime objects
    df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
    
    # Convert string values to floats
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
        
    df['symbol'] = symbol
    df['timeframe'] = interval
    
    return df

def save_to_db(df):
    """
    Save DataFrame records to the database using an upsert strategy.
    """
    session = SessionLocal()
    try:
        # Prepare records for insertion
        records = df.to_dict(orient='records')
        
        # We don't need 'open_time' for the database insertion
        for record in records:
            record.pop('open_time', None)
            
        # Prepare the insert statement
        stmt = insert(MarketData).values(records)
        
        # Define the upsert strategy (ignore duplicates on constraint)
        on_conflict_stmt = stmt.on_conflict_do_nothing(
            index_elements=['symbol', 'timeframe', 'timestamp']
        )
        
        # Execute the statement
        session.execute(on_conflict_stmt)
        session.commit()
        print(f"Successfully processed {len(records)} records.")
    except Exception as e:
        session.rollback()
        print(f"Error saving to database: {e}")
    finally:
        session.close()

def run_fetcher():
    """
    Core execution logic for the data fetcher.
    """
    print("--- Fetcher Started ---")
    # Ensure tables exist before trying to save
    init_db()
    
    print("Fetching data from Binance...")
    df = fetch_binance_klines(symbol='BTCUSDT', interval='15m', limit=100)
    
    print("Saving data to database...")
    save_to_db(df)
    print("--- Fetcher Completed ---")

if __name__ == "__main__":
    run_fetcher()
