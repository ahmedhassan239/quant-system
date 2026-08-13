"""
analyzer/risk.py
────────────────
Risk management utilities for the Execution Analyzer.

Provides:
    evaluate_pyramid_scale_in     – Tier-based pyramiding with break-even mandate.
    find_weakest_active_position  – Stagnant detection for position rotation.
    apply_long_risk_management    – Full ATR TSL / Break-Even / Partial-TP / SL logic for LONG.
    apply_short_risk_management   – Full ATR TSL / Break-Even / Partial-TP / SL logic for SHORT.

The ``apply_*`` functions encapsulate the large risk-management blocks
that previously lived inside ``run_analyzer`` (lines 1578–1979 of the
original file). Extracting them here keeps ``core.py`` lean while
preserving every line of the underlying risk math unchanged.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import func

from config import (
    MAX_SCALE_INS,
    PYRAMID_TIER1_PNL,
    PYRAMID_TIER2_PNL,
    PYRAMID_TIER1_SIZE_PCT,
    PYRAMID_TIER2_SIZE_PCT,
    DISABLE_STAGNANT_EXIT,
)
from database import (
    SessionLocal,
    PortfolioState,
    TradingSignal,
    TradeHistory,
)
from futures_executor import (
    set_stop_loss_order,
    execute_partial_tp_scaleout,
    get_position_info,
)

from analyzer.notifications import send_telegram_alert
from analyzer.portfolio import _close_position_handler, load_portfolio
from analyzer.signals import calculate_strength_score
from analyzer.utils import log_to_db
from analyzer.constants import (
    STAGNANT_HOURS_THRESHOLD,
    STAGNANT_PNL_BAND,
    BREAK_EVEN_TRIGGER_PCT,
)


# ══════════════════════════════════════════════════════════════════════
#  PYRAMIDING ENGINE
# ══════════════════════════════════════════════════════════════════════

def evaluate_pyramid_scale_in(
    portfolio: Dict[str, Any],
    current_price: float,
    symbol: str,
    session: object,
    futures_client: Optional[object] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Evaluate and execute pyramiding (scaling into a winning position).

    Tier Rules:
        Tier 1 (dca_level == 0): Unrealised PnL ≥ PYRAMID_TIER1_PNL → add PYRAMID_TIER1_SIZE_PCT of initial size.
        Tier 2 (dca_level == 1): Unrealised PnL ≥ PYRAMID_TIER2_PNL → add PYRAMID_TIER2_SIZE_PCT of initial size.

    Strict Risk Rule — Break-Even Mandate:
        1. Calculate hypothetical new Average Entry Price after the add.
        2. New Stop-Loss MUST be at or better than new_avg_entry_price.
        3. If the proposed SL would be on the wrong side of current price
           (i.e. guarantees an immediate loss), the scale-in is ABORTED.

    Args:
        portfolio:      Current portfolio state dict.
        current_price:  Latest market price.
        symbol:         Trading pair symbol.
        session:        Active SQLAlchemy session.
        futures_client: Optional Binance Futures client to place live orders.

    Returns:
        ``(True, updated_portfolio)`` if scale-in was executed.
        ``(False, portfolio)``        if any guard rejected the scale-in.
    """
    if not portfolio or not portfolio.get('asset_balance') or portfolio['asset_balance'] <= 0:
        return False, portfolio

    dca_level = int(portfolio.get('dca_level', 0) or 0)
    if dca_level >= MAX_SCALE_INS:
        return False, portfolio

    direction = portfolio.get('position_direction')
    if not direction or direction not in ('LONG', 'SHORT'):
        return False, portfolio

    ep = float(portfolio.get('average_entry_price') or 0.0)
    cp = float(current_price)
    if ep <= 0 or cp <= 0:
        return False, portfolio

    # Calculate current unrealised PnL %
    if direction == 'LONG':
        unrealized_pnl = (cp - ep) / ep
    else:
        unrealized_pnl = (ep - cp) / ep

    # Check Tier Eligibility
    tier_num = 0
    scale_size_pct = 0.0

    if dca_level == 0 and unrealized_pnl >= PYRAMID_TIER1_PNL:
        tier_num = 1
        scale_size_pct = PYRAMID_TIER1_SIZE_PCT
    elif dca_level == 1 and unrealized_pnl >= PYRAMID_TIER2_PNL:
        tier_num = 2
        scale_size_pct = PYRAMID_TIER2_SIZE_PCT
    else:
        return False, portfolio

    # Quantities and Costs
    existing_qty = float(portfolio['asset_balance'])
    if dca_level == 0:
        initial_qty = existing_qty
    else:
        initial_qty = existing_qty / 1.50

    add_qty = initial_qty * scale_size_pct
    if add_qty <= 0:
        return False, portfolio

    existing_cost = existing_qty * ep
    add_cost = add_qty * cp
    new_qty = existing_qty + add_qty
    new_avg_entry_price = (existing_cost + add_cost) / new_qty

    # ── Strict Risk Rule: Break-Even Mandate ──
    # The new SL MUST be at or better than new_avg_entry_price
    current_sl = float(portfolio.get('stop_loss_price', 0) or 0)

    if direction == 'LONG':
        new_stop_loss = max(current_sl, new_avg_entry_price)
        if new_stop_loss >= cp:
            print(
                f"🛑 [PYRAMID ABORT] {symbol} LONG Tier {tier_num}: Proposed Break-Even SL "
                f"(${new_stop_loss:.2f}) >= Current Price (${cp:.2f}). Aborting scale-in.",
                flush=True,
            )
            return False, portfolio
    else:  # SHORT
        if current_sl > 0:
            new_stop_loss = min(current_sl, new_avg_entry_price)
        else:
            new_stop_loss = new_avg_entry_price
        if new_stop_loss <= cp:
            print(
                f"🛑 [PYRAMID ABORT] {symbol} SHORT Tier {tier_num}: Proposed Break-Even SL "
                f"(${new_stop_loss:.2f}) <= Current Price (${cp:.2f}). Aborting scale-in.",
                flush=True,
            )
            return False, portfolio

    # ── Execute Scale-In ──
    if futures_client:
        try:
            info = futures_client.futures_exchange_info()
            step_size = 0.001
            for s in info.get('symbols', []):
                if s['symbol'] == symbol:
                    for flt in s.get('filters', []):
                        if flt['filterType'] == 'LOT_SIZE':
                            step_size = float(flt['stepSize'])
                            break
                    break
            precision = (
                len(str(step_size).rstrip('0').split('.')[-1])
                if '.' in str(step_size)
                else 0
            )
            order_qty = round(add_qty - (add_qty % step_size), precision)
            if order_qty <= 0:
                print(
                    f"⚠️ [PYRAMID SKIP] {symbol}: Calculated scale-in qty too small after rounding.",
                    flush=True,
                )
                return False, portfolio

            side = 'BUY' if direction == 'LONG' else 'SELL'
            futures_client.futures_create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=order_qty,
            )
            set_stop_loss_order(futures_client, symbol, direction, new_stop_loss)
        except Exception as exc:
            print(
                f"❌ [PYRAMID ERROR] Failed Binance scale-in order for {symbol}: {exc}",
                flush=True,
            )
            return False, portfolio

    # Update Portfolio Dictionary & DB
    portfolio['dca_level'] = dca_level + 1
    portfolio['asset_balance'] = new_qty
    portfolio['average_entry_price'] = new_avg_entry_price
    portfolio['stop_loss_price'] = new_stop_loss
    portfolio['stop_loss'] = new_stop_loss
    portfolio['trailing_active'] = True

    pyramid_msg = (
        f"🔼 [PYRAMID] Scaled into winning position {symbol} ({direction}) | "
        f"Tier {tier_num} ({unrealized_pnl * 100:+.2f}% PnL) | "
        f"New Avg Price: ${new_avg_entry_price:.2f} | "
        f"Added Qty: {add_qty:.4f} | "
        f"SL moved to Break-Even: ${new_stop_loss:.2f}"
    )
    print(pyramid_msg, flush=True)
    log_to_db(session, symbol, "PYRAMID", pyramid_msg)
    send_telegram_alert(pyramid_msg)

    try:
        new_state = PortfolioState(
            timestamp=datetime.now(),
            symbol=symbol,
            decision=portfolio.get('decision', direction),
            current_price=cp,
            usdt_balance=portfolio.get('usdt_balance', 1000.0),
            asset_balance=new_qty,
            position_direction=direction,
            average_entry_price=new_avg_entry_price,
            highest_price_since_entry=portfolio.get('highest_price_since_entry', cp),
            lowest_price_since_entry=portfolio.get('lowest_price_since_entry', cp),
            stop_loss_price=new_stop_loss,
            stop_loss=new_stop_loss,
            trailing_active=True,
            partial_tp_hit=portfolio.get('partial_tp_hit', False),
            dca_level=portfolio['dca_level'],
            total_portfolio_value=portfolio.get('total_portfolio_value', 1000.0),
        )
        session.add(new_state)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Warning: Failed to persist pyramid scale-in to DB: {e}", flush=True)

    return True, portfolio


# ══════════════════════════════════════════════════════════════════════
#  WEAKEST POSITION SCANNER (for Position Rotation)
# ══════════════════════════════════════════════════════════════════════

def find_weakest_active_position(
    session: object,
    futures_client: Optional[object] = None,
) -> Tuple[Optional[str], float, Optional[Dict[str, Any]], bool]:
    """Scan all active positions to find the weakest candidate for rotation.

    Evaluation criteria per position:
        - Unrealised PnL % (calculated from live or DB entry price).
        - Hours open (from first ``PortfolioState`` row with asset_balance > 0).
        - Strength Score from the latest ``TradingSignal`` Z-Score + volume.

    Stagnancy definition:
        ``hours_open >= STAGNANT_HOURS_THRESHOLD AND pnl_pct < STAGNANT_PNL_BAND * 100``.
        Stagnant positions receive a Strength Score of 0.0 regardless of signals.

    Data source priority:
        1. Live Binance Futures position list (most accurate).
        2. Local DB ``PortfolioState`` fallback (if client unavailable).

    Args:
        session:        Active SQLAlchemy session.
        futures_client: Optional Binance Futures client for live data.

    Returns:
        ``(weakest_symbol, weakest_score, weakest_portfolio, is_stagnant)``
        or ``(None, 0.0, None, False)`` if no active positions exist.
    """
    active_items = []

    # 1. Fetch live open positions from Binance Futures API if client is available
    if futures_client:
        try:
            info = futures_client.futures_position_information()
            for p in info:
                amt = float(p.get('positionAmt', 0))
                if amt != 0:
                    sym = p['symbol']
                    ep = float(p.get('entryPrice', 0))
                    cp = float(p.get('markPrice', 0) or ep)
                    direction = 'LONG' if amt > 0 else 'SHORT'
                    active_items.append({
                        'symbol':        sym,
                        'amount':        abs(amt),
                        'entry_price':   ep,
                        'current_price': cp,
                        'direction':     direction,
                    })
        except Exception as e:
            print(
                f"Warning: Failed to fetch live Binance positions in weakest scanner: {e}",
                flush=True,
            )

    # 2. Fallback to DB if futures_client not available or returned no items
    if not active_items:
        latest_ids = (
            session.query(func.max(PortfolioState.id).label('max_id'))
            .group_by(PortfolioState.symbol)
            .subquery()
        )
        db_rows = (
            session.query(PortfolioState)
            .filter(
                PortfolioState.id.in_(session.query(latest_ids.c.max_id)),
                func.abs(PortfolioState.asset_balance) > 0,
            )
            .all()
        )
        for row in db_rows:
            active_items.append({
                'symbol':        row.symbol,
                'amount':        float(abs(row.asset_balance or 0)),
                'entry_price':   float(row.average_entry_price or 0),
                'current_price': float(row.current_price or 0),
                'direction':     row.position_direction or 'LONG',
            })

    if not active_items:
        return None, 0.0, None, False

    weakest_symbol = None
    weakest_score: float = float('inf')
    weakest_portfolio = None
    weakest_is_stagnant = False

    for item in active_items:
        sym = item['symbol']
        port = load_portfolio(session, sym)

        ep = item['entry_price'] or float(port.get('average_entry_price') or 0)
        cp = item['current_price'] or float(port.get('current_price') or ep)
        direction = item['direction'] or port.get('position_direction') or 'LONG'

        if not port.get('average_entry_price') or port.get('average_entry_price') == 0:
            port['average_entry_price'] = ep
            port['position_direction'] = direction
            port['asset_balance'] = item['amount']

        # Calculate position duration (in hours)
        first_entry = (
            session.query(PortfolioState.timestamp)
            .filter(
                PortfolioState.symbol == sym,
                PortfolioState.asset_balance != 0,
            )
            .order_by(PortfolioState.id.asc())
            .first()
        )
        entry_time = first_entry[0] if first_entry else datetime.now()
        hours_open = (
            (datetime.now() - entry_time).total_seconds() / 3600.0
            if entry_time
            else 0.0
        )

        # Calculate current unrealised PnL %
        pnl_pct = 0.0
        if ep > 0 and cp > 0:
            if direction == 'LONG':
                pnl_pct = ((cp - ep) / ep) * 100.0
            else:
                pnl_pct = ((ep - cp) / ep) * 100.0

        # Flag stagnant trades: open >= STAGNANT_HOURS_THRESHOLD hours with PnL < threshold
        is_stag = (hours_open >= STAGNANT_HOURS_THRESHOLD and pnl_pct < STAGNANT_PNL_BAND * 100)

        # Fetch current signal Z-score for the position
        latest_sig = (
            session.query(TradingSignal)
            .filter(TradingSignal.symbol == sym)
            .order_by(TradingSignal.id.desc())
            .first()
        )
        z_val = latest_sig.z_score if (latest_sig and latest_sig.z_score is not None) else 0.0
        v_ratio = getattr(latest_sig, 'bullish_ob_vol_ratio', 1.0) or 1.0

        pos_score = 0.0 if is_stag else calculate_strength_score(z_val, v_ratio)

        if pos_score < weakest_score:
            weakest_score = pos_score
            weakest_symbol = sym
            weakest_portfolio = port
            weakest_is_stagnant = is_stag

    if weakest_score == float('inf'):
        weakest_score = 0.0

    return weakest_symbol, float(weakest_score or 0.0), weakest_portfolio, weakest_is_stagnant


# ══════════════════════════════════════════════════════════════════════
#  ATR TRAILING STOP / BREAK-EVEN / PARTIAL TP — LONG
# ══════════════════════════════════════════════════════════════════════

def apply_long_risk_management(
    portfolio: Dict[str, Any],
    symbol: str,
    session: object,
    current_price: float,
    entry_price: float,
    highest_price: Optional[float],
    stop_loss: float,
    trailing_active: bool,
    current_atr: float,
    tsl_atr_activation_mult: float,
    tsl_atr_trail_mult: float,
    partial_tp_pct: float,
    futures_client: Optional[object],
    bullish_ob: Optional[Dict[str, Any]],
    bearish_ob: Optional[Dict[str, Any]],
    current_rsi: Optional[float],
    current_zscore: Optional[float],
    macro_info: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    """Apply full LONG-side risk management for an active position.

    Checks (in order):
        1. Update highest-price watermark.
        2. Partial Take Profit (50% scale-out at ``partial_tp_pct``).
        3. Break-Even trigger (move SL to entry at +BREAK_EVEN_TRIGGER_PCT).
        4. Dynamic ATR Trailing Stop (activates at tsl_atr_activation_mult × ATR).
        5. Stop Loss hit check (close position if price <= SL).
        6. Stagnant Trade Closer (if DISABLE_STAGNANT_EXIT is False).

    Each check is skipped if ``risk_exit_triggered`` is already ``True``.

    Args:
        portfolio:               Current portfolio state dict.
        symbol:                  Trading pair symbol.
        session:                 Active SQLAlchemy session.
        current_price:           Current market price.
        entry_price:             Position average entry price.
        highest_price:           Peak price since entry (or None).
        stop_loss:               Current stop-loss price (0 = none set).
        trailing_active:         Whether the TSL is currently active.
        current_atr:             Latest 14-period ATR value.
        tsl_atr_activation_mult: TSL activation threshold multiplier.
        tsl_atr_trail_mult:      TSL trailing distance multiplier.
        partial_tp_pct:          Partial TP trigger threshold (e.g. 0.03 = 3%).
        futures_client:          Optional Binance Futures client.
        bullish_ob:              Latest bullish OB (for close handler context).
        bearish_ob:              Latest bearish OB (for close handler context).
        current_rsi:             Current RSI (for close handler context).
        current_zscore:          Current Z-Score (for close handler context).
        macro_info:              Latest macro state (for close handler context).

    Returns:
        ``(updated_portfolio, risk_exit_triggered)``
    """
    risk_exit_triggered = False
    cp = float(current_price)
    ep = float(entry_price)

    position_size = float(portfolio.get('asset_balance', 0) or 0)
    if position_size <= 0 and futures_client:
        try:
            live_pos = get_position_info(futures_client, symbol)
            if live_pos and live_pos.get('size', 0) > 0:
                position_size = float(live_pos['size'])
        except Exception:
            pass

    position_value = abs(ep * position_size)
    unrealized_pnl = (cp - ep) * position_size
    unrealized_pct = (
        (abs(unrealized_pnl) / position_value)
        if (position_value > 0 and unrealized_pnl > 0)
        else (unrealized_pnl / position_value if position_value > 0 else 0.0)
    )

    # Track new peak price
    if highest_price is None or cp > float(highest_price):
        portfolio['highest_price_since_entry'] = cp
        highest_price = cp

    import logging as _risk_logging
    _risk_logger = _risk_logging.getLogger("Analyzer.risk")

    if unrealized_pnl > 0:
        _risk_logger.info(
            f"🔎 [TP-MATH] {symbol} | uPnL: ${unrealized_pnl:.2f} | "
            f"Value: ${position_value:.2f} | ROE: {unrealized_pct * 100:.2f}% | "
            f"Target: {partial_tp_pct * 100:.2f}%"
        )

    # ── Partial Take Profit (Scale-Out 50%) & Auto Break-Even ──
    if (
        not risk_exit_triggered
        and unrealized_pct >= partial_tp_pct
        and not portfolio.get('partial_tp_hit', False)
    ):
        total_qty = float(portfolio.get('asset_balance', 0) or 0)
        if total_qty > 0 and futures_client:
            tsl_trailing_dist = tsl_atr_trail_mult * current_atr if current_atr else None
            tp_order, rem_qty = execute_partial_tp_scaleout(
                futures_client, symbol, 'LONG', total_qty, ep,
                trailing_distance=tsl_trailing_dist, atr_val=current_atr,
            )
            if tp_order and rem_qty > 0:
                portfolio['partial_tp_hit'] = True
                closed_qty = total_qty - rem_qty
                portfolio['asset_balance'] = rem_qty
                if portfolio.get('total_cost'):
                    portfolio['total_cost'] = float(portfolio['total_cost']) * (rem_qty / total_qty)
                portfolio['usdt_balance'] = float(portfolio.get('usdt_balance', 0)) + (closed_qty * cp)
                if stop_loss < ep:
                    stop_loss = ep
                    portfolio['stop_loss_price'] = ep
                    portfolio['stop_loss'] = ep

                pnl_realized_est = (cp - ep) * closed_qty
                msg = (
                    f"🎯 [{symbol}] PARTIAL TAKE PROFIT EXECUTED (LONG) | "
                    f"Closed 50% size ({closed_qty} units) at ${cp:.4f} (+{unrealized_pct * 100:.2f}%) | "
                    f"Est. Profit: +${pnl_realized_est:.2f} | Remaining Qty: {rem_qty} | "
                    f"Stop Loss locked at Entry ${ep:.4f}"
                )
                print(msg, flush=True)
                log_to_db(session, symbol, "ENTRY", msg)

                history_record = TradeHistory(
                    symbol=symbol,
                    direction='LONG',
                    entry_price=ep,
                    exit_price=cp,
                    quantity=closed_qty,
                    pnl_usd=pnl_realized_est,
                    pnl_pct=unrealized_pct * 100,
                    outcome='WIN',
                    exit_reason='PARTIAL_TAKE_PROFIT',
                    closed_at=datetime.utcnow(),
                )
                session.add(history_record)

                try:
                    latest_record = (
                        session.query(PortfolioState)
                        .filter(
                            PortfolioState.symbol == symbol,
                            PortfolioState.position_direction == 'LONG',
                        )
                        .order_by(PortfolioState.id.desc())
                        .first()
                    )
                    if latest_record:
                        latest_record.partial_tp_hit = True
                        latest_record.asset_balance = rem_qty
                        latest_record.total_cost = portfolio['total_cost']
                        latest_record.usdt_balance = portfolio['usdt_balance']
                        latest_record.stop_loss_price = stop_loss
                        latest_record.stop_loss = stop_loss
                    session.commit()
                except Exception as e:
                    session.rollback()
                    print(f"Warning: Failed to persist partial TP state to DB: {e}", flush=True)

    # ── Break-Even Trigger (+1.0%) ──
    if not risk_exit_triggered and unrealized_pct >= BREAK_EVEN_TRIGGER_PCT:
        if stop_loss < ep:
            old_sl = stop_loss
            stop_loss = ep
            portfolio['stop_loss_price'] = stop_loss
            portfolio['stop_loss'] = stop_loss
            msg = (
                f"🛡️ BREAK-EVEN TRIGGERED: {symbol} LONG at {unrealized_pct * 100:+.2f}% PnL. "
                f"Moved SL from ${old_sl:.4f} to Entry ${ep:.4f}"
            )
            print(msg, flush=True)
            log_to_db(session, symbol, "INFO", msg)

            if futures_client:
                set_stop_loss_order(futures_client, symbol, 'LONG', stop_loss)

            try:
                latest_record = (
                    session.query(PortfolioState)
                    .filter(
                        PortfolioState.symbol == symbol,
                        PortfolioState.position_direction == 'LONG',
                    )
                    .order_by(PortfolioState.id.desc())
                    .first()
                )
                if latest_record:
                    latest_record.stop_loss_price = stop_loss
                    latest_record.stop_loss = stop_loss
                    session.commit()
            except Exception as e:
                session.rollback()
                print(f"Warning: Failed to persist break-even SL to DB: {e}", flush=True)

    # ── Trailing Stop Loss (Profit-Locking) ──
    # Dynamic Volatility-Based TSL: activates when profit >= tsl_atr_activation_mult * ATR
    tsl_activation_dist = tsl_atr_activation_mult * current_atr
    tsl_trailing_dist = tsl_atr_trail_mult * current_atr
    price_move_fav = cp - ep

    if not risk_exit_triggered and price_move_fav >= tsl_activation_dist:
        if not trailing_active:
            trailing_active = True
            portfolio['trailing_active'] = True
            msg = (
                f"📈 [{symbol}] DYNAMIC ATR TRAILING STOP ACTIVATED for LONG | "
                f"Move: +${price_move_fav:.4f} (+{unrealized_pct * 100:.2f}%) | "
                f"Threshold: +${tsl_activation_dist:.4f} ({tsl_atr_activation_mult}x ATR: ${current_atr:.4f})"
            )
            print(msg, flush=True)
            log_to_db(session, symbol, "INFO", msg)

        # Trail strictly by tsl_atr_trail_mult × ATR from the PEAK price
        peak = float(highest_price) if highest_price else cp
        new_sl = peak - tsl_trailing_dist

        if new_sl > stop_loss:
            old_sl = stop_loss
            portfolio['stop_loss_price'] = new_sl
            portfolio['stop_loss'] = new_sl
            stop_loss = new_sl
            locked_pnl = ((new_sl - ep) / ep) * 100
            msg = (
                f"📈 DYNAMIC ATR TRAILING STOP UPDATED: {symbol} | "
                f"New SL: ${new_sl:.4f} (was ${old_sl:.4f}) | "
                f"Peak: ${peak:.2f} | Trailing Dist: ${tsl_trailing_dist:.4f} "
                f"({tsl_atr_trail_mult}x ATR) | Locked Profit: {locked_pnl:+.2f}%"
            )
            print(msg, flush=True)
            log_to_db(session, symbol, "INFO", msg)

            if futures_client:
                set_stop_loss_order(
                    futures_client, symbol, 'LONG', stop_loss,
                    trailing_distance=tsl_trailing_dist, atr_val=current_atr,
                )

        # Persist trailing state to DB for frontend
        try:
            latest_record = (
                session.query(PortfolioState)
                .filter(
                    PortfolioState.symbol == symbol,
                    PortfolioState.position_direction == 'LONG',
                )
                .order_by(PortfolioState.id.desc())
                .first()
            )
            if latest_record:
                latest_record.stop_loss_price = stop_loss
                latest_record.stop_loss = stop_loss
                latest_record.trailing_active = True
                latest_record.highest_price_since_entry = float(highest_price) if highest_price else cp
                session.commit()
        except Exception as e:
            session.rollback()
            print(f"Warning: Failed to persist trailing SL to DB: {e}", flush=True)

    # ── Check Stop Loss hit ──
    if not risk_exit_triggered and stop_loss > 0 and cp <= stop_loss:
        sl_type = 'TRAILING_STOP' if trailing_active else 'STOP_LOSS'
        msg = (
            f"🚨 [{symbol}] LONG {sl_type} HIT at ${cp:.2f} "
            f"(SL: ${stop_loss:.2f}) — EXECUTING CLOSE ON BINANCE"
        )
        print(msg, flush=True)
        log_to_db(session, symbol, "EXIT", msg)
        portfolio = _close_position_handler(
            portfolio, current_price, symbol, session, sl_type,
            futures_client, bullish_ob, bearish_ob, current_rsi, current_zscore, macro_info,
        )
        risk_exit_triggered = True

    # ── Stagnant Trade Closer (>2h open, flat PnL) ──
    # ALPHA MODE: When DISABLE_STAGNANT_EXIT is True, trades are NOT
    # closed for being sideways. Give the setup time to play out.
    if not risk_exit_triggered and not DISABLE_STAGNANT_EXIT:
        first_entry_row = (
            session.query(PortfolioState)
            .filter(
                PortfolioState.symbol == symbol,
                PortfolioState.position_direction == 'LONG',
            )
            .order_by(PortfolioState.id.asc())
            .first()
        )
        if first_entry_row:
            open_seconds = (datetime.now() - first_entry_row.timestamp).total_seconds()
            stagnant_seconds = STAGNANT_HOURS_THRESHOLD * 3600
            if open_seconds > stagnant_seconds and -STAGNANT_PNL_BAND <= unrealized_pct <= STAGNANT_PNL_BAND:
                open_hours = open_seconds / 3600
                msg = (
                    f"⏰ STAGNANT POSITION CLOSED: {symbol} LONG open for "
                    f"{open_hours:.1f}h with flat PnL ({unrealized_pct * 100:+.2f}%). "
                    f"Freeing slot for fresh opportunities."
                )
                print(msg, flush=True)
                log_to_db(session, symbol, "EXIT", msg)
                portfolio = _close_position_handler(
                    portfolio, current_price, symbol, session, 'STAGNANT',
                    futures_client, bullish_ob, bearish_ob, current_rsi, current_zscore, macro_info,
                )
                risk_exit_triggered = True

    return portfolio, risk_exit_triggered


# ══════════════════════════════════════════════════════════════════════
#  ATR TRAILING STOP / BREAK-EVEN / PARTIAL TP — SHORT
# ══════════════════════════════════════════════════════════════════════

def apply_short_risk_management(
    portfolio: Dict[str, Any],
    symbol: str,
    session: object,
    current_price: float,
    entry_price: float,
    lowest_price: Optional[float],
    stop_loss: float,
    trailing_active: bool,
    current_atr: float,
    tsl_atr_activation_mult: float,
    tsl_atr_trail_mult: float,
    partial_tp_pct: float,
    futures_client: Optional[object],
    bullish_ob: Optional[Dict[str, Any]],
    bearish_ob: Optional[Dict[str, Any]],
    current_rsi: Optional[float],
    current_zscore: Optional[float],
    macro_info: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    """Apply full SHORT-side risk management for an active position.

    Mirror of ``apply_long_risk_management`` with inverted price direction
    logic (trough watermark, SL above price, trailing from trough).

    Checks (in order):
        1. Update lowest-price watermark (trough).
        2. Partial Take Profit (50% scale-out).
        3. Break-Even trigger (move SL to entry).
        4. Dynamic ATR Trailing Stop (trail from trough).
        5. Stop Loss hit check (close if price >= SL).
        6. Stagnant Trade Closer.

    Args / Returns: See ``apply_long_risk_management``.
    """
    risk_exit_triggered = False
    cp = float(current_price)
    ep = float(entry_price)

    position_size = float(portfolio.get('asset_balance', 0) or 0)
    if position_size <= 0 and futures_client:
        try:
            live_pos = get_position_info(futures_client, symbol)
            if live_pos and live_pos.get('size', 0) > 0:
                position_size = float(live_pos['size'])
        except Exception:
            pass

    position_value = abs(ep * position_size)
    unrealized_pnl = (ep - cp) * position_size
    unrealized_pct = (
        (abs(unrealized_pnl) / position_value)
        if (position_value > 0 and unrealized_pnl > 0)
        else (unrealized_pnl / position_value if position_value > 0 else 0.0)
    )

    # Track new trough price
    if lowest_price is None or cp < float(lowest_price):
        portfolio['lowest_price_since_entry'] = cp
        lowest_price = cp

    import logging as _risk_logging
    _risk_logger = _risk_logging.getLogger("Analyzer.risk")

    if unrealized_pnl > 0:
        _risk_logger.info(
            f"🔎 [TP-MATH] {symbol} | uPnL: ${unrealized_pnl:.2f} | "
            f"Value: ${position_value:.2f} | ROE: {unrealized_pct * 100:.2f}% | "
            f"Target: {partial_tp_pct * 100:.2f}%"
        )

    # ── Partial Take Profit (Scale-Out 50%) & Auto Break-Even ──
    if (
        not risk_exit_triggered
        and unrealized_pct >= partial_tp_pct
        and not portfolio.get('partial_tp_hit', False)
    ):
        total_qty = float(portfolio.get('asset_balance', 0) or 0)
        if total_qty > 0 and futures_client:
            tsl_trailing_dist = tsl_atr_trail_mult * current_atr if current_atr else None
            tp_order, rem_qty = execute_partial_tp_scaleout(
                futures_client, symbol, 'SHORT', total_qty, ep,
                trailing_distance=tsl_trailing_dist, atr_val=current_atr,
            )
            if tp_order and rem_qty > 0:
                portfolio['partial_tp_hit'] = True
                closed_qty = total_qty - rem_qty
                portfolio['asset_balance'] = rem_qty
                if portfolio.get('total_cost'):
                    portfolio['total_cost'] = float(portfolio['total_cost']) * (rem_qty / total_qty)
                portfolio['usdt_balance'] = (
                    float(portfolio.get('usdt_balance', 0)) + (closed_qty * (2 * ep - cp))
                )
                if stop_loss > ep or stop_loss == 0:
                    stop_loss = ep
                    portfolio['stop_loss_price'] = ep
                    portfolio['stop_loss'] = ep

                pnl_realized_est = (ep - cp) * closed_qty
                msg = (
                    f"🎯 [{symbol}] PARTIAL TAKE PROFIT EXECUTED (SHORT) | "
                    f"Closed 50% size ({closed_qty} units) at ${cp:.4f} (+{unrealized_pct * 100:.2f}%) | "
                    f"Est. Profit: +${pnl_realized_est:.2f} | Remaining Qty: {rem_qty} | "
                    f"Stop Loss locked at Entry ${ep:.4f}"
                )
                print(msg, flush=True)
                log_to_db(session, symbol, "ENTRY", msg)

                history_record = TradeHistory(
                    symbol=symbol,
                    direction='SHORT',
                    entry_price=ep,
                    exit_price=cp,
                    quantity=closed_qty,
                    pnl_usd=pnl_realized_est,
                    pnl_pct=unrealized_pct * 100,
                    outcome='WIN',
                    exit_reason='PARTIAL_TAKE_PROFIT',
                    closed_at=datetime.utcnow(),
                )
                session.add(history_record)

                try:
                    latest_record = (
                        session.query(PortfolioState)
                        .filter(
                            PortfolioState.symbol == symbol,
                            PortfolioState.position_direction == 'SHORT',
                        )
                        .order_by(PortfolioState.id.desc())
                        .first()
                    )
                    if latest_record:
                        latest_record.partial_tp_hit = True
                        latest_record.asset_balance = rem_qty
                        latest_record.total_cost = portfolio['total_cost']
                        latest_record.usdt_balance = portfolio['usdt_balance']
                        latest_record.stop_loss_price = stop_loss
                        latest_record.stop_loss = stop_loss
                    session.commit()
                except Exception as e:
                    session.rollback()
                    print(f"Warning: Failed to persist partial TP state to DB: {e}", flush=True)

    # ── Break-Even Trigger (+1.0%) ──
    if not risk_exit_triggered and unrealized_pct >= BREAK_EVEN_TRIGGER_PCT:
        if stop_loss > ep or stop_loss == 0:
            old_sl = stop_loss
            stop_loss = ep
            portfolio['stop_loss_price'] = stop_loss
            portfolio['stop_loss'] = stop_loss
            msg = (
                f"🛡️ BREAK-EVEN TRIGGERED: {symbol} SHORT at {unrealized_pct * 100:+.2f}% PnL. "
                f"Moved SL from ${old_sl:.4f} to Entry ${ep:.4f}"
            )
            print(msg, flush=True)
            log_to_db(session, symbol, "INFO", msg)

            if futures_client:
                set_stop_loss_order(futures_client, symbol, 'SHORT', stop_loss)

            try:
                latest_record = (
                    session.query(PortfolioState)
                    .filter(
                        PortfolioState.symbol == symbol,
                        PortfolioState.position_direction == 'SHORT',
                    )
                    .order_by(PortfolioState.id.desc())
                    .first()
                )
                if latest_record:
                    latest_record.stop_loss_price = stop_loss
                    latest_record.stop_loss = stop_loss
                    session.commit()
            except Exception as e:
                session.rollback()
                print(f"Warning: Failed to persist break-even SL to DB: {e}", flush=True)

    # ── Trailing Stop Loss (Profit-Locking) ──
    tsl_activation_dist = tsl_atr_activation_mult * current_atr
    tsl_trailing_dist = tsl_atr_trail_mult * current_atr
    price_move_fav = ep - cp

    if not risk_exit_triggered and price_move_fav >= tsl_activation_dist:
        if not trailing_active:
            trailing_active = True
            portfolio['trailing_active'] = True
            msg = (
                f"📈 [{symbol}] DYNAMIC ATR TRAILING STOP ACTIVATED for SHORT | "
                f"Move: +${price_move_fav:.4f} (+{unrealized_pct * 100:.2f}%) | "
                f"Threshold: +${tsl_activation_dist:.4f} ({tsl_atr_activation_mult}x ATR: ${current_atr:.4f})"
            )
            print(msg, flush=True)
            log_to_db(session, symbol, "INFO", msg)

        # Trail strictly by tsl_atr_trail_mult × ATR from the TROUGH price
        trough = float(lowest_price) if lowest_price else cp
        new_sl = trough + tsl_trailing_dist

        if stop_loss == 0 or new_sl < stop_loss:
            old_sl = stop_loss
            portfolio['stop_loss_price'] = new_sl
            portfolio['stop_loss'] = new_sl
            stop_loss = new_sl
            locked_pnl = ((ep - new_sl) / ep) * 100
            msg = (
                f"📈 DYNAMIC ATR TRAILING STOP UPDATED: {symbol} | "
                f"New SL: ${new_sl:.4f} (was ${old_sl:.4f}) | "
                f"Trough: ${trough:.2f} | Trailing Dist: ${tsl_trailing_dist:.4f} "
                f"({tsl_atr_trail_mult}x ATR) | Locked Profit: {locked_pnl:+.2f}%"
            )
            print(msg, flush=True)
            log_to_db(session, symbol, "INFO", msg)

            if futures_client:
                set_stop_loss_order(
                    futures_client, symbol, 'SHORT', stop_loss,
                    trailing_distance=tsl_trailing_dist, atr_val=current_atr,
                )

        # Persist trailing state to DB for frontend
        try:
            latest_record = (
                session.query(PortfolioState)
                .filter(
                    PortfolioState.symbol == symbol,
                    PortfolioState.position_direction == 'SHORT',
                )
                .order_by(PortfolioState.id.desc())
                .first()
            )
            if latest_record:
                latest_record.stop_loss_price = stop_loss
                latest_record.stop_loss = stop_loss
                latest_record.trailing_active = True
                latest_record.lowest_price_since_entry = float(lowest_price) if lowest_price else cp
                session.commit()
        except Exception as e:
            session.rollback()
            print(f"Warning: Failed to persist trailing SL to DB: {e}", flush=True)

    # ── Check Stop Loss hit ──
    if not risk_exit_triggered and stop_loss > 0 and cp >= stop_loss:
        sl_type = 'TRAILING_STOP' if trailing_active else 'STOP_LOSS'
        msg = (
            f"🚨 [{symbol}] SHORT {sl_type} HIT at ${cp:.2f} "
            f"(SL: ${stop_loss:.2f}) — EXECUTING CLOSE ON BINANCE"
        )
        print(msg, flush=True)
        log_to_db(session, symbol, "EXIT", msg)
        portfolio = _close_position_handler(
            portfolio, current_price, symbol, session, sl_type,
            futures_client, bullish_ob, bearish_ob, current_rsi, current_zscore, macro_info,
        )
        risk_exit_triggered = True

    # ── Stagnant Trade Closer (>2h open, flat PnL) ──
    if not risk_exit_triggered and not DISABLE_STAGNANT_EXIT:
        first_entry_row = (
            session.query(PortfolioState)
            .filter(
                PortfolioState.symbol == symbol,
                PortfolioState.position_direction == 'SHORT',
            )
            .order_by(PortfolioState.id.asc())
            .first()
        )
        if first_entry_row:
            open_seconds = (datetime.now() - first_entry_row.timestamp).total_seconds()
            stagnant_seconds = STAGNANT_HOURS_THRESHOLD * 3600
            if open_seconds > stagnant_seconds and -STAGNANT_PNL_BAND <= unrealized_pct <= STAGNANT_PNL_BAND:
                open_hours = open_seconds / 3600
                msg = (
                    f"⏰ STAGNANT POSITION CLOSED: {symbol} SHORT open for "
                    f"{open_hours:.1f}h with flat PnL ({unrealized_pct * 100:+.2f}%). "
                    f"Freeing slot for fresh opportunities."
                )
                print(msg, flush=True)
                log_to_db(session, symbol, "EXIT", msg)
                portfolio = _close_position_handler(
                    portfolio, current_price, symbol, session, 'STAGNANT',
                    futures_client, bullish_ob, bearish_ob, current_rsi, current_zscore, macro_info,
                )
                risk_exit_triggered = True

    return portfolio, risk_exit_triggered
