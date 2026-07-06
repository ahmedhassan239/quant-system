import time
import schedule
from data_fetcher import run_fetcher
from analyzer import run_analyzer, send_telegram_alert

def job():
    print("\n" + "="*50, flush=True)
    print(f"Running scheduled job at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("="*50, flush=True)
    
    # 1. Fetch latest data
    run_fetcher()
    
    # 2. Analyze the newly fetched data
    run_analyzer()
    
    print("\nJob completed. Sleeping until next interval...", flush=True)
    print("="*50 + "\n", flush=True)

def main():
    print("Quant Engine heartbeat started...", flush=True)
    
    # Run the job immediately once on startup
    job()
    
    # Schedule the job to run every 15 minutes
    schedule.every(15).minutes.do(job)
    
    print("Scheduled job to run every 15 minutes. Daemon is active.", flush=True)
    
    # Send a one-time startup test message to Telegram
    send_telegram_alert("✅ Quant Engine Started Successfully! Telegram alerts are active and monitoring PAXGUSDT.")

    # Keep the container/script running indefinitely
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
