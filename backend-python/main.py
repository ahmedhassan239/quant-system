import time
import schedule
import threading
from scanner import scan, update_radar
from data_fetcher import run_fetcher
from analyzer import run_analyzer, run_macro_analyzer, send_telegram_alert, MAX_GLOBAL_POSITIONS
from config import (TIMEFRAME, ALERT_PREFIX, ALERT_EMOJI, ENGINE_ROLE,
                    ENV_TYPE, SCHEDULE_INTERVAL_MINUTES, BINANCE_FUTURES_BASE_URL,
                    FUTURES_LEVERAGE, FUTURES_MARGIN_TYPE,
                    MACRO_SMA_PERIOD, ZSCORE_LONG_THRESHOLD, ZSCORE_SHORT_THRESHOLD,
                    REAL_WORLD_WHITELIST, MOCK_TOKENS_BLACKLIST)
from database import (SLOT_BUDGET, TOTAL_CAPITAL, MAX_CONCURRENT_POSITIONS,
                      init_shared_db, get_active_symbols, save_wallet_balance,
                      SessionLocal, get_open_position_symbols, sync_missing_stop_losses)
from futures_executor import create_futures_client, get_futures_balance, count_all_open_positions

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
    """Background thread to poll Wallet Balance every 5 minutes (300s)."""
    while True:
        try:
            if futures_client:
                balance = get_futures_balance(futures_client)
                save_wallet_balance(balance)
        except Exception as e:
            print(f"Wallet balance fetch error: {e}", flush=True)
        time.sleep(300)

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

    # 1. Fetch dynamic symbols from shared DB (Radar volume-ranked list)
    radar_symbols = get_active_symbols()

    # 2. Fetch currently open position symbols from local DB & live Binance
    open_pos_symbols = set()
    db_session = SessionLocal()
    try:
        sync_missing_stop_losses(db_session)
        open_pos_symbols.update(get_open_position_symbols(db_session))
    except Exception as e:
        print(f"⚠️ Error querying open position symbols from DB: {e}", flush=True)
    finally:
        db_session.close()

    if futures_client:
        try:
            pos_risk = futures_client.futures_position_information()
            for p in pos_risk:
                if float(p.get('positionAmt', 0)) != 0:
                    open_pos_symbols.add(p['symbol'])
        except Exception as e:
            print(f"⚠️ Error fetching Binance live positions: {e}", flush=True)

    # Combine open position symbols + Radar symbols (open positions first, no duplicates)
    all_symbols = list(open_pos_symbols)
    for s in radar_symbols:
        if s not in all_symbols:
            all_symbols.append(s)

    # STRICT SAFETY RULE: Filter out any mock tokens or non-whitelisted assets under all circumstances
    all_symbols = [s for s in all_symbols if s in REAL_WORLD_WHITELIST and s not in MOCK_TOKENS_BLACKLIST]

    print(f"Trading active symbols (Scanned: {len(radar_symbols)}, Open: {len(open_pos_symbols)}, Total Whitelisted: {len(all_symbols)}): {all_symbols}", flush=True)

    # 3. Fetch latest Futures candle data for all processed symbols
    run_fetcher(symbols=all_symbols)

    # 4. Run the appropriate analyzer based on engine role
    if ENGINE_ROLE.upper() == "MACRO":
        # ── Macro Trend Engine (1h) — analysis only, no orders ──
        for sym in all_symbols:
            run_macro_analyzer(symbol=sym)
    else:
        # ── Execution Engine (15m) — trades with MTF confluence ──
        import logging
        logger = logging.getLogger("ExecutionEngine")

        current_open_count = count_all_open_positions(futures_client)
        trades_opened_this_cycle = 0

        for sym in all_symbols:
            is_open_position = sym in open_pos_symbols

            if is_open_position:
                # ── RULE 1 & 2: ALWAYS evaluate risk management for open positions ──
                print(f"🛡️ [{sym}] Open position detected — evaluating risk management (SL/TSL/TP/Stagnant) unconditionally.", flush=True)
                newly_executed = run_analyzer(symbol=sym, futures_client=futures_client)
                if newly_executed:
                    trades_opened_this_cycle += 1
            else:
                if trades_opened_this_cycle >= 3:
                    logger.warning(f"Max trades per cycle (3) reached. Cooling down for {sym}.")
                    print(f"🛑 Max trades per cycle (3) reached. Skipping new entry for {sym}.", flush=True)
                    continue

                # Run analyzer (evaluates new entry or trade upgrade if portfolio is full)
                newly_executed = run_analyzer(symbol=sym, futures_client=futures_client)
                if newly_executed:
                    current_open_count = count_all_open_positions(futures_client)
                    trades_opened_this_cycle += 1

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

    # Run scanner_job immediately on startup (both engines)
    job()            # Print header banner
    scanner_job()    # Fetch data + analyze

    # Schedule recurring runs based on engine role
    if ENGINE_ROLE.upper() == "MACRO":
        schedule.every(60).minutes.do(scanner_job)
    else:
        schedule.every(SCHEDULE_INTERVAL_MINUTES).minutes.do(scanner_job)

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
