import time
import schedule
from scanner import scan
from data_fetcher import run_fetcher
from analyzer import run_analyzer, send_telegram_alert
from config import (TIMEFRAME, ALERT_PREFIX, ALERT_EMOJI,
                    ENV_TYPE, SCHEDULE_INTERVAL_MINUTES, BINANCE_BASE_URL)
from database import SLOT_BUDGET, TOTAL_CAPITAL

def job():
    print("\n" + "="*60, flush=True)
    print(f"{ALERT_PREFIX} Running scheduled job at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("="*60, flush=True)
    
    # 1. Scan for tradeable symbols
    symbols = scan()
    
    # 2. Fetch latest candle data for all scanned symbols
    run_fetcher(symbols=symbols)
    
    # 3. Run the analyzer on each symbol
    for sym in symbols:
        run_analyzer(symbol=sym)
    
    print("\nJob completed. Sleeping until next interval...", flush=True)
    print("="*60 + "\n", flush=True)

def main():
    print(f"{ALERT_PREFIX} Quant Engine heartbeat started (Multi-Asset Mode)...", flush=True)
    print(f"  → ENV_TYPE: {ENV_TYPE}", flush=True)
    print(f"  → TIMEFRAME: {TIMEFRAME}", flush=True)
    print(f"  → API: {BINANCE_BASE_URL}", flush=True)
    print(f"  → Schedule: every {SCHEDULE_INTERVAL_MINUTES} minutes", flush=True)
    
    # Run the job immediately once on startup
    job()
    
    # Schedule the job dynamically based on TIMEFRAME
    schedule.every(SCHEDULE_INTERVAL_MINUTES).minutes.do(job)
    
    print(f"{ALERT_PREFIX} Scheduled job to run every {SCHEDULE_INTERVAL_MINUTES} minutes. Daemon is active.", flush=True)
    
    # Build environment-aware startup alert
    env_warning = ""
    if ENV_TYPE.upper() == "TESTNET":
        env_warning = "\n⚠️ This is the TESTNET bot — not live trading."

    send_telegram_alert(
        f"{ALERT_EMOJI} *{ALERT_PREFIX} Quant Engine Started (Multi-Asset Mode)*\n\n"
        f"Monitoring top volatile USDT pairs + PAXGUSDT anchor.\n"
        f"Max 2 concurrent positions | ${SLOT_BUDGET:,.0f} per slot | *{TIMEFRAME} timeframe*.\n"
        f"Global Capital: ${TOTAL_CAPITAL:,.0f} | Slot Budget: ${SLOT_BUDGET:,.0f}"
        f"{env_warning}"
    )

    # Keep the container/script running indefinitely
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
