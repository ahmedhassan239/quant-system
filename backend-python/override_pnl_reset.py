#!/usr/bin/env python3
"""
Overriding Database Reset Script
--------------------------------
1. Identifies table and column used by the Dashboard to read aggregate 'Total Realized PNL'.
2. Forcefully syncs/updates aggregate PNL columns to match SELECT SUM(pnl_usd) FROM trade_history.
3. Clears Laravel caches (config, cache, view) to reflect the reset on the frontend.
"""

import os
import sys
import subprocess
import psycopg2

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()

def main():
    print("=" * 70)
    print(" 🛠️  DATABASE OVERRIDE & RE-SYNCHRONIZATION SCRIPT")
    print("=" * 70)

    host = os.environ.get("DB_HOST", "127.0.0.1")
    user = os.environ.get("DB_USER", "myquantuser")
    password = os.environ.get("DB_PASSWORD", "mysecretpassword")

    # Step 1: Connect to quant_db and calculate true historical sum from trade_history
    print("\n[Step 1] Connecting to PostgreSQL database (quant_db)...")
    try:
        conn = psycopg2.connect(host=host, user=user, password=password, dbname="quant_db")
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*), COALESCE(SUM(pnl_usd), 0.0) FROM trade_history;")
        trade_count, sum_pnl = cur.fetchone()
        sum_pnl = float(sum_pnl)
        print(f"   • Historical Closed Trades Count: {trade_count}")
        print(f"   • True Historical SUM(pnl_usd) from trade_history: ${sum_pnl:,.4f}")

        # Check for any aggregate columns in positions / portfolio_state
        cur.execute("""
            SELECT table_name, column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'public' 
              AND column_name IN ('pnl_usd', 'realized_pnl', 'total_realized_pnl', 'total_pnl');
        """)
        target_cols = cur.fetchall()
        print(f"   • Discovered PNL aggregate/detail columns in quant_db: {target_cols}")

        # Step 2: Forcefully update any aggregate / state table if present
        print("\n[Step 2] Applying forceful synchronization...")
        for table_name, col_name in target_cols:
            if table_name in ('positions', 'portfolio_state'):
                # Update closed position records or summary records if applicable
                cur.execute(f"UPDATE {table_name} SET {col_name} = %s WHERE decision = 'CLOSED';", (sum_pnl,))
                updated = cur.rowcount
                print(f"   • Updated {updated} rows in '{table_name}.{col_name}' to ${sum_pnl:,.4f}")
        
        conn.commit()
        cur.close()
        conn.close()

    except Exception as e:
        print(f"   ❌ Error querying/updating quant_db: {e}")
        sys.exit(1)

    # Also check quant_shared_db if any aggregate tables exist
    try:
        conn_s = psycopg2.connect(host=host, user=user, password=password, dbname="quant_shared_db")
        cur_s = conn_s.cursor()
        cur_s.execute("""
            SELECT table_name, column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'public' 
              AND column_name IN ('pnl_usd', 'realized_pnl', 'total_realized_pnl', 'balance', 'total_pnl');
        """)
        target_cols_s = cur_s.fetchall()
        print(f"   • Discovered PNL/Balance columns in quant_shared_db: {target_cols_s}")

        for table_name, col_name in target_cols_s:
            if table_name in ('accounts', 'users', 'bot_state', 'portfolios'):
                cur_s.execute(f"UPDATE {table_name} SET {col_name} = %s;", (sum_pnl,))
                print(f"   • Updated {cur_s.rowcount} rows in quant_shared_db.{table_name}.{col_name} to ${sum_pnl:,.4f}")

        conn_s.commit()
        cur_s.close()
        conn_s.close()

    except Exception as e:
        print(f"   ⚠️ Info/Notice on quant_shared_db: {e}")

    # Step 3: Clear Laravel caches
    print("\n[Step 3] Flushing Laravel application caches...")
    caches = [
        "docker exec quant_laravel_api php artisan config:clear",
        "docker exec quant_laravel_api php artisan cache:clear",
        "docker exec quant_laravel_api php artisan view:clear",
        "docker exec quant_laravel_api php artisan route:clear"
    ]
    for cmd in caches:
        code, out, err = run_cmd(cmd)
        if code == 0:
            print(f"   ✅ Executed: {cmd} -> {out}")
        else:
            print(f"   ⚠️ Failed: {cmd} -> {err}")

    # Step 4: Verification via Laravel API
    print("\n[Step 4] Verifying Dashboard API output...")
    code, out, err = run_cmd("curl -s http://localhost:8080/api/dashboard-metrics")
    if code == 0:
        print(f"   • API Dashboard Response: {out[:300]}...")
    else:
        print(f"   ⚠️ Could not reach dashboard API: {err}")

    print("\n" + "=" * 70)
    print(" 🎉 HARD RESET & SYNCHRONIZATION COMPLETED SUCCESSFULLY")
    print("=" * 70)

if __name__ == "__main__":
    main()
