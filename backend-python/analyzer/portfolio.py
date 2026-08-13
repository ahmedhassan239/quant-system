"""
analyzer/portfolio.py
─────────────────────
Portfolio state management for the Execution Analyzer.

Handles all DB-aware position lifecycle operations:
    - Loading the latest portfolio snapshot from the DB.
    - Closing a position (PnL calc, Futures close, DB archive, Telegram).
    - Persisting lightweight HOLD-cycle tracking updates.

All heavy formatting/alerting in this module calls into
``notifications.send_telegram_alert``. Direct Telegram API calls
are not made from here.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Any, Dict, Optional

from config import ALERT_PREFIX, STOP_LOSS_PCT
from database import (
    SessionLocal,
    PortfolioState,
    TradeHistory,
    SLOT_BUDGET,
    TOTAL_CAPITAL,
)
from futures_executor import close_position, get_position_info

from analyzer.notifications import send_telegram_alert
from analyzer.utils import log_to_db


# ══════════════════════════════════════════════════════════════════════
#  PORTFOLIO STATE LOADER
# ══════════════════════════════════════════════════════════════════════

def load_portfolio(session: object, symbol: str) -> Dict[str, Any]:
    """Load the latest portfolio state for a symbol from the database.

    Includes a multi-level Stop-Loss fallback chain to handle the case
    where the most recent row has a NULL ``stop_loss_price``:
        1. Try ``stop_loss_price`` on the latest row.
        2. Try ``stop_loss`` on the latest row (legacy column alias).
        3. Walk backwards through history rows to find the most recent
           non-NULL SL value for an active position.

    Args:
        session: Active SQLAlchemy session.
        symbol:  Trading pair (e.g. ``'BTCUSDT'``).

    Returns:
        A dict with the following keys (all may be ``None`` for a new symbol):
            ``usdt_balance``, ``asset_balance``, ``average_entry_price``,
            ``dca_level``, ``last_exec_price``, ``total_cost``,
            ``highest_price_since_entry``, ``lowest_price_since_entry``,
            ``position_direction``, ``stop_loss_price``, ``stop_loss``,
            ``trailing_active``, ``partial_tp_hit``, ``strategy``.

    Returns a zero-balance default dict when no DB row exists for the symbol.
    """
    last_state = session.query(PortfolioState).filter(
        PortfolioState.symbol == symbol
    ).order_by(PortfolioState.id.desc()).first()

    if last_state:
        sl_val = getattr(last_state, 'stop_loss_price', None)
        if sl_val is None:
            sl_val = getattr(last_state, 'stop_loss', None)

        # If stop_loss_price is None on the latest row but position is active,
        # walk back through history to find the most recent non-null local SL.
        if (
            sl_val is None
            and getattr(last_state, 'asset_balance', 0)
            and float(last_state.asset_balance) > 0
        ):
            prev_sl = (
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
            if prev_sl:
                sl_val = (
                    prev_sl.stop_loss_price
                    if prev_sl.stop_loss_price is not None
                    else prev_sl.stop_loss
                )

        return {
            'usdt_balance':                float(last_state.usdt_balance) if last_state.usdt_balance is not None else SLOT_BUDGET,
            'asset_balance':               last_state.asset_balance,
            'average_entry_price':         getattr(last_state, 'average_entry_price', None),
            'dca_level':                   getattr(last_state, 'dca_level', 0),
            'last_exec_price':             getattr(last_state, 'last_exec_price', None),
            'total_cost':                  getattr(last_state, 'total_cost', 0.0),
            'highest_price_since_entry':   last_state.highest_price_since_entry,
            'lowest_price_since_entry':    getattr(last_state, 'lowest_price_since_entry', None),
            'position_direction':          getattr(last_state, 'position_direction', None),
            'stop_loss_price':             float(sl_val) if sl_val is not None else None,
            'stop_loss':                   float(sl_val) if sl_val is not None else None,
            'trailing_active':             getattr(last_state, 'trailing_active', False),
            'partial_tp_hit':              getattr(last_state, 'partial_tp_hit', False),
            'strategy':                    getattr(last_state, 'strategy', None),
        }

    # No DB row exists for this symbol — return zero-balance defaults
    return {
        'usdt_balance':              SLOT_BUDGET,
        'asset_balance':             0.0,
        'average_entry_price':       None,
        'dca_level':                 0,
        'last_exec_price':           None,
        'total_cost':                0.0,
        'highest_price_since_entry': None,
        'lowest_price_since_entry':  None,
        'position_direction':        None,
        'stop_loss_price':           None,
        'stop_loss':                 None,
        'trailing_active':           False,
        'partial_tp_hit':            False,
        'strategy':                  None,
    }


# ══════════════════════════════════════════════════════════════════════
#  CLOSE POSITION HANDLER
# ══════════════════════════════════════════════════════════════════════

def _close_position_handler(
    portfolio: Dict[str, Any],
    current_price: float,
    symbol: str,
    session: object,
    exit_reason: str,
    futures_client: Optional[object] = None,
    bullish_ob: Optional[Dict[str, Any]] = None,
    bearish_ob: Optional[Dict[str, Any]] = None,
    current_rsi: Optional[float] = None,
    current_zscore: Optional[float] = None,
    macro_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Close an open position: compute PnL, execute on Futures, archive to DB, alert Telegram.

    Data resolution order (source of truth cascade):
        1. Live Binance position (via ``get_position_info``).
        2. Local ``portfolio`` dict.
        3. Hardcoded safe defaults (e.g. direction = 'LONG', price = current_price).

    Execution guard:
        If ``futures_client`` is provided and ``close_position`` returns ``None``
        (API failure / already closed), the local DB update is **aborted** so
        the next cycle can retry safely.

    Args:
        portfolio:      Latest portfolio state dict (from ``load_portfolio``).
        current_price:  Current market price at the time of exit.
        symbol:         Trading pair symbol.
        session:        Active SQLAlchemy session.
        exit_reason:    Exit label (``'STOP_LOSS'``, ``'TRAILING_STOP'``,
                        ``'SIGNAL'``, ``'TREND_REVERSAL_EJECT'``, ``'STAGNANT'``,
                        ``'POSITION_UPGRADE'``, etc.).
        futures_client: Optional live Binance Futures client.
        bullish_ob:     Latest bullish order block dict (for Telegram context).
        bearish_ob:     Latest bearish order block dict (for Telegram context).
        current_rsi:    Current RSI (for Telegram context).
        current_zscore: Current Z-Score (for Telegram context).
        macro_info:     Latest macro state dict (for Telegram context).

    Returns:
        Updated ``portfolio`` dict with ``asset_balance = 0.0`` and reset fields.
    """
    # ── 1. Pull accurately from active position / live Binance position ──
    live_pos = None
    if futures_client:
        try:
            live_pos = get_position_info(futures_client, symbol)
        except Exception as e:
            print(f"⚠️ [{symbol}] Error checking live position in close handler: {e}", flush=True)

    direction = portfolio.get('position_direction') or portfolio.get('direction')
    asset_balance = float(portfolio.get('asset_balance') or 0.0)
    buy_price = portfolio.get('average_entry_price') or portfolio.get('entry_price')

    if live_pos and live_pos.get('size', 0) > 0:
        if not direction or direction == 'None':
            direction = live_pos.get('direction')
        if asset_balance <= 0:
            asset_balance = float(live_pos.get('size', 0.0))
        if not buy_price or float(buy_price) <= 0:
            buy_price = float(live_pos.get('entry_price', 0.0))

    # ── 2. Fallback: NEVER pass None to a non-null column ──
    if not direction or direction == 'None':
        direction = 'LONG'
    if not buy_price or float(buy_price) <= 0:
        buy_price = float(current_price)
    if not asset_balance or asset_balance < 0:
        asset_balance = 0.0

    total_cost = float(portfolio.get('total_cost') or (asset_balance * float(buy_price)))
    pnl_pct_val = None
    pnl_usd_val = None
    pnl_section = ""

    # ── Calculate PnL ──
    if buy_price and float(buy_price) > 0:
        ep = float(buy_price)
        cp = float(current_price)
        if direction == 'LONG':
            pnl_pct_val = ((cp - ep) / ep) * 100
            pnl_usd_val = (cp - ep) * asset_balance
            sell_value = asset_balance * cp
        else:  # SHORT
            pnl_pct_val = ((ep - cp) / ep) * 100
            pnl_usd_val = (ep - cp) * asset_balance
            sell_value = asset_balance * (2 * ep - cp)
        sign = "+" if pnl_usd_val >= 0 else ""
        pnl_section = f"\n- PnL (This Trade): {sign}{pnl_pct_val:.2f}% ({sign}${pnl_usd_val:.2f})"
    else:
        sell_value = asset_balance * float(current_price)

    # ── Execute Futures close order (use LIVE Binance position size) ──
    if futures_client and asset_balance > 0:
        import logging as _close_logging
        _close_logger = _close_logging.getLogger("FuturesExecutor")
        # Query the REAL position size from Binance to avoid quantity mismatches
        close_qty = live_pos['size'] if (live_pos and live_pos.get('size', 0) > 0) else asset_balance
        _close_logger.warning(
            f"🚨 SL TRIGGERED & EXECUTED: {symbol} at {current_price} "
            f"(exit_reason={exit_reason}, closing qty={close_qty})"
        )
        order = close_position(futures_client, symbol, direction, close_qty)
        if order:
            order_id = (
                order.get('orderId', 'ALREADY_CLOSED')
                if isinstance(order, dict)
                else 'ALREADY_CLOSED'
            )
            print(
                f"✅ [{symbol}] Futures CLOSE {direction} executed | "
                f"OrderID: {order_id} | Qty: {close_qty}",
                flush=True,
            )
        else:
            print(
                f"⚠️ [{symbol}] Futures CLOSE {direction} order failed. "
                "Aborting local DB update so it can retry.",
                flush=True,
            )
            return portfolio

    # ── Reset portfolio ──
    close_label = f"CLOSE_{direction}"
    portfolio['usdt_balance'] += round(sell_value, 2)
    portfolio['asset_balance'] = 0.0
    portfolio['average_entry_price'] = None
    portfolio['dca_level'] = 0
    portfolio['last_exec_price'] = None
    portfolio['total_cost'] = 0.0
    portfolio['highest_price_since_entry'] = None
    portfolio['lowest_price_since_entry'] = None
    portfolio['position_direction'] = None

    total_value = float(portfolio['usdt_balance'])

    # 1. Archive to TradeHistory
    outcome = 'WIN' if pnl_usd_val and pnl_usd_val > 0 else 'LOSS'
    history_record = TradeHistory(
        symbol=symbol,
        direction=direction,
        entry_price=float(buy_price) if buy_price else 0.0,
        exit_price=float(current_price),
        quantity=float(asset_balance),
        pnl_usd=float(pnl_usd_val) if pnl_usd_val is not None else 0.0,
        pnl_pct=float(pnl_pct_val) if pnl_pct_val is not None else 0.0,
        outcome=outcome,
        exit_reason=exit_reason,
        closed_at=datetime.utcnow(),
    )

    try:
        session.add(history_record)
        # 2. Delete active position from PortfolioState and save CLOSED record
        session.query(PortfolioState).filter(
            PortfolioState.symbol == symbol
        ).delete(synchronize_session=False)
        closed_rec = PortfolioState(
            timestamp=datetime.now(),
            symbol=symbol,
            decision='CLOSED',
            current_price=float(current_price),
            usdt_balance=float(portfolio['usdt_balance']),
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
            pnl_pct=float(pnl_pct_val) if pnl_pct_val is not None else 0.0,
            pnl_usd=float(pnl_usd_val) if pnl_usd_val is not None else 0.0,
            total_portfolio_value=float(portfolio['usdt_balance']),
        )
        session.add(closed_rec)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Warning: Failed to archive trade and delete active position: {e}", flush=True)
        traceback.print_exc()

    # Build Telegram alert
    reason_labels = {
        'SIGNAL':                '📉 Technical Signal (MTF Confluence)',
        'STOP_LOSS':             f'🛑 Hard Stop-Loss (-{0:.1f}%)',
        'TRAILING_STOP':         '📐 Trailing Stop (pulled back from extreme)',
        'TREND_REVERSAL_EJECT':  '🚨 TREND REVERSAL EJECT — Macro thesis broken',
    }
    reason_text = reason_labels.get(exit_reason, exit_reason)

    rsi_info = f"- RSI: {float(current_rsi):.1f}\n" if current_rsi is not None else ""
    zscore_info = f"- Z-Score: {float(current_zscore):+.2f}\n" if current_zscore is not None else ""
    macro_str = ""
    if macro_info:
        macro_str = f"- Macro Trend: {macro_info.get('macro_trend', 'N/A')}\n"

    ob_info = ""
    if direction == 'LONG' and bearish_ob:
        ob_info = (
            f"- Bearish OB: ${float(bearish_ob['low']):.2f} – ${float(bearish_ob['high']):.2f}\n"
        )
    elif direction == 'SHORT' and bullish_ob:
        ob_info = (
            f"- Bullish OB: ${float(bullish_ob['low']):.2f} – ${float(bullish_ob['high']):.2f}\n"
        )

    alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')

    alert_msg = (
        f"{ALERT_PREFIX} 🚨 *QUANT ALERT: CLOSE {direction}* 🚨\n"
        f"\n"
        f"*Symbol:* {symbol}\n"
        f"*Price:* ${float(current_price):.2f}\n"
        f"*Time:* {alert_time}\n"
        f"\n"
        f"💡 *Exit Reason:* {reason_text}\n"
        f"{macro_str}"
        f"{rsi_info}"
        f"{zscore_info}"
        f"{ob_info}"
        f"\n"
        f"💼 *Virtual Portfolio:*{pnl_section}\n"
        f"- Slot Budget: ${SLOT_BUDGET:,.0f}\n"
        f"- Global Total Capital: ${TOTAL_CAPITAL:,.0f}\n"
        f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
        f"- Asset Balance: {portfolio['asset_balance']:.6f}\n"
        f"- Total Value: ${total_value:.2f}"
    )
    send_telegram_alert(alert_msg)

    return portfolio


# ══════════════════════════════════════════════════════════════════════
#  TRACKING UPDATE (HOLD CYCLE)
# ══════════════════════════════════════════════════════════════════════

def _save_tracking_update(
    portfolio: Dict[str, Any],
    current_price: float,
    symbol: str,
    session: object,
    futures_client: Optional[object] = None,
) -> None:
    """Persist a lightweight portfolio snapshot for an active HOLD cycle.

    Updates the ``highest/lowest_price_since_entry`` watermarks and the
    current price in the DB without modifying any PnL or decision logic.

    Retry behaviour:
        Attempts the DB write up to 3 times with a 1-second sleep between
        retries. Logs a warning after all attempts are exhausted.

    Args:
        portfolio:      Current portfolio state dict.
        current_price:  Latest market price.
        symbol:         Trading pair symbol.
        session:        Active SQLAlchemy session.
        futures_client: Optional Binance client to cross-reference live data.
    """
    total_value = float(portfolio['usdt_balance']) + (
        float(portfolio['asset_balance']) * float(current_price)
    )

    db_decision = 'HOLD'
    db_position_direction = portfolio.get('position_direction')
    db_entry_price = (
        float(portfolio['average_entry_price'])
        if portfolio['average_entry_price'] is not None
        else None
    )
    db_pnl_usd = None

    if futures_client:
        pos_info = get_position_info(futures_client, symbol)
        if pos_info and pos_info['size'] > 0:
            db_decision = pos_info['direction']          # Force 'LONG' or 'SHORT'
            db_position_direction = pos_info['direction']
            db_entry_price = pos_info['entry_price']
            db_pnl_usd = pos_info['unrealized_pnl']

    portfolio_record = PortfolioState(
        timestamp=datetime.now(),
        symbol=symbol,
        decision=db_decision,
        current_price=float(current_price),
        usdt_balance=float(portfolio['usdt_balance']),
        asset_balance=float(portfolio['asset_balance']),
        position_direction=db_position_direction,
        average_entry_price=db_entry_price,
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
        stop_loss_price=(
            float(portfolio['stop_loss_price'])
            if portfolio.get('stop_loss_price') is not None
            else None
        ),
        stop_loss=(
            float(portfolio['stop_loss_price'])
            if portfolio.get('stop_loss_price') is not None
            else None
        ),
        strategy=portfolio.get('strategy'),
        trailing_active=portfolio.get('trailing_active', False),
        partial_tp_hit=portfolio.get('partial_tp_hit', False),
        pnl_pct=None,
        pnl_usd=db_pnl_usd,
        total_portfolio_value=float(round(total_value, 2)),
    )

    import time as _time
    for attempt in range(3):
        try:
            session.add(portfolio_record)
            session.commit()
            break
        except Exception as e:
            session.rollback()
            if attempt == 2:
                print(
                    f"Warning: Failed to save tracking update to DB after 3 attempts: {e}",
                    flush=True,
                )
                traceback.print_exc()
            else:
                _time.sleep(1)
