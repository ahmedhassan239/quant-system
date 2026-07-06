import os
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker

# Database configuration
DB_USER = os.environ.get("DB_USER", "quant_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "secret123")
DB_HOST = os.environ.get("DB_HOST", "postgres_db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "quant_db")

# Create database URL
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Create SQLAlchemy engine
engine = create_engine(DATABASE_URL)

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class
Base = declarative_base()

# Define MarketData model
class MarketData(Base):
    __tablename__ = "market_data"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint('symbol', 'timeframe', 'timestamp', name='_symbol_timeframe_timestamp_uc'),
    )

class TradingSignal(Base):
    __tablename__ = "trading_signals"
    
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    current_price = Column(Float, nullable=False)
    rsi = Column(Float, nullable=True)
    bullish_ob_low = Column(Float, nullable=True)
    bullish_ob_high = Column(Float, nullable=True)
    bearish_ob_low = Column(Float, nullable=True)
    bearish_ob_high = Column(Float, nullable=True)
    decision = Column(String, nullable=False)

class PortfolioState(Base):
    __tablename__ = "portfolio_state"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, nullable=False)
    symbol = Column(String, nullable=False)
    decision = Column(String, nullable=False)       # BUY or SELL that triggered this snapshot
    current_price = Column(Float, nullable=False)
    usdt_balance = Column(Float, nullable=False)
    paxg_balance = Column(Float, nullable=False)
    last_buy_price = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)          # % profit/loss for this trade (SELL only)
    pnl_usd = Column(Float, nullable=True)          # $ profit/loss for this trade (SELL only)
    total_portfolio_value = Column(Float, nullable=False)  # USDT + PAXG value at current_price

def init_db():
    """Create tables if they don't exist"""
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    init_db()
    print("Database initialized.", flush=True)
