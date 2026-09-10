import time
import requests
import pandas as pd
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from database import SessionLocal, MarketData, engine, init_db
from config import BINANCE_FUTURES_BASE_URL, TIMEFRAME, STABLECOIN_BLACKLIST, MOCK_TOKENS_BLACKLIST

# Import centralized rate-limit state from futures_executor
try:
    from futures_executor import is_rate_limited, rate_limit_remaining_seconds, _register_rate_limit_ban
except ImportError:
    # Fallback if futures_executor is not available (standalone usage)
    def is_rate_limited(): return False
    def rate_limit_remaining_seconds(): return 0.0
    def _register_rate_limit_ban(e): pass

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
            # Sync with centralized rate-limit state
            from binance.exceptions import BinanceAPIException
            try:
                # Create a mock exception to register the ban
                class _MockBanErr:
                    code = -1003
                    message = response.text
                    def __str__(self): return self.message
                _register_rate_limit_ban(_MockBanErr())
            except Exception:
                pass
            sleep_secs = max(60, rate_limit_remaining_seconds())
            print(f"⚠️ Rate limit hit (-1003/418/429) on {symbol}. Pausing Fetcher for {sleep_secs:.0f} seconds...", flush=True)
            time.sleep(sleep_secs)
            raise Exception("Rate limit hit")

        if response.status_code >= 400:
            if symbol not in BLACKLISTED_SYMBOLS:
                BLACKLISTED_SYMBOLS.add(symbol)
                print(f"🚫 [{symbol}] added to auto-blacklist due to API error (HTTP {response.status_code}).", flush=True)
            response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (418, 429):
            sleep_secs = max(60, rate_limit_remaining_seconds())
            print(f"⚠️ Rate limit hit (HTTP {e.response.status_code}) on {symbol}. Pausing Fetcher for {sleep_secs:.0f} seconds...", flush=True)
            time.sleep(sleep_secs)
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

def run_fetcher(symbols=None, interval=TIMEFRAME, limit=250):
    """
    Core execution logic for the data fetcher.
    Fetches Futures candle data for each symbol SEQUENTIALLY with throttling.
    Defaults to BTCUSDT if no symbols provided.
    Strictly forbids fetching candles for mock tokens or auto-blacklisted symbols.
    Uses limit=250 to ensure SMA 200 has enough warmup data.

    ARCHITECTURE NOTE (post-ban fix):
      Previously used ThreadPoolExecutor(max_workers=10) which fired up to 10
      concurrent HTTP requests per chunk. This caused Binance -1003 bans.
      Now processes symbols sequentially with a 150ms inter-request gap via
      the centralized throttle in futures_executor.
    """
    if symbols is None:
        symbols = ['BTCUSDT']
    else:
        symbols = [s for s in symbols if s not in MOCK_TOKENS_BLACKLIST and s not in STABLECOIN_BLACKLIST and s not in BLACKLISTED_SYMBOLS]

    # ── Hard guard: skip entire fetch if IP is currently banned ──
    if is_rate_limited():
        remaining = rate_limit_remaining_seconds()
        print(f"🚫 Fetcher SKIPPED — rate-limit ban active ({remaining:.0f}s remaining). No API calls will be made.", flush=True)
        return

    print(f"--- Fetcher Started (Futures — {len(symbols)} symbols, sequential) ---", flush=True)
    init_db()

    success_count = 0
    fail_count = 0

    for sym in symbols:
        # Re-check ban before every symbol (another function may have triggered it)
        if is_rate_limited():
            print(f"🚫 Fetcher HALTED mid-cycle — rate-limit ban detected. Remaining symbols skipped.", flush=True)
            break

        try:
            time.sleep(0.15)  # 150ms throttle between kline requests
            df = fetch_binance_klines(symbol=sym, interval=interval, limit=limit)
            if df is None:
                fail_count += 1
                continue
            save_to_db(df)
            success_count += 1
        except Exception as e:
            fail_count += 1
            err_str = str(e)
            print(f"  ⚠️ Failed to fetch {sym}: {err_str}", flush=True)
            if "Rate limit hit" in err_str:
                print(f"🚫 Fetcher HALTED — rate-limit ban. Remaining symbols skipped.", flush=True)
                break

    print(f"--- Fetcher Completed ({success_count}/{len(symbols)} symbols processed successfully) ---", flush=True)

if __name__ == "__main__":
    run_fetcher()

