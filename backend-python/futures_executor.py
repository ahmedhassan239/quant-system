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

import os
import requests
import logging
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import (BINANCE_API_KEY, BINANCE_API_SECRET,
                    FUTURES_LEVERAGE, FUTURES_MARGIN_TYPE,
                    STOP_LIMIT_SLIPPAGE_CAP)

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
#  SESSION IN-MEMORY BLACKLIST (Auto-blacklisting for API restrictions)
# ──────────────────────────────────────────────────────────────────────
SESSION_BLACKLIST: set[str] = set()

def is_symbol_blacklisted(symbol: str) -> bool:
    """Check if a symbol is in-memory blacklisted for this session."""
    return symbol in SESSION_BLACKLIST

def add_to_session_blacklist(symbol: str, reason: str = "") -> None:
    """Add a symbol to the session blacklist and log a warning."""
    if symbol not in SESSION_BLACKLIST:
        SESSION_BLACKLIST.add(symbol)
        logger.warning(f"🚫 [{symbol}] AUTO-BLACKLISTED for session ({reason}). Skipping further operations on this symbol.")

def _check_api_exception_for_blacklist(symbol: str, e: BinanceAPIException) -> bool:
    """
    Check if a BinanceAPIException code indicates a symbol restriction (-4411, -2027)
    and auto-blacklist it for the session.

    Error Codes:
      -4411: TradFi-Perps / region agreement required or restricted contract
      -2027: Exceeds max allowable position for symbol / account
    """
    if e.code in (-4411, -2027):
        add_to_session_blacklist(symbol, f"Binance error code {e.code}: {e.message}")
        return True
    return False


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
      2. Set leverage to FUTURES_LEVERAGE (1x default, easily configurable via ENV)

    Must be called before the first order on any symbol.
    Safe to call repeatedly — idempotent.
    """
    if is_symbol_blacklisted(symbol):
        logger.warning(f"[{symbol}] Skipping setup_symbol — symbol is in SESSION_BLACKLIST.")
        return

    # ── 1. Margin type ──
    try:
        client.futures_change_margin_type(symbol=symbol,
                                          marginType=FUTURES_MARGIN_TYPE)
        logger.info(f"[{symbol}] Margin type set to {FUTURES_MARGIN_TYPE}")
    except BinanceAPIException as e:
        # Code -4046: "No need to change margin type."
        # Code -4067: "Position side cannot be changed with open orders."
        #   → Safe to ignore: margin is already configured, proceed to entry.
        if e.code in (-4046, -4067):
            logger.debug(f"[{symbol}] Margin type already {FUTURES_MARGIN_TYPE} (code {e.code})")
        elif _check_api_exception_for_blacklist(symbol, e):
            logger.warning(f"[{symbol}] Margin setup restricted [{e.code}]. Auto-blacklisted for session.")
            raise
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
        # Code -4067: same scenario — open orders prevent changes, safe to proceed.
        if e.code == -4067:
            logger.debug(f"[{symbol}] Leverage unchanged (open orders exist, code -4067). Proceeding.")
        elif _check_api_exception_for_blacklist(symbol, e):
            logger.warning(f"[{symbol}] Leverage setup restricted [{e.code}]. Auto-blacklisted for session.")
            raise
        else:
            logger.error(f"[{symbol}] Failed to set leverage: {e}")
            raise


# ──────────────────────────────────────────────────────────────────────
#  EXECUTION ENGINE HELPER
# ──────────────────────────────────────────────────────────────────────

def execute_safe_market_order(client: Client, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> dict:
    """
    Slippage-protected execution. Checks the order book spread.
    If expected slippage > 0.5%, it places a GTC LIMIT order instead of MARKET.
    """
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        current_price = float(ticker['price'])
        
        ob = client.futures_order_book(symbol=symbol, limit=5)
        best_bid = float(ob['bids'][0][0])
        best_ask = float(ob['asks'][0][0])
        
        if side == Client.SIDE_SELL:
            expected_slippage = (current_price - best_bid) / current_price
            limit_price = best_ask
        else:
            expected_slippage = (best_ask - current_price) / current_price
            limit_price = best_bid

        if expected_slippage > 0.005:
            logger.warning(f"[{symbol}] ⚠️ Slippage protection triggered! Expected: {expected_slippage:.2%} > 0.5%. Falling back to LIMIT order at {limit_price}.")
            return client.futures_create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_LIMIT,
                timeInForce='GTC',
                price=limit_price,
                quantity=quantity,
                reduceOnly=reduce_only,
            )
        else:
            return client.futures_create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_MARKET,
                quantity=quantity,
                reduceOnly=reduce_only,
            )
    except BinanceAPIException:
        raise
    except Exception as slip_err:
        logger.error(f"[{symbol}] Error calculating slippage, defaulting to MARKET: {slip_err}")
        return client.futures_create_order(
            symbol=symbol,
            side=side,
            type=Client.ORDER_TYPE_MARKET,
            quantity=quantity,
            reduceOnly=reduce_only,
        )

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
    if is_symbol_blacklisted(symbol):
        logger.warning(f"[{symbol}] Skipping open_position — symbol is in SESSION_BLACKLIST.")
        return None

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

        # Pre-validation for Pyramiding/Scale-In: Check if notional value is >= $5
        notional_value = quantity * mark_price
        if notional_value < 5.0:
            logger.warning(f"[{symbol}] Pyramid order skipped: Notional value < $5 (Calculated: ${notional_value:.2f})")
            return None

        side = Client.SIDE_BUY if direction == 'LONG' else Client.SIDE_SELL

        logger.info(f"[{symbol}] OPENING {direction} | Side: {side} | "
                    f"Qty: {quantity} | Mark: ${mark_price:,.2f} | "
                    f"Notional: ~${usdt_amount:,.2f}")

        order = execute_safe_market_order(
            client=client,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reduce_only=False
        )

        logger.info(f"[{symbol}] ✅ {direction} OPENED | OrderID: {order['orderId']} | "
                    f"Status: {order['status']}")
        return order

    except BinanceAPIException as e:
        if _check_api_exception_for_blacklist(symbol, e):
            logger.warning(f"[{symbol}] Gracefully caught API restriction [{e.code}] during open_position. Auto-blacklisted for session.")
        else:
            logger.error(f"[{symbol}] ❌ Failed to open {direction}: [{e.code}] {e.message}")
        return None
    except Exception as e:
        logger.error(f"[{symbol}] ❌ Unexpected error opening {direction}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
#  CLOSE POSITION
# ──────────────────────────────────────────────────────────────────────

def _cancel_all_symbol_orders(client: Client, symbol: str) -> None:
    """
    Safely cancel all open normal and algo orders for a symbol.
    Prevents Binance API error [-2022] ReduceOnly Order is rejected.
    """
    try:
        client.futures_cancel_all_open_orders(symbol=symbol)
    except Exception as err:
        logger.debug(f"[{symbol}] Note canceling open orders: {err}")
    try:
        if hasattr(client, 'futures_cancel_all_algo_open_orders'):
            client.futures_cancel_all_algo_open_orders(symbol=symbol)
    except Exception as err:
        logger.debug(f"[{symbol}] Note canceling algo open orders: {err}")


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

        # ── Clear existing open orders (e.g. SL) to prevent -2022 ReduceOnly rejection ──
        _cancel_all_symbol_orders(client, symbol)

        # Opposite side to close
        side = Client.SIDE_SELL if direction == 'LONG' else Client.SIDE_BUY

        logger.info(f"[{symbol}] CLOSING {direction} | Side: {side} | Qty: {quantity}")

        order = execute_safe_market_order(
            client=client,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reduce_only=True
        )

        logger.info(f"[{symbol}] ✅ {direction} CLOSED | OrderID: {order['orderId']} | "
                    f"Status: {order['status']}")
        return order

    except BinanceAPIException as e:
        if e.code in (-2022, -4509):
            logger.warning(f"[{symbol}] Position already closed by Binance (ghost position). Handling gracefully.")
            return True
        elif _check_api_exception_for_blacklist(symbol, e):
            logger.warning(f"[{symbol}] Gracefully caught API restriction [{e.code}] during close_position.")
        else:
            logger.error(f"[{symbol}] ❌ Failed to close {direction}: [{e.code}] {e.message}")
        return None
    except Exception as e:
        logger.error(f"[{symbol}] ❌ Unexpected error closing {direction}: {e}")
        return None


def execute_partial_tp_scaleout(client: Client, symbol: str, direction: str,
                                total_quantity: float, entry_price: float,
                                trailing_distance: float = None, atr_val: float = None) -> tuple[dict | None, float]:
    """
    Execute a 50% partial Take Profit (scale-out) market close and immediately
    move the Stop Loss for the remaining position to Entry Price (Break-Even).
    """
    direction = direction.upper()
    if direction not in ('LONG', 'SHORT'):
        logger.error(f"[{symbol}] Invalid direction '{direction}' for partial TP")
        return None, total_quantity

    if (total_quantity <= 0 or not total_quantity) and client:
        live_pos = get_position_info(client, symbol)
        if live_pos and live_pos.get('size', 0) > 0:
            total_quantity = float(live_pos['size'])

    qty_to_close = _round_quantity(client, symbol, total_quantity * 0.5)
    if qty_to_close <= 0:
        logger.error(f"[{symbol}] 50% partial close quantity is 0 after rounding — cannot scale out")
        return None, total_quantity

    remaining_qty = _round_quantity(client, symbol, total_quantity - qty_to_close)
    if remaining_qty <= 0:
        logger.error(f"[{symbol}] Remaining quantity would be 0 after closing 50% — cannot scale out")
        return None, total_quantity

    side = Client.SIDE_SELL if direction == 'LONG' else Client.SIDE_BUY
    logger.info(f"[{symbol}] 🎯 EXECUTING PARTIAL TAKE PROFIT (50%) | Direction: {direction} | Closing Qty: {qty_to_close} | Remaining Qty: {remaining_qty}")

    # ── Clear existing open orders (e.g. SL covering 100% position) to prevent -2022 ReduceOnly rejection ──
    _cancel_all_symbol_orders(client, symbol)

    # Add a small delay to ensure Binance matching engine clears the order quota
    import time
    time.sleep(0.5)

    try:
        order = execute_safe_market_order(
            client=client,
            symbol=symbol,
            side=side,
            quantity=qty_to_close,
            reduce_only=True
        )
    except BinanceAPIException as api_err:
        if api_err.code in (-2022, -4509):
            logger.warning(f"[{symbol}] TP rejected — Position already closed by Binance (ghost position). Handling gracefully.")
            return True, remaining_qty
        elif _check_api_exception_for_blacklist(symbol, api_err):
            logger.warning(f"[{symbol}] Gracefully caught Partial TP API restriction [{api_err.code}]. Auto-blacklisted for session.")
        else:
            logger.error(f"[{symbol}] ❌ Binance API rejected Partial TP order (status {api_err.status_code}): [{api_err.code}] {api_err.message}")
        return None, total_quantity
    except Exception as exec_err:
        logger.error(f"[{symbol}] ❌ Exception during Partial TP market order: {exec_err}")
        return None, total_quantity

    order_id = order.get('orderId') or order.get('algoId')
    if not order or not isinstance(order, dict) or not order_id:
        logger.error(f"[{symbol}] ❌ Partial TP market order failed or orderId missing: {order}")
        return None, total_quantity

    logger.info(f"[{symbol}] ✅ PARTIAL TP EXECUTED | OrderID: {order_id} | Status: {order.get('status', 'UNKNOWN')}")

    # Auto Break-Even: immediately set stop loss for remaining position to entry_price
    logger.info(f"[{symbol}] 🛡️ AUTO BREAK-EVEN: Moving Stop Loss for remaining {remaining_qty} to Entry Price ${entry_price:,.4f}")
    sl_order = set_stop_loss_order(client, symbol, direction, entry_price, trailing_distance=trailing_distance, atr_val=atr_val, quantity=remaining_qty)
    if not sl_order:
        logger.warning(f"[{symbol}] ⚠️ Note: Could not immediately set Break-Even stop loss order after partial TP.")
    else:
        logger.info(f"[{symbol}] ✅ BREAK-EVEN STOP LOSS LOCKED AT ${entry_price:,.4f}")

    return order, remaining_qty


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
    Also ensures the quantity is bounded by minQty and maxQty limits.
    """
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        min_qty = float(f['minQty'])
                        max_qty = float(f['maxQty'])
                        if step_size > 0:
                            import math
                            # Bound the quantity to Binance minimum and maximum limits
                            bounded_qty = max(min_qty, min(raw_qty, max_qty))
                            precision = len(f['stepSize'].rstrip('0').split('.')[-1]) if '.' in f['stepSize'] else 0
                            steps = math.floor(round(bounded_qty / step_size, 8))
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


def set_stop_loss_order(client: Client, symbol: str, direction: str, stop_price: float,
                        trailing_distance: float = None, atr_val: float = None,
                        quantity: float = None) -> dict | None:
    """
    Cancel existing open orders (e.g. old SL) and place a new STOP (Stop-Limit)
    reduce-only order with a slippage-capped limit price.

    FLASH-CRASH IMMUNITY:
      Instead of STOP_MARKET (which executes at ANY price and causes 50-90%
      slippage on illiquid Testnet), we place a STOP (Stop-Limit) order where:
        - stopPrice = the trigger price (when the mark price crosses this, the
                      limit order is placed on the book)
        - price     = the worst acceptable execution price, calculated as:
            LONG close (SELL):  price = stopPrice * (1 - STOP_LIMIT_SLIPPAGE_CAP)
            SHORT close (BUY):  price = stopPrice * (1 + STOP_LIMIT_SLIPPAGE_CAP)

      If the flash crash blows through the limit price, the order simply
      does NOT fill (stays open as a limit order) — protecting the account.
      The analyzer's Virtual Soft-Stop loop will catch it on the next tick.

    VIRTUAL SOFT-STOP FALLBACK:
      If Binance rejects the Stop-Limit (margin issues, etc.), this function
      returns a virtual marker dict {'virtual': True, ...} so the caller
      knows to monitor the price in software and trigger
      execute_safe_market_order() if breached.

    Supports dynamic ATR trailing stop loss tracking.
    """
    direction = direction.upper()
    if direction not in ('LONG', 'SHORT'):
        logger.error(f"[{symbol}] Invalid direction '{direction}' for SL")
        return None

    try:
        # 1. Cancel existing orders (to clear old SLs, both normal and conditional algo orders)
        _cancel_all_symbol_orders(client, symbol)

        # 2. Format the stop price strictly to symbol's tick size precision
        rounded_stop_price = _round_price(client, symbol, stop_price)

        # 3. Calculate slippage-capped limit execution price
        #    LONG → SELL to close → limit price BELOW stop (worst-case sell price)
        #    SHORT → BUY to close → limit price ABOVE stop (worst-case buy price)
        if direction == 'LONG':
            raw_limit_price = rounded_stop_price * (1 - STOP_LIMIT_SLIPPAGE_CAP)
        else:
            raw_limit_price = rounded_stop_price * (1 + STOP_LIMIT_SLIPPAGE_CAP)
        rounded_limit_price = _round_price(client, symbol, raw_limit_price)

        # 4. Determine side (close LONG = SELL, close SHORT = BUY)
        side = Client.SIDE_SELL if direction == 'LONG' else Client.SIDE_BUY

        # 5. Resolve quantity — STOP (Stop-Limit) does NOT support closePosition,
        #    so we must always provide an explicit quantity.
        if not quantity or quantity <= 0:
            try:
                live_pos = get_position_info(client, symbol)
                if live_pos and live_pos.get('size', 0) > 0:
                    quantity = live_pos['size']
                else:
                    logger.warning(f"[{symbol}] Cannot set SL: no quantity provided and no live position found.")
                    return None
            except Exception as pos_err:
                logger.error(f"[{symbol}] Cannot resolve position size for SL: {pos_err}")
                return None

        quantity = _round_quantity(client, symbol, quantity)
        if quantity <= 0:
            logger.error(f"[{symbol}] Quantity is 0 after rounding — cannot set SL")
            return None

        if atr_val is not None and trailing_distance is not None:
            logger.info(
                f"[{symbol}] 📐 SETTING DYNAMIC ATR STOP-LIMIT {direction} | Side: {side} | "
                f"Stop Trigger: ${rounded_stop_price} | Limit Price: ${rounded_limit_price} | "
                f"Slippage Cap: {STOP_LIMIT_SLIPPAGE_CAP*100:.1f}% | "
                f"ATR(14): {atr_val:.4f} | Trail Dist: {trailing_distance:.4f}"
            )
        else:
            logger.info(
                f"[{symbol}] 🛡️ SETTING STOP-LIMIT {direction} | Side: {side} | "
                f"Stop Trigger: ${rounded_stop_price} | Limit Price: ${rounded_limit_price} | "
                f"Slippage Cap: {STOP_LIMIT_SLIPPAGE_CAP*100:.1f}% | Qty: {quantity}"
            )

        # 6. Place STOP (Stop-Limit) order — flash-crash immune
        try:
            order_params = {
                'symbol': symbol,
                'side': side,
                'type': 'STOP',               # ← Stop-Limit (NOT STOP_MARKET)
                'stopPrice': rounded_stop_price,
                'price': rounded_limit_price,  # ← Slippage-capped limit price
                'timeInForce': 'GTC',
                'quantity': quantity,
                'reduceOnly': True,
            }

            order = client.futures_create_order(**order_params)
        except BinanceAPIException as api_err:
            if _check_api_exception_for_blacklist(symbol, api_err):
                logger.warning(f"[{symbol}] Gracefully caught SL API restriction [{api_err.code}]. Auto-blacklisted for session.")
                return None
            else:
                # ── VIRTUAL SOFT-STOP FALLBACK ──
                # Binance rejected the Stop-Limit (margin, notional, or other issue).
                # Return a virtual marker so the analyzer loop monitors mark price
                # and triggers execute_safe_market_order() if breached.
                logger.warning(
                    f"[{symbol}] ⚠️ Binance rejected Stop-Limit [{api_err.code}]: {api_err.message}. "
                    f"ACTIVATING VIRTUAL SOFT-STOP at ${rounded_stop_price}. "
                    f"The analyzer loop will monitor mark price and close via safe market order if breached."
                )
                return {
                    'virtual': True,
                    'symbol': symbol,
                    'direction': direction,
                    'stopPrice': rounded_stop_price,
                    'limitPrice': rounded_limit_price,
                    'quantity': quantity,
                    'side': side,
                    'status': 'VIRTUAL_SOFT_STOP',
                }
        except Exception as exec_err:
            logger.error(f"[{symbol}] ❌ Exception during SL futures_create_order: {type(exec_err).__name__} - {exec_err}")
            # Also activate virtual soft-stop on unexpected errors
            logger.warning(f"[{symbol}] ⚠️ ACTIVATING VIRTUAL SOFT-STOP (fallback) at ${rounded_stop_price}.")
            return {
                'virtual': True,
                'symbol': symbol,
                'direction': direction,
                'stopPrice': rounded_stop_price,
                'limitPrice': rounded_limit_price,
                'quantity': quantity,
                'side': side,
                'status': 'VIRTUAL_SOFT_STOP',
            }

        order_id = order.get('orderId') or order.get('algoId')
        status = order.get('status') or order.get('algoStatus', 'UNKNOWN')

        if not order or not isinstance(order, dict) or not order_id:
            logger.error(f"[{symbol}] ❌ SL order failed or orderId/algoId missing in response. Exact raw response: {order}")
            return None

        logger.info(f"[{symbol}] ✅ STOP-LIMIT SET | OrderID/AlgoID: {order_id} | Status: {status}")
        return order

    except Exception as e:
        logger.error(f"[{symbol}] ❌ Unexpected error setting Stop Loss: {type(e).__name__} - {e}")
        return None


def execute_virtual_stop_check(client: Client, symbol: str, direction: str,
                               stop_price: float, quantity: float = None) -> dict | None | bool:
    """
    Virtual Soft-Stop: Check if the current mark price has breached the stop price
    and, if so, execute a slippage-protected market close via execute_safe_market_order().

    This is the fallback mechanism when Binance rejects the native Stop-Limit order.
    Called by the analyzer's main loop on every tick for positions with virtual stops.

    Args:
        client:     python-binance Client
        symbol:     e.g. "BTCUSDT"
        direction:  "LONG" or "SHORT" (the position direction)
        stop_price: The virtual stop trigger price
        quantity:   Position size to close (fetched from Binance if not provided)

    Returns:
        order dict  — if stop was breached and close order was placed
        None        — if stop has NOT been breached (price is safe)
        False       — on error during execution
    """
    direction = direction.upper()
    if direction not in ('LONG', 'SHORT'):
        logger.error(f"[{symbol}] Invalid direction '{direction}' for virtual stop check")
        return False

    try:
        # Fetch current mark price
        mark_data = client.futures_mark_price(symbol=symbol)
        mark_price = float(mark_data['markPrice'])

        # Check if stop price is breached
        # LONG: price drops BELOW stop → breached
        # SHORT: price rises ABOVE stop → breached
        breached = False
        if direction == 'LONG' and mark_price <= stop_price:
            breached = True
        elif direction == 'SHORT' and mark_price >= stop_price:
            breached = True

        if not breached:
            return None  # Price is safe, no action needed

        # ── STOP BREACHED: Execute safe market close ──
        logger.warning(
            f"[{symbol}] 🚨 VIRTUAL SOFT-STOP BREACHED | {direction} | "
            f"Mark: ${mark_price:.4f} vs Stop: ${stop_price:.4f} | "
            f"Executing slippage-protected market close..."
        )

        # Resolve quantity if not provided
        if not quantity or quantity <= 0:
            live_pos = get_position_info(client, symbol)
            if live_pos and live_pos.get('size', 0) > 0:
                quantity = live_pos['size']
            else:
                logger.warning(f"[{symbol}] Virtual stop breached but no position found on Binance.")
                return False

        quantity = _round_quantity(client, symbol, quantity)
        if quantity <= 0:
            logger.error(f"[{symbol}] Virtual stop: quantity is 0 after rounding")
            return False

        # Close via the existing slippage-protected executor
        side = Client.SIDE_SELL if direction == 'LONG' else Client.SIDE_BUY

        # Cancel any lingering orders first
        _cancel_all_symbol_orders(client, symbol)

        order = execute_safe_market_order(
            client=client,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reduce_only=True
        )

        logger.info(
            f"[{symbol}] ✅ VIRTUAL SOFT-STOP EXECUTED | OrderID: {order.get('orderId')} | "
            f"Status: {order.get('status', 'UNKNOWN')}"
        )
        return order

    except BinanceAPIException as e:
        if e.code in (-2022, -4509):
            logger.warning(f"[{symbol}] Virtual stop: Position already closed (ghost position). Handling gracefully.")
            return True
        logger.error(f"[{symbol}] ❌ Virtual stop execution failed: [{e.code}] {e.message}")
        return False
    except Exception as e:
        logger.error(f"[{symbol}] ❌ Unexpected error in virtual stop check: {type(e).__name__} - {e}")
        return False


def update_stop_loss_price(client: Client, symbol: str, direction: str, stop_price: float,
                           trailing_distance: float = None, atr_val: float = None,
                           quantity: float = None) -> dict | None:
    """
    Real-time adjustment of Stop Loss order when dynamic ATR changes or price moves.
    Delegates directly to set_stop_loss_order.
    """
    return set_stop_loss_order(client, symbol, direction, stop_price,
                               trailing_distance=trailing_distance, atr_val=atr_val,
                               quantity=quantity)


# ──────────────────────────────────────────────────────────────────────
#  AUTOMATED TELEGRAM PORTFOLIO REPORT (12-Hour Binance Direct)
# ──────────────────────────────────────────────────────────────────────

def send_telegram_daily_report(client: Client) -> None:
    """
    Generate and send an automated Telegram portfolio report directly from Binance Futures API.
    Summarizes:
      - Total Wallet Balance
      - Total Realized PNL (calculated from futures_income_history)
      - Total Unrealized PNL
      - Active Open Positions count & detailed list with ROE %
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        logger.warning("[WARNING] Telegram credentials missing. Cannot send report.")
        return

    try:
        account_info = client.futures_account()
        wallet_balance = float(account_info['totalWalletBalance'])
        unrealized_pnl = float(account_info['totalUnrealizedProfit'])
        
        # Calculate Realized PNL from income history
        income_history = client.futures_income_history(incomeType="REALIZED_PNL", limit=1000)
        total_realized_pnl = sum(float(item['income']) for item in income_history)
        
        positions = account_info['positions']
        open_positions = [p for p in positions if float(p['positionAmt']) != 0]
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"📊 **تقرير المحفظة الآلي - Quant Bot** 🤖\n"
        msg += f"🕒 `{now_str}`\n"
        msg += "━━━━━━━━━━━━━━━━━━\n"
        msg += f"💰 **رصيد المحفظة:** `${wallet_balance:.2f}`\n"
        
        realized_icon = "🟢" if total_realized_pnl > 0 else ("🔴" if total_realized_pnl < 0 else "⚪")
        unrealized_icon = "🟢" if unrealized_pnl > 0 else ("🔴" if unrealized_pnl < 0 else "⚪")
        
        msg += f"💸 **الأرباح المحققة (Realized):** {realized_icon} `${total_realized_pnl:.2f}`\n"
        msg += f"📈 **الأرباح العائمة (Unrealized):** {unrealized_icon} `${unrealized_pnl:.2f}`\n"
        msg += f"📝 **الصفقات المفتوحة:** `{len(open_positions)}`\n"
        msg += "━━━━━━━━━━━━━━━━━━\n"
        
        if open_positions:
            for pos in open_positions:
                symbol = pos['symbol']
                amt = float(pos['positionAmt'])
                pnl = float(pos['unrealizedProfit'])
                entry = float(pos['entryPrice'])
                
                direction = "🟢 LONG" if amt > 0 else "🔴 SHORT"
                pos_value = abs(amt) * entry
                roe = (pnl / pos_value * 100) if pos_value > 0 else 0
                
                msg += f"{direction} - {symbol} | PNL: `${pnl:+.2f}` | ROE: `{roe:+.2f}%`\n"
        else:
            msg += "لا توجد صفقات مفتوحة حالياً 💤\n"
            
        msg += "━━━━━━━━━━━━━━━━━━\n"
        msg += "⚡ *تم التحديث تلقائياً بواسطة محرك التنفيذ*"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
        
        requests.post(url, data=payload)
            
    except Exception as e:
        logger.error(f"[ERROR] Executing daily Telegram report failed: {e}")
