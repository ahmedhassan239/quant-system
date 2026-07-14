"""
scanner.py — Multi-Asset Radar for the Quant Futures Bot
====================================================================
Dynamic Whitelist Scanner: Fetches live 24h ticker data from Binance
Futures, filters strictly against a curated whitelist of ~50 major
real-world coins, sorts by 24h quote volume, and returns the Top N.

This ensures testnet garbage coins (TACUSDT, KORUUSDT, Chinese-char
symbols, etc.) are automatically excluded without needing regex hacks.

Usage:
    python scanner.py
"""

import requests
from config import BINANCE_FUTURES_BASE_URL, TIMEFRAME, ALERT_PREFIX

# ──────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
BINANCE_TICKER_URL = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/24hr"

TOP_N = 15

# ──────────────────────────────────────────────────────────────────────
#  KNOWN MAJORS WHITELIST
#  Only symbols in this set will be accepted by the dynamic scanner.
#  Add/remove pairs as needed — this is the single source of truth.
# ──────────────────────────────────────────────────────────────────────
KNOWN_MAJORS_WHITELIST = {
    # ── Top 10 by Market Cap ──
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
    'ADAUSDT', 'DOGEUSDT', 'TRXUSDT', 'AVAXUSDT', 'DOTUSDT',
    'ZECUSDT', 'PAXGUSDT','TRXUSDT','XLMUSDT','HBARUSDT',
    # ── Large Cap Altcoins ──
    'LINKUSDT', 'MATICUSDT', 'NEARUSDT', 'UNIUSDT', 'LTCUSDT',
    'BCHUSDT', 'APTUSDT', 'FILUSDT', 'ARBUSDT', 'OPUSDT',
    'ATOMUSDT', 'ICPUSDT', 'ETCUSDT', 'XLMUSDT', 'INJUSDT',
    'IMXUSDT', 'SUIUSDT', 'SEIUSDT', 'TIAUSDT', 'STXUSDT',
    # ── Mid Cap / High Volume ──
    'FETUSDT', 'RENDERUSDT', 'AAVEUSDT', 'GRTUSDT', 'ALGOUSDT',
    'FTMUSDT', 'SANDUSDT', 'MANAUSDT', 'AXSUSDT', 'GALAUSDT',
    'THETAUSDT', 'EOSUSDT', 'MKRUSDT', 'SNXUSDT', 'COMPUSDT',
    'LDOUSDT', 'RUNEUSDT', 'ENAUSDT', 'WLDUSDT', 'JUPUSDT',
    # ── Meme / Momentum ──
    'SHIBUSDT', 'PEPEUSDT', 'WIFUSDT', 'BONKUSDT', 'FLOKIUSDT',
}


# ──────────────────────────────────────────────────────────────────────
#  SCANNER LOGIC
# ──────────────────────────────────────────────────────────────────────

def fetch_top_symbols():
    """
    Fetch all 24h tickers from Binance Futures, accept ONLY symbols
    present in KNOWN_MAJORS_WHITELIST, sort by 24h quoteVolume
    descending, and return the Top N most liquid pairs.

    Returns:
        list[dict]: Sorted list of candidate dicts.
    """
    response = requests.get(BINANCE_TICKER_URL, timeout=15)
    response.raise_for_status()
    tickers = response.json()

    candidates = []

    for t in tickers:
        symbol = t['symbol']

        # Strict whitelist gate — reject everything not in the list
        if symbol not in KNOWN_MAJORS_WHITELIST:
            continue

        quote_volume = float(t.get('quoteVolume', 0))
        price_change_pct = abs(float(t.get('priceChangePercent', 0)))

        candidates.append({
            'symbol': symbol,
            'price_change_pct': price_change_pct,
            'quote_volume': quote_volume,
            'last_price': float(t.get('lastPrice', 0)),
        })

    # Sort by highest 24h quote volume (most liquid first)
    candidates.sort(key=lambda x: x['quote_volume'], reverse=True)

    return candidates[:TOP_N]


def scan():
    """
    Run the dynamic whitelist scanner and return the list of symbols
    to trade. Fetches live data, filters by whitelist, ranks by volume.
    """
    top = fetch_top_symbols()
    symbols = [c['symbol'] for c in top]

    # Pretty-print the results
    print("=" * 70, flush=True)
    print(f"  {ALERT_PREFIX} DYNAMIC RADAR — Top {TOP_N} Whitelisted USDT Futures Pairs", flush=True)
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

def update_radar():
    """
    Run the scanner and update the active_symbols table in the shared DB.
    Called every 60 minutes by the Macro engine.
    """
    from database import update_active_symbols
    symbols = scan()
    update_active_symbols(symbols)
    return symbols


if __name__ == "__main__":
    scan()
