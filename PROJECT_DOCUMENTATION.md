# Quant System — Production Documentation

> **Version:** 2.1 (Stability & Self-Healing)  
> **Last Updated:** 2026-07-30  
> **Environment:** Binance Futures (Testnet / Live)

---

### Recent Stable Updates (V2.1)
- **Timeframe & Confluence:** Execution on 5m candles. Macro Trend (1h Z-Score & SMA-50) is used strictly as an Entry Filter.
- **Dynamic Radar & Universe Selection:** Scans 40+ top-volume symbols. Strictly validates against Binance `exchangeInfo` for `PERPETUAL` and `TRADING` contracts to eliminate `400 Bad Request` errors.
- **Risk Management & Exits (The Holy Grail):** Exits are purely managed by Dynamic ATR Trailing Stops (activate at 3.0x ATR, trail at 2.0x ATR) and Smart Partial Take Profits (+1.5% ROE with auto Break-Even SL). The legacy Macro Trend "Kill-Switch" (`TREND_INVALIDATION`) has been completely removed to prevent whipsaw losses.
- **Self-Healing Mechanics:** Gracefully handles Binance API exceptions like `[-2022] ReduceOnly Order is rejected` without crashing, preventing ghost positions and fake "Max slots reached" errors.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture & Tech Stack](#2-architecture--tech-stack)
3. [Container Topology (Docker)](#3-container-topology-docker)
4. [Market Regime Detection Engine](#4-market-regime-detection-engine)
5. [The Three Trading Strategies](#5-the-three-trading-strategies)
6. [Database Schema & Migrations](#6-database-schema--migrations)
7. [API & Frontend Integration](#7-api--frontend-integration)
8. [Operations & Deployment Guide](#8-operations--deployment-guide)
9. [Configuration Reference](#9-configuration-reference)
10. [Telegram Alert System](#10-telegram-alert-system)

---

## 1. Project Overview

The **Quant System** is an automated, multi-container Binance Futures trading bot built around a **Multi-Timeframe (MTF) Confluence** architecture. It operates two independent analysis engines that communicate via a shared PostgreSQL database:

- **Macro Engine (1h):** Determines market trend direction (`UPTREND` / `DOWNTREND`) using SMA-50 and Z-Score. Writes its verdict to a shared database.
- **Execution Engine (5m):** Reads the macro verdict, detects the current **Market Regime** (`TREND` / `RANGE` / `STORM`), and executes the appropriate trading strategy with full risk management.

### Core Design Principles

| Principle | Implementation |
|-----------|---------------|
| **Capital Preservation First** | `StormStrategy` enforces zero market orders during extreme volatility |
| **MTF Confluence** | No trade is placed without macro trend alignment |
| **Strategy Pattern** | Regime → Strategy routing via `StrategyRouter` (SOLID design) |
| **Idempotent Migrations** | `ALTER TABLE IF NOT EXISTS` guards prevent re-migration errors |
| **Fail-Safe Defaults** | Missing indicators default to `RANGE` (most conservative non-storm state) |

---

## 2. Architecture & Tech Stack

```
┌─────────────────────────────────────────────────────────────────────┐
│                         QUANT SYSTEM                                │
│                                                                     │
│  ┌──────────────────┐          ┌──────────────────────────────┐    │
│  │  Macro Engine    │  writes  │   quant_shared_db            │    │
│  │  (Python 1h)     │ ───────► │   (MacroState table)         │    │
│  └──────────────────┘          └──────────────┬───────────────┘    │
│                                               │ reads               │
│                                               ▼                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │            Execution Engine (Python 5m)                       │  │
│  │                                                               │  │
│  │  data_fetcher → RegimeDetector → StrategyRouter               │  │
│  │                    ↓TREND          ↓RANGE        ↓STORM       │  │
│  │              TrendStrategy   RangeStrategy  StormStrategy      │  │
│  │                                                               │  │
│  │  Risk Management (SL / TSL / Pyramiding) — always runs first  │  │
│  └───────────────────────┬───────────────────────────────────────┘  │
│                          │ writes                                    │
│                          ▼                                           │
│  ┌────────────────────────────────┐    ┌─────────────────────────┐  │
│  │   exec_15m_db (PostgreSQL)     │    │   Binance Futures API   │  │
│  │   positions / signals / logs   │◄──►│   (Testnet / Live)      │  │
│  └──────────────┬─────────────────┘    └─────────────────────────┘  │
│                 │ reads                                              │
│                 ▼                                                    │
│  ┌───────────────────────┐   REST   ┌──────────────────────────┐   │
│  │  Laravel API          │ ───────► │  Vue.js Dashboard        │   │
│  │  (DashboardController)│          │  (Regime Badges + PNL)   │   │
│  └───────────────────────┘          └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Tech Stack

| Layer | Technology | Role |
|-------|-----------|------|
| **Analysis Engine** | Python 3.11, pandas, numpy, python-binance | Regime detection, indicator computation, order execution |
| **Task Scheduling** | `schedule` library (in-process) | Runs scanner every 5 minutes |
| **API Gateway** | Laravel 10 (PHP 8.2) | Bridges DB ↔ Vue.js dashboard |
| **Frontend** | Vue.js 3 + Vite + Tailwind CSS | Real-time dashboard with 3-second polling |
| **Database** | PostgreSQL 15 | All persistent state (positions, signals, logs, macro state) |
| **Cache** | Redis 7 | Available for future rate-limiting / caching |
| **Containerization** | Docker + Docker Compose | 6-service orchestration on a bridge network |
| **Notifications** | Telegram Bot API | Trade alerts, PNL reports, mode changes |

---

## 3. Container Topology (Docker)

The system is defined in `docker-compose.yml` and runs **6 containers** on a shared bridge network `quant_network`.

| Container | Image | Ports | Role |
|-----------|-------|-------|------|
| `quant_postgres` | `postgres:15-alpine` | `127.0.0.1:5432` | Primary datastore |
| `quant_redis` | `redis:7-alpine` | internal | Cache layer |
| `quant_macro_engine` | Custom Python build | none | 1h trend analysis |
| `quant_exec_engine` | Custom Python build | none | 5m execution + regime routing |
| `quant_laravel_api` | Custom PHP build | `8080:8000` | REST API for dashboard |
| `quant_vue_dashboard` | Custom Node build | `5173:5173` | Live trading dashboard |

### Inter-Container Communication

The two Python engines communicate **exclusively through the database** — no direct HTTP calls:

```
quant_macro_engine  ──writes──►  quant_shared_db.macro_state
quant_exec_engine   ──reads────►  quant_shared_db.macro_state
quant_exec_engine   ──writes──►  exec_15m_db.positions / signals / logs
quant_laravel_api   ──reads────►  exec_15m_db.positions / trade_history
quant_laravel_api   ──reads────►  Binance REST API (live positions)
```

### Startup Sequence

The `postgres_db` container uses `pg_isready` health check before dependents start. It auto-creates required databases at first boot:

```yaml
# docker-compose.yml (abbreviated)
postgres_db:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
  command: >
    sh -c "... CREATE DATABASE exec_15m_db; CREATE DATABASE macro_1h_db; ..."
```

Both engine containers declare `depends_on: postgres_db: condition: service_healthy`.

---

## 4. Market Regime Detection Engine

**Module:** `backend-python/market_regime.py`

The Regime Detection Engine is a **middleware layer** between raw indicator computation and trading strategy execution. It runs on every analysis cycle, before any entry logic, and classifies the current market into one of three mutually exclusive states.

### 4.1 Core Indicators

#### ATR(14) — Average True Range
Measures raw price volatility using Wilder's EMA smoothing:

```
True Range (TR)  = max(High − Low,  |High − PrevClose|,  |Low − PrevClose|)
ATR(14)          = EMA(TR, alpha=1/14, min_periods=14)
ATR_MA(50)       = Rolling mean of ATR over prior 50 candles
ATR_Ratio        = ATR(14)_current / ATR_MA(50)
```

#### ADX(14) — Average Directional Index
Measures **trend strength** (not direction):

```
+DM  = max(High − PrevHigh, 0)  [only if > −DM, else 0]
−DM  = max(PrevLow − Low, 0)   [only if > +DM, else 0]

+DI(14) = 100 × EWM(+DM, α=1/14) / ATR(14)
−DI(14) = 100 × EWM(−DM, α=1/14) / ATR(14)

DX      = 100 × |+DI − −DI| / (+DI + −DI)
ADX(14) = EWM(DX, α=1/14)
```

- `ADX < 25` → Weak / no trend (choppy / ranging market)
- `ADX ≥ 25` → Established, sustained trend

#### Z-Score(50) — Statistical Price Deviation

```
SMA(50)   = Rolling mean of close prices over 50 periods
σ(50)     = Rolling standard deviation over 50 periods
Z-Score   = (Close − SMA(50)) / σ(50)
```

- `|Z| > 3.0` → Extreme outlier → crisis / flash crash event (STORM)
- `|Z| ≤ 1.0` → Price within 1σ → typical range (RANGE)
- `|Z| ≥ 1.2` → Price stretched → suggests directional move (TREND)

---

### 4.2 Regime Classification Rules

Rules are evaluated in **strict priority order — highest risk wins:**

```
STEP 1: STORM CHECK (Capital Preservation — evaluated first, always)
  IF  ATR_Ratio > 3.0            ← Volatility spike >3x its baseline
  OR  |Z-Score| > 3.0            ← Statistical crisis / black swan
  THEN → STORM 🌪️
  ─────────────────────────────────────────────── (else, continue)

STEP 2: RANGE CHECK (Chop Harvester)
  IF  ADX < 25                   ← Weak or absent directional trend
  AND |Z-Score| ≤ 1.0            ← Price close to statistical mean
  THEN → RANGE ⚖️
  ─────────────────────────────────────────────── (else, continue)

STEP 3: TREND CHECK (Momentum Rider)
  IF  ADX ≥ 25                   ← Strong, established directional trend
  AND |Z-Score| ≥ 1.2            ← Price stretched from mean
  THEN → TREND 🚀
  ─────────────────────────────────────────────── (else, fallback)

STEP 4: FALLBACK → RANGE (conservative default)
  Ambiguous conditions (ADX ≈ 25, Z between 1.0–1.2, or insufficient data)
  default to RANGE to avoid entering in uncertain market conditions.
```

### 4.3 Regime Thresholds Reference

| Constant | Value | Description |
|----------|-------|-------------|
| `STORM_ATR_MULT` | `3.0` | ATR spike multiplier for STORM trigger |
| `STORM_ZSCORE` | `3.0` | Absolute Z-Score threshold for STORM |
| `RANGE_ADX_MAX` | `25.0` | Maximum ADX for RANGE classification |
| `RANGE_ZSCORE_MAX` | `1.0` | Maximum |Z-Score| for RANGE |
| `TREND_ADX_MIN` | `25.0` | Minimum ADX for TREND |
| `TREND_ZSCORE_MIN` | `1.2` | Minimum |Z-Score| for TREND |
| `ATR_MA_PERIOD` | `50` | Window for ATR moving average baseline |
| `ADX_PERIOD` | `14` | ADX EMA smoothing window |

> **Minimum Data Requirement:** The system needs at least `ADX_PERIOD × 2 + ATR_MA_PERIOD = 78 candles`. If fewer rows are available, `RANGE` is returned as the default.

---

### 4.4 StrategyRouter — Full Execution Flowchart

```
scanner_job() calls run_analyzer(symbol, futures_client)
    │
    ├─ [1] Fetch latest candles from market_data table
    │
    ├─ [2] Compute indicators:
    │      • RSI(14) via EMA
    │      • Z-Score(50) = (Close − SMA50) / σ50
    │      • ATR(14) via Wilder EMA
    │
    ├─ [3] RISK MANAGEMENT (unconditional — BEFORE regime):
    │      • Macro trend invalidation → immediate close
    │      • Hard Stop-Loss breach → immediate close
    │      • ATR Trailing Stop-Loss → close + log
    │      • Break-Even lock (+1.0% PnL) → SL moved to entry
    │      • Partial TP 50% scale-out (+1.5% ROE)
    │      • Stagnant trade closer (>2h, <0.5% PnL)
    │
    ├─ [4] Pyramiding check → scale into winning positions
    │
    ├─ [5] Whale Hunter (Strategy C bypass):
    │      IF volume_spike > 10x 50-period avg AND macro-aligned
    │      THEN set decision=LONG/SHORT, strategy='WHALE_STRIKE'
    │           active_mode = MarketRegime.TREND  (momentum = TREND)
    │
    ├─ [6] StrategyRouter.route(symbol, df, indicators...)
    │          │
    │          ├─ RegimeDetector.detect(df)
    │          │     └─ Returns (MarketRegime, meta_dict)
    │          │
    │          ├─ STORM? → StormStrategy.execute()
    │          │              ├─ Returns decision='WAIT' (no market orders)
    │          │              └─ Places LIMIT order directly (4% deep, TTL=900s)
    │          │
    │          ├─ RANGE? → RangeStrategy.execute()
    │          │              └─ RSI extreme + OB touch → LONG/SHORT
    │          │
    │          └─ TREND? → TrendStrategy.execute()
    │                         └─ Pullback/Breakout via Z-Score + OB/Volume
    │
    ├─ [7] If decision in (LONG, SHORT):
    │      • Place market order via Binance Futures API
    │      • Write PortfolioState(active_mode=regime.value) to DB
    │      • Send Telegram alert with 🤖 Active Mode: [X emoji]
    │
    ├─ [8] Save TradingSignal row to DB
    │
    └─ [9] Sync live Binance position → DB (Binance is source of truth)
```

---

## 5. The Three Trading Strategies

Each strategy implements the abstract `TradingStrategy` base class. They are **stateless between calls** — all context is passed as parameters, making them safe for multi-symbol analysis.

---

### 5.1 TrendStrategy — Momentum Rider 🚀

**Regime:** `ADX ≥ 25` AND `|Z-Score| ≥ 1.2`

Encapsulates the original MTF Confluence logic. Highest-conviction mode that rides established directional moves.

#### Long Entry Conditions (requires macro `UPTREND` + `RSI > 55`)

| Sub-Strategy | Trigger | Stop-Loss Placement |
|-------------|---------|---------------------|
| **A: Pullback** | Price ≥ Bullish OB Low AND Z-Score < −1.2 | Bullish OB Low × 0.999 |
| **B: Breakout** | Bullish consolidation broken with volume AND Z-Score > +1.2 | Breakout candle Low × 0.999 |

#### Short Entry Conditions (requires macro `DOWNTREND` + `RSI < 45`)

| Sub-Strategy | Trigger | Stop-Loss Placement |
|-------------|---------|---------------------|
| **A: Pullback** | Price ≤ Bearish OB High AND Z-Score > +1.2 | Bearish OB High × 1.001 |
| **B: Breakout** | Bearish consolidation broken with volume AND Z-Score < −1.2 | Breakout candle High × 1.001 |

#### Conviction Tier System (Position Sizing)

| Tier | |Z-Score| | Volume Ratio | Allocation |
|------|---------|-------------|------------|
| **Tier 1** | ≥ 2.0 | ≥ 4× avg | 20% of `SLOT_BUDGET` |
| **Tier 2** | ≥ 1.0 | ≥ 2× avg | 10% of `SLOT_BUDGET` |
| **Tier 3** | < 1.0 | < 2× avg | 5% of `SLOT_BUDGET` |

#### Post-Entry Risk Management

| Mechanism | Trigger | Action |
|-----------|---------|--------|
| **Break-Even Lock** | Unrealized PnL ≥ +1.0% | Move SL → entry price |
| **Partial TP (50% scale-out)** | Unrealized ROE ≥ +1.5% (`PARTIAL_TP_PCT`) | Close 50% at market; lock SL at entry |
| **ATR Trailing Stop** | Profit distance ≥ `2.0 × ATR(14)` | Trail SL at `Peak − 1.5 × ATR` (LONG) |
| **Pyramiding Tier 1** | Unrealized PnL ≥ +2.0% | Add 50% of initial size; move SL to break-even |
| **Pyramiding Tier 2** | Unrealized PnL ≥ +4.0% | Add 25% of initial size; move SL to new avg entry |
| **Stagnant Closer** | Open > 2h AND PnL in (−0.5%, +0.5%) | Market close to free the slot |
| **Trend Invalidation** | Macro engine flips direction against position | Immediate market close |

---

### 5.2 RangeStrategy — Chop Harvester ⚖️

**Regime:** `ADX < 25` AND `|Z-Score| ≤ 1.0`

No directional bias exists. Price oscillates within a statistical channel. Harvests small mean-reversion moves using RSI extremes confirmed by Order Block touches.

#### Entry Conditions

| Direction | RSI Condition | Order Block Condition | Stop-Loss |
|-----------|--------------|----------------------|-----------|
| **LONG** | `RSI < 35` (oversold) | Price within Bullish OB Low–High range | Entry × (1 − 0.5%) |
| **SHORT** | `RSI > 65` (overbought) | Price within Bearish OB Low–High range | Entry × (1 + 0.5%) |

> **Important:** RangeStrategy does **not** require macro trend alignment. In a range, both LONG and SHORT entries are valid.

#### Risk Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| **Stop-Loss** | ±0.5% from entry | Tight — price should not deviate far in a range |
| **Target Profit** | +1.0–1.5% | Via existing `PARTIAL_TP_PCT` mechanism |
| **Trailing Stop** | Disabled | Range oscillation would prematurely trigger TSL |

---

### 5.3 StormStrategy — Capital Preservation Mode 🌪️

**Regime:** `ATR_Ratio > 3.0` OR `|Z-Score| > 3.0`

The market is experiencing extreme volatility — flash crashes, news events, liquidation cascades. The sole objective is **not losing capital** while opportunistically capturing extreme dislocations.

#### The Zero Market Order Constraint

`StormStrategy.execute()` **always returns `'WAIT'`** as its decision, preventing the market-order execution path from firing. Limit orders are placed directly via `_place_storm_limit()` — completely bypassing the `if decision == 'LONG':` block in `run_analyzer()`.

#### Limit Order Mechanics

```
_place_storm_limit(symbol, direction, current_price, futures_client)
    │
    ├─ limit_price = current × (1 − 0.04)    [LONG: buy 4% below]
    │               current × (1 + 0.04)    [SHORT: sell 4% above]
    │
    ├─ sl_price    = limit × (1 − 0.002)     [LONG: SL 0.2% below fill]
    │               limit × (1 + 0.002)     [SHORT: SL 0.2% above fill]
    │
    ├─ quantity    = 5% of usdt_balance / limit_price
    │
    ├─ futures_client.futures_create_order(
    │       type='LIMIT', timeInForce='GTC',
    │       price=limit_price, quantity=quantity
    │   )
    │
    └─ threading.Timer(900, _cancel_if_unfilled, args=(order_id,))
           timer.daemon = True   ← dies with main process, no leak
           timer.start()
```

#### Limit Order Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| **Direction** | Follows macro trend | UPTREND → LONG limit; DOWNTREND → SHORT limit |
| **Limit Price Offset** | 4% from current price | Deep enough to capture panic lows / euphoric highs |
| **Position Size** | 5% of available USDT | Conservative allocation during storm |
| **Hard Stop-Loss** | ±0.2% from limit fill | Ultra-tight — storm can reverse violently |
| **Order TTL** | 900 seconds (15 min) | Auto-cancelled if unfilled |

#### TTL Auto-Cancellation Thread

```python
# One-shot daemon timer — no polling, no memory leak
timer = threading.Timer(900, _cancel_if_unfilled, args=(symbol, order_id, futures_client))
timer.daemon = True   # Automatically killed if main process exits
timer.start()

# _cancel_if_unfilled():
#   1. futures_client.futures_get_open_orders(symbol) → check if pending
#   2. If still open → futures_client.futures_cancel_order(orderId=order_id)
#   3. If filled/closed → log and return
```

#### Silenced Binance Error Codes

| Code | Meaning | Action |
|------|---------|--------|
| `-4411` | Restricted contract (TradFi-mapped) | Silent skip, log DEBUG |
| `-2027` | Order would immediately trigger | Silent skip, log DEBUG |
| `-2011` | Unknown order (filled / cancelled between calls) | Debug log only |

---

## 6. Database Schema & Migrations

### 6.1 Database Layout

| Database | Used By | Contains |
|----------|---------|---------|
| `exec_15m_db` | Execution Engine + Laravel | `positions`, `trading_signals`, `bot_logs`, `trade_history`, `market_data` |
| `quant_shared_db` | Both engines + Laravel | `macro_state`, `active_symbols`, `wallet_balance` |

### 6.2 `positions` Table — Full Schema (PortfolioState)

```sql
CREATE TABLE positions (
    id                        SERIAL PRIMARY KEY,
    timestamp                 TIMESTAMP NOT NULL,
    symbol                    VARCHAR NOT NULL,
    decision                  VARCHAR NOT NULL,    -- LONG | SHORT | HOLD | CLOSE_LONG | CLOSE_SHORT
    entry_reason              VARCHAR,             -- Human-readable: "Tier 2 [Z:1.4, OB:2.3x] - Pullback | Macro: UPTREND"
    current_price             FLOAT NOT NULL,
    usdt_balance              FLOAT NOT NULL,
    asset_balance             FLOAT NOT NULL,      -- 0.0 when no active position
    position_direction        VARCHAR,             -- 'LONG' | 'SHORT' | NULL
    average_entry_price       FLOAT,
    dca_level                 INTEGER DEFAULT 0,   -- Number of scale-ins executed (0 = initial entry)
    last_exec_price           FLOAT,
    total_cost                FLOAT DEFAULT 0.0,   -- Total USDT spent (used for avg entry calc)
    highest_price_since_entry FLOAT,               -- Peak tracker for LONG trailing stop
    lowest_price_since_entry  FLOAT,               -- Trough tracker for SHORT trailing stop
    stop_loss_price           FLOAT,
    stop_loss                 FLOAT,               -- Duplicate for backward compat
    strategy                  VARCHAR,             -- E.g. "Tier 2 [Z:1.4, OB:2.3x] - Pullback"
    trailing_active           BOOLEAN DEFAULT FALSE,
    partial_tp_hit            BOOLEAN DEFAULT FALSE,
    pnl_pct                   FLOAT,
    pnl_usd                   FLOAT,
    total_portfolio_value     FLOAT NOT NULL,
    active_mode               VARCHAR(10)          -- 'TREND' | 'RANGE' | 'STORM' | NULL
);
```

### 6.3 Other Key Tables

#### `trading_signals` — Per-Cycle Signal Snapshots
```sql
CREATE TABLE trading_signals (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR NOT NULL,
    timeframe       VARCHAR NOT NULL,
    timestamp       TIMESTAMP NOT NULL,
    current_price   FLOAT NOT NULL,
    rsi             FLOAT,
    z_score         FLOAT,
    macro_trend     VARCHAR,           -- 'UPTREND' | 'DOWNTREND'
    bullish_ob_low  FLOAT, bullish_ob_high FLOAT,
    bearish_ob_low  FLOAT, bearish_ob_high FLOAT,
    decision        VARCHAR NOT NULL,  -- 'LONG' | 'SHORT' | 'WAIT'
    strategy_type   VARCHAR            -- 'PULLBACK' | 'BREAKOUT'
);
```

#### `trade_history` — Closed Trade Archive
```sql
CREATE TABLE trade_history (
    id           SERIAL PRIMARY KEY,
    symbol       VARCHAR NOT NULL,
    direction    VARCHAR NOT NULL,  -- 'LONG' | 'SHORT'
    entry_price  FLOAT NOT NULL,
    exit_price   FLOAT NOT NULL,
    quantity     FLOAT NOT NULL,
    pnl_usd      FLOAT NOT NULL,
    pnl_pct      FLOAT NOT NULL,
    outcome      VARCHAR NOT NULL,  -- 'WIN' | 'LOSS'
    exit_reason  VARCHAR NOT NULL,  -- 'STOP_LOSS' | 'TRAILING_STOP' | 'SIGNAL' | 'PARTIAL_TAKE_PROFIT' | 'STAGNANT'
    closed_at    TIMESTAMP DEFAULT NOW()
);
```

#### `macro_state` — Cross-Engine MTF Communication (in `quant_shared_db`)
```sql
CREATE TABLE macro_state (
    id            SERIAL PRIMARY KEY,
    symbol        VARCHAR UNIQUE NOT NULL,
    macro_trend   VARCHAR NOT NULL,    -- 'UPTREND' | 'DOWNTREND'
    sma_50        FLOAT NOT NULL,
    z_score       FLOAT NOT NULL,
    std_dev       FLOAT,
    sdc_upper     FLOAT,               -- SMA + 2σ (upper deviation channel)
    sdc_lower     FLOAT,               -- SMA − 2σ (lower deviation channel)
    current_price FLOAT NOT NULL,
    updated_at    TIMESTAMP NOT NULL
);
```

---

### 6.4 The `active_mode` Column

**Migration date:** 2026-07-29

Stores the Market Regime at the moment a position row was written. This enables:

1. **Laravel API** → includes `active_mode` in the position payload
2. **Vue.js Dashboard** → renders the correct regime badge per position
3. **Post-hoc analysis** → correlate regime with trade outcomes in `trade_history`

**Possible values:** `'TREND'` | `'RANGE'` | `'STORM'` | `NULL` (pre-migration rows)

**Write timing:**
- Every new LONG/SHORT entry → `active_mode = current_regime.value`
- Every WAIT-cycle DB sync (heartbeat row) → `active_mode = current_regime.value`
- CLOSE rows → `active_mode` is not set (inherits NULL)

---

### 6.5 Idempotent Migration Pattern in `init_db()`

```python
# database.py — called at container startup
def init_db():
    """Create tables if they don't exist, then run idempotent migrations."""
    Base.metadata.create_all(bind=engine)

    _safe_migrations = [
        "ALTER TABLE positions ADD COLUMN IF NOT EXISTS partial_tp_hit BOOLEAN DEFAULT FALSE;",
        # Market Regime Detection migration (2026-07-29)
        "ALTER TABLE positions ADD COLUMN IF NOT EXISTS active_mode VARCHAR(10) DEFAULT NULL;",
    ]
    try:
        with engine.connect() as conn:
            for migration_sql in _safe_migrations:
                try:
                    conn.execute(text(migration_sql))
                except Exception:
                    pass   # Column already exists — safe no-op
            conn.commit()
    except Exception:
        pass
```

**Why this is safe:**
- `ADD COLUMN IF NOT EXISTS` is PostgreSQL-native and produces a no-op if the column exists
- The inner `try/except` catches per-statement errors without aborting the migration loop
- Never uses `DROP COLUMN` — migrations are always additive

---

## 7. API & Frontend Integration

### 7.1 Laravel API Endpoint

**Endpoint:** `GET /api/dashboard-metrics`  
**Controller:** `App\Http\Controllers\DashboardController::getDashboardMetrics()`

#### Full Response Payload

```json
{
  "wallet_balance": "1250.00",
  "active_unrealized_pnl": "+12.50",
  "realized_pnl": "87.32",
  "total_margin_balance": "1262.50",
  "win_rate": 62.5,
  "active_positions": [
    {
      "id": 1042,
      "symbol": "BTCUSDT",
      "direction": "LONG",
      "entry_price": "67450.00",
      "current_price": "68120.00",
      "unrealized_pnl": "+42.18",
      "allocated_usdt": "1349.00",
      "entry_reason": "Tier 2 [Z:1.4, OB:2.3x] - Pullback | Macro: UPTREND | OB: $67200–$67480",
      "stop_loss": "67246.7500",
      "stop_loss_price": "67246.7500",
      "strategy": "Tier 2 [Z:1.4, OB:2.3x] - Pullback",
      "active_mode": "TREND"
    }
  ]
}
```

#### Data Source Hierarchy for Positions

```
1. PRIMARY source  → Binance positionRisk API
   Fields: positionAmt, markPrice, unRealizedProfit, entryPrice
   Filter:  abs(positionAmt) × markPrice >= $2.00  (dust filter)

2. SECONDARY source → positions table (joined by symbol, latest row)
   Fields: stop_loss, stop_loss_price, strategy, entry_reason, active_mode

3. NULL-SAFE fallback → active_mode ?? null  (pre-migration rows return null)
```

#### Key Controller Logic (PHP)

```php
// DashboardController.php
$localRecord = DB::table('positions')
    ->where('symbol', $binancePos['symbol'])
    ->whereNotNull('stop_loss')       // prefer rows with SL data
    ->orderBy('id', 'desc')
    ->first();

return [
    ...
    // Market Regime Detection: 'TREND' | 'RANGE' | 'STORM' | null
    'active_mode' => $localRecord->active_mode ?? null,
];
```

---

### 7.2 Vue.js Dashboard — Regime Badge Rendering

**Component:** `frontend-vue/src/components/Dashboard.vue`  
**Polling:** `fetchMetrics()` runs every **3 seconds** via `setInterval`

#### `getModeBadge()` Helper Function

```javascript
// Dashboard.vue <script setup>
const getModeBadge = (mode) => {
  const m = (mode || 'TREND').toUpperCase()  // null-safe: defaults to TREND
  const CONFIG = {
    TREND: {
      label: 'TREND', emoji: '🚀',
      classes: 'text-emerald-400 bg-emerald-500/20 border-emerald-500/30',
    },
    RANGE: {
      label: 'RANGE', emoji: '⚖️',
      classes: 'text-amber-400 bg-amber-500/20 border-amber-500/30',
    },
    STORM: {
      label: 'STORM', emoji: '🌪️',
      classes: 'text-red-400 bg-red-500/20 border-red-500/30',
      // Pulse animation applied inline via :style
    },
  }
  return CONFIG[m] || CONFIG.TREND
}
```

#### Badge Visual Reference

| Regime | Badge | Text Color | Background | Animation |
|--------|-------|-----------|------------|-----------|
| `TREND` | `🚀 TREND` | `text-emerald-400` | `bg-emerald-500/20` | None |
| `RANGE` | `⚖️ RANGE` | `text-amber-400` | `bg-amber-500/20` | None |
| `STORM` | `🌪️ STORM` | `text-red-400` | `bg-red-500/20` | CSS `pulse` (1.5s infinite) |

The STORM pulse uses inline CSS animation via `:style` — no Tailwind animation plugin required:

```html
:style="pos.active_mode === 'STORM'
  ? 'animation: pulse 1.5s cubic-bezier(0.4,0,0.6,1) infinite'
  : ''"
```

The badge appears in both:
- **Desktop table:** New `Mode` column between `Symbol` and `Direction`
- **Mobile card:** Stacked above the `Direction` badge in the top-right corner

---

## 8. Operations & Deployment Guide

### 8.1 Prerequisites

```bash
docker --version        # >= 24.0 required
docker compose version  # >= 2.20 required
```

### 8.2 Environment File (`.env`)

```env
# PostgreSQL
POSTGRES_USER=quant_user
POSTGRES_PASSWORD=your_secure_password_here
POSTGRES_DB=exec_15m_db

# Binance Futures Testnet API (two separate key pairs — macro + exec)
TESTNET_15M_API_KEY=your_testnet_macro_api_key
TESTNET_15M_SECRET_KEY=your_testnet_macro_secret
TESTNET_5M_API_KEY=your_testnet_exec_api_key
TESTNET_5M_SECRET_KEY=your_testnet_exec_secret

# Telegram
TELEGRAM_BOT_TOKEN=bot123456789:ABCDEFxxxxxxxxxx
TELEGRAM_CHAT_ID=-1001234567890
```

### 8.3 First-Time Setup

```bash
# 1. Clone repository
git clone <repo-url> && cd quant-system

# 2. Configure environment
cp .env.example .env
# Edit .env with real credentials

# 3. Build and start all 6 containers
docker compose up -d --build

# 4. Verify health
docker compose ps
# All services should show STATUS: running (postgres should show: healthy)
```

### 8.4 Common Operations

| Operation | Command |
|-----------|---------|
| Start all containers | `docker compose up -d` |
| Stop all (preserve data) | `docker compose down` |
| Rebuild all after code change | `docker compose up -d --build` |
| Rebuild one service | `docker compose up -d --build quant_exec_engine` |
| Restart one service (no rebuild) | `docker compose restart quant_exec_engine` |
| Force recreate container | `docker compose up -d --force-recreate quant_exec_engine` |
| Check container status | `docker compose ps` |

### 8.5 Log Monitoring

```bash
# Stream all container logs
docker compose logs -f

# Stream specific container
docker compose logs -f quant_exec_engine

# Search for regime events
docker compose logs quant_exec_engine | grep "\[MODE:"
# Output example:
# [MODE: RANGE] BTCUSDT | ADX: 18.4 | RSI: 52.1 | Z: +0.312 | Strategy: WAIT

# Search for trade executions
docker compose logs quant_exec_engine | grep -E "LONG|SHORT|STORM PENDING"

# Search for errors
docker compose logs quant_exec_engine 2>&1 | grep -i "error\|exception\|traceback"

# Last 200 lines from exec engine
docker compose logs --tail=200 quant_exec_engine
```

### 8.6 Database Operations

```bash
# Connect directly to PostgreSQL
docker exec -it quant_postgres psql -U quant_user -d exec_15m_db
```

```sql
-- Active positions with regime
SELECT symbol, position_direction, active_mode, average_entry_price,
       stop_loss_price, trailing_active, dca_level, timestamp
FROM positions
WHERE asset_balance > 0
ORDER BY timestamp DESC;

-- Win rate by regime
SELECT
    th.exit_reason,
    p.active_mode,
    COUNT(*) FILTER (WHERE th.outcome = 'WIN') AS wins,
    COUNT(*) AS total,
    ROUND(100.0 * COUNT(*) FILTER (WHERE th.outcome = 'WIN') / COUNT(*), 1) AS win_rate_pct
FROM trade_history th
JOIN (
    SELECT DISTINCT ON (symbol) symbol, active_mode
    FROM positions ORDER BY symbol, id DESC
) p USING (symbol)
GROUP BY th.exit_reason, p.active_mode
ORDER BY p.active_mode, th.exit_reason;

-- Verify active_mode migration
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'positions' AND column_name = 'active_mode';
```

```bash
# Backup database
docker exec quant_postgres pg_dump -U quant_user exec_15m_db \
  > quant_db_backup_$(date +%Y%m%d_%H%M).sql

# Restore from backup
docker exec -i quant_postgres psql -U quant_user -d exec_15m_db \
  < quant_db_backup_20260729_1200.sql
```

### 8.7 Verification Checklist

After deployment, verify each layer is working:

```bash
# 1. Macro trend is being computed and written
docker exec -it quant_postgres psql -U quant_user -d quant_shared_db \
  -c "SELECT symbol, macro_trend, z_score, updated_at FROM macro_state ORDER BY updated_at DESC LIMIT 5;"

# 2. Regime detection is firing in exec engine
docker compose logs quant_exec_engine --tail=50 | grep "MODE:"

# 3. API is returning active_mode
curl -s http://localhost:8080/api/dashboard-metrics | python3 -m json.tool | grep active_mode

# 4. Dashboard loads and shows badges
# Open http://localhost:5173 in browser
# Active positions should show 🚀/⚖️/🌪️ badges

# 5. Telegram alerts are arriving
# Engine startup alert should have been sent on container boot
# Trade alerts include "🤖 Active Mode: [TREND 🚀]" line
```

### 8.8 Hot Reload Behavior

| Layer | Reload Method | Restart Needed? |
|-------|--------------|----------------|
| Python engine (`.py` files) | Volume-mounted; changes visible immediately | Yes (restart container to apply to running process) |
| Laravel API (`routes/`, `Controllers/`) | Volume-mounted; PHP reads fresh on each request | No |
| Vue.js dashboard (`.vue` files) | Vite HMR — browser updates within 1 second | No |
| `docker-compose.yml` changes | Requires `docker compose up -d` re-apply | Yes |
| New Python package in `requirements.txt` | Requires `--build` | Yes |

---

## 9. Configuration Reference

### Key `config.py` Parameters

| Variable | Default | Env Override | Description |
|----------|---------|-------------|-------------|
| `ATR_PERIOD` | `14` | `ATR_PERIOD` | ATR smoothing window (periods) |
| `TSL_ATR_ACTIVATION_MULT` | `2.0` | `TSL_ATR_ACTIVATION_MULT` | TSL activates when profit ≥ 2× ATR |
| `TSL_ATR_TRAIL_MULT` | `1.5` | `TSL_ATR_TRAIL_MULT` | Trail SL at 1.5× ATR from peak |
| `PARTIAL_TP_PCT` | `0.015` | `PARTIAL_TP_PCT` | 50% scale-out at +1.5% ROE |
| `HARD_STOP_LOSS_PCT` | `0.05` | — | Absolute 5% hard stop |
| `ZSCORE_LONG_THRESHOLD` | `-1.2` | — | Z-Score gate for Pullback LONG |
| `ZSCORE_SHORT_THRESHOLD` | `1.2` | — | Z-Score gate for Pullback SHORT |
| `ZSCORE_SMA_PERIOD` | `50` | — | Z-Score rolling window |
| `MAX_CONCURRENT_POSITIONS` | `10` | `MAX_GLOBAL_POSITIONS` | Max open positions |
| `SLOT_BUDGET` | `100.0` | — | Max USDT per slot |
| `TOTAL_CAPITAL` | `1000.0` | — | Total virtual capital |
| `FUTURES_LEVERAGE` | `1` | `FUTURES_LEVERAGE` | Binance Futures leverage |
| `FUTURES_MARGIN_TYPE` | `ISOLATED` | `FUTURES_MARGIN_TYPE` | Margin isolation |
| `WHALE_VOLUME_MULTIPLIER` | `10.0` | `WHALE_VOLUME_MULTIPLIER` | Volume spike threshold |
| `SCHEDULE_INTERVAL_MINUTES` | `5` | `TIMEFRAME` | Derived from timeframe (`5m` → 5) |
| `TESTNET_FORCE_TRADES` | `False` | — | ⚠️ Force trades — NEVER true in production |

### Regime Detection Thresholds (`market_regime.py`, hardcoded)

| Constant | Value | Modify By |
|----------|-------|-----------|
| `RegimeDetector.STORM_ATR_MULT` | `3.0` | Edit `market_regime.py` |
| `RegimeDetector.STORM_ZSCORE` | `3.0` | Edit `market_regime.py` |
| `RegimeDetector.RANGE_ADX_MAX` | `25.0` | Edit `market_regime.py` |
| `RegimeDetector.RANGE_ZSCORE_MAX` | `1.0` | Edit `market_regime.py` |
| `RegimeDetector.TREND_ADX_MIN` | `25.0` | Edit `market_regime.py` |
| `RegimeDetector.TREND_ZSCORE_MIN` | `1.2` | Edit `market_regime.py` |
| `StormStrategy.LIMIT_OFFSET_PCT` | `0.04` | Edit `market_regime.py` |
| `StormStrategy.HARD_SL_PCT` | `0.002` | Edit `market_regime.py` |
| `StormStrategy.ORDER_TTL_SECONDS` | `900` | Edit `market_regime.py` |
| `RangeStrategy.RSI_LONG_THRESHOLD` | `35.0` | Edit `market_regime.py` |
| `RangeStrategy.RSI_SHORT_THRESHOLD` | `65.0` | Edit `market_regime.py` |
| `RangeStrategy.STOP_LOSS_PCT` | `0.005` | Edit `market_regime.py` |

---

## 10. Telegram Alert System

All alerts are sent via `send_telegram_alert()` in `analyzer.py`. The bot fails silently (no crash) if credentials are missing.

### Daily Schedule

| Time | Alert |
|------|-------|
| **Container boot** | Engine startup alert |
| **Every trade** | Entry / exit / scale-up alert |
| **08:00 daily** | Periodic PNL report |
| **20:00 daily** | Periodic PNL report |

### Alert Types Reference

| Alert | Trigger | Key Fields |
|-------|---------|-----------|
| **Engine Started** | Container boot | Role, timeframe, leverage, capital, schedule |
| **Macro Trend Change** | UPTREND↔DOWNTREND flip | Symbol, price, SMA-50, Z-Score |
| **OPEN / SCALE-UP LONG** | New LONG position or pyramid | 🤖 Mode badge, strategy, OB zone, SL, portfolio |
| **OPEN / SCALE-UP SHORT** | New SHORT position or pyramid | 🤖 Mode badge, strategy, OB zone, SL, portfolio |
| **CLOSE LONG / SHORT** | Exit by any reason | Exit reason, PnL %, PnL USD |
| **WHALE STRIKE** | Volume spike > 10× avg | Direction, vol ratio, macro alignment |
| **STORM Limit Placed** | StormStrategy fires | Symbol, direction, limit price, SL, TTL=15min |
| **Periodic PNL Report** | 08:00 & 20:00 | Realized PNL, win rate, active count, wallet |

### Active Mode Line Format in Trade Alerts

```
🚨 QUANT ALERT: OPEN LONG 🚨

🤖 Active Mode: [TREND 🚀]
Symbol: BTCUSDT
Price: $67,450.00
Time: 2026-07-29 02:30 PM

💡 MTF Confluence:
- Strategy: A (Pullback)
- Macro Trend: 📈 UPTREND
- 15m Z-Score: -1.42 (threshold: -1.2)
- Bullish OB: $67,200.00 - $67,480.00
- OB Volume: 2.3x avg

🛡️ Risk Management:
- Entry Price: $67,450.00
- Stop-Loss: $67,246.75 (Dynamic)
- Trailing Stop: 2.0x ATR act / 1.5x ATR trail

💼 Virtual Portfolio:
- Slot Budget: $100
- USDT Balance: $895.20
- Asset Balance: 0.001482 BTC
- Total Value: $997.34
- DCA Iteration: 1/3
```

### Alert Prefix Format

```python
# config.py
# Testnet: "🧪 [TESTNET - 5m EXEC]"
# Live:    "🚀 [LIVE - 5m EXEC]"
ALERT_PREFIX = f"🚀 [LIVE - {TIMEFRAME} {_role_label}]"
```

---

*Documentation for Quant System v2.0 — Market Regime Detection. For deeper detail on any module, consult the inline docstrings in `market_regime.py`, `analyzer.py`, and `database.py`. All public functions are documented at source level.*
