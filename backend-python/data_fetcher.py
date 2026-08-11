import time
import requests
import pandas as pd
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from database import SessionLocal, MarketData, engine, init_db
from config import BINANCE_FUTURES_BASE_URL, TIMEFRAME, STABLECOIN_BLACKLIST, MOCK_TOKENS_BLACKLIST

# Global in-memory blacklist set for unsupported/corrupted testnet symbols
BLACKLISTED_SYMBOLS: set[str] = set()

def fetch_binance_klines(symbol='BTCUSDT', interval=TIMEFRAME, limit=100):
    """
    Fetch klines/candlestick data from the Binance Futures API.
    Uses /fapi/v1/klines for Futures Testnet.
    URL and interval are driven by environment variables via config.py.
    """
    if symbol in BLACKLISTED_SYMBOLS:
        print(f"⚠️ [{symbol}] is in auto-blacklist. Skipping API request.", flush=True)
        return None

    url = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code in (418, 429) or (response.status_code >= 400 and '-1003' in response.text):
            print(f"⚠️ Rate limit hit (-1003/418/429) on {symbol}. Pausing Fetcher for 60 seconds...", flush=True)
            time.sleep(60)
            raise Exception("Rate limit hit")

        if response.status_code >= 400:
            if symbol not in BLACKLISTED_SYMBOLS:
                BLACKLISTED_SYMBOLS.add(symbol)
                print(f"🚫 [{symbol}] added to auto-blacklist due to API error (HTTP {response.status_code}).", flush=True)
            response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (418, 429):
            print(f"⚠️ Rate limit hit (HTTP {e.response.status_code}) on {symbol}. Pausing Fetcher for 60 seconds...", flush=True)
            time.sleep(60)
            raise Exception("Rate limit hit")
        if e.response is not None and e.response.status_code >= 400:
            if symbol not in BLACKLISTED_SYMBOLS:
                BLACKLISTED_SYMBOLS.add(symbol)
                print(f"🚫 [{symbol}] added to auto-blacklist due to API error.", flush=True)
        raise e

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

from concurrent.futures import ThreadPoolExecutor, as_completed

def _fetch_and_save_symbol(sym, interval, limit):
    """Helper worker to fetch and persist candles for a single symbol."""
    try:
        df = fetch_binance_klines(symbol=sym, interval=interval, limit=limit)
        if df is None:
            return sym, False, "Blacklisted symbol"
        save_to_db(df)
        return sym, True, None
    except Exception as e:
        return sym, False, str(e)

def run_fetcher(symbols=None, interval=TIMEFRAME, limit=250, chunk_size=10):
    """
    Core execution logic for the data fetcher.
    Fetches Futures candle data for each symbol in the list in concurrent chunks.
    Defaults to BTCUSDT if no symbols provided.
    Strictly forbids fetching candles for mock tokens or auto-blacklisted symbols.
    Uses limit=250 to ensure SMA 200 has enough warmup data.
    """
    if symbols is None:
        symbols = ['BTCUSDT']
    else:
        # Ensure under NO circumstances should the bot pull candles for mock tokens, stablecoins, or auto-blacklisted symbols
        symbols = [s for s in symbols if s not in MOCK_TOKENS_BLACKLIST and s not in STABLECOIN_BLACKLIST and s not in BLACKLISTED_SYMBOLS]

    print(f"--- Fetcher Started (Futures — {len(symbols)} symbols) ---", flush=True)
    # Ensure tables exist before trying to save
    init_db()

    # Split symbols into chunks of chunk_size (default 10) to stay safely within rate limits
    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]
    success_count = 0
    fail_count = 0

    rate_limit_hit = False

    for chunk_idx, chunk in enumerate(chunks):
        if rate_limit_hit:
            print("⚠️ Rate limit triggered in previous chunk. Exiting fetch cycle gracefully.", flush=True)
            break
            
        with ThreadPoolExecutor(max_workers=len(chunk)) as executor:
            futures = []
            for sym in chunk:
                futures.append(executor.submit(_fetch_and_save_symbol, sym, interval, limit))
                time.sleep(1.0)  # Increased stagger to 1.0s to avoid breaching Binance rate limits
            for future in as_completed(futures):
                sym, success, err = future.result()
                if success:
                    success_count += 1
                else:
                    fail_count += 1
                    print(f"  ⚠️ Failed to fetch {sym}: {err}", flush=True)
                    if err and "Rate limit hit" in err:
                        rate_limit_hit = True

        # Rate-limit safety: slight pause between chunks
        if chunk_idx < len(chunks) - 1 and not rate_limit_hit:
            time.sleep(0.5)

    print(f"--- Fetcher Completed ({success_count}/{len(symbols)} symbols processed successfully) ---", flush=True)

if __name__ == "__main__":
    run_fetcher()
