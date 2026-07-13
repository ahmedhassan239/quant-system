import re

with open('analyzer.py', 'r') as f:
    content = f.read()

# 1. Constants
content = content.replace('''MIN_PROFIT_PCT = 0.005        # +0.5%  — signal SELL only if above this
HARD_STOP_LOSS_PCT = 0.015    # -1.5%  — immediate exit
TRAILING_ACTIVATE_PCT = 0.015 # +1.5%  — trailing stop activates here
TRAILING_PULLBACK_PCT = 0.005 # -0.5%  — pullback from peak triggers exit''', '''MAX_BUYS = 4
DCA_WEIGHTS = [0.10, 0.20, 0.30, 0.40]
SAFETY_ORDER_DIP_PCT = 0.02
TRADING_FEE = 0.001
MIN_PROFIT_PCT = 0.01         # +1.0%
HARD_STOP_LOSS_PCT = 0.05     # -5.0%
TRAILING_ACTIVATE_PCT = 0.015
TRAILING_PULLBACK_PCT = 0.005''')

# 2. load_portfolio
old_load = """def load_portfolio(session, symbol):
    \"\"\"
    Load the latest portfolio state from the database.
    Returns a dict with usdt_balance, paxg_balance, last_buy_price,
    and highest_price_since_entry (for trailing stop).
    \"\"\"
    last_state = session.query(PortfolioState).filter(
        PortfolioState.symbol == symbol
    ).order_by(PortfolioState.id.desc()).first()

    if last_state:
        return {
            'usdt_balance': last_state.usdt_balance,
            'paxg_balance': last_state.paxg_balance,
            'last_buy_price': last_state.last_buy_price,
            'highest_price_since_entry': last_state.highest_price_since_entry
        }
    # First run — return defaults
    return {
        'usdt_balance': DEFAULT_USDT_BALANCE,
        'paxg_balance': 0.0,
        'last_buy_price': None,
        'highest_price_since_entry': None
    }"""
new_load = """def load_portfolio(session, symbol):
    \"\"\"
    Load the latest portfolio state from the database.
    Returns a dict with usdt_balance, paxg_balance, average_entry_price,
    dca_level, last_exec_price, total_cost, and highest_price_since_entry.
    \"\"\"
    last_state = session.query(PortfolioState).filter(
        PortfolioState.symbol == symbol
    ).order_by(PortfolioState.id.desc()).first()

    if last_state:
        return {
            'usdt_balance': last_state.usdt_balance,
            'paxg_balance': last_state.paxg_balance,
            'average_entry_price': getattr(last_state, 'average_entry_price', None),
            'dca_level': getattr(last_state, 'dca_level', 0),
            'last_exec_price': getattr(last_state, 'last_exec_price', None),
            'total_cost': getattr(last_state, 'total_cost', 0.0),
            'highest_price_since_entry': last_state.highest_price_since_entry
        }
    # First run — return defaults
    return {
        'usdt_balance': DEFAULT_USDT_BALANCE,
        'paxg_balance': 0.0,
        'average_entry_price': None,
        'dca_level': 0,
        'last_exec_price': None,
        'total_cost': 0.0,
        'highest_price_since_entry': None
    }"""
content = content.replace(old_load, new_load)

# 3. _execute_sell
old_sell = """def _execute_sell(portfolio, current_price, symbol, session, exit_reason,
                  bullish_ob=None, bearish_ob=None, current_rsi=None):
    \"\"\"
    Execute a SELL: convert PAXG → USDT, compute PnL, save to DB,
    and send a Telegram alert.  Returns the updated portfolio dict.
    \"\"\"
    sell_value = float(portfolio['paxg_balance']) * float(current_price)
    buy_price = portfolio.get('last_buy_price')
    pnl_pct_val = None
    pnl_usd_val = None
    pnl_section = ""

    if buy_price and float(buy_price) > 0:
        pnl_pct_val = float(((float(current_price) - float(buy_price)) / float(buy_price)) * 100)
        pnl_usd_val = float(sell_value - (float(portfolio['paxg_balance']) * float(buy_price)))
        sign = "+" if pnl_pct_val >= 0 else ""
        pnl_section = f"\\n- PnL (This Trade): {sign}{pnl_pct_val:.2f}% ({sign}${pnl_usd_val:.2f})"

    portfolio['usdt_balance'] = round(sell_value * (1 - TRADING_FEE), 2)
    portfolio['paxg_balance'] = 0.0
    portfolio['last_buy_price'] = None
    portfolio['highest_price_since_entry'] = None"""
# Fix the above replacement logic to match original file which had no fee. Let's just find _execute_sell
content = re.sub(
    r"def _execute_sell\(.*?(?=\n    # Calculate total portfolio value)",
    """def _execute_sell(portfolio, current_price, symbol, session, exit_reason,
                  bullish_ob=None, bearish_ob=None, current_rsi=None):
    \"\"\"
    Execute a SELL: convert PAXG → USDT, compute PnL, save to DB,
    and send a Telegram alert.  Returns the updated portfolio dict.
    \"\"\"
    sell_value = float(portfolio['paxg_balance']) * float(current_price) * (1 - TRADING_FEE)
    buy_price = portfolio.get('average_entry_price')
    total_cost = portfolio.get('total_cost', 0.0)
    pnl_pct_val = None
    pnl_usd_val = None
    pnl_section = ""

    if buy_price and float(buy_price) > 0:
        pnl_pct_val = float(((float(current_price) - float(buy_price)) / float(buy_price)) * 100)
        pnl_usd_val = float(sell_value - total_cost)
        sign = "+" if pnl_pct_val >= 0 else ""
        pnl_section = f"\\n- PnL (This Trade): {sign}{pnl_pct_val:.2f}% ({sign}${pnl_usd_val:.2f})"

    portfolio['usdt_balance'] += round(sell_value, 2)
    portfolio['paxg_balance'] = 0.0
    portfolio['average_entry_price'] = None
    portfolio['dca_level'] = 0
    portfolio['last_exec_price'] = None
    portfolio['total_cost'] = 0.0
    portfolio['highest_price_since_entry'] = None
""", content, flags=re.DOTALL)

# In PortfolioState record creation in _execute_sell
content = content.replace("last_buy_price=None,", "average_entry_price=None,\n        dca_level=0,\n        last_exec_price=None,\n        total_cost=0.0,")

# 4. _save_tracking_update
content = content.replace("last_buy_price=float(portfolio['last_buy_price']) if portfolio['last_buy_price'] is not None else None,",
"""average_entry_price=float(portfolio['average_entry_price']) if portfolio['average_entry_price'] is not None else None,
        dca_level=int(portfolio['dca_level']),
        last_exec_price=float(portfolio['last_exec_price']) if portfolio['last_exec_price'] is not None else None,
        total_cost=float(portfolio['total_cost']),""")

# 5. Add SMA 200 to df
content = content.replace("""        # 1. Calculate 14-period RSI
        df = calculate_rsi(df, period=14)
        
        # 2. Detect Order Blocks""", """        # 1. Calculate 14-period RSI
        df = calculate_rsi(df, period=14)
        df['SMA_200'] = df['close'].rolling(window=200).mean()
        
        # 2. Detect Order Blocks""")

# Get current candle data
content = content.replace("""        # 3. Get current candle data
        last_row = df.iloc[-1]
        current_price = last_row['close']
        current_rsi = last_row['RSI']

        # 4. Load portfolio state""", """        # 3. Get current candle data
        last_row = df.iloc[-1]
        current_price = last_row['close']
        current_rsi = last_row['RSI']
        current_sma = float(last_row['SMA_200']) if pd.notna(last_row['SMA_200']) else None

        # 4. Load portfolio state""")

# Risk management exits: change entry_price from last_buy_price
content = content.replace("entry_price = portfolio.get('last_buy_price')", "entry_price = portfolio.get('average_entry_price')")

# Signal logic
old_signal = """        decision = 'WAIT'

        if not risk_exit_triggered:
            if bullish_ob and current_price <= bullish_ob['high'] and current_rsi < 30:
                decision = 'BUY'

            if bearish_ob and current_price >= bearish_ob['low'] and current_rsi > 70:
                decision = 'SELL'"""
new_signal = """        decision = 'WAIT'

        if not risk_exit_triggered:
            dca_level = portfolio.get('dca_level', 0)
            if current_rsi < 30:
                if dca_level == 0 and bullish_ob and current_price <= bullish_ob['high'] and current_sma and current_price > current_sma:
                    decision = 'BUY'
                elif 0 < dca_level < MAX_BUYS:
                    last_exec_price = portfolio.get('last_exec_price')
                    if last_exec_price and current_price <= last_exec_price * (1 - SAFETY_ORDER_DIP_PCT):
                        decision = 'BUY'

            if bearish_ob and current_price >= bearish_ob['low'] and current_rsi > 70:
                decision = 'SELL'"""
content = content.replace(old_signal, new_signal)

# Execute BUY logic
old_buy_exec = """            if decision != last_decision:
                # ── Execute BUY ──
                if decision == 'BUY' and not in_position and portfolio['usdt_balance'] > 0:
                    paxg_bought = float(portfolio['usdt_balance']) / float(current_price)
                    portfolio['paxg_balance'] = round(paxg_bought, 6)
                    portfolio['last_buy_price'] = float(current_price)
                    portfolio['highest_price_since_entry'] = float(current_price)
                    portfolio['usdt_balance'] = 0.0

                    # Calculate total portfolio value at current price
                    total_value = float(portfolio['paxg_balance']) * float(current_price)

                    # Save portfolio snapshot to database
                    portfolio_record = PortfolioState(
                        timestamp=datetime.now(),
                        symbol=symbol,
                        decision='BUY',
                        current_price=float(current_price),
                        usdt_balance=float(portfolio['usdt_balance']),
                        paxg_balance=float(portfolio['paxg_balance']),
                        last_buy_price=float(portfolio['last_buy_price']),
                        highest_price_since_entry=float(portfolio['highest_price_since_entry']),
                        pnl_pct=None,
                        pnl_usd=None,
                        total_portfolio_value=float(round(total_value, 2))
                    )"""
new_buy_exec = """            if decision != last_decision or decision == 'BUY':
                # ── Execute BUY ──
                dca_level = portfolio.get('dca_level', 0)
                if decision == 'BUY' and dca_level < MAX_BUYS:
                    spend = DEFAULT_USDT_BALANCE * DCA_WEIGHTS[dca_level]
                    if portfolio['usdt_balance'] >= spend:
                        effective_usdt = spend * (1 - TRADING_FEE)
                        paxg_bought = effective_usdt / float(current_price)
                        
                        # Update average entry price
                        old_paxg = float(portfolio.get('paxg_balance', 0) or 0)
                        old_avg = float(portfolio.get('average_entry_price', 0) or 0)
                        old_val = old_paxg * old_avg
                        new_val = paxg_bought * float(current_price)
                        
                        portfolio['paxg_balance'] = round(old_paxg + paxg_bought, 6)
                        portfolio['average_entry_price'] = (old_val + new_val) / portfolio['paxg_balance']
                        portfolio['dca_level'] = dca_level + 1
                        portfolio['last_exec_price'] = float(current_price)
                        portfolio['total_cost'] = float(portfolio.get('total_cost', 0)) + effective_usdt
                        portfolio['usdt_balance'] -= spend
                        if dca_level == 0:
                            portfolio['highest_price_since_entry'] = float(current_price)

                        # Calculate total portfolio value at current price
                        total_value = portfolio['usdt_balance'] + (float(portfolio['paxg_balance']) * float(current_price))

                        # Save portfolio snapshot to database
                        portfolio_record = PortfolioState(
                            timestamp=datetime.now(),
                            symbol=symbol,
                            decision='BUY',
                            current_price=float(current_price),
                            usdt_balance=float(portfolio['usdt_balance']),
                            paxg_balance=float(portfolio['paxg_balance']),
                            average_entry_price=float(portfolio['average_entry_price']),
                            dca_level=int(portfolio['dca_level']),
                            last_exec_price=float(portfolio['last_exec_price']),
                            total_cost=float(portfolio['total_cost']),
                            highest_price_since_entry=float(portfolio['highest_price_since_entry']),
                            pnl_pct=None,
                            pnl_usd=None,
                            total_portfolio_value=float(round(total_value, 2))
                        )"""
content = content.replace(old_buy_exec, new_buy_exec)

# Telegram BUY Alert
old_telegram_buy = """                    # Telegram alert for BUY
                    rsi_label = "Oversold"
                    ob_type = "Bullish"
                    ob_low = bullish_ob['low'] if bullish_ob else 0
                    ob_high = bullish_ob['high'] if bullish_ob else 0
                    alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')

                    alert_msg = (
                        f"🚨 *QUANT ALERT: BUY* 🚨\\n"
                        f"\\n"
                        f"*Symbol:* {symbol}\\n"
                        f"*Price:* ${float(current_price):.2f}\\n"
                        f"*Time:* {alert_time}\\n"
                        f"\\n"
                        f"💡 *Why this decision?*\\n"
                        f"- RSI is at {float(current_rsi):.1f} (Indicates {rsi_label}).\\n"
                        f"- Price entered {ob_type} Order Block between ${float(ob_low):.2f} and ${float(ob_high):.2f}.\\n"
                        f"\\n"
                        f"🛡️ *Risk Management Active:*\\n"
                        f"- Stop-Loss: -1.5% (${float(current_price) * (1 - HARD_STOP_LOSS_PCT):.2f})\\n"
                        f"- Trailing Stop activates at +1.5%\\n"
                        f"\\n"
                        f"💼 *Virtual Portfolio:*\\n"
                        f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\\n"
                        f"- PAXG Balance: {portfolio['paxg_balance']:.6f} PAXG\\n"
                        f"- Total Value: ${total_value:.2f}"
                    )
                    send_telegram_alert(alert_msg)"""
new_telegram_buy = """                    # Telegram alert for BUY
                    alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
                    dca_str = f"Step {portfolio['dca_level']}/{MAX_BUYS}"
                    
                    reason_msg = ""
                    if dca_level == 0:
                        ob_low = bullish_ob['low'] if bullish_ob else 0
                        ob_high = bullish_ob['high'] if bullish_ob else 0
                        reason_msg = f"- RSI: {float(current_rsi):.1f}\\n- Price entered Bullish OB: ${float(ob_low):.2f} - ${float(ob_high):.2f}\\n- Above SMA 200 (${float(current_sma):.2f})"
                    else:
                        reason_msg = f"- RSI: {float(current_rsi):.1f}\\n- Price dropped >= 2.0% from last execution"

                    alert_msg = (
                        f"🚨 *QUANT ALERT: BUY (DCA {dca_str})* 🚨\\n"
                        f"\\n"
                        f"*Symbol:* {symbol}\\n"
                        f"*Price:* ${float(current_price):.2f}\\n"
                        f"*Time:* {alert_time}\\n"
                        f"\\n"
                        f"💡 *Why this decision?*\\n"
                        f"{reason_msg}\\n"
                        f"\\n"
                        f"🛡️ *Risk Management:*\\n"
                        f"- Avg Entry Price: ${float(portfolio['average_entry_price']):.2f}\\n"
                        f"- Stop-Loss: -{HARD_STOP_LOSS_PCT*100}% (${float(portfolio['average_entry_price']) * (1 - HARD_STOP_LOSS_PCT):.2f})\\n"
                        f"\\n"
                        f"💼 *Virtual Portfolio:*\\n"
                        f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\\n"
                        f"- PAXG Balance: {portfolio['paxg_balance']:.6f} PAXG\\n"
                        f"- Total Value: ${total_value:.2f}"
                    )
                    send_telegram_alert(alert_msg)"""
content = content.replace(old_telegram_buy, new_telegram_buy)

# Execute SELL min-profit gate
content = content.replace("ep = float(portfolio['last_buy_price']) if portfolio['last_buy_price'] else 0", "ep = float(portfolio['average_entry_price']) if portfolio['average_entry_price'] else 0")

# Final check trailing status print
old_print_risk = """        if in_position and portfolio.get('last_buy_price'):
            ep = float(portfolio['last_buy_price'])"""
new_print_risk = """        if in_position and portfolio.get('average_entry_price'):
            ep = float(portfolio['average_entry_price'])"""
content = content.replace(old_print_risk, new_print_risk)

with open('analyzer.py', 'w') as f:
    f.write(content)

