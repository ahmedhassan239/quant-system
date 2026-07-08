"""
scanner.py — Multi-Asset Radar for the Smart DCA Strategy
==========================================================
⚠️ TESTNET OVERRIDE: Dynamic radar bypassed — using a static
   8-symbol list because Binance Testnet has very low liquidity
   and the volume filter returns too few pairs.

   To restore dynamic scanning, set USE_STATIC_SYMBOLS = False.

Usage:
    python scanner.py
"""

import requests
from config import BINANCE_BASE_URL, TIMEFRAME, ALERT_PREFIX

# ──────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
BINANCE_TICKER_URL = f"{BINANCE_BASE_URL}/v3/ticker/24hr"
MIN_QUOTE_VOLUME = 50_000_000  # $50M minimum 24h USDT volume

# Stablecoin / peg-asset fragments to exclude
EXCLUDED_FRAGMENTS = {'USDC', 'FDUSD', 'TUSD', 'EUR', 'BUSD', 'DAI', 'RLUSD', 'USD1'}

# Portfolio anchor — always included regardless of filters
ANCHOR_SYMBOL = 'PAXGUSDT'

TOP_N = 10

# ──────────────────────────────────────────────────────────────────────
#  ⚠️ TESTNET OVERRIDE: Static symbol list
#     Set to False to re-enable the dynamic volume/volatility radar.
# ──────────────────────────────────────────────────────────────────────
USE_STATIC_SYMBOLS = True

TARGET_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ZECUSDT",
    "ADAUSDT",
    "PAXGUSDT",
]


# ──────────────────────────────────────────────────────────────────────
#  SCANNER LOGIC
# ──────────────────────────────────────────────────────────────────────

def fetch_top_symbols():
    """
    Fetch all 24h tickers from Binance, filter, and return the Top N
    most volatile USDT pairs with sufficient liquidity.

    Returns:
        list[dict]: Sorted list of candidate dicts.
    """
    response = requests.get(BINANCE_TICKER_URL, timeout=15)
    response.raise_for_status()
    tickers = response.json()

    candidates = []

    for t in tickers:
        symbol = t['symbol']

        # Rule 1: Must be a USDT pair
        if not symbol.endswith('USDT'):
            continue

        # Rule 2: Exclude stablecoins and peg-assets
        if any(frag in symbol for frag in EXCLUDED_FRAGMENTS):
            continue

        # Rule 3: Minimum 24h quote volume ($50M)
        quote_volume = float(t.get('quoteVolume', 0))
        if quote_volume < MIN_QUOTE_VOLUME:
            continue

        # Collect volatility metric (absolute % change)
        price_change_pct = abs(float(t.get('priceChangePercent', 0)))

        candidates.append({
            'symbol': symbol,
            'price_change_pct': price_change_pct,
            'quote_volume': quote_volume,
            'last_price': float(t.get('lastPrice', 0)),
        })

    # Sort by highest absolute price change (most volatile first)
    candidates.sort(key=lambda x: x['price_change_pct'], reverse=True)

    return candidates[:TOP_N]


def scan():
    """
    Return the list of symbols to trade.

    When USE_STATIC_SYMBOLS is True (Testnet mode), returns the hardcoded
    TARGET_SYMBOLS list directly, skipping the volume/volatility radar.
    """
    if USE_STATIC_SYMBOLS:
        symbols = list(TARGET_SYMBOLS)
        print("=" * 70, flush=True)
        print(f"  {ALERT_PREFIX} STATIC SYMBOL LIST (Testnet Override)", flush=True)
        print("=" * 70, flush=True)
        for i, sym in enumerate(symbols, 1):
            print(f"  {i:<4} {sym}", flush=True)
        print("=" * 70, flush=True)
        print(f"\n  Result: {symbols}\n", flush=True)
        return symbols

    # ── Dynamic radar (production mode) ──
    top = fetch_top_symbols()
    symbols = [c['symbol'] for c in top]

    # Force-append anchor if not already present
    if ANCHOR_SYMBOL not in symbols:
        symbols.append(ANCHOR_SYMBOL)

    # Pretty-print the results
    print("=" * 70, flush=True)
    print(f"  {ALERT_PREFIX} MULTI-ASSET RADAR — Top 10 Volatile USDT Pairs", flush=True)
    print("=" * 70, flush=True)
    print(f"  {'#':<4} {'Symbol':<14} {'Price':>12} {'24h Chg %':>10} {'24h Vol ($M)':>14}", flush=True)
    print("-" * 70, flush=True)

    for i, c in enumerate(top, 1):
        vol_m = c['quote_volume'] / 1_000_000
        sign = "+" if c['price_change_pct'] >= 0 else ""
        print(
            f"  {i:<4} {c['symbol']:<14} ${c['last_price']:>10,.2f} "
            f"{sign}{c['price_change_pct']:>8.2f}% "
            f"${vol_m:>12,.1f}M",
            flush=True,
        )

    print("=" * 70, flush=True)
    print(f"\n  Result: {symbols}\n", flush=True)

    return symbols


if __name__ == "__main__":
    scan()

