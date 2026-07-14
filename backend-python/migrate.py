import os
import sys
sys.path.append('/app')
from database import engine, Base, init_db, init_shared_db, PortfolioState, TradingSignal
from config import ENV_TYPE

def migrate():
    print("Dropping tables for Sniper upgrade...", flush=True)
    # Drop portfolio_state and trading_signals
    PortfolioState.__table__.drop(bind=engine, checkfirst=True)
    TradingSignal.__table__.drop(bind=engine, checkfirst=True)
    
    print("Recreating tables...", flush=True)
    init_db()
    init_shared_db()
    print("Migration complete.", flush=True)

if __name__ == "__main__":
    migrate()
