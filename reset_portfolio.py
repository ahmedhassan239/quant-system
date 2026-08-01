import os
import sys
from sqlalchemy import create_engine, text

def load_env():
    """Manually parse .env file to load credentials."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    # Remove quotes if they exist
                    val = val.strip("\"'")
                    os.environ.setdefault(key, val)

def main():
    print("=" * 60)
    print("⚠️  WARNING: You are about to wipe the portfolio and trade history!")
    print("This will reset all positions, trade history, signals, and local wallet balance.")
    print("This action is IRREVERSIBLE.")
    print("=" * 60)
    
    confirm = input("Are you sure you want to proceed? (Type 'Y' or 'YES' to confirm): ")
    if confirm.strip().upper() not in ('Y', 'YES'):
        print("Aborted.")
        sys.exit(0)

    print("\n[1] Loading environment and connecting to databases...")
    load_env()
    
    DB_USER = os.environ.get("POSTGRES_USER", "quant_user")
    DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "secret123")
    DB_HOST = os.environ.get("DB_HOST", "localhost")  # Use localhost assuming script runs on host machine
    DB_PORT = os.environ.get("DB_PORT", "5432")
    
    # Core DB from .env
    DB_NAME = os.environ.get("POSTGRES_DB", "quant_db")
    # Shared DB where macro states and wallet balances live
    MACRO_DB_NAME = os.environ.get("MACRO_DB_NAME", "quant_shared_db")

    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    SHARED_DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{MACRO_DB_NAME}"

    try:
        engine = create_engine(DATABASE_URL)
        shared_engine = create_engine(SHARED_DATABASE_URL)
        
        with engine.begin() as conn:
            print(f"[2] Wiping per-engine data from {DB_NAME}...")
            
            # Using CASCADE to safely truncate tables if there are any unforeseen dependencies
            tables_to_truncate = ["positions", "trade_history", "trading_signals", "bot_logs"]
            for table in tables_to_truncate:
                try:
                    conn.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE;"))
                    print(f" -> Cleared '{table}'")
                except Exception as e:
                    print(f" -> Skipping '{table}' (table might not exist: {e})")
            
        with shared_engine.begin() as conn:
            print(f"\n[3] Wiping shared data and resetting wallet in {MACRO_DB_NAME}...")
            
            try:
                # Set local DB wallet balance to exactly 1000.0
                conn.execute(text("TRUNCATE TABLE wallet_balance RESTART IDENTITY CASCADE;"))
                conn.execute(text("INSERT INTO wallet_balance (balance, updated_at) VALUES (1000.0, NOW());"))
                print(" -> Reset 'wallet_balance' to exactly 1000.0 USD.")
            except Exception as e:
                print(f" -> Could not reset 'wallet_balance' (table might not exist: {e})")

            try:
                # Optionally clear macro states to ensure everything starts fresh
                conn.execute(text("TRUNCATE TABLE macro_state RESTART IDENTITY CASCADE;"))
                print(" -> Cleared 'macro_state'.")
            except Exception as e:
                pass
            
    except Exception as e:
        print(f"\n❌ Error connecting or executing SQL: {e}")
        print("Make sure your PostgreSQL container is running (docker-compose up -d postgres_db) and accessible on localhost:5432.")
        sys.exit(1)
        
    print("\n✅ Local Database Reset successfully completed!")
    
    print("\n" + "=" * 60)
    print("DOCKER RESTART INSTRUCTIONS")
    print("=" * 60)
    print("To apply these changes and restart the bot execution engines, run:")
    print("  docker-compose down")
    print("  docker-compose up -d")
    print("\nNOTE ON BINANCE TESTNET LIVE BALANCE:")
    print("If your bot fetches the live balance directly from the Binance Futures Testnet API,")
    print("you must manually adjust your Binance Testnet Futures wallet to exactly $1000.")
    print("Steps:")
    print("  1. Go to https://testnet.binancefuture.com")
    print("  2. Click 'Wallet' -> 'Futures'")
    print("  3. Click 'Transfer' and move any excess USDT from Futures to Spot.")
    print("     (Or if you have less than $1000, transfer USDT from Spot to Futures).")
    print("=" * 60)

if __name__ == "__main__":
    main()
