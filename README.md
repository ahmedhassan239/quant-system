# Quant Trading Engine 📈🤖

## Overview
A fully automated, stateful algorithmic trading engine designed to monitor cryptocurrency markets (currently tailored for PAXGUSDT). The system fetches live market data, performs technical analysis using RSI and Order Blocks, executes paper trades via a Virtual Portfolio, and sends real-time intelligent alerts via Telegram. 

All metrics, signals, and portfolio states are persisted in a PostgreSQL database and visualized in real-time using a Metabase dashboard.

## Core Features
*   **Automated Market Fetcher:** Scheduled job pulling live 15-minute candlestick data from Binance API.
*   **Technical Analyzer:** Evaluates market conditions using Relative Strength Index (RSI) and detects Bullish/Bearish Order Blocks (OB).
*   **Signal State Management:** A stateful logic system that prevents consecutive duplicate signals (e.g., ignoring 'SELL' signals if the current position is already 'SOLD').
*   **Virtual Portfolio (Paper Trading):** Simulates real trades starting with a base USDT balance, calculates PnL per trade, and updates the portfolio state in the database.
*   **Intelligent Telegram Alerts:** Notifies the user of 'BUY' or 'SELL' actions, including the current price, RSI value, Order Block conditions, and the Virtual Portfolio balance.
*   **Real-time Dashboard:** Seamless integration with Metabase for tracking `Market Data`, `Trading Signals`, and `Portfolio State`.

## Tech Stack
*   **Language:** Python 3
*   **Database:** PostgreSQL
*   **Containerization:** Docker & Docker Compose
*   **Visualization:** Metabase
*   **Integrations:** Binance API, Telegram Bot API

## System Architecture
1.  **Fetcher Module:** Queries Binance and stores raw candlestick data.
2.  **Analyzer Module:** Processes data, checks against trading conditions, and generates 'BUY', 'SELL', or 'WAIT' signals.
3.  **State Manager:** Validates the generated signal against the last executed trade to prevent alert spam.
4.  **Portfolio Manager:** Updates `portfolio_state` table with simulated USDT/PAXG balances.
5.  **Notifier:** Dispatches formatted Telegram messages containing trade reasoning and wallet updates.

## Environment Variables (.env)
To run this project locally or on a production server, the following variables must be configured in a `.env` file:
TELEGRAM_BOT_TOKEN="your_bot_token_here"
TELEGRAM_CHAT_ID="your_chat_id_here"
DB_USER="your_db_user"
DB_PASSWORD="your_db_password"
DB_NAME="quant_system"

## Deployment
The system is fully containerized. To build and run the engine alongside the database and Metabase:
```bash
docker compose up -d --build
```