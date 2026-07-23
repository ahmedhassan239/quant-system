"""
scanner.py — Dynamic Full-Market Radar for the Quant Futures Bot
====================================================================
TRUE Full-Market Scanner: Fetches live 24h ticker data from Binance
Futures for ALL active USDT perpetual pairs, cross-checks against
/fapi/v1/exchangeInfo for TRADING status, filters out stablecoin pairs,
sorts the entire valid universe by 24h quoteVolume descending, and
returns the Top N most liquid pairs dynamically.

No whitelist. No static symbol list. Pure liquidity-ranked discovery.

Usage:
    python scanner.py
"""

import requests
from config import BINANCE_FUTURES_BASE_URL, TIMEFRAME, ALERT_PREFIX

# ──────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
BINANCE_TICKER_URL       = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/24hr"
BINANCE_EXCHANGE_INFO_URL = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/exchangeInfo"

TOP_N = 15
MIN_QUOTE_VOLUME = 15_000_000.0  # Minimum 24h quote volume ($15M USDT)

# Stablecoin / fiat-pegged quote pairs to exclude.
# These end with USDT but do not represent tradeable crypto assets.
STABLECOIN_BLACKLIST = {
    'USDCUSDT', 'BUSDUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'DAIUSDT',
    'USDPUSDT', 'EURUSDT', 'GBPUSDT', 'AUDUSDT', 'JPYUSDT',
}


# ──────────────────────────────────────────────────────────────────────
#  ACTIVE SYMBOL RESOLVER  (via /fapi/v1/exchangeInfo)
# ──────────────────────────────────────────────────────────────────────

def _fetch_active_usdt_perpetuals() -> set | None:
    """
    Query /fapi/v1/exchangeInfo and return the set of all USDT-margined
    PERPETUAL contract symbols whose status is 'TRADING'.

    This is the authoritative source for active contracts — it eliminates
    testnet garbage, delisted symbols, and non-perpetual instruments.

    Returns:
        set[str]  — active symbols, or None if the request fails
                    (caller falls back to ticker-only filtering).
    """
    try:
        resp = requests.get(BINANCE_EXCHANGE_INFO_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(
            f"⚠️  [RADAR] exchangeInfo unavailable ({exc}). "
            "Falling back to ticker-only USDT filter.",
            flush=True,
        )
        return None

    active = set()
    for sym in data.get('symbols', []):
        if (
            sym.get('status')       == 'TRADING'
            and sym.get('contractType') == 'PERPETUAL'
            and sym.get('quoteAsset')   == 'USDT'
        ):
            active.add(sym['symbol'])

    print(f"  [RADAR] exchangeInfo resolved {len(active)} active USDT perpetuals.", flush=True)
    return active


# ──────────────────────────────────────────────────────────────────────
#  SCANNER LOGIC
# ──────────────────────────────────────────────────────────────────────

def fetch_top_symbols():
    """
    TRUE FULL-MARKET DYNAMIC SCAN — no whitelist.

    Pipeline:
      1. Resolve active USDT PERPETUAL contracts via /fapi/v1/exchangeInfo.
      2. Fetch all 24h tickers from /fapi/v1/ticker/24hr.
      3. Accept only symbols present in the active perpetuals set.
      4. Reject stablecoin / fiat-pegged pairs via STABLECOIN_BLACKLIST.
      5. Enforce minimum 24h quote volume of $10M USDT.
      6. Sort the entire valid universe by 24h quoteVolume descending.
      7. Return the Top N most liquid pairs.

    Returns:
        list[dict]: Volume-ranked list of the top N candidate dicts,
                    preserving strict quoteVolume descending order.
    """
    # ── Step 1: Active perpetual universe ─────────────────────────────
    active_perps = _fetch_active_usdt_perpetuals()

    # ── Step 2: Fetch all 24h tickers ─────────────────────────────────
    response = requests.get(BINANCE_TICKER_URL, timeout=15)
    response.raise_for_status()
    tickers = response.json()

    candidates = []

    for t in tickers:
        symbol = t.get('symbol', '')

        # Must be a USDT-quoted pair
        if not symbol.endswith('USDT'):
            continue

        # Must be an active USDT perpetual (when exchangeInfo resolved)
        if active_perps is not None and symbol not in active_perps:
            continue

        # Exclude stablecoin / fiat pairs
        if symbol in STABLECOIN_BLACKLIST:
            continue

        quote_volume    = float(t.get('quoteVolume', 0))
        price_change_pct = float(t.get('priceChangePercent', 0))

        # Enforce hard minimum 24h Quote Volume of $10M USDT
        if quote_volume < MIN_QUOTE_VOLUME:
            continue

        candidates.append({
            'symbol':          symbol,
            'price_change_pct': price_change_pct,
            'quote_volume':    quote_volume,
            'last_price':      float(t.get('lastPrice', 0)),
        })

    # ── Step 3: Rank by 24h quoteVolume, take Top N ───────────────────
    candidates.sort(key=lambda x: x['quote_volume'], reverse=True)

    return candidates[:TOP_N]


def scan():
    """
    Run the full-market dynamic radar and return the volume-ranked
    list of top 15 symbols to trade.

    Symbols are returned in strict descending quoteVolume order
    (e.g. [BTCUSDT, ETHUSDT, SOLUSDT, ...]).
    No static overrides or ALWAYS_INCLUDE lists are applied.
    """
    top     = fetch_top_symbols()
    symbols = [c['symbol'] for c in top]

    # ── Pretty-print the ranked results ───────────────────────────────
    print("=" * 70, flush=True)
    print(
        f"  🚀 [LIVE - 5m EXEC] DYNAMIC RADAR — "
        f"Top {TOP_N} Full Market Liquid USDT Futures Pairs",
        flush=True,
    )
    print("=" * 70, flush=True)
    print(
        f"  {'#':<4} {'Symbol':<14} {'Price':>12} {'24h Chg %':>10} {'24h Vol ($M)':>14}",
        flush=True,
    )
    print("-" * 70, flush=True)

    for i, c in enumerate(top, 1):
        vol_m = c['quote_volume'] / 1_000_000
        chg   = c['price_change_pct']
        sign  = "+" if chg >= 0 else ""
        print(
            f"  {i:<4} {c['symbol']:<14} ${c['last_price']:>10,.4f} "
            f"{sign}{chg:>8.2f}% "
            f"${vol_m:>12,.1f}M",
            flush=True,
        )

    print("=" * 70, flush=True)
    print(f"\n  Result: {symbols}\n", flush=True)

    return symbols


def update_radar():
    """
    Run the full-market scanner and persist the active symbol list to
    the shared DB. Called every 5 minutes by the execution scheduler.
    """
    from database import update_active_symbols
    symbols = scan()
    update_active_symbols(symbols)
    return symbols


if __name__ == "__main__":
    scan()
