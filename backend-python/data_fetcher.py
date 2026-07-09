import requests
import pandas as pd
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from database import SessionLocal, MarketData, engine, init_db
from config import BINANCE_FUTURES_BASE_URL, TIMEFRAME

def fetch_binance_klines(symbol='PAXGUSDT', interval=TIMEFRAME, limit=100):
    """
    Fetch klines/candlestick data from the Binance Futures API.
    Uses /fapi/v1/klines for Futures Testnet.
    URL and interval are driven by environment variables via config.py.
    """
    url = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    
    response = requests.get(url, params=params, timeout=10)
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
        print(f"Successfully processed {len(records)} records.", flush=True)
    except Exception as e:
        session.rollback()
        print(f"Error saving to database: {e}", flush=True)
    finally:
        session.close()

def run_fetcher(symbols=None, interval=TIMEFRAME, limit=250):
    """
    Core execution logic for the data fetcher.
    Fetches Futures candle data for each symbol in the list.
    Defaults to PAXGUSDT if no symbols provided.
    Uses limit=250 to ensure SMA 200 has enough warmup data.
    """
    if symbols is None:
        symbols = ['PAXGUSDT']

    print("--- Fetcher Started (Futures) ---", flush=True)
    # Ensure tables exist before trying to save
    init_db()

    for sym in symbols:
        print(f"Fetching {limit} x {interval} Futures candles for {sym}...", flush=True)
        try:
            df = fetch_binance_klines(symbol=sym, interval=interval, limit=limit)
            save_to_db(df)
        except Exception as e:
            print(f"  ⚠️ Failed to fetch {sym}: {e}", flush=True)

    print("--- Fetcher Completed (Futures) ---", flush=True)

if __name__ == "__main__":
    run_fetcher()
