import os
import sys
import traceback
import pandas as pd

# Add current directory to path if needed
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import init_db, init_shared_db, get_active_symbols
from futures_executor import create_futures_client, get_futures_balance
from data_fetcher import run_fetcher, fetch_binance_klines
from analyzer import run_analyzer
from config import TIMEFRAME

def run_diagnostics():
    print("=" * 70)
    print("🔍 RUNNING MANUAL E2E DIAGNOSTICS (Single Iteration)")
    print("=" * 70)
    
    # 1. DB Status & Active Symbols
    print("\n[1] Testing Database Connection & Active Symbols...")
    try:
        init_db()
        init_shared_db()
        symbols = get_active_symbols()
        print(f"  ✅ DB Connection: SUCCESS")
        print(f"  ✅ Active Symbols Fetched: {len(symbols)}")
        print(f"     Symbols: {symbols}")
    except Exception as e:
        print(f"  ❌ DB Connection FAILED: {e}")
        traceback.print_exc()
        return

    if not symbols:
        print("  ❌ No active symbols found. Ensure the scanner has populated the database.")
        return

    # 2. Binance API Status
    print(f"\n[2] Testing Binance API Connection (Testnet)...")
    client = None
    try:
        client = create_futures_client()
        balance = get_futures_balance(client)
        print(f"  ✅ API Keys Valid & Authenticated.")
        print(f"  ✅ Wallet Balance: ${balance:.2f} USDT")
    except Exception as e:
        print(f"  ❌ API Connection FAILED (Check keys/permissions): {e}")
        traceback.print_exc()
        # Continuing anyway so we can test virtual execution
        print("  ⚠️ Proceeding in VIRTUAL mode without API keys.")

    # 3. Data Fetching Validation
    first_symbol = symbols[0]
    print(f"\n[3] Testing Data Fetcher for {first_symbol} (Timeframe: {TIMEFRAME})...")
    try:
        df = fetch_binance_klines(symbol=first_symbol, interval=TIMEFRAME, limit=250)
        print(f"  ✅ Data Fetch: SUCCESS")
        print(f"  ✅ Fetched {len(df)} candles for {first_symbol}.")
        if len(df) > 0:
            print(f"     Latest Candle: {df.iloc[-1]['timestamp']} | Close: ${df.iloc[-1]['close']:,.2f}")
    except Exception as e:
        print(f"  ❌ Data Fetching FAILED for {first_symbol}: {e}")
        traceback.print_exc()

    # Run the actual fetcher logic to ensure data is saved to DB for the analyzer
    print(f"  → Running full run_fetcher() to populate DB...")
    try:
        run_fetcher(symbols=[first_symbol], interval=TIMEFRAME, limit=250)
    except Exception as e:
        print(f"  ❌ run_fetcher() FAILED: {e}")
        traceback.print_exc()

    # 4. Analyzer Validation
    print(f"\n[4] Testing Execution Analyzer for {first_symbol}...")
    try:
        # Run exactly one iteration for the first symbol
        run_analyzer(symbol=first_symbol, timeframe=TIMEFRAME, futures_client=client)
        print(f"\n  ✅ Analyzer Execution Completed (Check debug output above).")
    except Exception as e:
        print(f"\n  ❌ Analyzer Execution FAILED with Exception: {e}")
        traceback.print_exc()

    print("\n" + "=" * 70)
    print("🏁 DIAGNOSTICS FINISHED")
    print("=" * 70)

if __name__ == "__main__":
    run_diagnostics()
