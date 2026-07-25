import os
import sys
from datetime import datetime
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend-python'))

from database import SessionLocal, PortfolioState, sync_missing_stop_losses

def test_sync():
    session = SessionLocal()
    
    # Create a mock open position without a stop loss
    mock_pos = PortfolioState(
        timestamp=datetime.utcnow(),
        symbol="TESTUSDT",
        decision="LONG",
        current_price=100.0,
        usdt_balance=1000.0,
        asset_balance=1.0,
        position_direction="LONG",
        average_entry_price=100.0,
        total_portfolio_value=1100.0,
        stop_loss_price=None,
        stop_loss=None
    )
    
    session.add(mock_pos)
    session.commit()
    print("Added mock position with NULL stop_loss_price")
    
    # Run sync
    sync_missing_stop_losses(session)
    
    # Verify
    updated_pos = session.query(PortfolioState).filter_by(id=mock_pos.id).first()
    print(f"Updated stop_loss_price: {updated_pos.stop_loss_price}")
    
    if updated_pos.stop_loss_price == 98.5:
        print("✅ Sync check passed!")
    else:
        print("❌ Sync check failed!")
        
    # Cleanup
    session.delete(updated_pos)
    session.commit()
    session.close()

if __name__ == "__main__":
    test_sync()
