import time
import schedule
import threading
from scanner import scan, update_radar
from data_fetcher import run_fetcher
from analyzer import run_analyzer, run_macro_analyzer, send_telegram_alert
from config import (TIMEFRAME, ALERT_PREFIX, ALERT_EMOJI, ENGINE_ROLE,
                    ENV_TYPE, SCHEDULE_INTERVAL_MINUTES, BINANCE_FUTURES_BASE_URL,
                    FUTURES_LEVERAGE, FUTURES_MARGIN_TYPE,
                    MACRO_SMA_PERIOD, ZSCORE_LONG_THRESHOLD, ZSCORE_SHORT_THRESHOLD)
from database import SLOT_BUDGET, TOTAL_CAPITAL, MAX_CONCURRENT_POSITIONS, init_shared_db, get_active_symbols, save_wallet_balance
from futures_executor import create_futures_client, get_futures_balance

# ── Initialize Futures client once at module level ──
futures_client = None

def init_futures():
    """Create the Futures client. Called once at startup."""
    global futures_client
    try:
        futures_client = create_futures_client()
        print(f"✅ Futures client initialized (Leverage: {FUTURES_LEVERAGE}x, "
              f"Margin: {FUTURES_MARGIN_TYPE})", flush=True)
    except Exception as e:
        print(f"⚠️ Failed to initialize Futures client: {e}", flush=True)
        print("   → Running in VIRTUAL-ONLY mode (no real orders).", flush=True)
        futures_client = None

def wallet_balance_worker():
    """Background thread to poll Wallet Balance every 30 seconds."""
    while True:
        try:
            if futures_client:
                balance = get_futures_balance(futures_client)
                save_wallet_balance(balance)
        except Exception as e:
            print(f"Wallet balance fetch error: {e}", flush=True)
        time.sleep(30)

def job():
    print("\n" + "="*60, flush=True)
    print(f"{ALERT_PREFIX} Running scheduled job at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("="*60, flush=True)

def scanner_job():
    print("\n" + "="*60, flush=True)
    print(f"{ALERT_PREFIX} Running dynamic scanner (Radar) at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("="*60, flush=True)
    update_radar()
    print("Radar update complete.", flush=True)

    # 1. Fetch dynamic symbols from shared DB
    symbols = get_active_symbols()
    print(f"Trading active symbols: {symbols}", flush=True)

    # 2. Fetch latest Futures candle data for all scanned symbols
    run_fetcher(symbols=symbols)

    # 3. Run the appropriate analyzer based on engine role
    if ENGINE_ROLE.upper() == "MACRO":
        # ── Macro Trend Engine (1h) — analysis only, no orders ──
        for sym in symbols:
            run_macro_analyzer(symbol=sym)
    else:
        # ── Execution Engine (15m) — trades with MTF confluence ──
        for sym in symbols:
            run_analyzer(symbol=sym, futures_client=futures_client)

    print("\nJob completed. Sleeping until next interval...", flush=True)
    print("="*60 + "\n", flush=True)

def main():
    role_label = "MACRO TREND" if ENGINE_ROLE.upper() == "MACRO" else "EXECUTION"
    role_emoji = "🔭" if ENGINE_ROLE.upper() == "MACRO" else "⚡"

    print(f"{ALERT_PREFIX} Super Bot — {role_emoji} {role_label} Engine started...", flush=True)
    print(f"  → ENV_TYPE:     {ENV_TYPE}", flush=True)
    print(f"  → ENGINE_ROLE:  {ENGINE_ROLE}", flush=True)
    print(f"  → TIMEFRAME:    {TIMEFRAME}", flush=True)
    print(f"  → API:          {BINANCE_FUTURES_BASE_URL}", flush=True)
    print(f"  → Leverage:     {FUTURES_LEVERAGE}x", flush=True)
    print(f"  → Margin Type:  {FUTURES_MARGIN_TYPE}", flush=True)
    print(f"  → Schedule:     every {SCHEDULE_INTERVAL_MINUTES} minutes", flush=True)

    if ENGINE_ROLE.upper() == "MACRO":
        print(f"  → SMA Period:   {MACRO_SMA_PERIOD}", flush=True)
        print(f"  → Mode:         Analysis only (writes MacroState to shared DB)", flush=True)
    else:
        print(f"  → Z-Score LONG:  < {ZSCORE_LONG_THRESHOLD}", flush=True)
        print(f"  → Z-Score SHORT: > +{ZSCORE_SHORT_THRESHOLD}", flush=True)
        print(f"  → Mode:         Execution with MTF Confluence", flush=True)

    # Initialize shared DB for MTF communication
    init_shared_db()

    # Initialize Futures client (only needed for execution engine)
    if ENGINE_ROLE.upper() != "MACRO":
        init_futures()
        threading.Thread(target=wallet_balance_worker, daemon=True).start()

    # Run the job immediately once on startup
    if ENGINE_ROLE.upper() == "MACRO":
        scanner_job()
        schedule.every(60).minutes.do(scanner_job)
        
    job()

    # Schedule the job dynamically based on TIMEFRAME
    schedule.every(SCHEDULE_INTERVAL_MINUTES).minutes.do(job)

    print(f"{ALERT_PREFIX} Scheduled job to run every {SCHEDULE_INTERVAL_MINUTES} minutes. Daemon is active.", flush=True)

    # Build environment-aware startup alert
    env_warning = ""
    if ENV_TYPE.upper() == "TESTNET":
        env_warning = "\n⚠️ This is the TESTNET bot — Futures Testnet, not live trading."

    if ENGINE_ROLE.upper() == "MACRO":
        send_telegram_alert(
            f"{ALERT_EMOJI} *{ALERT_PREFIX} Super Bot — 🔭 Macro Trend Engine Started*\n\n"
            f"Computing 1h macro trends (SMA-{MACRO_SMA_PERIOD}, Z-Score, SDC).\n"
            f"Writing UPTREND/DOWNTREND to shared DB for the Execution Engine.\n"
            f"Schedule: every {SCHEDULE_INTERVAL_MINUTES} min | *{TIMEFRAME} timeframe*"
            f"{env_warning}"
        )
    else:
        send_telegram_alert(
            f"{ALERT_EMOJI} *{ALERT_PREFIX} Super Bot — ⚡ Execution Engine Started*\n\n"
            f"Trading with Dual-Strategy MTF Confluence (reads 1h macro trend from shared DB).\n"
            f"Strategy A (Pullback): Extreme Z-Score + Order Blocks\n"
            f"Strategy B (Breakout): Volume Anomalies + Consolidation Zones\n"
            f"Max {MAX_CONCURRENT_POSITIONS} positions | ${SLOT_BUDGET:,.0f}/slot | *{TIMEFRAME}*.\n"
            f"Leverage: {FUTURES_LEVERAGE}x | Margin: {FUTURES_MARGIN_TYPE}\n"
            f"Capital: ${TOTAL_CAPITAL:,.0f} | Slot: ${SLOT_BUDGET:,.0f}"
            f"{env_warning}"
        )

    # Keep the container/script running indefinitely
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
