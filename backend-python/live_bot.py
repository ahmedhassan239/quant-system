import os
import sys
import time
import logging
import datetime
import numpy as np
import pandas as pd
import joblib
import ccxt
import ta
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker

# ──────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
SYMBOL = 'BTC/USDT'
TIMEFRAME = '5m'
OHLCV_LIMIT = 250          # Candles to fetch (enough for SMA_200 warmup)
CONFIDENCE_THRESHOLD = 0.65
TRAILING_STOP_PCT = 0.015   # 1.5%
STOP_LOSS_PCT = 0.01        # 1%
TRADING_FEE = 0.001         # 0.1% Binance Spot fee
LOOP_INTERVAL_SEC = 300     # Poll every 300 seconds (5m candle)
SWING_LOOKBACK = 5          # Candles on each side to confirm a swing point
MODEL_PATH = 'models/quant_rf_model.pkl'

FEATURE_COLS = [
    'open', 'high', 'low', 'close', 'volume',
    'RSI_14', 'MACD', 'MACD_signal', 'BB_high', 'BB_low',
    'ATR_14', 'SMA_50', 'SMA_200',
]

# ──────────────────────────────────────────────────────────────────────
#  LOGGING SETUP
# ──────────────────────────────────────────────────────────────────────
os.makedirs('logs', exist_ok=True)

logger = logging.getLogger('LiveBot')
logger.setLevel(logging.DEBUG)

# File handler → logs/trading.log
fh = logging.FileHandler('logs/trading.log')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
))

# Console handler → stdout
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%H:%M:%S',
))

logger.addHandler(fh)
logger.addHandler(ch)

# ──────────────────────────────────────────────────────────────────────
#  DATABASE SETUP (PostgreSQL via SQLAlchemy)
# ──────────────────────────────────────────────────────────────────────
DATABASE_URL = "postgresql://quant_user:secret123@postgres_db:5432/quant_db"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class AccountBalance(Base):
    __tablename__ = 'account_balances'

    id = Column(Integer, primary_key=True, autoincrement=True)
    usdt_balance = Column(Float, nullable=False)
    btc_balance = Column(Float, nullable=False)
    total_equity = Column(Float, nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)


class TradeHistory(Base):
    __tablename__ = 'trade_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String, unique=True, nullable=False)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)       # BUY or SELL
    price = Column(Float, nullable=False)
    amount = Column(Float, nullable=False)      # BTC quantity
    cost = Column(Float, nullable=False)        # Total USDT cost
    confidence = Column(Float, nullable=True)   # Model confidence (BUY only)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)


# Create tables on startup (safe to call repeatedly)
Base.metadata.create_all(bind=engine)

# Safe migration: add total_equity column if table already exists without it
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE account_balances ADD COLUMN total_equity FLOAT;"))
        conn.commit()
        logger.info("Migration: added total_equity column to account_balances.")
    except Exception:
        conn.rollback()  # Column already exists, no action needed

logger.info("Database tables verified (account_balances, trade_history).")


def db_save_balance(usdt, btc, current_price=0.0):
    """Insert an AccountBalance snapshot with total equity into PostgreSQL."""
    equity = usdt + (btc * current_price)
    session = SessionLocal()
    try:
        record = AccountBalance(
            usdt_balance=usdt,
            btc_balance=btc,
            total_equity=equity,
        )
        session.add(record)
        session.commit()
        logger.debug(f"DB: Saved balance — USDT: {usdt:,.2f}  BTC: {btc:.6f}  "
                     f"Equity: ${equity:,.2f}")
    except Exception as e:
        session.rollback()
        logger.error(f"DB: Failed to save balance: {e}")
    finally:
        session.close()


def db_save_trade(order_id, symbol, side, price, amount, cost, confidence=None):
    """Insert a TradeHistory record into PostgreSQL."""
    session = SessionLocal()
    try:
        record = TradeHistory(
            order_id=order_id,
            symbol=symbol,
            side=side,
            price=price,
            amount=amount,
            cost=cost,
            confidence=confidence,
        )
        session.add(record)
        session.commit()
        logger.debug(f"DB: Saved trade — {side} {amount:.5f} {symbol} @ ${price:,.2f}")
    except Exception as e:
        session.rollback()
        logger.error(f"DB: Failed to save trade: {e}")
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────
#  EXCHANGE CONNECTION
# ──────────────────────────────────────────────────────────────────────

def create_exchange():
    """Create a ccxt Binance client pointed at the Testnet."""
    api_key = os.getenv('BINANCE_TESTNET_API_KEY')
    secret = os.getenv('BINANCE_TESTNET_SECRET_KEY')

    if not api_key or not secret:
        logger.error("Missing BINANCE_TESTNET_API_KEY or BINANCE_TESTNET_SECRET_KEY env vars.")
        sys.exit(1)

    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
        'timeout': 10000,
        'options': {'defaultType': 'spot'},
        'urls': {
            'api': {
                'public':  'https://testnet.binance.vision/api',
                'private': 'https://testnet.binance.vision/api',
            },
        },
    })
    exchange.set_sandbox_mode(True)
    return exchange

# ──────────────────────────────────────────────────────────────────────
#  FEATURE ENGINEERING (mirrors feature_engineer.py exactly)
# ──────────────────────────────────────────────────────────────────────

def compute_features(df):
    """
    Compute all 13 features the model was trained on.
    Operates in-place and returns the DataFrame.
    """
    # RSI (14)
    df['RSI_14'] = ta.momentum.RSIIndicator(close=df['close'], window=14).rsi()

    # MACD (12, 26, 9)
    macd_ind = ta.trend.MACD(close=df['close'])
    df['MACD'] = macd_ind.macd()
    df['MACD_signal'] = macd_ind.macd_signal()

    # Bollinger Bands (20)
    bb = ta.volatility.BollingerBands(close=df['close'], window=20)
    df['BB_high'] = bb.bollinger_hband()
    df['BB_low'] = bb.bollinger_lband()

    # ATR (14)
    df['ATR_14'] = ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'], window=14
    ).average_true_range()

    # SMA (50 & 200)
    df['SMA_50'] = df['close'].rolling(window=50).mean()
    df['SMA_200'] = df['close'].rolling(window=200).mean()

    return df

# ──────────────────────────────────────────────────────────────────────
#  DATA FETCHING
# ──────────────────────────────────────────────────────────────────────

def fetch_ohlcv(exchange):
    """Fetch OHLCV candles and return a DataFrame with features."""
    logger.debug(f"Fetching {OHLCV_LIMIT} x {TIMEFRAME} candles for {SYMBOL}...")

    raw = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=OHLCV_LIMIT)
    df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

    df = compute_features(df)
    df = df.dropna().reset_index(drop=True)

    logger.debug(f"Feature DataFrame ready: {len(df)} rows.")
    return df

# ──────────────────────────────────────────────────────────────────────
#  LIQUIDITY SWEEP / SMC DETECTION
# ──────────────────────────────────────────────────────────────────────

def find_swing_lows(df, lookback=SWING_LOOKBACK):
    """
    Identify Swing Lows (Sell-side Liquidity pools).
    A Swing Low at index i means df['low'][i] is the minimum of
    the surrounding 'lookback' candles on each side.
    Only uses confirmed (past) candles — never looks into the future.
    Returns a list of (index, price) tuples.
    """
    swing_lows = []
    # Stop at len-1 so the last candle (current/live) is excluded
    for i in range(lookback, len(df) - 1):
        window_low = df['low'].iloc[i]
        left = df['low'].iloc[i - lookback:i]
        right = df['low'].iloc[i + 1:i + 1 + lookback]
        if len(right) == 0:
            continue
        if window_low <= left.min() and window_low <= right.min():
            swing_lows.append((i, window_low))
    return swing_lows


def find_swing_highs(df, lookback=SWING_LOOKBACK):
    """
    Identify Swing Highs (Buy-side Liquidity pools).
    A Swing High at index i means df['high'][i] is the maximum of
    the surrounding 'lookback' candles on each side.
    Only uses confirmed (past) candles — never looks into the future.
    Returns a list of (index, price) tuples.
    """
    swing_highs = []
    for i in range(lookback, len(df) - 1):
        window_high = df['high'].iloc[i]
        left = df['high'].iloc[i - lookback:i]
        right = df['high'].iloc[i + 1:i + 1 + lookback]
        if len(right) == 0:
            continue
        if window_high >= left.max() and window_high >= right.max():
            swing_highs.append((i, window_high))
    return swing_highs


def detect_liquidity_sweep(df, n_recent=20):
    """
    Detect Liquidity Sweep signals on the latest candle.

    BUY signal:  Price swept below a recent Swing Low (wick below it)
                 but the candle CLOSED back above that level → rejection.

    SELL signal: Price swept above a recent Swing High (wick above it)
                 but the candle CLOSED back below that level → rejection.

    Returns (buy_signal: bool, sell_signal: bool, swept_level: float|None)
    """
    if len(df) < n_recent + SWING_LOOKBACK + 1:
        return False, False, None

    latest = df.iloc[-1]
    current_low = latest['low']
    current_high = latest['high']
    current_close = latest['close']

    # Only consider swing points from the last n_recent confirmed candles
    swing_lows = find_swing_lows(df, lookback=SWING_LOOKBACK)
    swing_highs = find_swing_highs(df, lookback=SWING_LOOKBACK)

    # Filter to recent swing lows (within last n_recent candles)
    recent_lows = [(idx, price) for idx, price in swing_lows
                   if idx >= len(df) - 1 - n_recent]
    recent_highs = [(idx, price) for idx, price in swing_highs
                    if idx >= len(df) - 1 - n_recent]

    buy_signal = False
    sell_signal = False
    swept_level = None

    # BUY: price wicked below a swing low but closed back above
    for _, sl_price in recent_lows:
        if current_low < sl_price and current_close > sl_price:
            buy_signal = True
            swept_level = sl_price
            break  # Take the first (most recent) sweep

    # SELL: price wicked above a swing high but closed back below
    if not buy_signal:
        for _, sh_price in recent_highs:
            if current_high > sh_price and current_close < sh_price:
                sell_signal = True
                swept_level = sh_price
                break

    return buy_signal, sell_signal, swept_level


# ──────────────────────────────────────────────────────────────────────
#  ORDER EXECUTION
# ──────────────────────────────────────────────────────────────────────

def execute_market_buy(exchange, usdt_amount):
    """Place a market buy for BTC/USDT using the given USDT amount."""
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = ticker['last']
        btc_qty = usdt_amount / price
        # Round down to Binance precision (5 decimals for BTC on testnet)
        btc_qty = float(f"{btc_qty:.5f}")

        logger.info(f"MARKET BUY  | {btc_qty} BTC @ ~${price:,.2f}  (${usdt_amount:,.2f} USDT)")
        order = exchange.create_market_buy_order(SYMBOL, btc_qty)
        logger.info(f"BUY ORDER FILLED  | ID: {order['id']}  Status: {order['status']}")
        return order
    except Exception as e:
        logger.error(f"BUY ORDER FAILED: {e}")
        return None


def execute_market_sell(exchange, btc_qty):
    """Place a market sell for BTC/USDT."""
    try:
        btc_qty = float(f"{btc_qty:.5f}")
        logger.info(f"MARKET SELL | {btc_qty} BTC")
        order = exchange.create_market_sell_order(SYMBOL, btc_qty)
        logger.info(f"SELL ORDER FILLED | ID: {order['id']}  Status: {order['status']}")
        return order
    except Exception as e:
        logger.error(f"SELL ORDER FAILED: {e}")
        return None

# ──────────────────────────────────────────────────────────────────────
#  MAIN TRADING LOOP
# ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 60)
    logger.info("   LIVE TRADING BOT — BINANCE TESTNET")
    logger.info("=" * 60)

    # Load model
    try:
        model = joblib.load(MODEL_PATH)
        logger.info(f"Loaded ML model from {MODEL_PATH}")
    except FileNotFoundError:
        logger.error(f"Model file {MODEL_PATH} not found. Run model_trainer.py first.")
        sys.exit(1)

    # Connect to exchange
    exchange = create_exchange()
    logger.info("Connected to Binance Testnet.")

    # Verify connectivity & log balance
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance['free'].get('USDT', 0)
        btc_free = balance['free'].get('BTC', 0)
        logger.info(f"Account balance — USDT: {usdt_free:,.2f}  |  BTC: {btc_free:.6f}")
        # Fetch initial price for equity calculation
        try:
            ticker = exchange.fetch_ticker(SYMBOL)
            init_price = ticker['last']
        except Exception:
            init_price = 0.0
        db_save_balance(usdt_free, btc_free, init_price)
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}")
        sys.exit(1)

    # Trading state
    in_position = False
    entry_price = 0.0
    highest_price = 0.0
    position_btc = 0.0
    trade_count = 0

    logger.info(f"Strategy: Confidence >= {CONFIDENCE_THRESHOLD*100:.0f}% OR Liquidity Sweep  |  "
                f"Trailing Stop: {TRAILING_STOP_PCT*100:.1f}%  |  "
                f"Hard SL: {STOP_LOSS_PCT*100:.1f}%")
    logger.info("Entering main loop...\n")

    while True:
        try:
            # ── Fetch fresh OHLCV + features ──
            df = fetch_ohlcv(exchange)
            if df.empty:
                logger.warning("Empty DataFrame after feature computation. Retrying...")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            # ── Get the latest candle's features ──
            latest = df.iloc[[-1]][FEATURE_COLS]
            current_price = df.iloc[-1]['close']
            confidence = model.predict_proba(latest)[:, 1][0]

            # ── Detect Liquidity Sweep (SMC) ──
            liq_buy, liq_sell, swept_level = detect_liquidity_sweep(df, n_recent=20)

            logger.debug(f"Price: ${current_price:,.2f}  |  Confidence: {confidence:.4f}  |  "
                         f"LiqBuy: {liq_buy}  LiqSell: {liq_sell}  |  "
                         f"In Position: {in_position}")

            # ── BUY LOGIC: ML confidence OR Liquidity Sweep ──
            ml_buy = confidence >= CONFIDENCE_THRESHOLD
            if (ml_buy or liq_buy) and not in_position:
                entry_reason = "ML_CONFIDENCE" if ml_buy else "LIQUIDITY_SWEEP"
                balance = exchange.fetch_balance()
                usdt_free = balance['free'].get('USDT', 0)

                if usdt_free < 10:
                    logger.warning(f"Insufficient USDT ({usdt_free:.2f}). Skipping buy.")
                else:
                    # Use 95% of available USDT (leave buffer for fees)
                    invest_amount = usdt_free * 0.95
                    order = execute_market_buy(exchange, invest_amount)

                    if order:
                        in_position = True
                        entry_price = current_price
                        highest_price = current_price
                        # Refresh balance to get actual BTC position
                        time.sleep(1)
                        balance = exchange.fetch_balance()
                        position_btc = balance['free'].get('BTC', 0)
                        trade_count += 1
                        sweep_info = (f"  Swept: ${swept_level:,.2f}"
                                      if swept_level else "")
                        logger.info(f"ENTERED LONG  |  Reason: {entry_reason}  |  "
                                    f"Entry: ${entry_price:,.2f}  |  "
                                    f"Position: {position_btc:.6f} BTC  |  "
                                    f"Confidence: {confidence:.2%}{sweep_info}")

                        # Log to PostgreSQL
                        buy_price = order.get('average', order.get('price', current_price))
                        buy_amount = order.get('amount', position_btc)
                        buy_cost = order.get('cost', invest_amount)
                        db_save_trade(
                            order_id=str(order['id']),
                            symbol=SYMBOL,
                            side='BUY',
                            price=float(buy_price) if buy_price else current_price,
                            amount=float(buy_amount) if buy_amount else position_btc,
                            cost=float(buy_cost) if buy_cost else invest_amount,
                            confidence=confidence,
                        )
                        usdt_after = balance['free'].get('USDT', 0)
                        db_save_balance(usdt_after, position_btc, current_price)

            # ── SELL LOGIC ──
            elif in_position:
                highest_price = max(highest_price, current_price)

                trailing_stop_price = highest_price * (1 - TRAILING_STOP_PCT)
                hard_stop_price = entry_price * (1 - STOP_LOSS_PCT)

                hit_ts = current_price <= trailing_stop_price
                hit_sl = current_price <= hard_stop_price

                if hit_ts or hit_sl:
                    exit_reason = "TRAILING STOP" if hit_ts else "HARD STOP LOSS"

                    # Refresh actual BTC balance before selling
                    balance = exchange.fetch_balance()
                    position_btc = balance['free'].get('BTC', 0)

                    if position_btc > 0:
                        order = execute_market_sell(exchange, position_btc)

                        if order:
                            pnl_pct = ((current_price - entry_price) / entry_price) * 100
                            logger.info(f"EXITED LONG   |  Reason: {exit_reason}  |  "
                                        f"Entry: ${entry_price:,.2f}  →  "
                                        f"Exit: ${current_price:,.2f}  |  "
                                        f"PnL: {pnl_pct:+.2f}%  |  "
                                        f"Highest: ${highest_price:,.2f}")

                            # Log to PostgreSQL
                            sell_price = order.get('average', order.get('price', current_price))
                            sell_amount = order.get('amount', position_btc)
                            sell_cost = order.get('cost', 0)
                            db_save_trade(
                                order_id=str(order['id']),
                                symbol=SYMBOL,
                                side='SELL',
                                price=float(sell_price) if sell_price else current_price,
                                amount=float(sell_amount) if sell_amount else position_btc,
                                cost=float(sell_cost) if sell_cost else 0.0,
                                confidence=None,
                            )
                            # Snapshot balance after sell
                            time.sleep(1)
                            balance = exchange.fetch_balance()
                            usdt_after = balance['free'].get('USDT', 0)
                            btc_after = balance['free'].get('BTC', 0)
                            db_save_balance(usdt_after, btc_after, current_price)

                            in_position = False
                            entry_price = 0.0
                            highest_price = 0.0
                            position_btc = 0.0
                    else:
                        logger.warning("Position BTC is 0 but in_position=True. Resetting state.")
                        in_position = False
                        entry_price = 0.0
                        highest_price = 0.0
                else:
                    logger.debug(f"HOLDING  |  Entry: ${entry_price:,.2f}  |  "
                                 f"Current: ${current_price:,.2f}  |  "
                                 f"Highest: ${highest_price:,.2f}  |  "
                                 f"TS@${trailing_stop_price:,.2f}  "
                                 f"SL@${hard_stop_price:,.2f}")

        except ccxt.NetworkError as e:
            logger.error(f"Network error: {e}. Retrying in {LOOP_INTERVAL_SEC}s...")
        except ccxt.ExchangeError as e:
            logger.error(f"Exchange error: {e}. Retrying in {LOOP_INTERVAL_SEC}s...")
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)

        time.sleep(LOOP_INTERVAL_SEC)


if __name__ == "__main__":
    main()
