"""
backtester.py — Smart DCA with Trailing Take Profit Backtester for PAXG/USDT
==============================================================================
Implements a Dollar Cost Averaging strategy with up to 4 entries (1 Base + 3
Safety Orders), average-entry-price tracking, trailing take-profit, a min-
profit signal exit gate, a portfolio stop-loss, and a post-stop-loss cooldown.

• No database dependencies — fully standalone.
• Deducts 0.1 % trading fee on every BUY and SELL.
• Prints a detailed trade log and performance report.
"""

import time
import requests
import pandas as pd
from datetime import datetime


# ──────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
SYMBOL = 'PAXGUSDT'
INTERVAL = '15m'
TARGET_CANDLES = 10_000           # ~104 days of 15m data
BINANCE_MAX_LIMIT = 1000          # Binance API cap per request
TRADING_FEE = 0.001               # 0.1 % per side
INITIAL_BALANCE = 1000.0          # Starting USDT

# DCA Settings
MAX_BUYS = 4                      # 1 Base Order + 3 Safety Orders
DCA_WEIGHTS = [0.10, 0.20, 0.30, 0.40] # Martingale volume scaling
SAFETY_ORDER_DIP_PCT = 0.02       # -2.0 % below last execution price

# Exit / Risk Management
TRAILING_ACTIVATE_PCT = 0.015     # +1.5 % from avg entry → activate trail
TRAILING_PULLBACK_PCT = 0.005     # -0.5 % pullback from peak → sell all
MIN_PROFIT_PCT = 0.01             # +1.0 % from avg entry → signal sell ok
PORTFOLIO_STOP_LOSS_PCT = 0.05    # -5.0 % from avg entry → sell everything

# Cooldown
COOLDOWN_CANDLES = 12             # Candles to skip after a stop-loss exit


# ──────────────────────────────────────────────────────────────────────
#  DATA FETCHING (with pagination)
# ──────────────────────────────────────────────────────────────────────

def fetch_historical_klines(symbol=SYMBOL, interval=INTERVAL,
                            target=TARGET_CANDLES):
    """
    Fetch `target` historical klines from Binance, paginating backward
    in batches of 1000.  Returns a clean DataFrame sorted by timestamp.
    """
    url = "https://api.binance.com/api/v3/klines"
    all_data = []
    end_time = None  # Start from the most recent candle

    print(f"Fetching {target:,} candles for {symbol} ({interval})...",
          flush=True)

    while len(all_data) < target:
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': BINANCE_MAX_LIMIT,
        }
        if end_time is not None:
            params['endTime'] = end_time

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        batch = response.json()

        if not batch:
            print("No more data returned from Binance. Stopping pagination.",
                  flush=True)
            break

        all_data = batch + all_data       # prepend (older data first)
        end_time = batch[0][0] - 1        # move window backward

        print(f"  ... fetched {len(all_data):,} / {target:,} candles",
              flush=True)

        # Respect rate limits
        time.sleep(0.3)

    # Trim to exact target (keep the most recent candles)
    if len(all_data) > target:
        all_data = all_data[-target:]

    # Build DataFrame
    df = pd.DataFrame(all_data, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume',
        'ignore',
    ])

    df = df[['open_time', 'open', 'high', 'low', 'close', 'volume']]
    df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')

    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)

    df = df.drop(columns=['open_time'])
    df = df.reset_index(drop=True)

    print(f"Data ready: {len(df):,} candles  "
          f"({df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]})\n",
          flush=True)
    return df


# ──────────────────────────────────────────────────────────────────────
#  INDICATORS  (exact mirror of analyzer.py)
# ──────────────────────────────────────────────────────────────────────

def calculate_rsi(df, period=14):
    """
    Calculate 14-period RSI using pure Pandas and Wilder's Smoothing.
    """
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Apply standard Wilder's Smoothing (EMA with alpha=1/14)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period,
                        adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period,
                        adjust=False).mean()

    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    df.loc[avg_loss == 0, 'RSI'] = 100.0
    return df


def detect_order_blocks(df, idx, lookback=15):
    """
    Detect Bullish and Bearish Order Blocks using the `lookback`
    candles ending at `idx` (inclusive).

    Bullish OB: Find the lowest low in the window, then walk backward
                to find the last bearish candle (close < open).
                The OB zone is that candle's [low, high].

    Bearish OB: Find the highest high in the window, then walk backward
                to find the last bullish candle (close > open).
                The OB zone is that candle's [low, high].
    """
    start = max(0, idx - lookback + 1)
    window = df.iloc[start:idx + 1]

    if len(window) < lookback:
        return None, None

    # -- Bullish OB --
    min_idx = window['low'].idxmin()
    min_loc = df.index.get_loc(min_idx)

    bullish_ob = None
    for i in range(min_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] < row['open']:  # bearish candle
            bullish_ob = {'high': row['high'], 'low': row['low']}
            break

    # -- Bearish OB --
    max_idx = window['high'].idxmax()
    max_loc = df.index.get_loc(max_idx)

    bearish_ob = None
    for i in range(max_loc, -1, -1):
        row = df.iloc[i]
        if row['close'] > row['open']:  # bullish candle
            bearish_ob = {'high': row['high'], 'low': row['low']}
            break

    return bullish_ob, bearish_ob


# ──────────────────────────────────────────────────────────────────────
#  BACKTEST ENGINE — Smart DCA with Trailing Take Profit
# ──────────────────────────────────────────────────────────────────────

def _close_all(paxg_balance, current_price, avg_entry_price,
               entry_time, current_time, total_cost, reason):
    """
    Sell the entire PAXG position.

    Parameters
    ----------
    paxg_balance     : total PAXG held across all DCA fills
    current_price    : close price on the exit candle
    avg_entry_price  : volume-weighted average entry price
    entry_time       : timestamp of the *first* buy (Base Order)
    current_time     : timestamp of the exit candle
    total_cost       : cumulative USDT spent (after buy-side fees)
    reason           : exit label string

    Returns
    -------
    (usdt_received, trade_record)
    """
    gross_usdt = paxg_balance * current_price
    usdt_received = gross_usdt * (1 - TRADING_FEE)

    pnl_usd = usdt_received - total_cost
    pnl_pct = ((current_price - avg_entry_price) / avg_entry_price) * 100

    trade = {
        'entry_time': entry_time,
        'avg_entry_price': round(avg_entry_price, 2),
        'exit_time': current_time,
        'exit_price': round(current_price, 2),
        'dca_fills': 0,          # set by caller
        'pnl_usd': round(pnl_usd, 2),
        'pnl_pct': round(pnl_pct, 2),
        'exit_reason': reason,
    }
    return usdt_received, trade


def run_backtest():
    df = fetch_historical_klines()
    df = calculate_rsi(df, period=14)
    df['SMA_200'] = df['close'].rolling(window=200).mean()

    # ── Portfolio state ──
    usdt_balance = INITIAL_BALANCE
    paxg_balance = 0.0            # total PAXG across all DCA fills
    avg_entry_price = 0.0         # volume-weighted average entry
    last_exec_price = 0.0         # price of the most recent fill (for SO spacing)
    total_cost = 0.0              # cumulative USDT committed (after buy fee)
    num_buys = 0                  # how many fills so far (0 = no position)

    # Trailing-stop state
    highest_since_activation = 0.0
    trailing_active = False

    # Cooldown state
    cooldown_remaining = 0        # candles left before new BUY is allowed

    # Trade log
    trades = []
    entry_time = None             # timestamp of the Base Order

    print("Running Smart DCA backtest simulation...", flush=True)

    # Start at index 15 so the lookback window is always full
    for i in range(15, len(df)):
        row = df.iloc[i]
        current_price = float(row['close'])
        current_rsi = float(row['RSI']) if pd.notna(row['RSI']) else None
        current_sma = float(row['SMA_200']) if pd.notna(row['SMA_200']) else None
        current_time = row['timestamp']

        if current_rsi is None or current_sma is None:
            continue

        # ── Tick down cooldown counter ──
        if cooldown_remaining > 0:
            cooldown_remaining -= 1

        # ── Risk-management exits (checked BEFORE signal logic) ──
        if num_buys > 0 and paxg_balance > 0:
            unrealized_pct = (current_price - avg_entry_price) / avg_entry_price

            # 1) Portfolio Stop-Loss: -3.0 % from average entry
            if unrealized_pct <= -PORTFOLIO_STOP_LOSS_PCT:
                usdt_received, trade = _close_all(
                    paxg_balance, current_price, avg_entry_price,
                    entry_time, current_time, total_cost, 'STOP_LOSS')
                trade['dca_fills'] = num_buys
                trades.append(trade)

                usdt_balance += usdt_received
                paxg_balance = 0.0
                avg_entry_price = 0.0
                last_exec_price = 0.0
                total_cost = 0.0
                num_buys = 0
                trailing_active = False
                highest_since_activation = 0.0
                cooldown_remaining = COOLDOWN_CANDLES   # ← activate cooldown
                continue

            # 2) Trailing Stop: activate at +1.5 % from avg entry,
            #    trigger sell when price pulls back 0.5 % from peak
            if unrealized_pct >= TRAILING_ACTIVATE_PCT:
                if not trailing_active:
                    trailing_active = True
                    highest_since_activation = current_price
                else:
                    if current_price > highest_since_activation:
                        highest_since_activation = current_price
            elif trailing_active:
                # Still track higher closes while trailing is active
                if current_price > highest_since_activation:
                    highest_since_activation = current_price

            if trailing_active:
                pullback_pct = ((highest_since_activation - current_price)
                                / highest_since_activation)
                if pullback_pct >= TRAILING_PULLBACK_PCT:
                    usdt_received, trade = _close_all(
                        paxg_balance, current_price, avg_entry_price,
                        entry_time, current_time, total_cost,
                        'TRAILING_STOP')
                    trade['dca_fills'] = num_buys
                    trades.append(trade)

                    usdt_balance += usdt_received
                    paxg_balance = 0.0
                    avg_entry_price = 0.0
                    last_exec_price = 0.0
                    total_cost = 0.0
                    num_buys = 0
                    trailing_active = False
                    highest_since_activation = 0.0
                    cooldown_remaining = 0             # no cooldown on wins
                    continue

        # ── Signal Logic ──
        bullish_ob, bearish_ob = detect_order_blocks(df, i, lookback=15)

        # ── Evaluate SELL signal (before BUY so we don't buy and sell
        #    on the same candle) ──
        if num_buys > 0 and paxg_balance > 0:
            if (bearish_ob
                    and current_price >= bearish_ob['low']
                    and current_rsi > 70):
                profit_pct = (current_price - avg_entry_price) / avg_entry_price
                if profit_pct >= MIN_PROFIT_PCT:
                    usdt_received, trade = _close_all(
                        paxg_balance, current_price, avg_entry_price,
                        entry_time, current_time, total_cost, 'SIGNAL')
                    trade['dca_fills'] = num_buys
                    trades.append(trade)

                    usdt_balance += usdt_received
                    paxg_balance = 0.0
                    avg_entry_price = 0.0
                    last_exec_price = 0.0
                    total_cost = 0.0
                    num_buys = 0
                    trailing_active = False
                    highest_since_activation = 0.0
                    cooldown_remaining = 0
                    continue

        # ── Evaluate BUY signals ──

        # --- Base Order (first entry) ---
        if (num_buys == 0
                and cooldown_remaining == 0
                and bullish_ob
                and current_price <= bullish_ob['high']
                and current_rsi < 30
                and current_price > current_sma):

            spend = INITIAL_BALANCE * DCA_WEIGHTS[0]
            if usdt_balance < spend:
                continue

            effective_usdt = spend * (1 - TRADING_FEE)
            paxg_bought = effective_usdt / current_price

            usdt_balance -= spend
            paxg_balance += paxg_bought
            total_cost += effective_usdt
            avg_entry_price = current_price
            last_exec_price = current_price
            num_buys = 1

            entry_time = current_time
            trailing_active = False
            highest_since_activation = 0.0

        # --- Safety Orders (DCA fills 2-4) ---
        elif (0 < num_buys < MAX_BUYS
                and current_rsi < 30
                and current_price <= last_exec_price * (1 - SAFETY_ORDER_DIP_PCT)):

            spend = INITIAL_BALANCE * DCA_WEIGHTS[num_buys]
            if usdt_balance < spend:
                continue

            effective_usdt = spend * (1 - TRADING_FEE)
            paxg_bought = effective_usdt / current_price

            # Update weighted average entry price
            old_value = paxg_balance * avg_entry_price
            new_value = paxg_bought * current_price
            paxg_balance += paxg_bought
            avg_entry_price = (old_value + new_value) / paxg_balance

            usdt_balance -= spend
            total_cost += effective_usdt
            last_exec_price = current_price
            num_buys += 1

            # Reset trailing state on new DCA fill (avg entry changed)
            trailing_active = False
            highest_since_activation = 0.0

    # ── Close open position at last candle price ──
    if num_buys > 0 and paxg_balance > 0:
        final_price = float(df.iloc[-1]['close'])
        final_time = df.iloc[-1]['timestamp']
        usdt_received, trade = _close_all(
            paxg_balance, final_price, avg_entry_price,
            entry_time, final_time, total_cost, 'END_OF_DATA')
        trade['dca_fills'] = num_buys
        trades.append(trade)
        usdt_balance += usdt_received
        paxg_balance = 0.0

    # ──────────────────────────────────────────────────────────────────
    #  REPORT
    # ──────────────────────────────────────────────────────────────────
    final_balance = usdt_balance
    total_pnl_usd = final_balance - INITIAL_BALANCE
    total_pnl_pct = (total_pnl_usd / INITIAL_BALANCE) * 100

    total_trades = len(trades)
    winning = [t for t in trades if t['pnl_usd'] > 0]
    losing = [t for t in trades if t['pnl_usd'] <= 0]
    win_rate = (len(winning) / total_trades * 100) if total_trades > 0 else 0.0

    # Exit-reason breakdown
    signal_exits = [t for t in trades if t['exit_reason'] == 'SIGNAL']
    sl_exits = [t for t in trades if t['exit_reason'] == 'STOP_LOSS']
    ts_exits = [t for t in trades if t['exit_reason'] == 'TRAILING_STOP']
    eod_exits = [t for t in trades if t['exit_reason'] == 'END_OF_DATA']

    # DCA stats
    total_dca_fills = sum(t['dca_fills'] for t in trades)
    avg_dca_fills = (total_dca_fills / total_trades) if total_trades > 0 else 0

    w = 76  # report width

    print("\n" + "═" * w, flush=True)
    print("║" + " PAXG/USDT SMART DCA BACKTEST REPORT ".center(w - 2) + "║",
          flush=True)
    print("═" * w, flush=True)

    print(f"║  Symbol           : {SYMBOL:<52}║", flush=True)
    print(f"║  Interval         : {INTERVAL:<52}║", flush=True)
    print(f"║  Candles          : {len(df):<51,}║", flush=True)
    print(f"║  Period           : {str(df['timestamp'].iloc[0].date())}"
          f" → {str(df['timestamp'].iloc[-1].date()):<30}║", flush=True)
    print(f"║  Trading Fee      : {TRADING_FEE*100:.1f}% per side"
          f"{'':>38}║", flush=True)

    print("╠" + "─" * (w - 2) + "╣", flush=True)
    print("║" + " DCA & Risk Parameters ".center(w - 2) + "║", flush=True)
    print("╠" + "─" * (w - 2) + "╣", flush=True)
    print(f"║  Max Buys (BO+SO) : {MAX_BUYS}  (Weights = {DCA_WEIGHTS}){'':>19}║", flush=True)
    print(f"║  Trend Filter     : SMA 200 (Base Orders only){'':>25}║", flush=True)
    print(f"║  SO Dip Trigger   : -{SAFETY_ORDER_DIP_PCT*100:.1f}% "
          f"below last exec price"
          f"{'':>25}║", flush=True)
    print(f"║  Trailing Activate: +{TRAILING_ACTIVATE_PCT*100:.1f}%  →  "
          f"Pullback trigger: -{TRAILING_PULLBACK_PCT*100:.1f}%"
          f"{'':>19}║", flush=True)
    print(f"║  Min Profit Gate  : +{MIN_PROFIT_PCT*100:.1f}%"
          f" (signal sell only above){'':>24}║", flush=True)
    print(f"║  Portfolio Stop   : -{PORTFOLIO_STOP_LOSS_PCT*100:.1f}%"
          f" from avg entry"
          f"{'':>31}║", flush=True)
    print(f"║  Cooldown (SL)    : {COOLDOWN_CANDLES} candles"
          f"{'':>43}║", flush=True)

    print("╠" + "─" * (w - 2) + "╣", flush=True)
    print("║" + " Performance ".center(w - 2) + "║", flush=True)
    print("╠" + "─" * (w - 2) + "╣", flush=True)

    print(f"║  Initial Balance  : ${INITIAL_BALANCE:>13,.2f}{'':>36}║",
          flush=True)
    print(f"║  Final Balance    : ${final_balance:>13,.2f}{'':>36}║",
          flush=True)

    sign = "+" if total_pnl_usd >= 0 else ""
    print(f"║  Net PnL (USD)    : {sign}${total_pnl_usd:>12,.2f}{'':>36}║",
          flush=True)
    print(f"║  Net PnL (%)      : {sign}{total_pnl_pct:>13.2f}%{'':>35}║",
          flush=True)

    print("╠" + "─" * (w - 2) + "╣", flush=True)
    print("║" + " Trade Statistics ".center(w - 2) + "║", flush=True)
    print("╠" + "─" * (w - 2) + "╣", flush=True)

    print(f"║  Total Rounds     : {total_trades:>13}{'':>38}║", flush=True)
    print(f"║  Winning Rounds   : {len(winning):>13}{'':>38}║", flush=True)
    print(f"║  Losing Rounds    : {len(losing):>13}{'':>38}║", flush=True)
    print(f"║  Win Rate         : {win_rate:>12.1f}%{'':>38}║", flush=True)
    print(f"║  Avg DCA Fills    : {avg_dca_fills:>13.1f}{'':>38}║",
          flush=True)

    if winning:
        best = max(winning, key=lambda t: t['pnl_usd'])
        print(f"║  Best Trade       : +${best['pnl_usd']:>11,.2f} "
              f"(+{best['pnl_pct']:.2f}%){'':>25}║", flush=True)
    if losing:
        worst = min(losing, key=lambda t: t['pnl_usd'])
        print(f"║  Worst Trade      :  ${worst['pnl_usd']:>11,.2f} "
              f"({worst['pnl_pct']:.2f}%){'':>25}║", flush=True)

    print("╠" + "─" * (w - 2) + "╣", flush=True)
    print("║" + " Exit Reasons ".center(w - 2) + "║", flush=True)
    print("╠" + "─" * (w - 2) + "╣", flush=True)
    print(f"║  Signal (RSI+OB)  : {len(signal_exits):>13}{'':>38}║",
          flush=True)
    print(f"║  Portfolio Stop   : {len(sl_exits):>13}{'':>38}║",
          flush=True)
    print(f"║  Trailing Stop    : {len(ts_exits):>13}{'':>38}║",
          flush=True)
    if eod_exits:
        print(f"║  End-of-Data      : {len(eod_exits):>13}{'':>38}║",
              flush=True)

    print("═" * w, flush=True)

    # ── Detailed Trade Log ──
    if trades:
        print("\n" + "─" * w, flush=True)
        print("  #  │  ENTRY DATE          │  EXIT DATE           │"
              " Avg Entry │  PnL ($)   │ PnL (%) │ DCA │ EXIT REASON",
              flush=True)
        print("─" * w, flush=True)

        for idx, t in enumerate(trades, 1):
            entry_dt = t['entry_time'].strftime('%Y-%m-%d %H:%M')
            exit_dt = t['exit_time'].strftime('%Y-%m-%d %H:%M')
            pnl_sign = "+" if t['pnl_usd'] >= 0 else ""
            reason = t.get('exit_reason', 'N/A')
            avg_p = t.get('avg_entry_price', 0)
            fills = t.get('dca_fills', 0)
            print(f"  {idx:>2} │  {entry_dt}  │  {exit_dt}  │"
                  f" {avg_p:>8.2f} │"
                  f" {pnl_sign}{t['pnl_usd']:>9.2f} │"
                  f" {pnl_sign}{t['pnl_pct']:>6.2f}% │"
                  f"  {fills}  │ {reason}",
                  flush=True)

        print("─" * w, flush=True)
    else:
        print("\n  No trades were executed during this period.", flush=True)

    print("", flush=True)


if __name__ == "__main__":
    run_backtest()
