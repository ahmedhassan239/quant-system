# Algorithmic Crypto Trading System 📈🤖

![Status](https://img.shields.io/badge/Status-Stable-brightgreen)
![Environment](https://img.shields.io/badge/Environment-Mainnet%20%7C%20Testnet-blue)
![Execution](https://img.shields.io/badge/Execution-Binance%20Futures-F3BA2F)

## Overview
A fully automated, stateful, enterprise-grade quantitative trading engine designed for cryptocurrency perpetual futures. After extensive iterations, the bot has transitioned from a highly sensitive, whipsaw-prone state to a robust, self-healing, sniper-like execution engine. 

It actively scans the entire Binance Futures market dynamically, executing trades using Isolated Margin at 1x Leverage (configurable) based strictly on confluence between macro trends, market regimes, and highly optimized technical parameters.

## Core Architecture & Logic
The engine is built on a highly modular architecture that segregates market data ingestion, dynamic universe selection, signal generation, and isolated risk execution.

### Timeframe & Confluence
- **Execution Timeframe:** 5m candles. All exact trade entries and exits trigger off the 5m timeframe for precision.
- **Macro Trend Timeframe:** 1h candles. The 1h macro trend (governed by Z-Score and SMA-50) is used **strictly as an Entry Filter**. It dictates the overarching directional bias (LONG or SHORT) but never forces an early exit.

### Dynamic Radar & Universe Selection
- **Deep Market Scanning:** The dynamic scanner autonomously evaluates 40+ top-volume symbols.
- **Strict Validation:** The scanner validates symbols directly against Binance's `exchangeInfo` endpoint. It strictly filters for actively trading `PERPETUAL` contracts, instantly eliminating `400 Bad Request` errors caused by unsupported assets (e.g., Stock tokens, delisted coins) and keeping our auto-blacklist clean.

## Market Regimes
The bot utilizes adaptive, regime-aware logic to define when to enter trades.

- **TREND:** 
  - Activated when ADX >= 25 and strict Z-Score >= 1.5. 
  - Employs aggressive Breakout logic to catch heavy momentum waves.
- **RANGE:** 
  - Activated when the market lacks clear directional momentum.
  - Relies on Mean Reversion tactics (RSI overbought/oversold levels combined with Order Block touches).
- **STORM:** 
  - A capital protection state activated during extreme Z-Scores (> 3.0) or massive ATR volatility spikes. 
  - Suspends new entries and focuses entirely on managing risk for open positions.

## Risk & Trade Management Rules (The Holy Grail)
Exits are purely mathematical and managed by Dynamic ATR trailing logic. The legacy "Kill-Switch" which preemptively closed positions on minor Macro Trend flips (`TREND_INVALIDATION` / `TREND_REVERSAL`) has been completely removed to avoid death-by-a-thousand-cuts (whipsaw losses).

- **Dynamic ATR Trailing Stop (TSL):** 
  - **Activation:** The TSL becomes active only once the trade reaches a profit cushion of **3.0x ATR**.
  - **Trailing Distance:** Once activated, the Stop-Loss trails the peak price tightly at **2.0x ATR**.
- **Smart Partial Take Profit (TP):** 
  - Executes a **50% Partial Take Profit** automatically when the position reaches +1.5% ROE.
  - Immediately moves the remaining position's Stop-Loss to **Break-Even**.
  - Allows **Pyramiding** (scaling in) on winning trades without exposing the initial capital to risk.

## Self-Healing Mechanics
The execution engine is highly fault-tolerant and recovers autonomously from API rejections and desynchronization:
- **Graceful Error Handling:** Safely catches the dreaded Binance `[-2022] ReduceOnly Order is rejected` exception (which occurs when Binance liquidates/closes the position before the bot can). 
- **State Reconciliation:** Syncs the database silently and clears closed positions without crashing the main loop or throwing `TypeError: 'bool' object is not subscriptable`.
- **Slot Management:** Accurate local state management prevents ghost positions and fake "Max slots reached" blocking.

## Setup & Deployment (Docker Compose)
The system is fully containerized for isolated deployment alongside PostgreSQL and Metabase.

### 1. Environment Configuration
Create a `.env` file in the root directory:
```env
ENV_TYPE="TESTNET" # or "LIVE"
BINANCE_API_KEY="your_api_key"
BINANCE_API_SECRET="your_api_secret"
TELEGRAM_BOT_TOKEN="your_bot_token"
TELEGRAM_CHAT_ID="your_chat_id"
DB_USER="your_db_user"
DB_PASSWORD="your_db_password"
DB_NAME="quant_system"
MIN_24H_VOLUME_USDT="75000000.0"
TOP_N="45"
```

### 2. Launching the Engine
Build and deploy the containers in detached mode:
```bash
docker compose up -d --build
```