"""
futures_executor.py — Binance Futures Testnet Execution Engine
===============================================================
Centralizes all Futures order execution logic:
  • Client creation (python-binance → Futures Testnet)
  • Per-symbol setup (ISOLATED margin, 1x leverage)
  • Position open/close (LONG ↔ BUY, SHORT ↔ SELL)
  • Position info & balance queries

Used by analyzer.py and main.py.  Both the 5m and 15m engine
containers share this module.
"""

import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import (BINANCE_API_KEY, BINANCE_API_SECRET,
                    FUTURES_LEVERAGE, FUTURES_MARGIN_TYPE)

logger = logging.getLogger('FuturesExecutor')
logger.setLevel(logging.DEBUG)

# Ensure at least a console handler exists
if not logger.handlers:
    import sys
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
        datefmt='%H:%M:%S',
    ))
    logger.addHandler(_ch)


# ──────────────────────────────────────────────────────────────────────
#  CLIENT CREATION
# ──────────────────────────────────────────────────────────────────────

def create_futures_client() -> Client:
    """
    Create a python-binance Client configured for Binance Futures Testnet.

    Reads API key/secret from config.py (which reads from env vars).
    Sets testnet=True so the client targets testnet.binancefuture.com.
    """
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        raise RuntimeError(
            "BINANCE_API_KEY and BINANCE_API_SECRET must be set in the "
            "environment.  Generate keys at https://testnet.binancefuture.com"
        )

    client = Client(
        api_key=BINANCE_API_KEY,
        api_secret=BINANCE_API_SECRET,
        testnet=True,
    )
    logger.info("✅ Futures Testnet client created (testnet=True)")
    return client


# ──────────────────────────────────────────────────────────────────────
#  PER-SYMBOL SETUP
# ──────────────────────────────────────────────────────────────────────

def setup_symbol(client: Client, symbol: str) -> None:
    """
    Ensure a symbol is configured for trading:
      1. Set margin type to ISOLATED (skip if already set)
      2. Set leverage to 1x

    Must be called before the first order on any symbol.
    Safe to call repeatedly — idempotent.
    """
    # ── 1. Margin type ──
    try:
        client.futures_change_margin_type(symbol=symbol,
                                          marginType=FUTURES_MARGIN_TYPE)
        logger.info(f"[{symbol}] Margin type set to {FUTURES_MARGIN_TYPE}")
    except BinanceAPIException as e:
        # Code -4046: "No need to change margin type."
        if e.code == -4046:
            logger.debug(f"[{symbol}] Margin type already {FUTURES_MARGIN_TYPE}")
        else:
            logger.error(f"[{symbol}] Failed to set margin type: {e}")
            raise

    # ── 2. Leverage ──
    try:
        resp = client.futures_change_leverage(symbol=symbol,
                                              leverage=FUTURES_LEVERAGE)
        actual = resp.get('leverage', FUTURES_LEVERAGE)
        logger.info(f"[{symbol}] Leverage set to {actual}x")
    except BinanceAPIException as e:
        logger.error(f"[{symbol}] Failed to set leverage: {e}")
        raise


# ──────────────────────────────────────────────────────────────────────
#  OPEN POSITION
# ──────────────────────────────────────────────────────────────────────

def open_position(client: Client, symbol: str, direction: str,
                  usdt_amount: float) -> dict | None:
    """
    Open a Futures position.

    Args:
        client:      python-binance Client
        symbol:      e.g. "BTCUSDT"
        direction:   "LONG" or "SHORT"
        usdt_amount: How much USDT notional to commit

    Returns:
        Order dict on success, None on failure.

    Mechanics:
        LONG  → SIDE_BUY   (market)
        SHORT → SIDE_SELL  (market)
    """
    direction = direction.upper()
    if direction not in ('LONG', 'SHORT'):
        logger.error(f"[{symbol}] Invalid direction '{direction}' — must be LONG or SHORT")
        return None

    try:
        # Ensure symbol is configured
        setup_symbol(client, symbol)

        # Fetch mark price to calculate quantity
        mark_data = client.futures_mark_price(symbol=symbol)
        mark_price = float(mark_data['markPrice'])
        if mark_price <= 0:
            logger.error(f"[{symbol}] Invalid mark price: {mark_price}")
            return None

        # Calculate quantity (raw — will be rounded by exchange info)
        raw_qty = usdt_amount / mark_price

        # Fetch exchange info for precision
        quantity = _round_quantity(client, symbol, raw_qty)
        if quantity <= 0:
            logger.error(f"[{symbol}] Calculated quantity is 0 after rounding")
            return None

        side = Client.SIDE_BUY if direction == 'LONG' else Client.SIDE_SELL

        logger.info(f"[{symbol}] OPENING {direction} | Side: {side} | "
                    f"Qty: {quantity} | Mark: ${mark_price:,.2f} | "
                    f"Notional: ~${usdt_amount:,.2f}")

        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type='MARKET',
            quantity=quantity,
        )

        logger.info(f"[{symbol}] ✅ {direction} OPENED | OrderID: {order['orderId']} | "
                    f"Status: {order['status']}")
        return order

    except BinanceAPIException as e:
        logger.error(f"[{symbol}] ❌ Failed to open {direction}: "
                     f"[{e.code}] {e.message}")
        return None
    except Exception as e:
        logger.error(f"[{symbol}] ❌ Unexpected error opening {direction}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
#  CLOSE POSITION
# ──────────────────────────────────────────────────────────────────────

def close_position(client: Client, symbol: str, direction: str,
                   quantity: float) -> dict | None:
    """
    Close an existing Futures position.

    Args:
        client:    python-binance Client
        symbol:    e.g. "BTCUSDT"
        direction: "LONG" or "SHORT" (the direction being CLOSED)
        quantity:  Position size to close

    Returns:
        Order dict on success, None on failure.

    Mechanics:
        Close LONG  → SIDE_SELL  (market)
        Close SHORT → SIDE_BUY   (market)
    """
    direction = direction.upper()
    if direction not in ('LONG', 'SHORT'):
        logger.error(f"[{symbol}] Invalid direction '{direction}' — must be LONG or SHORT")
        return None

    try:
        quantity = _round_quantity(client, symbol, quantity)
        if quantity <= 0:
            logger.error(f"[{symbol}] Quantity is 0 after rounding — nothing to close")
            return None

        # Opposite side to close
        side = Client.SIDE_SELL if direction == 'LONG' else Client.SIDE_BUY

        logger.info(f"[{symbol}] CLOSING {direction} | Side: {side} | Qty: {quantity}")

        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type=Client.ORDER_TYPE_MARKET,
            quantity=quantity,
            reduceOnly=True,
        )

        logger.info(f"[{symbol}] ✅ {direction} CLOSED | OrderID: {order['orderId']} | "
                    f"Status: {order['status']}")
        return order

    except BinanceAPIException as e:
        logger.error(f"[{symbol}] ❌ Failed to close {direction}: "
                     f"[{e.code}] {e.message}")
        return None
    except Exception as e:
        logger.error(f"[{symbol}] ❌ Unexpected error closing {direction}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
#  POSITION & BALANCE QUERIES
# ──────────────────────────────────────────────────────────────────────

def get_position_info(client: Client, symbol: str) -> dict:
    """
    Fetch current Futures position for a symbol.

    Returns:
        dict with keys: symbol, size, direction, entry_price, unrealized_pnl
        size=0 means no open position.
    """
    try:
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            amt = float(pos.get('positionAmt', 0))
            if amt != 0:
                mark_price = float(pos.get('markPrice') or pos.get('entryPrice') or 0)
                if abs(amt) * mark_price < 2.0:
                    logger.debug(f"[{symbol}] Ignoring micro/dust position: amt={amt}, val=${abs(amt)*mark_price:.2f}")
                    continue
                return {
                    'symbol': symbol,
                    'size': abs(amt),
                    'direction': 'LONG' if amt > 0 else 'SHORT',
                    'entry_price': float(pos.get('entryPrice', 0)),
                    'unrealized_pnl': float(pos.get('unRealizedProfit', 0)),
                }
        # No open position
        return {
            'symbol': symbol,
            'size': 0.0,
            'direction': None,
            'entry_price': 0.0,
            'unrealized_pnl': 0.0,
        }
    except BinanceAPIException as e:
        logger.error(f"[{symbol}] Failed to fetch position info: [{e.code}] {e.message}")
        return {'symbol': symbol, 'size': 0.0, 'direction': None,
                'entry_price': 0.0, 'unrealized_pnl': 0.0}
    except Exception as e:
        logger.error(f"[{symbol}] Unexpected error fetching position info: {e}")
        return {'symbol': symbol, 'size': 0.0, 'direction': None,
                'entry_price': 0.0, 'unrealized_pnl': 0.0}


def get_futures_balance(client: Client) -> float:
    """
    Fetch available USDT balance from Futures account.

    Returns:
        Available USDT balance as float, or 0.0 on error.
    """
    try:
        balances = client.futures_account_balance()
        for b in balances:
            if b['asset'] == 'USDT':
                available = float(b.get('availableBalance', 0))
                logger.debug(f"Futures USDT balance: ${available:,.2f}")
                return available
        logger.warning("No USDT asset found in Futures account balance")
        return 0.0
    except BinanceAPIException as e:
        logger.error(f"Failed to fetch Futures balance: [{e.code}] {e.message}")
        return 0.0
    except Exception as e:
        logger.error(f"Unexpected error fetching Futures balance: {e}")
        return 0.0


# ──────────────────────────────────────────────────────────────────────
#  GLOBAL POSITION COUNTER (Live Binance)
# ──────────────────────────────────────────────────────────────────────

def count_all_open_positions(client: Client) -> int:
    """
    Count ALL open Futures positions on Binance (any symbol with positionAmt != 0).
    This is the source-of-truth counter that prevents race conditions
    vs. counting from the local DB.

    Returns:
        int: number of symbols with an active position.
    """
    try:
        positions = client.futures_position_information()
        count = 0
        for pos in positions:
            amt = float(pos.get('positionAmt', 0))
            if amt != 0:
                mark_price = float(pos.get('markPrice') or pos.get('entryPrice') or 0)
                if abs(amt) * mark_price >= 2.0:
                    count += 1
                else:
                    logger.debug(f"Ignoring dust in count: symbol={pos.get('symbol')}, amt={amt}, val=${abs(amt)*mark_price:.2f}")
        logger.debug(f"Live Binance open positions: {count}")
        return count
    except BinanceAPIException as e:
        logger.error(f"Failed to count open positions: [{e.code}] {e.message}")
        return 999  # Fail-safe: assume max so we don't open more
    except Exception as e:
        logger.error(f"Unexpected error counting open positions: {e}")
        return 999  # Fail-safe


# ──────────────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────────────

def _round_quantity(client: Client, symbol: str, raw_qty: float) -> float:
    """
    Round a quantity strictly to the exchange-allowed step size (precision)
    for a given Futures symbol without floating-point modulo artifacts.
    """
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        if step_size > 0:
                            precision = len(f['stepSize'].rstrip('0').split('.')[-1]) if '.' in f['stepSize'] else 0
                            import math
                            steps = math.floor(round(raw_qty / step_size, 8))
                            rounded = round(steps * step_size, precision)
                            return float(rounded)
        # Fallback: 5 decimal places
        return round(raw_qty, 5)
    except Exception as e:
        logger.warning(f"[{symbol}] Could not fetch step size, using 5 decimals: {e}")
        return round(raw_qty, 5)


def _round_price(client: Client, symbol: str, raw_price: float) -> float:
    """
    Round a price strictly to the exchange-allowed tick size (precision)
    for a given Futures symbol without floating-point modulo artifacts.
    """
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'PRICE_FILTER':
                        tick_size = float(f['tickSize'])
                        if tick_size > 0:
                            precision = len(f['tickSize'].rstrip('0').split('.')[-1]) if '.' in f['tickSize'] else 0
                            steps = round(raw_price / tick_size)
                            rounded = round(steps * tick_size, precision)
                            return float(rounded)
        # Fallback: 2 decimal places
        return round(raw_price, 2)
    except Exception as e:
        logger.warning(f"[{symbol}] Could not fetch tick size, using 2 decimals: {e}")
        return round(raw_price, 2)


def set_stop_loss_order(client: Client, symbol: str, direction: str, stop_price: float) -> dict | None:
    """
    Cancel existing open orders (e.g. old SL) and place a new STOP_MARKET reduce-only order.
    """
    direction = direction.upper()
    if direction not in ('LONG', 'SHORT'):
        logger.error(f"[{symbol}] Invalid direction '{direction}' for SL")
        return None

    try:
        # 1. Cancel existing orders (to clear old SLs, both normal and conditional algo orders)
        try:
            client.futures_cancel_all_open_orders(symbol=symbol)
        except Exception as err:
            logger.debug(f"[{symbol}] Note canceling normal open orders: {err}")
        try:
            if hasattr(client, 'futures_cancel_all_algo_open_orders'):
                client.futures_cancel_all_algo_open_orders(symbol=symbol)
        except Exception as err:
            logger.debug(f"[{symbol}] Note canceling algo open orders: {err}")

        # 2. Format the stop price strictly to symbol's tick size precision
        rounded_price = _round_price(client, symbol, stop_price)
        
        # 3. Determine side (close LONG = SELL, close SHORT = BUY)
        side = Client.SIDE_SELL if direction == 'LONG' else Client.SIDE_BUY
        
        logger.info(f"[{symbol}] SETTING STOP LOSS {direction} | Side: {side} | Stop Price: {rounded_price}")

        # 4. Place order wrapped in try-except with safe error handling
        try:
            order = client.futures_create_order(
                symbol=symbol,
                side=side,
                type='STOP_MARKET',
                stopPrice=rounded_price,
                closePosition='true'
            )
        except BinanceAPIException as api_err:
            logger.error(
                f"[{symbol}] ❌ Binance API rejected SL order (status {api_err.status_code}): "
                f"[{api_err.code}] {api_err.message} | Raw Response: {getattr(api_err, 'response', 'N/A')}"
            )
            return None
        except Exception as exec_err:
            logger.error(f"[{symbol}] ❌ Exception during SL futures_create_order: {type(exec_err).__name__} - {exec_err}")
            return None

        order_id = order.get('orderId') or order.get('algoId')
        status = order.get('status') or order.get('algoStatus', 'UNKNOWN')

        if not order or not isinstance(order, dict) or not order_id:
            logger.error(f"[{symbol}] ❌ SL order failed or orderId/algoId missing in response. Exact raw response: {order}")
            return None

        logger.info(f"[{symbol}] ✅ STOP LOSS SET | OrderID/AlgoID: {order_id} | Status: {status}")
        return order

    except Exception as e:
        logger.error(f"[{symbol}] ❌ Unexpected error setting Stop Loss: {type(e).__name__} - {e}")
        return None
