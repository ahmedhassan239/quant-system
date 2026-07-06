"""
backtester.py — Standalone Historical Backtester for PAXG/USDT
==============================================================
Replicates the EXACT trading logic from analyzer.py (RSI + Order Blocks)
against ~10,000 historical 15m candles fetched from Binance.

• No database dependencies — fully standalone.
• Deducts 0.1% trading fee on every BUY and SELL.
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
TARGET_CANDLES = 10_000       # ~104 days of 15m data
BINANCE_MAX_LIMIT = 1000      # Binance API cap per request
TRADING_FEE = 0.001           # 0.1% per side
INITIAL_BALANCE = 1000.0      # Starting USDT


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
#  BACKTEST ENGINE
# ──────────────────────────────────────────────────────────────────────

def run_backtest():
    df = fetch_historical_klines()
    df = calculate_rsi(df, period=14)

    # Portfolio state
    usdt_balance = INITIAL_BALANCE
    paxg_balance = 0.0
    last_buy_price = 0.0
    state = 'WAIT'        # WAIT = holding USDT, LONG = holding PAXG
    last_decision = None   # Duplicate-signal filter (mirrors analyzer.py)

    # Trade log
    trades = []
    entry_time = None
    entry_price = 0.0

    print("Running backtest simulation...", flush=True)

    # Start at index 15 so the lookback window is always full
    for i in range(15, len(df)):
        row = df.iloc[i]
        current_price = float(row['close'])
        current_rsi = float(row['RSI']) if pd.notna(row['RSI']) else None
        current_time = row['timestamp']

        if current_rsi is None:
            continue

        bullish_ob, bearish_ob = detect_order_blocks(df, i, lookback=15)

        # ── Decision Logic (exact mirror of analyzer.py) ──
        decision = 'WAIT'

        if (bullish_ob
                and current_price <= bullish_ob['high']
                and current_rsi < 30):
            decision = 'BUY'

        if (bearish_ob
                and current_price >= bearish_ob['low']
                and current_rsi > 70):
            decision = 'SELL'

        # Only act on state changes (mirrors analyzer.py dedup logic)
        if decision == 'WAIT' or decision == last_decision:
            continue

        # ── Execute BUY ──
        if decision == 'BUY' and state == 'WAIT' and usdt_balance > 0:
            # Deduct fee from USDT before buying
            effective_usdt = usdt_balance * (1 - TRADING_FEE)
            paxg_balance = effective_usdt / current_price
            last_buy_price = current_price
            usdt_balance = 0.0
            state = 'LONG'
            last_decision = 'BUY'

            entry_time = current_time
            entry_price = current_price

        # ── Execute SELL ──
        elif decision == 'SELL' and state == 'LONG' and paxg_balance > 0:
            gross_usdt = paxg_balance * current_price
            # Deduct fee from proceeds
            usdt_balance = gross_usdt * (1 - TRADING_FEE)
            pnl_usd = usdt_balance - (paxg_balance * last_buy_price)
            pnl_pct = ((current_price - last_buy_price)
                       / last_buy_price) * 100

            trades.append({
                'entry_time': entry_time,
                'entry_price': entry_price,
                'exit_time': current_time,
                'exit_price': current_price,
                'pnl_usd': round(pnl_usd, 2),
                'pnl_pct': round(pnl_pct, 2),
            })

            paxg_balance = 0.0
            last_buy_price = 0.0
            state = 'WAIT'
            last_decision = 'SELL'

    # ── Close open position at last candle price ──
    if state == 'LONG' and paxg_balance > 0:
        final_price = float(df.iloc[-1]['close'])
        gross_usdt = paxg_balance * final_price
        usdt_balance = gross_usdt * (1 - TRADING_FEE)
        pnl_usd = usdt_balance - (paxg_balance * last_buy_price)
        pnl_pct = ((final_price - last_buy_price) / last_buy_price) * 100

        trades.append({
            'entry_time': entry_time,
            'entry_price': entry_price,
            'exit_time': df.iloc[-1]['timestamp'],
            'exit_price': final_price,
            'pnl_usd': round(pnl_usd, 2),
            'pnl_pct': round(pnl_pct, 2),
        })
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

    w = 62  # report width

    print("\n" + "═" * w, flush=True)
    print("║" + " PAXG/USDT BACKTEST REPORT ".center(w - 2) + "║",
          flush=True)
    print("═" * w, flush=True)

    print(f"║  Symbol          : {SYMBOL:<38}║", flush=True)
    print(f"║  Interval        : {INTERVAL:<38}║", flush=True)
    print(f"║  Candles          : {len(df):<37,}║", flush=True)
    print(f"║  Period          : {str(df['timestamp'].iloc[0].date())}"
          f" → {str(df['timestamp'].iloc[-1].date()):<16}║", flush=True)
    print(f"║  Trading Fee     : {TRADING_FEE*100:.1f}% per side"
          f"{'':<24}║", flush=True)

    print("╠" + "─" * (w - 2) + "╣", flush=True)

    print(f"║  Initial Balance : ${INITIAL_BALANCE:>13,.2f}{'':<22}║",
          flush=True)
    print(f"║  Final Balance   : ${final_balance:>13,.2f}{'':<22}║",
          flush=True)

    sign = "+" if total_pnl_usd >= 0 else ""
    print(f"║  Net PnL (USD)   : {sign}${total_pnl_usd:>12,.2f}{'':<22}║",
          flush=True)
    print(f"║  Net PnL (%)     : {sign}{total_pnl_pct:>13.2f}%{'':<21}║",
          flush=True)

    print("╠" + "─" * (w - 2) + "╣", flush=True)

    print(f"║  Total Trades    : {total_trades:>13}{'':<24}║", flush=True)
    print(f"║  Winning Trades  : {len(winning):>13}{'':<24}║", flush=True)
    print(f"║  Losing Trades   : {len(losing):>13}{'':<24}║", flush=True)
    print(f"║  Win Rate        : {win_rate:>12.1f}%{'':<24}║", flush=True)

    if winning:
        best = max(winning, key=lambda t: t['pnl_usd'])
        print(f"║  Best Trade      : +${best['pnl_usd']:>11,.2f} "
              f"(+{best['pnl_pct']:.2f}%){'':<11}║", flush=True)
    if losing:
        worst = min(losing, key=lambda t: t['pnl_usd'])
        print(f"║  Worst Trade     :  ${worst['pnl_usd']:>11,.2f} "
              f"({worst['pnl_pct']:.2f}%){'':<11}║", flush=True)

    print("═" * w, flush=True)

    # ── Detailed Trade Log ──
    if trades:
        print("\n" + "─" * w, flush=True)
        print("  #  │  ENTRY DATE          │  EXIT DATE           │"
              "  PnL ($)   │  PnL (%)", flush=True)
        print("─" * w, flush=True)

        for idx, t in enumerate(trades, 1):
            entry_dt = t['entry_time'].strftime('%Y-%m-%d %H:%M')
            exit_dt = t['exit_time'].strftime('%Y-%m-%d %H:%M')
            pnl_sign = "+" if t['pnl_usd'] >= 0 else ""
            print(f"  {idx:>2} │  {entry_dt}  │  {exit_dt}  │ "
                  f"{pnl_sign}{t['pnl_usd']:>9.2f} │ "
                  f"{pnl_sign}{t['pnl_pct']:>6.2f}%", flush=True)

        print("─" * w, flush=True)
    else:
        print("\n  No trades were executed during this period.", flush=True)

    print("", flush=True)


if __name__ == "__main__":
    run_backtest()
