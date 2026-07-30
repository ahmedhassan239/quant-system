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
from config import (
    BINANCE_FUTURES_BASE_URL, TIMEFRAME, ALERT_PREFIX,
    MIN_24H_VOLUME_USDT, TOP_N, STABLECOIN_BLACKLIST, MOCK_TOKENS_BLACKLIST,
    VIP_SYMBOLS, DEFAULT_SYMBOLS
)
from data_fetcher import BLACKLISTED_SYMBOLS

# ──────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
BINANCE_TICKER_URL        = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/24hr"
BINANCE_LIVE_TICKER_URL   = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_EXCHANGE_INFO_URL = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/exchangeInfo"


# ──────────────────────────────────────────────────────────────────────
#  SCANNER LOGIC
# ──────────────────────────────────────────────────────────────────────

def fetch_top_symbols():
    """
    DYNAMIC MARKET-WIDE LIQUIDITY RADAR.

    Pipeline:
      1. Fetch 24h ticker data from Binance Futures API.
      2. Filter for active USDT-margined perpetual pairs ending with 'USDT'.
      3. Exclude stablecoin pairs (STABLECOIN_BLACKLIST), mock/restricted tokens (MOCK_TOKENS_BLACKLIST), and API error auto-blacklisted symbols (BLACKLISTED_SYMBOLS).
      4. Always include VIP symbols (e.g. PAXGUSDT for Gold exposure) bypassing MIN_24H_VOLUME_USDT (unless blacklisted).
      5. For non-VIP symbols, strictly filter out any symbol with 24h volume below MIN_24H_VOLUME_USDT ($150M USDT).
      6. Sort candidates by 24h quoteVolume in descending order.
      7. Return the Top N most liquid pairs dynamically, excluding any BLACKLISTED_SYMBOLS.
    """
    tickers = None
    # Attempt to fetch real market tickers from Binance Futures
    for url in [BINANCE_LIVE_TICKER_URL, BINANCE_TICKER_URL]:
        try:
            resp = requests.get(url, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    tickers = data
                    break
        except Exception as e:
            print(f"⚠️ Warning: Could not fetch tickers from {url}: {e}", flush=True)

    if not tickers:
        print("⚠️ Warning: Failed to fetch tickers from Binance API. Returning default fallback symbols.", flush=True)
        return [{'symbol': s, 'price_change_pct': 0.0, 'quote_volume': 0.0, 'last_price': 0.0} for s in DEFAULT_SYMBOLS[:TOP_N] if s not in BLACKLISTED_SYMBOLS]

    candidates = []
    vip_candidates = []

    # ── Fetch Valid Symbols from exchangeInfo ──
    valid_symbols = set()
    try:
        resp = requests.get(BINANCE_EXCHANGE_INFO_URL, timeout=12)
        if resp.status_code == 200:
            info = resp.json()
            for s in info.get('symbols', []):
                if s.get('contractType') == 'PERPETUAL' and s.get('status') == 'TRADING':
                    valid_symbols.add(s.get('symbol'))
        else:
            print(f"⚠️ Warning: exchangeInfo returned status {resp.status_code}", flush=True)
    except Exception as e:
        print(f"⚠️ Warning: Could not fetch exchangeInfo from {BINANCE_EXCHANGE_INFO_URL}: {e}", flush=True)

    for t in tickers:
        symbol = t.get('symbol', '')

        # 0. Check if symbol is actually a valid TRADING perpetual contract in this environment
        if valid_symbols and symbol not in valid_symbols:
            continue

        # 1. Must be a USDT perpetual pair
        if not symbol.endswith('USDT'):
            continue

        # 2. AUTO-BLACKLIST & STATIC BLACKLISTS: Exclude mock tokens, restricted contracts, & API error symbols
        if symbol in BLACKLISTED_SYMBOLS or symbol in MOCK_TOKENS_BLACKLIST:
            continue

        # 3. Exclude stablecoin / fiat-pegged pairs
        if symbol in STABLECOIN_BLACKLIST:
            continue

        quote_volume     = float(t.get('quoteVolume', 0))
        price_change_pct = float(t.get('priceChangePercent', 0))
        last_price       = float(t.get('lastPrice', 0))

        item = {
            'symbol':           symbol,
            'price_change_pct': price_change_pct,
            'quote_volume':     quote_volume,
            'last_price':       last_price,
        }

        # 4. VIP SYMBOLS (e.g. PAXGUSDT) ALWAYS BYPASS VOLUME FILTER
        if symbol in VIP_SYMBOLS:
            vip_candidates.append(item)
            continue

        # 5. STRICT VOLUME FILTER: Must meet or exceed MIN_24H_VOLUME_USDT ($150,000,000.0)
        if quote_volume < MIN_24H_VOLUME_USDT:
            continue

        candidates.append(item)

    # If strict $150M filter yields fewer symbols (e.g. on testnet or off-peak), fallback to top quote_volume symbols
    if len(candidates) < 5:
        print(f"ℹ️ Note: Strict ${MIN_24H_VOLUME_USDT/1e6:,.0f}M threshold yielded {len(candidates)} symbols. Relaxing threshold to include top market pairs...", flush=True)
        candidates = []
        for t in tickers:
            symbol = t.get('symbol', '')
            if valid_symbols and symbol not in valid_symbols:
                continue
            if not symbol.endswith('USDT') or symbol in MOCK_TOKENS_BLACKLIST or symbol in STABLECOIN_BLACKLIST or symbol in VIP_SYMBOLS or symbol in BLACKLISTED_SYMBOLS:
                continue
            candidates.append({
                'symbol':           symbol,
                'price_change_pct': float(t.get('priceChangePercent', 0)),
                'quote_volume':     float(t.get('quoteVolume', 0)),
                'last_price':       float(t.get('lastPrice', 0)),
            })

    # 6. Sort by 24h quoteVolume descending & combine VIP symbols first
    candidates.sort(key=lambda x: x['quote_volume'], reverse=True)
    combined = vip_candidates + candidates[:TOP_N]

    # Filter out any auto-blacklisted symbols from final combined list
    combined = [c for c in combined if c['symbol'] not in BLACKLISTED_SYMBOLS]

    return combined


def scan():
    """
    Run the dynamic liquidity radar and return the volume-ranked
    list of top USDT perpetual futures symbols to trade.
    """
    top     = fetch_top_symbols()
    symbols = [c['symbol'] for c in top]

    vol_threshold_m = MIN_24H_VOLUME_USDT / 1_000_000

    # ── Pretty-print the ranked results ───────────────────────────────
    print("=" * 70, flush=True)
    print(
        f"  🚀 {ALERT_PREFIX} DYNAMIC MARKET RADAR — "
        f"Top {len(symbols)} (Min 24h Vol: ${vol_threshold_m:,.0f}M USDT)",
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
