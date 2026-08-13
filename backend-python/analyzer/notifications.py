"""
analyzer/notifications.py
─────────────────────────
All Telegram I/O for the Execution Analyzer package.

Functions:
    send_telegram_alert    – Send a message to the configured Telegram chat.
    send_periodic_report   – Build and send a full PNL / portfolio summary.
"""

from __future__ import annotations

import os
import traceback
from datetime import datetime
from typing import Optional

import requests
from sqlalchemy import func

from config import ALERT_PREFIX
from database import (
    SessionLocal,
    PortfolioState,
    TradeHistory,
)
from futures_executor import get_futures_balance


# ══════════════════════════════════════════════════════════════════════
#  TELEGRAM HELPER
# ══════════════════════════════════════════════════════════════════════

def send_telegram_alert(message: str) -> None:
    """Send a notification message to the configured Telegram chat.

    Uses the ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` environment
    variables. Fails silently so that a Telegram outage never crashes
    the trading bot.

    Retry behaviour:
        If the initial sendMessage returns HTTP 400 with a Markdown parse
        error (e.g. unescaped underscores), the message is re-sent without
        ``parse_mode`` as a plain-text fallback.

    Args:
        message: Text to send. Markdown special characters in symbol names
                 (e.g. underscores) are escaped automatically.
    """
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')

    if not token or not chat_id:
        print("Telegram credentials not configured. Skipping alert.", flush=True)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    escaped_message = message.replace("_", "\\_")
    payload = {"chat_id": chat_id, "text": escaped_message, "parse_mode": "Markdown"}

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 400 and "can't parse entities" in response.text.lower():
            print(
                f"Telegram Markdown parse failed ({response.text}). "
                "Retrying without parse_mode...",
                flush=True,
            )
            payload_plain = {"chat_id": chat_id, "text": message}
            response = requests.post(url, json=payload_plain, timeout=10)
        if response.status_code == 200:
            print("Telegram alert sent successfully.", flush=True)
        else:
            print(f"Telegram API error: {response.status_code} - {response.text}", flush=True)
    except requests.exceptions.RequestException as e:
        print(f"Failed to send Telegram alert: {e}", flush=True)
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════
#  PERIODIC PNL REPORT
# ══════════════════════════════════════════════════════════════════════

def send_periodic_report(futures_client: Optional[object] = None) -> Optional[str]:
    """Generate and send an automated periodic PNL / portfolio status report via Telegram.

    Summary sections:
        1. Total Realised PNL — sum of ``trade_history.pnl_usd``.
        2. Win Rate — winning trades / total closed trades.
        3. Active Positions count + Total Unrealised PNL (from Binance if available).
        4. Available Wallet Balance (Binance futures wallet or last known DB value).

    Args:
        futures_client: An initialised Binance futures client for live data.
                        If ``None``, falls back to local DB values.

    Returns:
        The formatted report string that was sent, or ``None`` if an
        error occurred before the report could be built.

    Edge cases:
        - No trades in DB → realised PNL = 0.0, win rate = 0.0 %.
        - Binance client unavailable → active count and unrealised PNL
          come from the DB only.
        - Telegram failure → report is still returned (already printed).
    """
    session = SessionLocal()
    try:
        # 1. Total Realised PNL from trade_history
        realized_pnl_result = session.query(func.sum(TradeHistory.pnl_usd)).scalar()
        realized_pnl = float(realized_pnl_result) if realized_pnl_result is not None else 0.0

        # 2. Win Rate & Trade Counts
        total_trades = session.query(TradeHistory).count()
        winning_trades = session.query(TradeHistory).filter(TradeHistory.outcome == 'WIN').count()
        win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

        # 3. Active Positions & Unrealised PNL
        active_db_positions = session.query(PortfolioState).filter(
            PortfolioState.asset_balance > 0.000001,
            PortfolioState.decision.in_(['LONG', 'SHORT'])
        ).all()
        active_count = len(active_db_positions)

        unrealized_pnl = 0.0
        wallet_balance = 0.0

        if futures_client:
            try:
                wallet_balance = get_futures_balance(futures_client)
                pos_risk = futures_client.futures_position_information()
                live_active = 0
                for p in pos_risk:
                    amt = float(p.get('positionAmt', 0))
                    if amt != 0:
                        mark_price = float(p.get('markPrice') or p.get('entryPrice') or 0)
                        if abs(amt) * mark_price >= 2.0:
                            live_active += 1
                            unrealized_pnl += float(p.get('unRealizedProfit', 0))
                if live_active > 0:
                    active_count = live_active
            except Exception as e:
                print(f"⚠️ Warning querying live Binance account for report: {e}", flush=True)

        if wallet_balance == 0.0 and active_db_positions:
            wallet_balance = float(active_db_positions[0].usdt_balance or 0.0)

        # 4. Format Telegram Report Message
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        realized_sign = "+" if realized_pnl >= 0 else ""
        unrealized_sign = "+" if unrealized_pnl >= 0 else ""

        report_msg = (
            f"📊 *{ALERT_PREFIX} Periodic PNL Report* 📊\n"
            f"──────────────────────────────\n"
            f"💰 *Realized PNL:* `${realized_sign}{realized_pnl:,.2f}`\n"
            f"📈 *Win Rate:* `{win_rate:.1f}%` ({winning_trades}/{total_trades} Trades)\n"
            f"🟢 *Active Positions:* `{active_count}`\n"
            f"🔄 *Unrealized PNL:* `${unrealized_sign}{unrealized_pnl:,.2f}`\n"
            f"💵 *Wallet Balance:* `${wallet_balance:,.2f}`\n"
            f"──────────────────────────────\n"
            f"⏰ *Generated:* `{now_str}`"
        )

        print("=" * 60, flush=True)
        print("📊 [PERIODIC REPORT] Sending Telegram Summary...", flush=True)
        print(report_msg, flush=True)
        print("=" * 60, flush=True)

        send_telegram_alert(report_msg)
        return report_msg

    except Exception as e:
        session.rollback()
        print(f"❌ Error generating periodic report: {e}", flush=True)
        traceback.print_exc()
        return None
    finally:
        session.close()
