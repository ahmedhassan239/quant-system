import time
import schedule
from data_fetcher import run_fetcher
from analyzer import run_analyzer

def job():
    print("\n" + "="*50)
    print(f"Running scheduled job at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*50)
    
    # 1. Fetch latest data
    run_fetcher()
    
    # 2. Analyze the newly fetched data
    run_analyzer()
    
    print("\nJob completed. Sleeping until next interval...")
    print("="*50 + "\n")

def main():
    print("Quant Engine heartbeat started...")
    
    # Run the job immediately once on startup
    job()
    
    # Schedule the job to run every 15 minutes
    schedule.every(15).minutes.do(job)
    
    print("Scheduled job to run every 15 minutes. Daemon is active.")
    
    # Keep the container/script running indefinitely
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
