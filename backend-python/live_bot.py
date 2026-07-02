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

# ──────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
SYMBOL = 'BTC/USDT'
TIMEFRAME = '1m'
OHLCV_LIMIT = 250          # Candles to fetch (enough for SMA_200 warmup)
CONFIDENCE_THRESHOLD = 0.05   # TEMPORARY: forced trade test (revert to 0.65)
TRAILING_STOP_PCT = 0.015   # 1.5%
STOP_LOSS_PCT = 0.01        # 1%
TRADING_FEE = 0.001         # 0.1% Binance Spot fee
LOOP_INTERVAL_SEC = 60      # Poll every 60 seconds (1m candle)
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
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}")
        sys.exit(1)

    # Trading state
    in_position = False
    entry_price = 0.0
    highest_price = 0.0
    position_btc = 0.0
    trade_count = 0

    logger.info(f"Strategy: Confidence >= {CONFIDENCE_THRESHOLD*100:.0f}%  |  "
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

            logger.debug(f"Price: ${current_price:,.2f}  |  Confidence: {confidence:.4f}  |  "
                         f"In Position: {in_position}")

            # ── BUY LOGIC ──
            if confidence >= CONFIDENCE_THRESHOLD and not in_position:
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
                        logger.info(f"ENTERED LONG  |  Entry: ${entry_price:,.2f}  |  "
                                    f"Position: {position_btc:.6f} BTC  |  "
                                    f"Confidence: {confidence:.2%}")

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
