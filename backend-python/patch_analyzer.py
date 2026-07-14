import re
import os

with open('/app/analyzer.py', 'r') as f:
    content = f.read()

# 1. Update config imports
content = content.replace("ZSCORE_SMA_PERIOD, OB_VOLUME_MULTIPLIER, OB_VOLUME_MA_PERIOD)", 
                          "ZSCORE_SMA_PERIOD, OB_VOLUME_MULTIPLIER, OB_VOLUME_MA_PERIOD, BREAKOUT_VOLUME_MULTIPLIER, BREAKOUT_CONSOLIDATION_PERIOD)")

# 2. Update Risk Constants
old_risk = """MAX_BUYS = 4
DCA_WEIGHTS = [0.10, 0.20, 0.30, 0.40]   # of SLOT_BUDGET → $50, $100, $150, $200
SAFETY_ORDER_DIP_PCT = 0.02               # -2.0 %
TRADING_FEE = 0.001                       # 0.1 % per side
MIN_PROFIT_PCT = 0.01                     # +1.0 %
HARD_STOP_LOSS_PCT = 0.05                 # -5.0 %
TRAILING_ACTIVATE_PCT = 0.015             # +1.5 %
TRAILING_PULLBACK_PCT = 0.005             # -0.5 %"""
new_risk = """MAX_BUYS = 1
ENTRY_WEIGHT = 1.0
TRADING_FEE = 0.001                       # 0.1 % per side
MIN_PROFIT_PCT = 0.01                     # +1.0 %
TRAILING_ACTIVATE_PCT = 0.02              # +2.0 %
TRAILING_PULLBACK_PCT = 0.005             # -0.5 %"""
content = content.replace(old_risk, new_risk)

# 3. Add detect_consolidation_breakout
breakout_func = """

# ══════════════════════════════════════════════════════════════════════
#  BREAKOUT DETECTION (with Volume Filter)
# ══════════════════════════════════════════════════════════════════════

def detect_consolidation_breakout(df, lookback=BREAKOUT_CONSOLIDATION_PERIOD, volume_multiplier=BREAKOUT_VOLUME_MULTIPLIER,
                                  volume_ma_period=OB_VOLUME_MA_PERIOD):
    if len(df) < lookback + 1:
        return None, None

    df_vol_ma = df['volume'].rolling(window=volume_ma_period).mean()
    recent_df = df.iloc[-(lookback+1):-1]
    current_candle = df.iloc[-1]
    
    consol_high = recent_df['high'].max()
    consol_low = recent_df['low'].min()
    
    vol_ma_at_current = df_vol_ma.iloc[-1]
    
    bullish_breakout = None
    bearish_breakout = None
    
    if vol_ma_at_current > 0 and current_candle['volume'] > volume_multiplier * vol_ma_at_current:
        if current_candle['close'] > consol_high:
            bullish_breakout = {
                'consolidation_high': consol_high,
                'consolidation_low': consol_low,
                'breakout_candle_low': current_candle['low'],
                'volume': current_candle['volume'],
                'vol_ratio': current_candle['volume'] / vol_ma_at_current,
            }
        elif current_candle['close'] < consol_low:
            bearish_breakout = {
                'consolidation_high': consol_high,
                'consolidation_low': consol_low,
                'breakout_candle_high': current_candle['high'],
                'volume': current_candle['volume'],
                'vol_ratio': current_candle['volume'] / vol_ma_at_current,
            }
            
    return bullish_breakout, bearish_breakout
"""
# Insert after detect_order_blocks function
content = content.replace("    return bullish_ob, bearish_ob\n", "    return bullish_ob, bearish_ob\n" + breakout_func)

# 4. Modify Risk Management exits in run_analyzer
old_risk_exit = """        # ──────────────────────────────────────────────────────────
        #  RISK MANAGEMENT EXITS (checked BEFORE signal logic)
        # ──────────────────────────────────────────────────────────
        risk_exit_triggered = False

        if in_position and entry_price and float(entry_price) > 0:
            cp = float(current_price)
            ep = float(entry_price)

            if pos_direction == 'LONG':
                unrealized_pct = (cp - ep) / ep

                if highest_price is None or cp > float(highest_price):
                    portfolio['highest_price_since_entry'] = cp
                    highest_price = cp

                if unrealized_pct <= -HARD_STOP_LOSS_PCT:
                    print(f"⛔ [{symbol}] LONG HARD STOP-LOSS triggered! Price ${cp:.2f} is "
                          f"{unrealized_pct*100:.2f}% below entry ${ep:.2f}", flush=True)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'STOP_LOSS',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True

                elif unrealized_pct >= TRAILING_ACTIVATE_PCT and highest_price:
                    hp = float(highest_price)
                    pullback_pct = (hp - cp) / hp
                    if pullback_pct >= TRAILING_PULLBACK_PCT:
                        print(f"📐 [{symbol}] LONG TRAILING STOP triggered! Price ${cp:.2f} pulled back "
                              f"{pullback_pct*100:.2f}% from peak ${hp:.2f}", flush=True)
                        portfolio = _close_position_handler(
                            portfolio, current_price, symbol, session, 'TRAILING_STOP',
                            futures_client, bullish_ob, bearish_ob, current_rsi,
                            current_zscore, macro_info)
                        risk_exit_triggered = True

            elif pos_direction == 'SHORT':
                unrealized_pct = (ep - cp) / ep

                if lowest_price is None or cp < float(lowest_price):
                    portfolio['lowest_price_since_entry'] = cp
                    lowest_price = cp

                if unrealized_pct <= -HARD_STOP_LOSS_PCT:
                    print(f"⛔ [{symbol}] SHORT HARD STOP-LOSS triggered! Price ${cp:.2f} is "
                          f"{abs(unrealized_pct)*100:.2f}% above entry ${ep:.2f}", flush=True)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'STOP_LOSS',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True

                elif unrealized_pct >= TRAILING_ACTIVATE_PCT and lowest_price:
                    lp = float(lowest_price)
                    bounce_pct = (cp - lp) / lp
                    if bounce_pct >= TRAILING_PULLBACK_PCT:
                        print(f"📐 [{symbol}] SHORT TRAILING STOP triggered! Price ${cp:.2f} bounced "
                              f"{bounce_pct*100:.2f}% from trough ${lp:.2f}", flush=True)
                        portfolio = _close_position_handler(
                            portfolio, current_price, symbol, session, 'TRAILING_STOP',
                            futures_client, bullish_ob, bearish_ob, current_rsi,
                            current_zscore, macro_info)
                        risk_exit_triggered = True"""
new_risk_exit = """        # ──────────────────────────────────────────────────────────
        #  RISK MANAGEMENT EXITS (checked BEFORE signal logic)
        # ──────────────────────────────────────────────────────────
        risk_exit_triggered = False

        if in_position and entry_price and float(entry_price) > 0:
            cp = float(current_price)
            ep = float(entry_price)
            stop_loss = float(portfolio.get('stop_loss_price', 0) or 0)
            trailing_active = portfolio.get('trailing_active', False)

            if pos_direction == 'LONG':
                unrealized_pct = (cp - ep) / ep

                if highest_price is None or cp > float(highest_price):
                    portfolio['highest_price_since_entry'] = cp
                    highest_price = cp

                # Activate trailing stop (move SL to breakeven + trail)
                if not trailing_active and unrealized_pct >= TRAILING_ACTIVATE_PCT:
                    portfolio['trailing_active'] = True
                    portfolio['stop_loss_price'] = max(stop_loss, ep) # Move to breakeven
                    stop_loss = portfolio['stop_loss_price']
                    trailing_active = True
                    print(f"✅ [{symbol}] LONG Trailing Stop ACTIVATED! SL moved to Breakeven ${stop_loss:.2f}", flush=True)

                if trailing_active and highest_price:
                    hp = float(highest_price)
                    trail_sl = hp * (1 - TRAILING_PULLBACK_PCT)
                    if trail_sl > stop_loss:
                        portfolio['stop_loss_price'] = trail_sl
                        stop_loss = trail_sl
                        
                # Check Stop Loss hit
                if stop_loss > 0 and cp <= stop_loss:
                    print(f"⛔ [{symbol}] LONG STOP-LOSS hit at ${cp:.2f} (SL: ${stop_loss:.2f})", flush=True)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'STOP_LOSS' if not trailing_active else 'TRAILING_STOP',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True


            elif pos_direction == 'SHORT':
                unrealized_pct = (ep - cp) / ep

                if lowest_price is None or cp < float(lowest_price):
                    portfolio['lowest_price_since_entry'] = cp
                    lowest_price = cp

                # Activate trailing stop
                if not trailing_active and unrealized_pct >= TRAILING_ACTIVATE_PCT:
                    portfolio['trailing_active'] = True
                    portfolio['stop_loss_price'] = min(stop_loss, ep) if stop_loss > 0 else ep # Move to breakeven
                    stop_loss = portfolio['stop_loss_price']
                    trailing_active = True
                    print(f"✅ [{symbol}] SHORT Trailing Stop ACTIVATED! SL moved to Breakeven ${stop_loss:.2f}", flush=True)

                if trailing_active and lowest_price:
                    lp = float(lowest_price)
                    trail_sl = lp * (1 + TRAILING_PULLBACK_PCT)
                    if stop_loss == 0 or trail_sl < stop_loss:
                        portfolio['stop_loss_price'] = trail_sl
                        stop_loss = trail_sl
                        
                # Check Stop Loss hit
                if stop_loss > 0 and cp >= stop_loss:
                    print(f"⛔ [{symbol}] SHORT STOP-LOSS hit at ${cp:.2f} (SL: ${stop_loss:.2f})", flush=True)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'STOP_LOSS' if not trailing_active else 'TRAILING_STOP',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info)
                    risk_exit_triggered = True"""
content = content.replace(old_risk_exit, new_risk_exit)

# 5. Signal logic
old_signal_logic = """        # ──────────────────────────────────────────────────────────
        #  SIGNAL LOGIC — MTF Confluence
        # ──────────────────────────────────────────────────────────
        decision = 'WAIT'

        if not risk_exit_triggered and current_zscore is not None:
            dca_level = portfolio.get('dca_level', 0)

            # ── LONG Confluence ──
            # macro=UPTREND + Bullish OB touched + 15m Z-Score < -1.5
            if macro_trend == 'UPTREND':
                if (bullish_ob and current_price <= bullish_ob['high']
                        and current_zscore < ZSCORE_LONG_THRESHOLD):
                    if dca_level == 0:
                        active_count = count_active_positions(session)
                        if active_count >= MAX_CONCURRENT_POSITIONS:
                            print(f"⏸️ [{symbol}] WAIT (Max Concurrent Slots Reached: "
                                  f"{active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                        else:
                            decision = 'LONG'
                            print(f"✨ [{symbol}] LONG CONFLUENCE: Macro=UPTREND + "
                                  f"Bullish OB + Z={current_zscore:+.2f} < {ZSCORE_LONG_THRESHOLD}",
                                  flush=True)
                    elif 0 < dca_level < MAX_BUYS and pos_direction == 'LONG':
                        last_exec_price = portfolio.get('last_exec_price')
                        if last_exec_price and current_price <= last_exec_price * (1 - SAFETY_ORDER_DIP_PCT):
                            decision = 'LONG'
                            print(f"✨ [{symbol}] LONG DCA Step {dca_level+1}: "
                                  f"Z={current_zscore:+.2f} | Dip confirmed", flush=True)

            # ── SHORT Confluence ──
            # macro=DOWNTREND + Bearish OB touched + 15m Z-Score > +1.5
            if decision == 'WAIT' and macro_trend == 'DOWNTREND':
                if (bearish_ob and current_price >= bearish_ob['low']
                        and current_zscore > ZSCORE_SHORT_THRESHOLD):
                    if not in_position:
                        active_count = count_active_positions(session)
                        if active_count >= MAX_CONCURRENT_POSITIONS:
                            print(f"⏸️ [{symbol}] WAIT (Max Concurrent Slots Reached: "
                                  f"{active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                        else:
                            decision = 'SHORT'
                            print(f"✨ [{symbol}] SHORT CONFLUENCE: Macro=DOWNTREND + "
                                  f"Bearish OB + Z={current_zscore:+.2f} > +{ZSCORE_SHORT_THRESHOLD}",
                                  flush=True)
                    elif pos_direction == 'LONG':
                        decision = 'SHORT'
                        print(f"🔄 [{symbol}] Reversing LONG → SHORT on confluence signal",
                              flush=True)

            # ── Log blocked directions ──
            if decision == 'WAIT' and not risk_exit_triggered:
                if macro_trend == 'UPTREND' and current_zscore and current_zscore > ZSCORE_SHORT_THRESHOLD:
                    print(f"🚫 [{symbol}] SHORT blocked: macro=UPTREND (only LONGs allowed)", flush=True)
                elif macro_trend == 'DOWNTREND' and current_zscore and current_zscore < ZSCORE_LONG_THRESHOLD:
                    print(f"🚫 [{symbol}] LONG blocked: macro=DOWNTREND (only SHORTs allowed)", flush=True)"""
new_signal_logic = """        # ── 3.5 Detect Breakouts ──
        bullish_breakout, bearish_breakout = detect_consolidation_breakout(df)

        # ──────────────────────────────────────────────────────────
        #  SIGNAL LOGIC — MTF Confluence (Dual-Strategy)
        # ──────────────────────────────────────────────────────────
        decision = 'WAIT'
        strategy_type = None
        new_stop_loss = 0.0

        if not risk_exit_triggered and current_zscore is not None:
            active_count = count_active_positions(session)

            # ── LONG Confluence ──
            if macro_trend == 'UPTREND' and not in_position:
                # Strategy A: Aggressive Pullback
                if (bullish_ob and current_price <= bullish_ob['high'] and current_zscore < ZSCORE_LONG_THRESHOLD):
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'LONG'
                        strategy_type = 'PULLBACK'
                        new_stop_loss = bullish_ob['low'] * 0.999 # Strictly below OB
                        print(f"✨ [{symbol}] LONG Strategy A (PULLBACK): Macro=UPTREND + Bullish OB + Z={current_zscore:+.2f}", flush=True)
                
                # Strategy B: Momentum Breakout
                elif bullish_breakout:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'LONG'
                        strategy_type = 'BREAKOUT'
                        new_stop_loss = bullish_breakout['breakout_candle_low'] * 0.999 # Below breakout candle
                        print(f"⚡ [{symbol}] LONG Strategy B (BREAKOUT): Macro=UPTREND + Breakout Confirmed (Vol {bullish_breakout['vol_ratio']:.1f}x)", flush=True)

            # ── SHORT Confluence ──
            if decision == 'WAIT' and macro_trend == 'DOWNTREND' and not in_position:
                # Strategy A: Aggressive Pullback
                if (bearish_ob and current_price >= bearish_ob['low'] and current_zscore > ZSCORE_SHORT_THRESHOLD):
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'SHORT'
                        strategy_type = 'PULLBACK'
                        new_stop_loss = bearish_ob['high'] * 1.001 # Strictly above OB
                        print(f"✨ [{symbol}] SHORT Strategy A (PULLBACK): Macro=DOWNTREND + Bearish OB + Z={current_zscore:+.2f}", flush=True)
                
                # Strategy B: Momentum Breakout
                elif bearish_breakout:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})", flush=True)
                    else:
                        decision = 'SHORT'
                        strategy_type = 'BREAKOUT'
                        new_stop_loss = bearish_breakout['breakout_candle_high'] * 1.001 # Above breakout candle
                        print(f"⚡ [{symbol}] SHORT Strategy B (BREAKOUT): Macro=DOWNTREND + Breakout Confirmed (Vol {bearish_breakout['vol_ratio']:.1f}x)", flush=True)

            # ── Log blocked directions ──
            if decision == 'WAIT' and not risk_exit_triggered:
                if macro_trend == 'UPTREND' and current_zscore and current_zscore > ZSCORE_SHORT_THRESHOLD:
                    print(f"🚫 [{symbol}] SHORT blocked: macro=UPTREND", flush=True)
                elif macro_trend == 'DOWNTREND' and current_zscore and current_zscore < ZSCORE_LONG_THRESHOLD:
                    print(f"🚫 [{symbol}] LONG blocked: macro=DOWNTREND", flush=True)"""
content = content.replace(old_signal_logic, new_signal_logic)

# Replace execute blocks
# Find EXECUTE SIGNALS logic and replace to save stop_loss_price and strategy_type
content = content.replace("decision=decision", "decision=decision,\n            strategy_type=strategy_type")

content = content.replace("spend = SLOT_BUDGET * DCA_WEIGHTS[dca_level]", "spend = SLOT_BUDGET * ENTRY_WEIGHT")
content = content.replace("spend = SLOT_BUDGET * DCA_WEIGHTS[0]", "spend = SLOT_BUDGET * ENTRY_WEIGHT")
content = content.replace("highest_price_since_entry=float(portfolio['highest_price_since_entry']),", "highest_price_since_entry=float(portfolio['highest_price_since_entry']),\n                            stop_loss_price=new_stop_loss,\n                            trailing_active=False,")
content = content.replace("lowest_price_since_entry=float(portfolio['lowest_price_since_entry']),", "lowest_price_since_entry=float(portfolio['lowest_price_since_entry']),\n                            stop_loss_price=new_stop_loss,\n                            trailing_active=False,")
content = content.replace("if decision == 'LONG' and dca_level < MAX_BUYS:", "if decision == 'LONG':")
content = content.replace("dca_str = f\"Step {portfolio['dca_level']}/{MAX_BUYS}\"", "dca_str = f\"{strategy_type}\"")
content = content.replace("HARD_STOP_LOSS_PCT*100", "0") # removing hard stop loss references in alerts

# Add the new variables to PortfolioState save inside _close_position_handler
content = content.replace("pnl_pct=float(pnl_pct_val) if pnl_pct_val is not None else None,", "stop_loss_price=None,\n        trailing_active=False,\n        pnl_pct=float(pnl_pct_val) if pnl_pct_val is not None else None,")
content = content.replace("lowest_price_since_entry=float(portfolio['lowest_price_since_entry']) if portfolio['lowest_price_since_entry'] is not None else None,", "lowest_price_since_entry=float(portfolio['lowest_price_since_entry']) if portfolio['lowest_price_since_entry'] is not None else None,\n        stop_loss_price=float(portfolio['stop_loss_price']) if portfolio.get('stop_loss_price') is not None else None,\n        trailing_active=portfolio.get('trailing_active', False),")
content = content.replace("pnl_pct=None,\n                            pnl_usd=None,", "pnl_pct=None,\n                            pnl_usd=None,")

# Write it back for next step
with open('/app/analyzer.py', 'w') as f:
    f.write(content)
