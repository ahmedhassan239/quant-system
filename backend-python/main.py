import time
import schedule
from scanner import scan
from data_fetcher import run_fetcher
from analyzer import run_analyzer, send_telegram_alert

def job():
    print("\n" + "="*60, flush=True)
    print(f"Running scheduled job at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
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
    print("Quant Engine heartbeat started (Multi-Asset Mode)...", flush=True)
    
    # Run the job immediately once on startup
    job()
    
    # Schedule the job to run every 15 minutes
    schedule.every(15).minutes.do(job)
    
    print("Scheduled job to run every 15 minutes. Daemon is active.", flush=True)
    
    # Send a one-time startup test message to Telegram
    send_telegram_alert(
        "✅ *Quant Engine Started (Multi-Asset Mode)*\n\n"
        "Monitoring top volatile USDT pairs + PAXGUSDT anchor.\n"
        "Max 2 concurrent positions | $500 per slot | 15m timeframe."
    )

    # Keep the container/script running indefinitely
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
