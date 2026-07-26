import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float, UniqueConstraint, text, func, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from config import REAL_WORLD_WHITELIST, MOCK_TOKENS_BLACKLIST

# ──────────────────────────────────────────────────────────────────────
#  PER-ENGINE DATABASE (portfolio, signals, market data)
# ──────────────────────────────────────────────────────────────────────
DB_USER = os.environ.get("DB_USER", "quant_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "secret123")
DB_HOST = os.environ.get("DB_HOST", "postgres_db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "quant_db")  # Overridden per container in docker-compose

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ──────────────────────────────────────────────────────────────────────
#  SHARED DATABASE (MTF communication — MacroState)
# ──────────────────────────────────────────────────────────────────────
MACRO_DB_NAME = os.environ.get("MACRO_DB_NAME", "quant_shared_db")
SHARED_DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{MACRO_DB_NAME}"

shared_engine = create_engine(SHARED_DATABASE_URL, pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=3600)
SharedSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=shared_engine)

# ──────────────────────────────────────────────────────────────────────
#  BASE CLASSES
# ──────────────────────────────────────────────────────────────────────
Base = declarative_base()           # Per-engine tables
SharedBase = declarative_base()     # Shared MTF tables

# ──────────────────────────────────────────────────────────────────────
#  GLOBAL PORTFOLIO CONSTANTS
# ──────────────────────────────────────────────────────────────────────
TOTAL_CAPITAL = 1000.0             # Total virtual capital
MAX_CONCURRENT_POSITIONS = 10       # Max symbols with open positions
SLOT_BUDGET = 100.0                # Max USDT allocated per symbol


# ══════════════════════════════════════════════════════════════════════
#  PER-ENGINE MODELS (in DB_NAME database)
# ══════════════════════════════════════════════════════════════════════

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
    z_score = Column(Float, nullable=True)
    macro_trend = Column(String, nullable=True)           # UPTREND / DOWNTREND (from macro engine)
    bullish_ob_low = Column(Float, nullable=True)
    bullish_ob_high = Column(Float, nullable=True)
    bearish_ob_low = Column(Float, nullable=True)
    bearish_ob_high = Column(Float, nullable=True)
    decision = Column(String, nullable=False)             # LONG, SHORT, or WAIT
    strategy_type = Column(String, nullable=True)         # PULLBACK or BREAKOUT

class PortfolioState(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, nullable=False)
    symbol = Column(String, nullable=False)
    decision = Column(String, nullable=False)       # LONG, SHORT, CLOSE_LONG, CLOSE_SHORT, HOLD
    entry_reason = Column(String, nullable=True)    # E.g. "Strategy A (Pullback) | Macro: UPTREND..."
    current_price = Column(Float, nullable=False)
    usdt_balance = Column(Float, nullable=False)
    asset_balance = Column(Float, nullable=False)
    position_direction = Column(String, nullable=True)  # 'LONG', 'SHORT', or None
    average_entry_price = Column(Float, nullable=True)
    dca_level = Column(Integer, default=0, nullable=False)
    last_exec_price = Column(Float, nullable=True)
    total_cost = Column(Float, nullable=False, default=0.0)
    highest_price_since_entry = Column(Float, nullable=True)
    lowest_price_since_entry = Column(Float, nullable=True)
    stop_loss_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    strategy = Column(String, nullable=True)
    trailing_active = Column(Boolean, nullable=True, default=False)
    pnl_pct = Column(Float, nullable=True)
    pnl_usd = Column(Float, nullable=True)
    total_portfolio_value = Column(Float, nullable=False)

class BotLog(Base):
    __tablename__ = "bot_logs"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)           # e.g. "INFO", "WARNING", "ENTRY", "EXIT", "ERROR"
    message = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class TradeHistory(Base):
    __tablename__ = "trade_history"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    direction = Column(String, nullable=False)            # LONG or SHORT
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    pnl_usd = Column(Float, nullable=False)
    pnl_pct = Column(Float, nullable=False)
    outcome = Column(String, nullable=False)              # WIN or LOSS
    exit_reason = Column(String, nullable=False)          # STOP_LOSS, TRAILING_STOP, MANUAL, SIGNAL
    closed_at = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════
#  SHARED MTF MODEL (in quant_shared_db database)
# ══════════════════════════════════════════════════════════════════════

class MacroState(SharedBase):
    """
    Cross-engine communication table.
    The 1h Macro Engine writes the trend for each symbol.
    The 15m Execution Engine reads it before making entry decisions.

    One row per symbol — upserted on every macro analysis cycle.
    """
    __tablename__ = "macro_state"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, unique=True, index=True)
    macro_trend = Column(String, nullable=False)          # 'UPTREND' or 'DOWNTREND'
    sma_50 = Column(Float, nullable=False)                # Current SMA-50 value
    z_score = Column(Float, nullable=False)               # Current Z-Score
    std_dev = Column(Float, nullable=True)                # Current 50-period σ
    sdc_upper = Column(Float, nullable=True)              # SMA + 2σ
    sdc_lower = Column(Float, nullable=True)              # SMA - 2σ
    current_price = Column(Float, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class WalletBalance(SharedBase):
    """
    Stores the live Futures Wallet Balance.
    Updated every 30 seconds by the background worker.
    """
    __tablename__ = "wallet_balance"
    id = Column(Integer, primary_key=True, index=True)
    balance = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

# ══════════════════════════════════════════════════════════════════════
#  HELPERS — Per-Engine
# ══════════════════════════════════════════════════════════════════════

def count_active_positions(session):
    """
    Count how many distinct symbols currently have an open position
    (asset_balance > 0) based on each symbol's LATEST portfolio state.
    """
    latest_ids = (
        session.query(func.max(PortfolioState.id).label('max_id'))
        .group_by(PortfolioState.symbol)
        .subquery()
    )

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


def get_open_position_symbols(session):
    """
    Return a list of symbol strings that currently have an open position (asset_balance > 0).
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

def sync_missing_stop_losses(session):
    """
    Finds active open positions (asset_balance > 0) with NULL stop_loss_price,
    preserves existing local stop_loss_price if available, or calculates a default 1.5% SL
    relative to average_entry_price if it's a completely new execution.
    """
    latest_ids = (
        session.query(func.max(PortfolioState.id).label('max_id'))
        .group_by(PortfolioState.symbol)
        .subquery()
    )

    rows = (
        session.query(PortfolioState)
        .filter(
            PortfolioState.id.in_(
                session.query(latest_ids.c.max_id)
            ),
            PortfolioState.asset_balance > 0,
            PortfolioState.stop_loss_price.is_(None)
        )
        .all()
    )
    
    updated = False
    for row in rows:
        prev_sl = (
            session.query(PortfolioState.stop_loss_price, PortfolioState.stop_loss)
            .filter(
                PortfolioState.symbol == row.symbol,
                PortfolioState.asset_balance > 0,
                (PortfolioState.stop_loss_price.isnot(None) | PortfolioState.stop_loss.isnot(None))
            )
            .order_by(PortfolioState.id.desc())
            .first()
        )

        if prev_sl and (prev_sl.stop_loss_price or prev_sl.stop_loss):
            val = float(prev_sl.stop_loss_price or prev_sl.stop_loss)
            row.stop_loss_price = val
            row.stop_loss = val
            print(f"🔄 Preserved existing local Stop Loss for {row.symbol} ({row.position_direction}): ${row.stop_loss_price:.4f}", flush=True)
            updated = True
        else:
            if row.position_direction == 'LONG':
                row.stop_loss_price = float(row.average_entry_price) * 0.985
                row.stop_loss = float(row.average_entry_price) * 0.985
                print(f"🔄 Synced missing Stop Loss for new {row.symbol} LONG execution: ${row.stop_loss_price:.4f}", flush=True)
                updated = True
            elif row.position_direction == 'SHORT':
                row.stop_loss_price = float(row.average_entry_price) * 1.015
                row.stop_loss = float(row.average_entry_price) * 1.015
                print(f"🔄 Synced missing Stop Loss for new {row.symbol} SHORT execution: ${row.stop_loss_price:.4f}", flush=True)
                updated = True
            
    if updated:
        session.commit()

# ══════════════════════════════════════════════════════════════════════
#  HELPERS — Shared MTF (MacroState)
# ══════════════════════════════════════════════════════════════════════

def save_macro_state(symbol, macro_trend, sma_50, z_score, std_dev,
                     sdc_upper, sdc_lower, current_price):
    """
    Upsert the MacroState for a symbol.
    Called by the 1h Macro Engine after each analysis cycle.
    """
    session = SharedSessionLocal()
    try:
        existing = session.query(MacroState).filter(
            MacroState.symbol == symbol
        ).first()

        if existing:
            existing.macro_trend = macro_trend
            existing.sma_50 = float(sma_50)
            existing.z_score = float(z_score)
            existing.std_dev = float(std_dev) if std_dev is not None else None
            existing.sdc_upper = float(sdc_upper) if sdc_upper is not None else None
            existing.sdc_lower = float(sdc_lower) if sdc_lower is not None else None
            existing.current_price = float(current_price)
            existing.updated_at = datetime.utcnow()
        else:
            record = MacroState(
                symbol=symbol,
                macro_trend=macro_trend,
                sma_50=float(sma_50),
                z_score=float(z_score),
                std_dev=float(std_dev) if std_dev is not None else None,
                sdc_upper=float(sdc_upper) if sdc_upper is not None else None,
                sdc_lower=float(sdc_lower) if sdc_lower is not None else None,
                current_price=float(current_price),
                updated_at=datetime.utcnow(),
            )
            session.add(record)

        session.commit()
        print(f"📊 [{symbol}] MacroState saved: {macro_trend} | Z: {z_score:+.2f} | "
              f"SMA-50: ${sma_50:,.2f}", flush=True)
    except Exception as e:
        session.rollback()
        print(f"⚠️ [{symbol}] Failed to save MacroState: {e}", flush=True)
    finally:
        session.close()


def get_macro_trend(symbol):
    """
    Fetch the latest MacroState for a symbol from the shared DB.
    Called by the 15m Execution Engine before signal logic.

    Returns:
        dict with keys: macro_trend, sma_50, z_score, sdc_upper, sdc_lower,
                        current_price, updated_at
        or None if no macro data exists yet.
    """
    session = SharedSessionLocal()
    try:
        state = session.query(MacroState).filter(
            MacroState.symbol == symbol
        ).first()

        if state:
            return {
                'macro_trend': state.macro_trend,
                'sma_50': state.sma_50,
                'z_score': state.z_score,
                'std_dev': state.std_dev,
                'sdc_upper': state.sdc_upper,
                'sdc_lower': state.sdc_lower,
                'current_price': state.current_price,
                'updated_at': state.updated_at,
            }
        return None
    except Exception as e:
        print(f"⚠️ [{symbol}] Failed to read MacroState: {e}", flush=True)
        return None
    finally:
        session.close()

def save_wallet_balance(balance: float):
    """
    Upsert the live Wallet Balance in the shared DB.
    """
    session = SharedSessionLocal()
    try:
        record = session.query(WalletBalance).first()
        if record:
            record.balance = balance
            record.updated_at = datetime.utcnow()
        else:
            record = WalletBalance(balance=balance, updated_at=datetime.utcnow())
            session.add(record)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"⚠️ Failed to save wallet balance: {e}", flush=True)
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
#  INITIALIZATION
# ══════════════════════════════════════════════════════════════════════

def init_db():
    """Create tables if they don't exist, drop/recreate positions for clean schema."""
    Base.metadata.create_all(bind=engine)

def init_shared_db():
    """Create the MacroState / ActiveSymbol tables in the shared database.

    Also runs a safe migration to add the `rank` column to active_symbols
    if the table already exists from a previous deployment without that column.
    """
    SharedBase.metadata.create_all(bind=shared_engine)

    # Safe migration: add `rank` to active_symbols if it is missing.
    # `ALTER TABLE … ADD COLUMN IF NOT EXISTS` is idempotent in PostgreSQL 9.6+.
    try:
        with shared_engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE active_symbols ADD COLUMN IF NOT EXISTS rank INTEGER NOT NULL DEFAULT 0;"
            ))
            conn.commit()
    except Exception:
        # Table may not exist yet (first-time init) — create_all above handles it.
        pass


if __name__ == "__main__":
    init_db()
    init_shared_db()
    print("Database initialized (per-engine + shared).", flush=True)

# ══════════════════════════════════════════════════════════════════════
#  DYNAMIC SYMBOLS (quant_shared_db)
# ══════════════════════════════════════════════════════════════════════

class ActiveSymbol(SharedBase):
    __tablename__ = "active_symbols"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
    # Volume-rank position from the Radar scan (0 = highest volume).
    # ORDER BY rank ASC reproduces the exact Radar-ordered list.
    rank = Column(Integer, nullable=False, default=0)

def get_active_symbols():
    """
    Fetch active symbols dynamically from quant_shared_db.

    Returns symbols in STRICT volume-rank order (rank ASC), exactly as
    the Radar scanner produced them. Falls back to a default list if the
    table is missing or empty.
    """
    default_symbols = ['BTCUSDT', 'ETHUSDT']
    session = SharedSessionLocal()
    try:
        if not shared_engine.dialect.has_table(shared_engine.connect(), "active_symbols"):
            return default_symbols

        # ORDER BY rank ASC preserves the Radar's volume-ranked order.
        # Do NOT sort alphabetically or by id — that destroys the ranking.
        rows = (
            session.query(ActiveSymbol)
            .filter(ActiveSymbol.is_active == True)
            .order_by(ActiveSymbol.rank.asc())
            .all()
        )
        symbol_list = [r.symbol for r in rows if r.symbol in REAL_WORLD_WHITELIST and r.symbol not in MOCK_TOKENS_BLACKLIST]
        return symbol_list if symbol_list else default_symbols
    except Exception as e:
        print(f"⚠️ Error fetching active symbols: {e}")
        return default_symbols
    finally:
        session.close()

def update_active_symbols(new_symbols: list[str]):
    """
    Update the active symbols in quant_shared_db, preserving the
    EXACT volume-rank order produced by the Radar scanner.

    The list index (0 = highest 24h volume) is stored in the `rank`
    column and read back via ORDER BY rank ASC in get_active_symbols(),
    guaranteeing the execution loop processes symbols in Radar order.
    """
    session = SharedSessionLocal()
    try:
        if not shared_engine.dialect.has_table(shared_engine.connect(), "active_symbols"):
            print("⚠️ active_symbols table not found. Run init_shared_db() first.")
            return

        # Ensure strict whitelist enforcement and exclude mock tokens
        new_symbols = [s for s in new_symbols if s in REAL_WORLD_WHITELIST and s not in MOCK_TOKENS_BLACKLIST]

        existing_records = session.query(ActiveSymbol).all()
        existing_dict = {record.symbol: record for record in existing_records}

        # Store rank (index) alongside each symbol so ORDER BY rank ASC
        # reproduces the Radar's volume-ranked order on read-back.
        for rank_idx, sym in enumerate(new_symbols):
            if sym in existing_dict:
                existing_dict[sym].is_active = True
                existing_dict[sym].rank = rank_idx
            else:
                new_record = ActiveSymbol(symbol=sym, is_active=True, rank=rank_idx)
                session.add(new_record)

        for sym, record in existing_dict.items():
            if sym not in new_symbols:
                record.is_active = False

        session.commit()
        print(
            f"Radar updated DB: {len(new_symbols)} active symbols synced "
            f"(rank order preserved).",
            flush=True,
        )
    except Exception as e:
        session.rollback()
        print(f"⚠️ Error updating active symbols: {e}", flush=True)
    finally:
        session.close()
