#!/usr/bin/env python3
"""
Safe Utility Script to Reset Total Realized PNL and Trade History
Without affecting currently open active trades in the positions (PortfolioState) table.
"""

import os
import sys
import socket

# Locate project root and load .env if environment variables are not set
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_file = os.path.join(project_root, ".env")
if os.path.exists(env_file):
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# Map POSTGRES_* to DB_* if DB_* not set
if "DB_USER" not in os.environ and "POSTGRES_USER" in os.environ:
    os.environ["DB_USER"] = os.environ["POSTGRES_USER"]
if "DB_PASSWORD" not in os.environ and "POSTGRES_PASSWORD" in os.environ:
    os.environ["DB_PASSWORD"] = os.environ["POSTGRES_PASSWORD"]
if "DB_NAME" not in os.environ and "POSTGRES_DB" in os.environ:
    os.environ["DB_NAME"] = os.environ["POSTGRES_DB"]

# Check DB_HOST resolution; fallback to 127.0.0.1 if running on local host outside Docker
db_host = os.environ.get("DB_HOST", "postgres_db")
try:
    socket.gethostbyname(db_host)
except socket.gaierror:
    print(f"ℹ️  Host '{db_host}' not resolved in DNS. Defaulting to 127.0.0.1 for local host connection.", flush=True)
    os.environ["DB_HOST"] = "127.0.0.1"

# Add backend-python to sys.path and import database modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from sqlalchemy import text, func
from database import SessionLocal, TradeHistory, PortfolioState

def reset_realized_pnl():
    print("═" * 65, flush=True)
    print(" 🛠️  REALIZED PNL & TRADE HISTORY RESET UTILITY", flush=True)
    print("═" * 65, flush=True)

    session = SessionLocal()
    try:
        # 1. Inspect current state before reset
        old_trades_count = session.query(TradeHistory).count()
        old_pnl_result = session.query(func.sum(TradeHistory.pnl_usd)).scalar()
        old_pnl = float(old_pnl_result) if old_pnl_result is not None else 0.0
        old_wins = session.query(TradeHistory).filter(TradeHistory.outcome == 'WIN').count()
        old_win_rate = (old_wins / old_trades_count * 100.0) if old_trades_count > 0 else 0.0

        active_pos_count = session.query(PortfolioState).count()

        print(f"📊 STATE BEFORE RESET:", flush=True)
        print(f"   • Closed Trades Count : {old_trades_count}", flush=True)
        print(f"   • Historical Win Rate : {old_win_rate:.2f}%", flush=True)
        print(f"   • Total Realized PNL  : ${old_pnl:,.2f}", flush=True)
        print(f"   • Active Open Trades  : {active_pos_count} (STRICTLY PROTECTED)", flush=True)
        print("─" * 65, flush=True)

        if old_trades_count == 0:
            print("ℹ️  Trade history is already empty. Realized PNL is $0.00.", flush=True)
        else:
            print("⏳ Wiping historical trades (truncating trade_history)...", flush=True)
            # TRUNCATE resets table and restarts ID auto-increment sequence without touching positions table
            session.execute(text("TRUNCATE TABLE trade_history RESTART IDENTITY;"))
            session.commit()
            print("✅ Successfully cleared trade_history table and reset ID sequence to 1.", flush=True)

        # 2. Verify state after reset
        new_trades_count = session.query(TradeHistory).count()
        new_pnl_result = session.query(func.sum(TradeHistory.pnl_usd)).scalar()
        new_pnl = float(new_pnl_result) if new_pnl_result is not None else 0.0
        new_active_pos_count = session.query(PortfolioState).count()

        print("─" * 65, flush=True)
        print(f"📈 VERIFICATION AFTER RESET:", flush=True)
        print(f"   • Closed Trades Count : {new_trades_count}", flush=True)
        print(f"   • Total Realized PNL  : ${new_pnl:,.2f}", flush=True)
        print(f"   • Active Open Trades  : {new_active_pos_count} (VERIFIED UNTOUCHED)", flush=True)
        print("═" * 65, flush=True)

        if new_trades_count == 0 and new_pnl == 0.0 and new_active_pos_count == active_pos_count:
            print("🎉 SUCCESS: Total Realized PNL is cleanly reset to $0.00 while active trades remain safe!", flush=True)
        else:
            print("⚠️ WARNING: Verification discrepancy detected! Please check logs.", flush=True)

    except Exception as e:
        session.rollback()
        print(f"❌ CRITICAL ERROR during PNL reset: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()

if __name__ == "__main__":
    reset_realized_pnl()
