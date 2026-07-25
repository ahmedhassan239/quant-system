import sys
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.append('backend-python')
from database import PortfolioState

engine = create_engine('postgresql://postgres:postgres@localhost:5432/quant_db')
Session = sessionmaker(bind=engine)
session = Session()

records = session.query(PortfolioState).order_by(PortfolioState.id.desc()).limit(5).all()
for r in records:
    print(r.symbol, r.decision, r.stop_loss, r.stop_loss_price)

