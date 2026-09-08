"""
analyzer/sync.py
────────────────
Binance position reconciliation layer for the Execution Analyzer.

Responsible for:
    - Syncing the local DB with the live Binance Futures position state.
    - Detecting and archiving "Ghost Positions" (positions closed externally).
    - Computing true NET realised PnL via the income_history API.
    - Maintaining price watermarks and enforcing SL guards.

Public API:
    sync_binance_position  – Main reconciliation entry point called from core.py.

Critical invariants preserved verbatim from the original analyzer.py:
    - NET_PNL_INCOME_TYPES fee allowlist (REALIZED_PNL + COMMISSION + FUNDING_FEE).
    - income_history retry loop (3 attempts × 3 s sleep).
    - Secondary fallback to futures_account_trades on exhausted retries.
    - NEVER use (SL − Entry) × Qty math for PnL — always use API data.
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from database import (
    SessionLocal,
    PortfolioState,
    TradeHistory,
)
from futures_executor import (
    get_position_info,
    set_stop_loss_order,
    execute_partial_tp_scaleout,
)

from analyzer.constants import (
    NET_PNL_INCOME_TYPES,
    INCOME_HISTORY_MAX_RETRIES,
    INCOME_HISTORY_RETRY_SLEEP_S,
)
from analyzer.portfolio import load_portfolio
from analyzer.notifications import send_telegram_alert
from analyzer.utils import log_to_db

import logging

logger = logging.getLogger("Analyzer.sync")


def sync_binance_position(
    symbol: str,
    portfolio: Dict[str, Any],
    session: object,
    futures_client: Optional[object],
    current_price: float,
    current_atr: float,
    tsl_atr_trail_mult: float,
    partial_tp_pct: float,
    sl_atr_mult: float,
    db_decision: str,
    active_mode_value: str,
) -> Tuple[Dict[str, Any], str, bool]:
    """Reconcile local DB state with live Binance Futures position.

    Called during WAIT cycles (``decision == 'WAIT'``) to ensure the DB
    always reflects the true exchange state for the Laravel dashboard.

    Handles four cases:
        A. Binance confirms a live position → sync entry price, SL, direction.
        B. Binance reports position closed but DB thinks it's open
           (Ghost Position) → fetch true NET PnL from income_history,
           archive to trade_history, clean up PortfolioState.
        C. No Binance client available → use local portfolio as source of truth.
        D. Position already flat in both Binance and DB → no-op.

    ── Ghost Position Archiver (Case B) ─────────────────────────────────
    PnL resolution strategy (in order):
        1. ``futures_income_history`` (all income types, no filter) aggregated
           by REALIZED_PNL + COMMISSION + FUNDING_FEE for the NET figure.
           Retried up to ``INCOME_HISTORY_MAX_RETRIES`` times with
           ``INCOME_HISTORY_RETRY_SLEEP_S`` between attempts (Binance latency).
        2. ``futures_account_trades`` (sum of ``realizedPnl``) as secondary
           fallback if income_history stays empty after all retries.
        3. PnL logged as $0.00 if both API calls fail or return empty.

    CRITICAL: (SL − Entry) × Qty math is NEVER used for PnL because it
    produces massive fake losses when the stop was not the actual exit price.

    Args:
        symbol:             Trading pair symbol.
        portfolio:          Current portfolio state dict.
        session:            Active SQLAlchemy session.
        futures_client:     Optional Binance Futures client.
        current_price:      Latest market price.
        current_atr:        Latest ATR value (for SL calculation if missing).
        tsl_atr_trail_mult: TSL trail multiplier (for partial TP).
        partial_tp_pct:     Partial TP threshold.
        sl_atr_mult:        SL distance multiplier from entry (for missing SL calc).
        db_decision:        Current decision string to be updated by sync.
        active_mode_value:  Current regime value for DB record.

    Returns:
        ``(updated_portfolio, updated_db_decision, in_position)``
    """
    in_position = (
        portfolio.get('asset_balance') is not None
        and portfolio['asset_balance'] is not None
        and float(portfolio['asset_balance']) > 0
    )

    db_pos_direction = portfolio.get('position_direction')
    db_entry_price = (
        float(portfolio['average_entry_price'])
        if portfolio.get('average_entry_price')
        else None
    )
    db_unrealized_pnl = None

    if futures_client:
        pos_info = get_position_info(futures_client, symbol)

        # ── Case A: Live position confirmed on Binance ──
        if pos_info and pos_info['size'] > 0:
            db_decision = pos_info['direction']           # 'LONG' or 'SHORT'
            db_pos_direction = pos_info['direction']
            db_entry_price = pos_info['entry_price']
            db_unrealized_pnl = pos_info['unrealized_pnl']
            in_position = True
            print(
                f"  🔒 [{symbol}] Binance sync: {db_decision} | "
                f"Entry=${db_entry_price:.2f} | uPnL=${db_unrealized_pnl:.2f}",
                flush=True,
            )

            # Active Position Sync: TP-MATH log & Partial Scale-Out
            u_pnl = float(
                pos_info.get('unRealizedProfit')
                if 'unRealizedProfit' in pos_info
                else pos_info.get('unrealized_pnl', 0.0)
            )
            pos_amt = abs(float(
                pos_info.get('positionAmt')
                if 'positionAmt' in pos_info
                else pos_info.get('size', 0.0)
            ))
            entry_price = float(
                pos_info.get('entryPrice')
                if 'entryPrice' in pos_info
                else pos_info.get('entry_price', 0.0)
            )

            position_value = pos_amt * entry_price
            unrealized_pct = abs(u_pnl) / position_value if position_value > 0 else 0.0

            logger.info(
                f"🔎 [TP-MATH] {symbol} | uPnL: ${u_pnl:.2f} | "
                f"Value: ${position_value:.2f} | ROE: {unrealized_pct * 100:.2f}% | "
                f"Target: {partial_tp_pct * 100:.2f}% | Hit: {portfolio.get('partial_tp_hit', False)}"
            )

            if (
                u_pnl > 0
                and unrealized_pct >= partial_tp_pct
                and not portfolio.get('partial_tp_hit', False)
            ):
                total_qty = pos_amt
                if total_qty > 0 and futures_client:
                    tsl_trailing_dist = tsl_atr_trail_mult * current_atr if current_atr else None
                    tp_order, rem_qty = execute_partial_tp_scaleout(
                        futures_client, symbol, db_pos_direction, total_qty, entry_price,
                        trailing_distance=tsl_trailing_dist, atr_val=current_atr,
                    )
                    if tp_order and rem_qty > 0:
                        portfolio['partial_tp_hit'] = True
                        closed_qty = total_qty - rem_qty
                        portfolio['asset_balance'] = rem_qty
                        if portfolio.get('total_cost'):
                            portfolio['total_cost'] = float(portfolio['total_cost']) * (rem_qty / total_qty)
                        cp = float(current_price)
                        if db_pos_direction == 'LONG':
                            portfolio['usdt_balance'] = (
                                float(portfolio.get('usdt_balance', 0)) + (closed_qty * cp)
                            )
                            pnl_realized_est = (cp - entry_price) * closed_qty
                        else:
                            portfolio['usdt_balance'] = (
                                float(portfolio.get('usdt_balance', 0))
                                + (closed_qty * (2 * entry_price - cp))
                            )
                            pnl_realized_est = (entry_price - cp) * closed_qty

                        msg = (
                            f"🎯 [{symbol}] PARTIAL TAKE PROFIT EXECUTED ({db_pos_direction}) | "
                            f"Closed 50% size ({closed_qty} units) at ${cp:.4f} (+{unrealized_pct * 100:.2f}%) | "
                            f"Est. Profit: +${pnl_realized_est:.2f} | Remaining Qty: {rem_qty} | "
                            f"Stop Loss locked at Entry ${entry_price:.4f}"
                        )
                        print(msg, flush=True)
                        logger.info(msg)
                        log_to_db(session, symbol, "ENTRY", msg)

                        history_record = TradeHistory(
                            symbol=symbol,
                            direction=db_pos_direction,
                            entry_price=entry_price,
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
                                    PortfolioState.position_direction == db_pos_direction,
                                )
                                .order_by(PortfolioState.id.desc())
                                .first()
                            )
                            if latest_record:
                                latest_record.partial_tp_hit = True
                                latest_record.asset_balance = rem_qty
                                latest_record.total_cost = portfolio['total_cost']
                                latest_record.usdt_balance = portfolio['usdt_balance']
                                latest_record.stop_loss_price = entry_price
                                latest_record.stop_loss = entry_price
                            session.commit()
                        except Exception as e:
                            session.rollback()
                            print(
                                f"Warning: Failed to persist partial TP state to DB: {e}",
                                flush=True,
                            )

            # Preserve existing local SL; only calculate if missing
            sl_val = portfolio.get('stop_loss_price')
            if sl_val is None or float(sl_val) <= 0:
                existing_sl_row = (
                    session.query(PortfolioState.stop_loss_price, PortfolioState.stop_loss)
                    .filter(
                        PortfolioState.symbol == symbol,
                        PortfolioState.asset_balance > 0,
                        (
                            PortfolioState.stop_loss_price.isnot(None)
                            | PortfolioState.stop_loss.isnot(None)
                        ),
                    )
                    .order_by(PortfolioState.id.desc())
                    .first()
                )

                if existing_sl_row and (existing_sl_row.stop_loss_price or existing_sl_row.stop_loss):
                    sl_val = float(existing_sl_row.stop_loss_price or existing_sl_row.stop_loss)
                    print(
                        f"🔄 [{symbol}] Preserved existing local Stop Loss during sync: ${sl_val:.4f}",
                        flush=True,
                    )
                else:
                    if db_pos_direction == 'LONG':
                        sl_val = float(db_entry_price) - (sl_atr_mult * current_atr)
                    elif db_pos_direction == 'SHORT':
                        sl_val = float(db_entry_price) + (sl_atr_mult * current_atr)
                    print(
                        f"🔄 [{symbol}] Calculated missing Stop Loss for new position during sync: ${sl_val:.4f}",
                        flush=True,
                    )

            portfolio['stop_loss_price'] = sl_val
            portfolio['stop_loss'] = sl_val

            if sl_val and float(sl_val) > 0 and futures_client:
                try:
                    open_ords = futures_client.futures_get_open_orders(symbol=symbol)
                    algo_ords = (
                        futures_client.futures_get_open_algo_orders(symbol=symbol)
                        if hasattr(futures_client, 'futures_get_open_algo_orders')
                        else []
                    )
                    has_sl = any(
                        o.get('type') in ('STOP', 'STOP_MARKET')
                        or o.get('orderType') in ('STOP', 'STOP_MARKET')
                        for o in (open_ords + algo_ords)
                    )
                    if not has_sl:
                        print(
                            f"🛡️ [{symbol}] Missing live SL on Binance! "
                            f"Placing emergency hard Stop Loss at ${float(sl_val):.4f}",
                            flush=True,
                        )
                        set_stop_loss_order(futures_client, symbol, db_pos_direction, float(sl_val))
                except Exception as sl_check_err:
                    print(
                        f"⚠️ [{symbol}] Could not check/set emergency SL on Binance: {sl_check_err}",
                        flush=True,
                    )

        # ── Case B: Ghost Position — Binance flat but DB thinks open ──
        elif pos_info and pos_info['size'] == 0.0:
            if in_position and db_pos_direction in ('LONG', 'SHORT'):
                print(
                    f"🧹 [{symbol}] Binance reports no position. "
                    "Fetching true realized PNL and archiving (MANUAL_CLOSE)...",
                    flush=True,
                )

                ep = float(db_entry_price) if db_entry_price and float(db_entry_price) > 0 else float(current_price)
                asset_bal = float(portfolio.get('asset_balance') or 0.0)

                # Start with 0.0 — NEVER use (SL-Entry)*Qty math which causes
                # fake massive losses when futures_income_history has API latency.
                realized_pnl_usd = 0.0
                if futures_client:
                    try:
                        last_closed = (
                            session.query(PortfolioState.timestamp)
                            .filter(
                                PortfolioState.symbol == symbol,
                                PortfolioState.asset_balance == 0,
                            )
                            .order_by(PortfolioState.id.desc())
                            .first()
                        )

                        query = session.query(PortfolioState.timestamp).filter(
                            PortfolioState.symbol == symbol,
                            PortfolioState.asset_balance > 0,
                        )
                        if last_closed:
                            query = query.filter(PortfolioState.timestamp > last_closed.timestamp)

                        first_open = query.order_by(PortfolioState.id.asc()).first()

                        if first_open:
                            created_at = first_open.timestamp
                            start_time_ms = int(created_at.timestamp() * 1000)

                            # ── Fetch ALL income types for this symbol (no incomeType filter).
                            # Aggregate REALIZED_PNL + COMMISSION + FUNDING_FEE to get the
                            # true NET_REALIZED_PNL (trade profit minus commissions minus funding fees).
                            # Querying only REALIZED_PNL causes wallet-balance drift because
                            # COMMISSION and FUNDING_FEE drain the account in the background.
                            #
                            # ── RETRY LOOP: Binance futures_income_history has a latency
                            # of a few seconds after a position closes. Retry up to
                            # INCOME_HISTORY_MAX_RETRIES times with INCOME_HISTORY_RETRY_SLEEP_S
                            # sleep before falling back to futures_account_trades.
                            # NEVER use (SL-Entry)*Qty math.
                            income_hist = []
                            net_components = []
                            for attempt in range(1, INCOME_HISTORY_MAX_RETRIES + 1):
                                income_hist = futures_client.futures_income_history(
                                    symbol=symbol,
                                    startTime=start_time_ms,
                                    limit=1000,
                                    # incomeType intentionally omitted → returns all streams
                                )
                                if income_hist:
                                    net_components = [
                                        float(x['income'])
                                        for x in income_hist
                                        if x['time'] >= start_time_ms
                                        and x.get('incomeType') in NET_PNL_INCOME_TYPES
                                    ]
                                    if net_components:
                                        break  # Got good data — exit retry loop
                                if attempt < INCOME_HISTORY_MAX_RETRIES:
                                    print(
                                        f"  ⏳ [{symbol}] income_history empty/no net components "
                                        f"on attempt {attempt}/{INCOME_HISTORY_MAX_RETRIES}. "
                                        f"Retrying in {INCOME_HISTORY_RETRY_SLEEP_S}s...",
                                        flush=True,
                                    )
                                    time.sleep(INCOME_HISTORY_RETRY_SLEEP_S)

                            if net_components:
                                gross_pnl = sum(
                                    float(x['income'])
                                    for x in income_hist
                                    if x['time'] >= start_time_ms
                                    and x.get('incomeType') == 'REALIZED_PNL'
                                )
                                commissions = sum(
                                    float(x['income'])
                                    for x in income_hist
                                    if x['time'] >= start_time_ms
                                    and x.get('incomeType') == 'COMMISSION'
                                )
                                funding_fees = sum(
                                    float(x['income'])
                                    for x in income_hist
                                    if x['time'] >= start_time_ms
                                    and x.get('incomeType') == 'FUNDING_FEE'
                                )
                                # commissions & funding_fees are already negative in Binance API
                                realized_pnl_usd = gross_pnl + commissions + funding_fees
                                print(
                                    f"  💸 NET Realized PNL: ${realized_pnl_usd:.4f} "
                                    f"[Gross PNL: ${gross_pnl:.4f} | "
                                    f"Commission: ${commissions:.4f} | "
                                    f"Funding Fee: ${funding_fees:.4f}]",
                                    flush=True,
                                )
                            else:
                                # ── SECONDARY FALLBACK: income_history still empty after all retries.
                                # Sum realizedPnl from futures_account_trades.
                                # This is always accurate (no latency issue) and avoids blind math.
                                print(
                                    f"  ⚠️ [{symbol}] income_history empty after "
                                    f"{INCOME_HISTORY_MAX_RETRIES} retries. "
                                    "Falling back to futures_account_trades...",
                                    flush=True,
                                )
                                try:
                                    acct_trades = futures_client.futures_account_trades(
                                        symbol=symbol,
                                        startTime=start_time_ms,
                                        limit=1000,
                                    )
                                    if acct_trades:
                                        closing_trades = [
                                            t for t in acct_trades
                                            if int(t.get('time', 0)) >= start_time_ms
                                            and t.get('realizedPnl') is not None
                                        ]
                                        if closing_trades:
                                            realized_pnl_usd = sum(
                                                float(t['realizedPnl']) for t in closing_trades
                                            )
                                            print(
                                                f"  💸 [{symbol}] PNL via futures_account_trades "
                                                f"({len(closing_trades)} trades): ${realized_pnl_usd:.4f}",
                                                flush=True,
                                            )
                                        else:
                                            print(
                                                f"  ⚠️ [{symbol}] No closing trades found in "
                                                f"futures_account_trades after {created_at}. "
                                                "Logging PNL as $0.00.",
                                                flush=True,
                                            )
                                    else:
                                        print(
                                            f"  ⚠️ [{symbol}] futures_account_trades returned empty. "
                                            "Logging PNL as $0.00.",
                                            flush=True,
                                        )
                                except Exception as trades_err:
                                    print(
                                        f"  ⚠️ [{symbol}] futures_account_trades error: {trades_err}. "
                                        "Logging PNL as $0.00.",
                                        flush=True,
                                    )
                        else:
                            print(
                                f"  ⚠️ Could not find position created_at in DB. "
                                "Logging PNL as $0.00.",
                                flush=True,
                            )

                    except Exception as e:
                        print(f"  ⚠️ API PNL fetch error: {e}. Logging PNL as $0.00.", flush=True)

                # ── LAST-RESORT FALLBACK: if both APIs returned $0.00, try
                # using the last known unrealized PnL from PortfolioState ──
                if realized_pnl_usd == 0.0:
                    try:
                        last_pnl_row = (
                            session.query(PortfolioState.pnl_usd)
                            .filter(
                                PortfolioState.symbol == symbol,
                                PortfolioState.pnl_usd.isnot(None),
                            )
                            .order_by(PortfolioState.id.desc())
                            .first()
                        )
                        if last_pnl_row and last_pnl_row.pnl_usd is not None and float(last_pnl_row.pnl_usd) != 0.0:
                            realized_pnl_usd = float(last_pnl_row.pnl_usd)
                            print(
                                f"  📊 [{symbol}] Using last known uPnL as estimate: ${realized_pnl_usd:.4f}",
                                flush=True,
                            )
                    except Exception as fallback_err:
                        print(
                            f"  ⚠️ [{symbol}] Failed to query last known uPnL: {fallback_err}",
                            flush=True,
                        )

                # Determine exit_reason based on PNL source
                exit_reason = 'MANUAL_CLOSE'
                if realized_pnl_usd != 0.0:
                    # Check if this came from the last-resort fallback
                    try:
                        last_pnl_row_check = (
                            session.query(PortfolioState.pnl_usd)
                            .filter(
                                PortfolioState.symbol == symbol,
                                PortfolioState.pnl_usd.isnot(None),
                            )
                            .order_by(PortfolioState.id.desc())
                            .first()
                        )
                        if (last_pnl_row_check and last_pnl_row_check.pnl_usd is not None
                                and abs(float(last_pnl_row_check.pnl_usd) - realized_pnl_usd) < 0.0001):
                            # PNL came from DB estimate, not from API
                            if not net_components and not (locals().get('closing_trades')):
                                exit_reason = 'MANUAL_CLOSE_ESTIMATED'
                    except Exception:
                        pass

                pnl_pct_val = 0.0
                if ep > 0 and asset_bal > 0:
                    pnl_pct_val = (realized_pnl_usd / (asset_bal * ep)) * 100

                outcome = 'WIN' if realized_pnl_usd > 0 else 'LOSS'
                history_record = TradeHistory(
                    symbol=symbol,
                    direction=db_pos_direction,
                    entry_price=ep,
                    exit_price=float(current_price),
                    quantity=asset_bal,
                    pnl_usd=realized_pnl_usd,
                    pnl_pct=pnl_pct_val,
                    outcome=outcome,
                    exit_reason=exit_reason,
                    closed_at=datetime.utcnow(),
                )

                try:
                    session.add(history_record)
                    session.query(PortfolioState).filter(
                        PortfolioState.symbol == symbol
                    ).delete(synchronize_session=False)

                    new_usdt_bal = (
                        float(portfolio.get('usdt_balance') or 0.0)
                        + (asset_bal * float(current_price))
                        + realized_pnl_usd
                    )
                    closed_rec = PortfolioState(
                        timestamp=datetime.now(),
                        symbol=symbol,
                        decision='CLOSED',
                        current_price=float(current_price),
                        usdt_balance=new_usdt_bal,
                        asset_balance=0.0,
                        position_direction=None,
                        average_entry_price=None,
                        dca_level=0,
                        last_exec_price=None,
                        total_cost=0.0,
                        highest_price_since_entry=None,
                        lowest_price_since_entry=None,
                        stop_loss_price=None,
                        stop_loss=None,
                        strategy=portfolio.get('strategy'),
                        trailing_active=False,
                        pnl_pct=pnl_pct_val,
                        pnl_usd=realized_pnl_usd,
                        total_portfolio_value=new_usdt_bal,
                        active_mode=portfolio.get('active_mode'),
                    )
                    session.add(closed_rec)
                    session.commit()
                    print(
                        f"  ✅ [{symbol}] Successfully archived ghost position with actual PNL.",
                        flush=True,
                    )
                except Exception as e:
                    session.rollback()
                    print(f"  ⚠️ Error archiving ghost position for {symbol}: {e}", flush=True)

                db_decision = 'MANUAL_CLOSE'
                portfolio['asset_balance'] = 0.0
                in_position = False

    # Fallback: if db_decision is still WAIT but local portfolio has a position
    if db_decision == 'WAIT' and in_position and db_pos_direction in ('LONG', 'SHORT'):
        db_decision = db_pos_direction

    if in_position:
        pos_direction = db_pos_direction or portfolio.get('position_direction')
        cp = float(current_price)

        # ── Update watermarks ──
        if pos_direction == 'LONG':
            old_highest = portfolio.get('highest_price_since_entry')
            if old_highest is None or cp > float(old_highest):
                portfolio['highest_price_since_entry'] = cp
                old_val = f"${float(old_highest):.2f}" if old_highest else "$0.00"
                print(
                    f"📈 [{symbol}] LONG new high watermark: ${cp:.2f} (was {old_val})",
                    flush=True,
                )

        elif pos_direction == 'SHORT':
            old_lowest = portfolio.get('lowest_price_since_entry')
            if old_lowest is None or cp < float(old_lowest):
                portfolio['lowest_price_since_entry'] = cp
                old_val = f"${float(old_lowest):.2f}" if old_lowest else "$0.00"
                print(
                    f"📉 [{symbol}] SHORT new low watermark: ${cp:.2f} (was {old_val})",
                    flush=True,
                )

        # ── Always save synced PortfolioState row ──
        total_value = float(portfolio['usdt_balance']) + (float(portfolio['asset_balance']) * cp)
        sl_val = portfolio.get('stop_loss_price') or portfolio.get('stop_loss')

        portfolio_record = PortfolioState(
            timestamp=datetime.now(),
            symbol=symbol,
            decision=db_decision,
            current_price=cp,
            usdt_balance=float(portfolio['usdt_balance']),
            asset_balance=float(portfolio['asset_balance']),
            position_direction=db_pos_direction,
            average_entry_price=float(db_entry_price) if db_entry_price else None,
            dca_level=int(portfolio['dca_level']),
            last_exec_price=(
                float(portfolio['last_exec_price'])
                if portfolio['last_exec_price'] is not None
                else None
            ),
            total_cost=float(portfolio['total_cost']),
            highest_price_since_entry=(
                float(portfolio['highest_price_since_entry'])
                if portfolio['highest_price_since_entry'] is not None
                else None
            ),
            lowest_price_since_entry=(
                float(portfolio['lowest_price_since_entry'])
                if portfolio['lowest_price_since_entry'] is not None
                else None
            ),
            stop_loss_price=float(sl_val) if sl_val is not None and float(sl_val) > 0 else None,
            stop_loss=float(sl_val) if sl_val is not None and float(sl_val) > 0 else None,
            strategy=portfolio.get('strategy'),
            trailing_active=portfolio.get('trailing_active', False),
            pnl_pct=None,
            pnl_usd=(float(db_unrealized_pnl) if db_unrealized_pnl is not None else None),
            total_portfolio_value=float(round(total_value, 2)),
            active_mode=active_mode_value,
        )
        for attempt in range(3):
            try:
                session.add(portfolio_record)
                session.commit()
                msg = (
                    f"✅ [{symbol}] Position state synced to DB: "
                    f"decision='{db_decision}', direction='{db_pos_direction}', "
                    f"entry=${db_entry_price or 0:.2f}"
                )
                print(f"  {msg}", flush=True)
                log_to_db(session, symbol, "INFO", msg)
                break
            except Exception as e:
                session.rollback()
                if attempt == 2:
                    print(
                        f"Warning: Failed to save synced portfolio state to DB after 3 attempts: {e}",
                        flush=True,
                    )
                    traceback.print_exc()
                else:
                    time.sleep(1)

    return portfolio, db_decision, in_position
