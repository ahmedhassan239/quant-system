import os
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, UniqueConstraint, text, func
from sqlalchemy.orm import declarative_base, sessionmaker

# Database configuration
DB_USER = os.environ.get("DB_USER", "quant_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "secret123")
DB_HOST = os.environ.get("DB_HOST", "postgres_db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "quant_db")  # Overridden per container in docker-compose

# Create database URL
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Create SQLAlchemy engine
engine = create_engine(DATABASE_URL)

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class
Base = declarative_base()

# ──────────────────────────────────────────────────────────────────────
#  GLOBAL PORTFOLIO CONSTANTS
# ──────────────────────────────────────────────────────────────────────
TOTAL_CAPITAL = 1000.0             # Total virtual capital
MAX_CONCURRENT_POSITIONS = 2       # Max symbols with open positions
SLOT_BUDGET = 500.0                # Max USDT allocated per symbol


# ──────────────────────────────────────────────────────────────────────
#  MODELS
# ──────────────────────────────────────────────────────────────────────

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
    decision = Column(String, nullable=False)       # BUY, SELL, or HOLD
    current_price = Column(Float, nullable=False)
    usdt_balance = Column(Float, nullable=False)
    asset_balance = Column(Float, nullable=False)    # Generic: qty of the traded asset
    average_entry_price = Column(Float, nullable=True)
    dca_level = Column(Integer, default=0, nullable=False)
    last_exec_price = Column(Float, nullable=True)
    total_cost = Column(Float, nullable=False, default=0.0)
    highest_price_since_entry = Column(Float, nullable=True)  # Trailing stop high watermark
    pnl_pct = Column(Float, nullable=True)          # % profit/loss for this trade (SELL only)
    pnl_usd = Column(Float, nullable=True)          # $ profit/loss for this trade (SELL only)
    total_portfolio_value = Column(Float, nullable=False)  # USDT + asset value at current_price


# ──────────────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────────────

def count_active_positions(session):
    """
    Count how many distinct symbols currently have an open position
    (asset_balance > 0) based on each symbol's LATEST portfolio state.
    """
    # Subquery: latest portfolio_state id per symbol
    latest_ids = (
        session.query(func.max(PortfolioState.id).label('max_id'))
        .group_by(PortfolioState.symbol)
        .subquery()
    )

    # Count how many of those latest rows have asset_balance > 0
    active_count = (
        session.query(func.count(PortfolioState.id))
        .filter(
            PortfolioState.id.in_(
                session.query(latest_ids.c.max_id)
            ),
            PortfolioState.asset_balance > 0
        )
        .scalar()
    )
    return active_count or 0


def get_active_symbols(session):
    """
    Return a list of symbol strings that currently have an open position.
    """
    latest_ids = (
        session.query(func.max(PortfolioState.id).label('max_id'))
        .group_by(PortfolioState.symbol)
        .subquery()
    )

    rows = (
        session.query(PortfolioState.symbol)
        .filter(
            PortfolioState.id.in_(
                session.query(latest_ids.c.max_id)
            ),
            PortfolioState.asset_balance > 0
        )
        .all()
    )
    return [r.symbol for r in rows]


# ──────────────────────────────────────────────────────────────────────
#  INITIALIZATION
# ──────────────────────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist, drop/recreate portfolio_state for clean schema."""
    PortfolioState.__table__.drop(bind=engine, checkfirst=True)
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    init_db()
    print("Database initialized.", flush=True)
