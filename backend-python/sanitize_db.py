import os
import sys
from datetime import datetime
from database import SessionLocal, TradeHistory, PortfolioState
from futures_executor import create_futures_client, close_position

def sanitize_db_and_binance():
    session = SessionLocal()
    print("─── 1. SANITIZING TRADE HISTORY REALIZED PNL ───")
    trades = session.query(TradeHistory).all()
    total_old_pnl = 0.0
    total_new_pnl = 0.0
    updated_count = 0

    for t in trades:
        ep = float(t.entry_price or 0.0)
        cp = float(t.exit_price or 0.0)
        qty = float(t.quantity or 0.0)
        old_pnl = float(t.pnl_usd or 0.0)
        total_old_pnl += old_pnl

        if t.direction == 'LONG':
            correct_pnl_usd = (cp - ep) * qty
            correct_pnl_pct = ((cp - ep) / ep) * 100.0 if ep > 0 else 0.0
        elif t.direction == 'SHORT':
            correct_pnl_usd = (ep - cp) * qty
            correct_pnl_pct = ((ep - cp) / ep) * 100.0 if ep > 0 else 0.0
        else:
            correct_pnl_usd = 0.0
            correct_pnl_pct = 0.0

        if abs(old_pnl - correct_pnl_usd) > 0.0001 or abs(float(t.pnl_pct or 0.0) - correct_pnl_pct) > 0.0001:
            t.pnl_usd = round(correct_pnl_usd, 4)
            t.pnl_pct = round(correct_pnl_pct, 4)
            t.outcome = 'WIN' if correct_pnl_usd > 0 else 'LOSS'
            updated_count += 1

        total_new_pnl += correct_pnl_usd

    session.commit()
    print(f"✅ Updated {updated_count} / {len(trades)} trades in TradeHistory.")
    print(f"   Total Old PnL: ${total_old_pnl:,.2f} -> Corrected Total PnL: ${total_new_pnl:,.2f}")

    print("\n─── 2. CLEARING GHOST RECORDS IN PORTFOLIO STATE ───")
    ghosts = session.query(PortfolioState).filter(
        (PortfolioState.asset_balance <= 0.000001) &
        (PortfolioState.decision.in_(['LONG', 'SHORT']))
    ).all()
    for g in ghosts:
        g.decision = 'CLOSED'
        g.position_direction = None
    session.commit()
    print(f"✅ Cleaned {len(ghosts)} ghost records in PortfolioState where decision was LONG/SHORT with 0 balance.")

    print("\n─── 3. SYNCING & CLEARING ORPHAN BINANCE TESTNET POSITIONS ───")
    try:
        client = create_futures_client()
    except Exception as e:
        print(f"⚠️ Could not connect to Binance client: {e}. Skipping live position cleanup.")
        session.close()
        return

    try:
        positions = client.futures_position_information()
        open_pos = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
        print(f"Found {len(open_pos)} open positions on Binance testnet.")
        
        for p in open_pos:
            sym = p['symbol']
            amt = float(p['positionAmt'])
            mark_price = float(p.get('markPrice') or p.get('entryPrice') or 0)
            val = abs(amt) * mark_price
            direction = 'LONG' if amt > 0 else 'SHORT'
            
            # Check if this position exists as active in DB
            db_active = session.query(PortfolioState).filter(
                PortfolioState.symbol == sym,
                PortfolioState.asset_balance > 0.000001
            ).order_by(PortfolioState.id.desc()).first()
            
            if val < 2.0:
                print(f"🧹 Closing DUST position on Binance: {sym} ({direction}, size={abs(amt)}, val=${val:.2f})")
                close_position(client, sym, direction, abs(amt))
            elif not db_active:
                print(f"🧹 Closing ORPHAN position on Binance (not active in DB): {sym} ({direction}, size={abs(amt)}, val=${val:.2f})")
                close_position(client, sym, direction, abs(amt))
            else:
                print(f"🔒 Keeping active synced position: {sym} ({direction}, size={abs(amt)}, val=${val:.2f})")

    except Exception as e:
        print(f"❌ Error during Binance cleanup: {e}")

    session.close()
    print("\n✅ Database and Binance state sanitization completed successfully.")

if __name__ == "__main__":
    sanitize_db_and_binance()
