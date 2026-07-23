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
import requests
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import (BINANCE_API_KEY, BINANCE_API_SECRET,
                    FUTURES_LEVERAGE, FUTURES_MARGIN_TYPE)

logger = logging.getLogger('FuturesExecutor')
logger.setLevel(logging.DEBUG)


def is_rate_limit_error(exc: Exception) -> bool:
    """Check if an exception is a Binance rate limit / IP ban error (-1003 or 418)."""
    if isinstance(exc, BinanceAPIException):
        if getattr(exc, 'code', None) in (-1003, 418) or getattr(exc, 'status_code', None) in (418, 429):
            return True
        msg = str(exc)
        if '-1003' in msg or '418' in msg or 'IP banned' in msg or 'way too many requests' in msg.lower():
            return True
    if isinstance(exc, requests.exceptions.HTTPError):
        if exc.response is not None and exc.response.status_code in (418, 429):
            return True
        msg = str(exc)
        if '418' in msg or '-1003' in msg or '429' in msg:
            return True
    msg = str(exc)
    if '-1003' in msg or '418' in msg or 'IP banned' in msg:
        return True
    return False


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
    """
    import os
    api_key = BINANCE_API_KEY or os.environ.get("BINANCE_API_KEY") or os.environ.get("TESTNET_5M_API_KEY") or os.environ.get("TESTNET_15M_API_KEY")
    api_secret = BINANCE_API_SECRET or os.environ.get("BINANCE_API_SECRET") or os.environ.get("TESTNET_5M_SECRET_KEY") or os.environ.get("TESTNET_15M_SECRET_KEY")

    if not api_key or not api_secret:
        raise RuntimeError(
            "BINANCE_API_KEY and BINANCE_API_SECRET must be set in the "
            "environment. Generate keys at https://testnet.binancefuture.com"
        )

    client = Client(
        api_key=api_key,
        api_secret=api_secret,
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
        if getattr(e, 'code', None) == -4131 or '-4131' in str(e):
            logger.warning(f"[{symbol}] ⚠️ Order rejected by Binance PERCENT_PRICE filter (-4131): price deviates beyond allowed threshold.")
        else:
            logger.error(f"[{symbol}] ❌ Failed to open {direction}: [{e.code}] {e.message}")
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
        if getattr(e, 'code', None) == -4131 or '-4131' in str(e):
            logger.warning(f"[{symbol}] ⚠️ Close order rejected by Binance PERCENT_PRICE filter (-4131): price deviates beyond allowed threshold.")
        else:
            logger.error(f"[{symbol}] ❌ Failed to close {direction}: [{e.code}] {e.message}")
        return None
    except Exception as e:
        logger.error(f"[{symbol}] ❌ Unexpected error closing {direction}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
#  POSITION & BALANCE QUERIES
# ──────────────────────────────────────────────────────────────────────

def fetch_all_positions(client: Client) -> dict[str, dict]:
    """
    Fetch all active Futures positions from Binance in a SINGLE API call.

    Returns:
        dict[str, dict]: A mapping of symbol -> position dict.
    """
    if not client:
        return {}

    try:
        raw_positions = client.futures_position_information()
        pos_dict = {}
        for pos in raw_positions:
            sym = pos.get('symbol')
            amt = float(pos.get('positionAmt', 0))
            if sym:
                pos_dict[sym] = {
                    'symbol': sym,
                    'size': abs(amt),
                    'direction': ('LONG' if amt > 0 else 'SHORT') if amt != 0 else None,
                    'entry_price': float(pos.get('entryPrice', 0)) if amt != 0 else 0.0,
                    'unrealized_pnl': float(pos.get('unRealizedProfit', 0)) if amt != 0 else 0.0,
                }
        return pos_dict
    except Exception as e:
        logger.error(f"Failed to fetch global position information: {e}")
        return {}


def get_position_info(client: Client, symbol: str, cached_positions: dict = None) -> dict:
    """
    Fetch current Futures position for a symbol.
    If `cached_positions` is provided, uses it to avoid API calls.

    Returns:
        dict with keys: symbol, size, direction, entry_price, unrealized_pnl
    """
    if cached_positions is not None:
        if symbol in cached_positions:
            return cached_positions[symbol]
        return {
            'symbol': symbol,
            'size': 0.0,
            'direction': None,
            'entry_price': 0.0,
            'unrealized_pnl': 0.0,
        }

    try:
        positions = client.futures_position_information(symbol=symbol)
        for pos in positions:
            amt = float(pos.get('positionAmt', 0))
            if amt != 0:
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
    except Exception as e:
        logger.error(f"[{symbol}] Failed to fetch position info from Binance API: {e}")
        return None


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
    except Exception as e:
        logger.error(f"Failed to fetch Futures balance: {e}")
        return 0.0


# ──────────────────────────────────────────────────────────────────────
#  GLOBAL POSITION COUNTER (Live Binance)
# ──────────────────────────────────────────────────────────────────────

def count_all_open_positions(client: Client, cached_positions: dict = None) -> int:
    """
    Count ALL open Futures positions on Binance (any symbol with positionAmt != 0).
    Uses `cached_positions` if provided to avoid extra API calls.
    """
    if cached_positions is not None:
        return sum(1 for pos in cached_positions.values() if pos.get('size', 0) > 0)
    try:
        positions = client.futures_position_information()
        return len([p for p in positions if float(p.get('positionAmt', 0)) != 0])
    except Exception as e:
        print(f"⚠️ Rate limit hit in count_all_open_positions: {e}")
        return 6  # Fallback to MAX_GLOBAL_POSITIONS to prevent new entries safely


# ──────────────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────────────

def _round_quantity(client: Client, symbol: str, raw_qty: float) -> float:
    """
    Round a quantity to the exchange-allowed step size (precision)
    for a given Futures symbol.
    """
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        if step_size > 0:
                            # Floor to the nearest step
                            precision = len(f['stepSize'].rstrip('0').split('.')[-1]) \
                                if '.' in f['stepSize'] else 0
                            rounded = round(raw_qty - (raw_qty % step_size), precision)
                            return rounded
        # Fallback: 5 decimal places
        return round(raw_qty, 5)
    except Exception as e:
        logger.warning(f"[{symbol}] Could not fetch step size, using 5 decimals: {e}")
        return round(raw_qty, 5)
