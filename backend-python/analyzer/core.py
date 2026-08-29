"""
analyzer/core.py
────────────────
Execution Analyzer Orchestrator.

This module contains the two public engine entry points and wires all
sub-modules together. No business logic lives here that belongs in a
sub-module — ``core.py`` only:
    • Queries data.
    • Calls sub-module functions in the correct order.
    • Makes final Binance order placement decisions.
    • Saves TradingSignal rows to the DB.

Public entry points:
    run_macro_analyzer  – 1h Macro Trend Engine (Engine A).
    run_analyzer        – 15m Execution Engine with MTF Confluence (Engine B).
"""

from __future__ import annotations

import logging
import traceback
import time
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from binance.exceptions import BinanceAPIException

from config import (
    TIMEFRAME,
    ALERT_PREFIX,
    ENGINE_ROLE,
    MACRO_SMA_PERIOD,
    MACRO_SDC_MULTIPLIER,
    ZSCORE_LONG_THRESHOLD,
    ZSCORE_SHORT_THRESHOLD,
    ZSCORE_SMA_PERIOD,
    ATR_PERIOD,
    REGIME_RISK_PARAMS,
    TESTNET_FORCE_TRADES,
    STOP_LOSS_PCT,
    MAX_GLOBAL_POSITIONS,
    DISABLE_TREND_REVERSAL_EJECT,
    CRASH_CATCHER_TSL_ATR_MULT,
    CRASH_CATCHER_ZSCORE_LONG,
    CRASH_CATCHER_RSI_LONG,
    CRASH_CATCHER_ZSCORE_SHORT,
    CRASH_CATCHER_RSI_SHORT,
)
from database import (
    SessionLocal,
    MarketData,
    TradingSignal,
    PortfolioState,
    engine,
    init_db,
    init_shared_db,
    count_active_positions,
    save_macro_state,
    get_macro_trend,
    SLOT_BUDGET,
    TOTAL_CAPITAL,
    MAX_CONCURRENT_POSITIONS,
)
from futures_executor import (
    open_position,
    get_futures_balance,
    get_position_info,
    count_all_open_positions,
    set_stop_loss_order,
    _check_api_exception_for_blacklist,
)
from market_regime import MarketRegime, strategy_router, RegimeDetector

from analyzer.constants import (
    TRADING_FEE,
    MIN_PROFIT_PCT,
    MAX_POSITION_USDT,
    MAX_WALLET_PCT,
    CONVICTION_TIER1_Z,
    CONVICTION_TIER1_VOL,
    CONVICTION_TIER1_ALLOC,
    CONVICTION_TIER2_Z,
    CONVICTION_TIER2_VOL,
    CONVICTION_TIER2_ALLOC,
    CONVICTION_TIER3_ALLOC,
    BINANCE_TIER1_ALLOC,
    BINANCE_TIER2_ALLOC,
    POSITION_UPGRADE_SCORE_GAP,
    ROTATION_CONFIRM_RETRIES,
    ROTATION_CONFIRM_SLEEP_S,
)
from analyzer.indicators import (
    calculate_rsi,
    calculate_zscore,
    calculate_sdc,
    calculate_atr,
    detect_order_blocks,
    detect_consolidation_breakout,
    detect_whale_strike,
)
from analyzer.signals import evaluate_extreme_reversion, calculate_strength_score
from analyzer.notifications import send_telegram_alert, send_periodic_report
from analyzer.portfolio import load_portfolio, _close_position_handler
from analyzer.risk import (
    evaluate_pyramid_scale_in,
    find_weakest_active_position,
    apply_long_risk_management,
    apply_short_risk_management,
)
from analyzer.sync import sync_binance_position
from analyzer.utils import log_to_db

logger = logging.getLogger("Analyzer")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    import sys
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(name)s | %(message)s',
        datefmt='%H:%M:%S',
    ))
    logger.addHandler(_ch)


# ══════════════════════════════════════════════════════════════════════
#  ENGINE A: MACRO TREND ANALYZER (1h)
# ══════════════════════════════════════════════════════════════════════

def run_macro_analyzer(symbol: str = 'BTCUSDT', timeframe: str = TIMEFRAME) -> None:
    """1h Macro Trend Engine.

    Computes:
        • SMA-50 (50-period Simple Moving Average)
        • Z-Score = (close - SMA) / σ
        • SDC (Standard Deviation Channel): SMA ± 2σ

    Decision:
        • If close > SMA-50 AND Z-Score > 0 → UPTREND
        • Otherwise → DOWNTREND

    Writes result to the shared MacroState DB table and sends a Telegram
    alert when the macro trend changes direction.

    Args:
        symbol:    Macro reference symbol (default: ``'BTCUSDT'``).
        timeframe: Candle timeframe to query from the DB (default: ``TIMEFRAME``).
    """
    print(f"\n{'=' * 60}", flush=True)
    print(f"--- Macro Analyzer Started [{symbol}] (1h Trend Engine) ---", flush=True)
    print(f"{'=' * 60}", flush=True)

    init_db()
    init_shared_db()

    session = SessionLocal()
    try:
        query = session.query(MarketData).filter(
            MarketData.symbol == symbol,
            MarketData.timeframe == timeframe,
        ).order_by(MarketData.timestamp.asc())

        df = pd.read_sql(query.statement, engine)

        if df.empty:
            print(f"No 1h data found for {symbol}. Skipping.", flush=True)
            return

        if len(df) < MACRO_SMA_PERIOD:
            print(
                f"Insufficient data for {symbol}: {len(df)} candles "
                f"(need {MACRO_SMA_PERIOD}). Skipping.",
                flush=True,
            )
            return

        # 1. Compute statistical indicators
        df = calculate_zscore(df, sma_period=MACRO_SMA_PERIOD)
        df = calculate_sdc(df, sma_period=MACRO_SMA_PERIOD, multiplier=MACRO_SDC_MULTIPLIER)

        # 2. Get latest values
        last_row = df.iloc[-1]
        current_price = float(last_row['close'])
        sma_50 = float(last_row[f'SMA_{MACRO_SMA_PERIOD}'])
        z_score = float(last_row['Z_Score']) if pd.notna(last_row['Z_Score']) else 0.0
        std_dev = float(last_row[f'STD_{MACRO_SMA_PERIOD}']) if pd.notna(last_row[f'STD_{MACRO_SMA_PERIOD}']) else 0.0
        sdc_upper = float(last_row['SDC_upper']) if pd.notna(last_row['SDC_upper']) else None
        sdc_lower = float(last_row['SDC_lower']) if pd.notna(last_row['SDC_lower']) else None

        # 3. Determine macro trend
        if current_price > sma_50 and z_score > 0:
            macro_trend = 'UPTREND'
        else:
            macro_trend = 'DOWNTREND'

        # 4. Check for trend change (for alert purposes)
        old_state = get_macro_trend(symbol)
        old_trend = old_state['macro_trend'] if old_state else None
        trend_changed = (old_trend is not None and old_trend != macro_trend)

        # 5. Save to shared DB
        save_macro_state(
            symbol=symbol,
            macro_trend=macro_trend,
            sma_50=sma_50,
            z_score=z_score,
            std_dev=std_dev,
            sdc_upper=sdc_upper,
            sdc_lower=sdc_lower,
            current_price=current_price,
        )

        # 6. Log summary
        trend_emoji = "🟢" if macro_trend == 'UPTREND' else "🔴"
        print(f"\n=== Macro Summary [{symbol}] ===", flush=True)
        print(f"  Price:     ${current_price:,.2f}", flush=True)
        print(f"  SMA-50:    ${sma_50:,.2f}", flush=True)
        print(f"  Z-Score:   {z_score:+.3f}", flush=True)
        print(f"  σ (STD):   ${std_dev:,.2f}", flush=True)
        if sdc_upper and sdc_lower:
            print(f"  SDC Upper: ${sdc_upper:,.2f}", flush=True)
            print(f"  SDC Lower: ${sdc_lower:,.2f}", flush=True)
        print(f"  Trend:     {trend_emoji} {macro_trend}", flush=True)
        if trend_changed:
            print(f"  ⚡ TREND CHANGED: {old_trend} → {macro_trend}", flush=True)

        # 7. Send Telegram alert on trend change
        if trend_changed:
            alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
            sdc_str = ""
            if sdc_upper and sdc_lower:
                sdc_str = f"- SDC Channel: ${sdc_lower:,.2f} — ${sdc_upper:,.2f}\n"

            alert_msg = (
                f"{ALERT_PREFIX} {trend_emoji} *MACRO TREND CHANGE: {macro_trend}*\n"
                f"\n"
                f"*Symbol:* {symbol}\n"
                f"*Price:* ${current_price:,.2f}\n"
                f"*Time:* {alert_time}\n"
                f"\n"
                f"📊 *Statistical Analysis (1h):*\n"
                f"- SMA-50: ${sma_50:,.2f}\n"
                f"- Z-Score: {z_score:+.3f}\n"
                f"- Std Dev: ${std_dev:,.2f}\n"
                f"{sdc_str}"
                f"\n"
                f"Previous: {old_trend} → New: *{macro_trend}*"
            )
            send_telegram_alert(alert_msg)

    except Exception as e:
        session.rollback()
        print(f"Error during macro analysis of {symbol}: {e}", flush=True)
        traceback.print_exc()
    finally:
        session.close()
        print(f"--- Macro Analyzer Completed [{symbol}] ---", flush=True)


# ══════════════════════════════════════════════════════════════════════
#  ENGINE B: EXECUTION ANALYZER (15m)
# ══════════════════════════════════════════════════════════════════════

def run_analyzer(
    symbol: str = 'PAXGUSDT',
    timeframe: str = TIMEFRAME,
    futures_client: Optional[object] = None,
) -> bool:
    """15m Execution Engine with MTF Confluence.

    Before evaluating entry signals, fetches the macro_trend from the
    shared DB (written by run_macro_analyzer on the 1h timeframe).

    Entry conditions:
        LONG:  macro_trend == 'UPTREND'  AND price touches Bullish OB
               AND 15m Z-Score < ZSCORE_LONG_THRESHOLD (dynamic oversold)
        SHORT: macro_trend == 'DOWNTREND' AND price touches Bearish OB
               AND 15m Z-Score > ZSCORE_SHORT_THRESHOLD (dynamic overbought)

    Exit conditions (checked BEFORE entry signals each cycle):
        - Trend Reversal Eject (macro flip, configurable via DISABLE_TREND_REVERSAL_EJECT)
        - Dynamic ATR Trailing Stop
        - Break-Even trigger
        - Partial Take Profit (50% scale-out)
        - Stop Loss hit
        - Stagnant Trade Closer (configurable via DISABLE_STAGNANT_EXIT)

    Args:
        symbol:         Trading pair to analyse (default: ``'PAXGUSDT'``).
        timeframe:      Candle timeframe string (default: ``TIMEFRAME``).
        futures_client: Optional Binance Futures client for live execution.

    Returns:
        ``True``  if a new Binance order was placed.
        ``False`` otherwise.
    """
    print(f"\n--- Execution Analyzer Started [{symbol}] (15m MTF Confluence) ---", flush=True)

    session = SessionLocal()
    try:
        # ── 0. Fetch macro trend from shared DB ──
        macro_info = get_macro_trend(symbol)
        if (
            macro_info is None
            or not macro_info.get('macro_trend')
            or macro_info.get('sma_50') is None
            or float(macro_info.get('sma_50', 0)) <= 0
        ):
            msg = f"🛑 [SKIP] {symbol}: Insufficient 1h Macro history or invalid SMA-50."
            print(msg, flush=True)
            log_to_db(session, symbol, "SKIP", msg)
            return False

        macro_trend = macro_info['macro_trend']
        macro_zscore = float(macro_info['z_score']) if macro_info.get('z_score') is not None else 0.0
        macro_sma = float(macro_info['sma_50'])
        macro_emoji = "🟢" if macro_trend == 'UPTREND' else "🔴"

        # ── 0b. Fetch BTC macro trend for the Hard SHORT Filter ──
        # Used across ALL entry paths (router, Whale Strike, Crash Catcher)
        # to suppress shorts when BTC is in a structural uptrend.
        btc_macro_info = get_macro_trend('BTCUSDT')
        btc_macro_trend = btc_macro_info.get('macro_trend') if btc_macro_info else None

        print(
            f"  {macro_emoji} [{symbol}] Macro: {macro_trend} | "
            f"1h Z: {macro_zscore:+.2f} | SMA-50: ${macro_sma:,.2f} | "
            f"BTC Macro: {btc_macro_trend or 'N/A'}",
            flush=True,
        )

        # ── 1. Query 15m market data ──
        query = session.query(MarketData).filter(
            MarketData.symbol == symbol,
            MarketData.timeframe == timeframe,
        ).order_by(MarketData.timestamp.asc())

        df = pd.read_sql(query.statement, engine)

        if df.empty:
            print(f"No data found for {symbol}. Skipping.", flush=True)
            return False

        # ── 2. Compute indicators ──
        df = calculate_rsi(df, period=14)
        df = calculate_zscore(df, sma_period=ZSCORE_SMA_PERIOD)
        df = calculate_atr(df, period=ATR_PERIOD)

        # ── 3. Detect volume-filtered Order Blocks ──
        bullish_ob, bearish_ob = detect_order_blocks(df, lookback=15)

        # ── 4. Get current candle data ──
        last_row = df.iloc[-1]
        current_price = float(last_row['close'])
        current_rsi = float(last_row['RSI']) if pd.notna(last_row['RSI']) else None
        current_zscore = float(last_row['Z_Score']) if pd.notna(last_row['Z_Score']) else None
        current_sma = (
            float(last_row[f'SMA_{ZSCORE_SMA_PERIOD}'])
            if pd.notna(last_row[f'SMA_{ZSCORE_SMA_PERIOD}'])
            else None
        )
        current_atr = (
            float(last_row['ATR'])
            if pd.notna(last_row['ATR']) and float(last_row['ATR']) > 0
            else float(current_price) * 0.005
        )

        print(
            f"  📊 [{symbol}] 15m Z-Score: {current_zscore:+.3f} | "
            f"RSI: {current_rsi:.1f}"
            if current_zscore and current_rsi
            else f"  📊 [{symbol}] Indicators computing...",
            flush=True,
        )

        # ── Strategy C Heartbeat: diagnose trend alignment + data starvation ──
        candle_count = len(df)
        sma_label = f"${current_sma:,.2f}" if current_sma else f"None ⚠️ DATA STARVATION"
        if current_sma is None:
            trend_action = f"SKIP (SMA-50 is None — only {candle_count} candles, need ≥{ZSCORE_SMA_PERIOD})"
        elif macro_trend == 'UPTREND' and current_price > current_sma:
            trend_action = "✅ LONG eligible (Price > SMA)"
        elif macro_trend == 'DOWNTREND' and current_price < current_sma:
            trend_action = "✅ SHORT eligible (Price < SMA)"
        else:
            trend_action = f"⏸️ SKIP ({macro_trend} but price {'<' if current_price < current_sma else '>'} SMA)"
        print(
            f"  🔍 [{symbol}] Candles: {candle_count} | Macro: {macro_trend} | "
            f"Price: ${current_price:,.2f} | SMA-50: {sma_label} | {trend_action}",
            flush=True,
        )

        # ── 5. Load portfolio state ──
        portfolio = load_portfolio(session, symbol)
        if futures_client:
            live_pos_start = get_position_info(futures_client, symbol)
            if live_pos_start and live_pos_start.get('size', 0) > 0:
                portfolio['asset_balance'] = live_pos_start['size']
                portfolio['average_entry_price'] = live_pos_start['entry_price']
                portfolio['position_direction'] = live_pos_start['direction']

        in_position = portfolio['asset_balance'] is not None and float(portfolio['asset_balance']) > 0
        entry_price = portfolio.get('average_entry_price')
        highest_price = portfolio.get('highest_price_since_entry')
        lowest_price = portfolio.get('lowest_price_since_entry')
        pos_direction = portfolio.get('position_direction')

        # ──────────────────────────────────────────────────────────
        #  RISK MANAGEMENT EXITS (checked BEFORE signal logic)
        # ──────────────────────────────────────────────────────────

        # ── Pre-compute Regime for Dynamic Risk Parameters ──
        active_regime, precomputed_meta = RegimeDetector.detect(df)
        active_mode_value = active_regime.value

        # ── Fetch Regime-Specific Risk Parameters ──
        rp = REGIME_RISK_PARAMS.get(active_mode_value, REGIME_RISK_PARAMS['RANGE'])
        sl_atr_mult              = rp['SL_ATR_MULT']
        partial_tp_pct           = rp['PARTIAL_TP_PCT']
        tsl_atr_activation_mult  = rp['TSL_ATR_ACTIVATION_MULT']
        tsl_atr_trail_mult       = rp['TSL_ATR_TRAIL_MULT']

        risk_exit_triggered = False

        if in_position and entry_price and float(entry_price) > 0:
            cp = float(current_price)
            ep = float(entry_price)
            stop_loss = float(portfolio.get('stop_loss_price', 0) or 0)
            trailing_active = portfolio.get('trailing_active', False)

            # ── 1. Trend Invalidation / Emergency Eject ──
            # ALPHA MODE: When DISABLE_TREND_REVERSAL_EJECT is True, the immediate
            # macro-flip eject is bypassed. The Dynamic ATR TSL handles exits organically.
            if not DISABLE_TREND_REVERSAL_EJECT:
                if pos_direction == 'LONG' and macro_trend == 'DOWNTREND':
                    msg = (
                        f"🚨🔴 [{symbol}] TREND REVERSAL EJECT — LONG position vs "
                        f"DOWNTREND macro. Thesis invalidated. CLOSING IMMEDIATELY "
                        f"at ${cp:.2f} (Entry: ${ep:.2f})"
                    )
                    print(msg, flush=True)
                    log_to_db(session, symbol, "EXIT", msg)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session,
                        'TREND_REVERSAL_EJECT',
                        futures_client, bullish_ob, bearish_ob,
                        current_rsi, current_zscore, macro_info,
                    )
                    risk_exit_triggered = True

                elif pos_direction == 'SHORT' and macro_trend == 'UPTREND':
                    msg = (
                        f"🚨🟢 [{symbol}] TREND REVERSAL EJECT — SHORT position vs "
                        f"UPTREND macro. Thesis invalidated. CLOSING IMMEDIATELY "
                        f"at ${cp:.2f} (Entry: ${ep:.2f})"
                    )
                    print(msg, flush=True)
                    log_to_db(session, symbol, "EXIT", msg)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session,
                        'TREND_REVERSAL_EJECT',
                        futures_client, bullish_ob, bearish_ob,
                        current_rsi, current_zscore, macro_info,
                    )
                    risk_exit_triggered = True
            else:
                if pos_direction == 'LONG' and macro_trend == 'DOWNTREND':
                    print(
                        f"⏸️ [{symbol}] TREND REVERSAL EJECT DISABLED (Alpha Mode) — "
                        "LONG vs DOWNTREND. ATR TSL will protect.",
                        flush=True,
                    )
                elif pos_direction == 'SHORT' and macro_trend == 'UPTREND':
                    print(
                        f"⏸️ [{symbol}] TREND REVERSAL EJECT DISABLED (Alpha Mode) — "
                        "SHORT vs UPTREND. ATR TSL will protect.",
                        flush=True,
                    )

            # ── 2. ATR TSL / Break-Even / Partial TP / SL Hit / Stagnant ──
            if pos_direction == 'LONG' and not risk_exit_triggered:
                portfolio, risk_exit_triggered = apply_long_risk_management(
                    portfolio=portfolio,
                    symbol=symbol,
                    session=session,
                    current_price=current_price,
                    entry_price=ep,
                    highest_price=highest_price,
                    stop_loss=stop_loss,
                    trailing_active=trailing_active,
                    current_atr=current_atr,
                    tsl_atr_activation_mult=tsl_atr_activation_mult,
                    tsl_atr_trail_mult=tsl_atr_trail_mult,
                    partial_tp_pct=partial_tp_pct,
                    futures_client=futures_client,
                    bullish_ob=bullish_ob,
                    bearish_ob=bearish_ob,
                    current_rsi=current_rsi,
                    current_zscore=current_zscore,
                    macro_info=macro_info,
                )

            elif pos_direction == 'SHORT' and not risk_exit_triggered:
                portfolio, risk_exit_triggered = apply_short_risk_management(
                    portfolio=portfolio,
                    symbol=symbol,
                    session=session,
                    current_price=current_price,
                    entry_price=ep,
                    lowest_price=lowest_price,
                    stop_loss=stop_loss,
                    trailing_active=trailing_active,
                    current_atr=current_atr,
                    tsl_atr_activation_mult=tsl_atr_activation_mult,
                    tsl_atr_trail_mult=tsl_atr_trail_mult,
                    partial_tp_pct=partial_tp_pct,
                    futures_client=futures_client,
                    bullish_ob=bullish_ob,
                    bearish_ob=bearish_ob,
                    current_rsi=current_rsi,
                    current_zscore=current_zscore,
                    macro_info=macro_info,
                )

        # ── Pyramiding (Scaling Into Winners) Check ──
        if in_position and not risk_exit_triggered:
            _, portfolio = evaluate_pyramid_scale_in(portfolio, current_price, symbol, session, futures_client)

        bullish_breakout, bearish_breakout = detect_consolidation_breakout(df)
        whale_strike = detect_whale_strike(df)

        # ──────────────────────────────────────────────────────────
        #  REGIME DETECTION & STRATEGY ROUTING
        # ──────────────────────────────────────────────────────────
        decision = 'WAIT'
        strategy_type = None
        new_stop_loss = 0.0
        active_mode_value = MarketRegime.RANGE.value  # conservative default
        cc_signal = None

        if not risk_exit_triggered:
            active_count = count_active_positions(session)

            # ── STRATEGY C: Whale Hunter (Volume Anomaly Spike > 10x) ──
            # Bypasses regime detection — volume creates its own regime.
            if whale_strike and not in_position:
                w_direction = whale_strike['direction']
                w_vol_ratio = whale_strike['vol_ratio']

                is_macro_aligned = False
                if w_direction == 'LONG' and macro_trend in ('UPTREND', 'NEUTRAL'):
                    is_macro_aligned = True
                elif w_direction == 'SHORT' and macro_trend in ('DOWNTREND', 'NEUTRAL'):
                    is_macro_aligned = True

                # BEAST MODE: Whale Strike SHORTs allowed regardless of BTC/asset macro trend
                # (bi-directional momentum scalping — intraday EMA-20 check in StrategyRouter handles filtering)

                if is_macro_aligned:
                    if active_count >= MAX_CONCURRENT_POSITIONS:
                        print(
                            f"⏸️ [{symbol}] WAIT (Max Slots Reached: {active_count}/{MAX_CONCURRENT_POSITIONS})",
                            flush=True,
                        )
                    else:
                        decision = w_direction
                        strategy_type = 'WHALE_STRIKE'
                        active_mode_value = MarketRegime.TREND.value
                        if w_direction == 'LONG':
                            new_stop_loss = whale_strike['candle_low'] * 0.999
                        else:
                            new_stop_loss = whale_strike['candle_high'] * 1.001

                        msg = (
                            f"🐋 [WHALE STRIKE DETECTED] {symbol} {decision}! "
                            f"Vol: {w_vol_ratio:.1f}x avg | Price: ${current_price:.2f} | Macro: {macro_trend}"
                        )
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)
                        send_telegram_alert(msg)

            # ── STRATEGY D: Crash Catcher (Extreme Mean Reversion Engine) ──────
            # ⚡ BYPASS PERMISSIONS: This block runs BEFORE the STORM freeze and
            #    BEFORE the StrategyRouter (which contains the Macro Hard Filter).
            #    It is the ONLY module authorised to place market orders during a
            #    STORM regime and to take counter-trend positions.
            if decision == 'WAIT':
                cc_signal = evaluate_extreme_reversion(
                    df=df,
                    current_price=current_price,
                    current_rsi=current_rsi,
                    current_zscore=current_zscore,
                    current_atr=current_atr,
                    in_position=in_position,
                    active_count=active_count,
                    max_positions=MAX_CONCURRENT_POSITIONS,
                )

                if cc_signal:
                    cc_direction = cc_signal['direction']

                    # BEAST MODE: Crash Catcher SHORTs allowed regardless of BTC/asset macro trend
                    # (bi-directional scalping — intraday momentum check in StrategyRouter handles filtering)
                    if False:  # SHORT ban removed
                        pass
                    else:
                        decision          = cc_direction
                        strategy_type     = cc_signal['strategy_type']
                        new_stop_loss     = cc_signal['stop_loss']
                        active_mode_value = 'CRASH_CATCHER'

                        # Override TSL parameters with Crash Catcher's aggressive profile
                        # (early activation at 0.5x ATR, tight trail at 0.3x ATR)
                        tsl_atr_activation_mult = CRASH_CATCHER_TSL_ATR_MULT
                        tsl_atr_trail_mult      = REGIME_RISK_PARAMS['CRASH_CATCHER']['TSL_ATR_TRAIL_MULT']
                        partial_tp_pct          = REGIME_RISK_PARAMS['CRASH_CATCHER']['PARTIAL_TP_PCT']

                        storm_bypass_msg = (
                            f"⚡ [CRASH CATCHER] STORM Freeze BYPASSED for {symbol} {decision} "
                            f"(counter-trend override authorised) | "
                            f"Z: {current_zscore:+.3f} | RSI: {current_rsi:.1f} | "
                            f"Vol: {cc_signal['vol_ratio']:.1f}x | Wick: {cc_signal['wick_ratio']:.2f} | "
                            f"SL: ${new_stop_loss:.4f} | Regime: {active_regime.value}"
                        )
                        print(storm_bypass_msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", storm_bypass_msg)

            # ── STORM OVERRIDE: Conditional freeze (primary engine only) ──
            # NOTE: Crash Catcher already set decision above if it fired.
            # If decision is still 'WAIT', evaluate whether STORM should
            # allow trend-aligned entries or freeze everything.
            if active_regime.value == 'STORM' and decision == 'WAIT':
                storm_direction = precomputed_meta.get('storm_direction')

                # Allow Strategy Router for trend-following entries IF:
                #   1. STORM was triggered by Z-Score (has directional bias)
                #   2. Macro trend is defined (UPTREND or DOWNTREND)
                if storm_direction and macro_trend in ('UPTREND', 'DOWNTREND'):
                    regime, routed_decision, routed_strategy_type, routed_stop_loss, regime_meta = strategy_router.route(
                        symbol=symbol,
                        df=df,
                        portfolio=portfolio,
                        current_price=current_price,
                        current_rsi=current_rsi,
                        current_zscore=current_zscore,
                        current_atr=current_atr,
                        bullish_ob=bullish_ob,
                        bearish_ob=bearish_ob,
                        bullish_breakout=bullish_breakout,
                        bearish_breakout=bearish_breakout,
                        macro_trend=macro_trend,
                        in_position=in_position,
                        active_count=active_count,
                        max_positions=MAX_CONCURRENT_POSITIONS,
                        futures_client=futures_client,
                        session=session,
                        precomputed_regime=active_regime,
                        precomputed_meta=precomputed_meta,
                        btc_macro_trend=btc_macro_trend,
                    )

                    # Accept only trend-following/breakout strategies aligned with STORM direction.
                    # Mean Reversion is always blocked during STORM.
                    if routed_decision in ('LONG', 'SHORT') and routed_strategy_type in ('PULLBACK', 'BREAKOUT', 'TREND_MOMENTUM'):
                        storm_aligned = (
                            (storm_direction == 'UP' and macro_trend == 'UPTREND' and routed_decision == 'LONG') or
                            (storm_direction == 'DOWN' and macro_trend == 'DOWNTREND' and routed_decision == 'SHORT')
                        )
                        if storm_aligned:
                            decision      = routed_decision
                            strategy_type = routed_strategy_type
                            new_stop_loss = routed_stop_loss
                            active_mode_value = regime.value
                            print(
                                f"🌪️✅ [{symbol}] STORM Trend-Aligned Entry ALLOWED — "
                                f"{decision} {strategy_type} (storm_dir={storm_direction}, macro={macro_trend})",
                                flush=True,
                            )
                            log_to_db(session, symbol, "ENTRY",
                                f"STORM Trend-Aligned {decision} {strategy_type} allowed "
                                f"(storm_dir={storm_direction}, macro={macro_trend})")
                        else:
                            print(
                                f"⏸️ [{symbol}] STORM Freeze — counter-trend {routed_decision} {routed_strategy_type} rejected "
                                f"(storm_dir={storm_direction}, macro={macro_trend})",
                                flush=True,
                            )
                    elif routed_decision in ('LONG', 'SHORT') and routed_strategy_type == 'RANGE_MEAN_REVERSION':
                        print(
                            f"⏸️ [{symbol}] STORM Freeze — Mean Reversion blocked during STORM",
                            flush=True,
                        )
                    # else: routed_decision was WAIT, nothing to do
                else:
                    print(f"⏸️ [{symbol}] STORM Regime Active — Freezing New Entries (primary engine)", flush=True)

            # ── STRATEGY ROUTER: Regime-Aware Entry Logic (non-STORM) ──
            if decision == 'WAIT' and active_regime.value != 'STORM':
                regime, routed_decision, routed_strategy_type, routed_stop_loss, regime_meta = strategy_router.route(
                    symbol=symbol,
                    df=df,
                    portfolio=portfolio,
                    current_price=current_price,
                    current_rsi=current_rsi,
                    current_zscore=current_zscore,
                    current_atr=current_atr,
                    bullish_ob=bullish_ob,
                    bearish_ob=bearish_ob,
                    bullish_breakout=bullish_breakout,
                    bearish_breakout=bearish_breakout,
                    macro_trend=macro_trend,
                    in_position=in_position,
                    active_count=active_count,
                    max_positions=MAX_CONCURRENT_POSITIONS,
                    futures_client=futures_client,
                    session=session,
                    precomputed_regime=active_regime,
                    precomputed_meta=precomputed_meta,
                    btc_macro_trend=btc_macro_trend,
                )
                active_mode_value = regime.value

                if routed_decision in ('LONG', 'SHORT'):
                    decision      = routed_decision
                    strategy_type = routed_strategy_type
                    new_stop_loss = routed_stop_loss
                    adx_str    = f"{regime_meta['adx']:.1f}" if regime_meta.get('adx') is not None else "N/A"
                    rsi_log    = f"{current_rsi:.1f}" if current_rsi is not None else "N/A"
                    zscore_log = f"{current_zscore:+.3f}" if current_zscore is not None else "N/A"
                    print(
                        f"[MODE: {regime.value}] Symbol: {symbol} | ADX: {adx_str} | "
                        f"RSI: {rsi_log} | Z: {zscore_log} | "
                        f"Strategy: {strategy_type}",
                        flush=True,
                    )
                elif routed_strategy_type == 'STORM_LIMIT_PENDING':
                    adx_str = f"{regime_meta['adx']:.1f}" if regime_meta.get('adx') is not None else "N/A"
                    print(
                        f"[🌪️ STORM PENDING] {symbol} | ADX: {adx_str} | "
                        "Limit order placed — awaiting fill (TTL: 15 min)",
                        flush=True,
                    )

            # ── TESTNET FORCE TRADES (override — kept for backward compat) ──
            if decision == 'WAIT' and TESTNET_FORCE_TRADES and current_zscore is not None and current_rsi is not None:
                current_sma_val = (
                    float(last_row[f'SMA_{ZSCORE_SMA_PERIOD}'])
                    if pd.notna(last_row.get(f'SMA_{ZSCORE_SMA_PERIOD}'))
                    else None
                )
                if (
                    current_sma_val
                    and macro_trend == 'UPTREND'
                    and current_price > current_sma_val
                    and current_rsi > 55.0
                    and current_zscore > 1.2
                ):
                    if active_count < MAX_CONCURRENT_POSITIONS and not in_position:
                        decision = 'LONG'
                        strategy_type = 'TREND_ALIGN'
                        active_mode_value = MarketRegime.TREND.value
                        new_stop_loss = current_sma_val * 0.995
                        msg = f"🧪 [{symbol}] LONG Strategy C (TREND_ALIGN): Macro={macro_trend} + Price > SMA-50"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)
                elif (
                    current_sma_val
                    and macro_trend == 'DOWNTREND'
                    and current_price < current_sma_val
                    and current_rsi < 45.0
                    and current_zscore < -1.2
                ):
                    if active_count < MAX_CONCURRENT_POSITIONS and not in_position:
                        decision = 'SHORT'
                        strategy_type = 'TREND_ALIGN'
                        active_mode_value = MarketRegime.TREND.value
                        new_stop_loss = current_sma_val * 1.005
                        msg = f"🧪 [{symbol}] SHORT Strategy C (TREND_ALIGN): Macro={macro_trend} + Price < SMA-50"
                        print(msg, flush=True)
                        log_to_db(session, symbol, "ENTRY", msg)

            # ── Stop-Loss Integrity Guard ──
            if decision in ('LONG', 'SHORT'):
                if new_stop_loss <= 0.0 or pd.isna(new_stop_loss):
                    if decision == 'LONG':
                        new_stop_loss = current_price * (1.0 - STOP_LOSS_PCT)
                    else:
                        new_stop_loss = current_price * (1.0 + STOP_LOSS_PCT)

                is_invalid = False
                if new_stop_loss <= 0.0 or pd.isna(new_stop_loss):
                    is_invalid = True
                elif decision == 'LONG' and new_stop_loss >= current_price:
                    is_invalid = True
                elif decision == 'SHORT' and new_stop_loss <= current_price:
                    is_invalid = True

                if is_invalid:
                    msg = (
                        f"🛑 [SKIP] {symbol}: Unable to calculate valid Stop-Loss price "
                        f"for {decision} (SL: ${new_stop_loss}, Price: ${current_price})."
                    )
                    print(msg, flush=True)
                    log_to_db(session, symbol, "SKIP", msg)
                    decision = 'WAIT'

            # ── DEBUG LOGGER: Why is the bot skipping? ──
            if decision == 'WAIT' and not risk_exit_triggered and not in_position:
                z_str   = f"{current_zscore:+.3f}" if current_zscore is not None else "N/A"
                rsi_str = f"{current_rsi:.1f}"    if current_rsi  is not None else "N/A"
                print(
                    f"  [DEBUG] {symbol} | [MODE: {active_mode_value}] "
                    f"Z: {z_str} | RSI: {rsi_str} | No entry signal.",
                    flush=True,
                )

        # ── 6. Save signal to Database ──
        db_decision = decision

        if decision == 'WAIT' and not risk_exit_triggered:
            if futures_client:
                pos_info = get_position_info(futures_client, symbol)
                if pos_info and pos_info['size'] > 0:
                    db_decision = pos_info['direction']
                    print(
                        f"  🔒 [{symbol}] DB decision synced to '{db_decision}' "
                        f"(live Binance position, size={pos_info['size']})",
                        flush=True,
                    )

            if db_decision == 'WAIT' and in_position and pos_direction in ('LONG', 'SHORT'):
                db_decision = pos_direction
                print(
                    f"  🔒 [{symbol}] DB decision synced to '{db_decision}' "
                    "(local portfolio has open position)",
                    flush=True,
                )

        if risk_exit_triggered:
            db_decision = f"CLOSE_{pos_direction}" if pos_direction in ('LONG', 'SHORT') else 'WAIT'

        signal = TradingSignal(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=pd.Timestamp(last_row['timestamp']).to_pydatetime(),
            current_price=float(current_price),
            rsi=float(current_rsi) if current_rsi is not None else None,
            z_score=float(current_zscore) if current_zscore is not None else None,
            macro_trend=macro_trend,
            bullish_ob_low=float(bullish_ob['low']) if bullish_ob else None,
            bullish_ob_high=float(bullish_ob['high']) if bullish_ob else None,
            bearish_ob_low=float(bearish_ob['low']) if bearish_ob else None,
            bearish_ob_high=float(bearish_ob['high']) if bearish_ob else None,
            decision=db_decision,
        )

        session.add(signal)
        session.commit()

        # ──────────────────────────────────────────────────────────
        #  EXECUTE SIGNALS (LONG / SHORT with position management)
        # ──────────────────────────────────────────────────────────
        if not risk_exit_triggered and decision in ('LONG', 'SHORT'):
            portfolio = load_portfolio(session, symbol)
            in_position = portfolio['asset_balance'] is not None and portfolio['asset_balance'] > 0
            pos_direction = portfolio.get('position_direction')

            last_signal = session.query(TradingSignal).filter(
                TradingSignal.symbol == symbol,
                TradingSignal.timeframe == timeframe,
                TradingSignal.decision.in_(['LONG', 'SHORT', 'CLOSE_LONG', 'CLOSE_SHORT']),
                TradingSignal.id != signal.id,
            ).order_by(TradingSignal.id.desc()).first()

            last_decision = last_signal.decision if last_signal else None

            if decision != last_decision or decision == 'LONG':

                # ── If we're in an opposite position, close it first ──
                if in_position and pos_direction and pos_direction != decision:
                    print(f"🔄 [{symbol}] Reversing: closing {pos_direction} before opening {decision}", flush=True)
                    portfolio = _close_position_handler(
                        portfolio, current_price, symbol, session, 'SIGNAL',
                        futures_client, bullish_ob, bearish_ob, current_rsi,
                        current_zscore, macro_info,
                    )
                    in_position = False

                # ── Scale-Up (Pyramiding) Safety Checks ──
                dca_level = portfolio.get('dca_level', 0)
                if decision == 'LONG' and in_position and pos_direction == 'LONG':
                    if dca_level >= 3:
                        print(f"🛑 [{symbol}] LONG Scale-Up Skipped: Max iterations (2) reached.", flush=True)
                        decision = 'WAIT'
                    else:
                        ep = float(portfolio.get('average_entry_price', 0))
                        sl = float(portfolio.get('stop_loss', 0))
                        if sl < ep or sl == 0:
                            print(
                                f"🛑 [{symbol}] LONG Scale-Up Skipped: Stop Loss (${sl:.4f}) "
                                f"not at/past Break-Even (${ep:.4f}).",
                                flush=True,
                            )
                            decision = 'WAIT'

                if decision == 'SHORT' and in_position and pos_direction == 'SHORT':
                    if dca_level >= 3:
                        print(f"🛑 [{symbol}] SHORT Scale-Up Skipped: Max iterations (2) reached.", flush=True)
                        decision = 'WAIT'
                    else:
                        ep = float(portfolio.get('average_entry_price', 0))
                        sl = float(portfolio.get('stop_loss', 0))
                        if sl > ep or sl == 0:
                            print(
                                f"🛑 [{symbol}] SHORT Scale-Up Skipped: Stop Loss (${sl:.4f}) "
                                f"not at/past Break-Even (${ep:.4f}).",
                                flush=True,
                            )
                            decision = 'WAIT'

                # ── Execute LONG (open or DCA) ──
                if decision == 'LONG':
                    vol_ratio = 0.0
                    if strategy_type == 'PULLBACK' and bullish_ob and 'vol_ratio' in bullish_ob:
                        vol_ratio = bullish_ob['vol_ratio']
                    elif strategy_type == 'BREAKOUT' and bullish_breakout and 'vol_ratio' in bullish_breakout:
                        vol_ratio = bullish_breakout['vol_ratio']

                    abs_z = abs(current_zscore) if current_zscore else 0.0

                    allocation_pct = CONVICTION_TIER3_ALLOC
                    tier_str = "Tier 3"
                    if abs_z >= CONVICTION_TIER1_Z and vol_ratio >= CONVICTION_TIER1_VOL:
                        allocation_pct = CONVICTION_TIER1_ALLOC
                        tier_str = "Tier 1"
                    elif abs_z >= CONVICTION_TIER2_Z and vol_ratio >= CONVICTION_TIER2_VOL:
                        allocation_pct = CONVICTION_TIER2_ALLOC
                        tier_str = "Tier 2"

                    if futures_client:
                        actual_balance = get_futures_balance(futures_client)
                    else:
                        actual_balance = portfolio.get('usdt_balance', 0.0)

                    spend = actual_balance * allocation_pct

                    if actual_balance < spend or actual_balance <= 0:
                        print(
                            f"⚠️ Skipping execution: Insufficient USDT balance "
                            f"({actual_balance:.2f} USDT available)",
                            flush=True,
                        )
                        return False

                    if portfolio['usdt_balance'] >= spend:
                        effective_usdt = spend * (1 - TRADING_FEE)
                        asset_bought = effective_usdt / float(current_price)

                        old_asset = float(portfolio.get('asset_balance', 0) or 0)
                        old_avg = float(portfolio.get('average_entry_price', 0) or 0)
                        old_val = old_asset * old_avg
                        new_val = asset_bought * float(current_price)

                        portfolio['asset_balance'] = round(old_asset + asset_bought, 6)
                        portfolio['average_entry_price'] = (old_val + new_val) / portfolio['asset_balance']
                        portfolio['dca_level'] = dca_level + 1
                        portfolio['last_exec_price'] = float(current_price)
                        portfolio['total_cost'] = float(portfolio.get('total_cost', 0)) + effective_usdt
                        portfolio['usdt_balance'] -= spend
                        portfolio['position_direction'] = 'LONG'

                        if dca_level == 0:
                            if not new_stop_loss or float(new_stop_loss) <= 0.0:
                                new_stop_loss = float(current_price) * 0.985
                        elif dca_level > 0:
                            new_stop_loss = max(float(new_stop_loss), portfolio['average_entry_price'])

                        portfolio['stop_loss_price'] = float(new_stop_loss)
                        portfolio['stop_loss'] = float(new_stop_loss)
                        if dca_level == 0:
                            portfolio['highest_price_since_entry'] = float(current_price)
                            portfolio['lowest_price_since_entry'] = None

                        order = None
                        total_value = portfolio['usdt_balance'] + (float(portfolio['asset_balance']) * float(current_price))

                        # Generate reason_msg / strategy_name
                        if strategy_type == 'PULLBACK':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}, OB:{vol_ratio:.1f}x] - Pullback"
                            ob_low = bullish_ob['low'] if bullish_ob else 0
                            ob_high = bullish_ob['high'] if bullish_ob else 0
                            reason_msg = (
                                f"{strategy_name} | Macro: {macro_trend} | "
                                f"OB: ${float(ob_low):.2f}-${float(ob_high):.2f}"
                            )
                        elif strategy_type == 'BREAKOUT':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}, OB:{vol_ratio:.1f}x] - Breakout"
                            reason_msg = f"{strategy_name} | Macro: {macro_trend} | Consolidation High Cleared"
                        elif strategy_type == 'TREND_ALIGN':
                            strategy_name = f"{tier_str} - Trend Align"
                            reason_msg = (
                                f"{strategy_name} | Macro: {macro_trend} | "
                                f"Price ${current_price:.2f} > SMA-50 ${current_sma:.2f} | 🧪 TESTNET ONLY"
                            )
                        elif strategy_type == 'TREND_MOMENTUM':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}] - Trend Momentum"
                            reason_msg = (
                                f"{strategy_name} | Macro: {macro_trend} | "
                                f"Price > EMA-20 + RSI {current_rsi:.1f} + Micro-breakout"
                            )
                        elif strategy_type in ('CRASH_CATCHER_LONG', 'CRASH_CATCHER_SHORT'):
                            _cc = cc_signal if cc_signal else {}
                            strategy_name = (
                                f"CRASH_CATCHER [Z:{abs_z:.1f}, "
                                f"Vol:{_cc.get('vol_ratio', 0):.1f}x, "
                                f"Wick:{_cc.get('wick_ratio', 0):.2f}] - Extreme MR"
                            )
                            reason_msg = (
                                f"{strategy_name} | SL: wick_low=${new_stop_loss:.4f} | "
                                f"TSL: {CRASH_CATCHER_TSL_ATR_MULT}x ATR | Counter-trend bypass active"
                            )
                        else:
                            strategy_name = f"{tier_str} - Unknown"
                            reason_msg = "Strategy: Unknown"

                        portfolio['strategy'] = strategy_name

                        highest_price = portfolio.get('highest_price_since_entry')
                        hp_since_entry = float(highest_price) if highest_price is not None else float(current_price)

                        portfolio_record = PortfolioState(
                            timestamp=datetime.now(),
                            symbol=symbol,
                            decision='LONG',
                            current_price=float(current_price),
                            usdt_balance=float(portfolio['usdt_balance']),
                            asset_balance=float(portfolio['asset_balance']),
                            position_direction='LONG',
                            average_entry_price=float(portfolio['average_entry_price']),
                            dca_level=int(portfolio['dca_level']),
                            last_exec_price=float(portfolio['last_exec_price']),
                            total_cost=float(portfolio['total_cost']),
                            highest_price_since_entry=hp_since_entry,
                            stop_loss_price=float(new_stop_loss),
                            stop_loss=float(new_stop_loss),
                            strategy=strategy_name,
                            trailing_active=False,
                            lowest_price_since_entry=None,
                            pnl_pct=None,
                            pnl_usd=None,
                            total_portfolio_value=float(round(total_value, 2)),
                            entry_reason=reason_msg,
                            active_mode=active_mode_value,
                        )
                        for attempt in range(3):
                            try:
                                session.add(portfolio_record)
                                session.commit()
                                break
                            except Exception as e:
                                session.rollback()
                                if attempt == 2:
                                    print(
                                        f"Warning: Failed to save portfolio state to DB after 3 attempts: {e}",
                                        flush=True,
                                    )
                                    traceback.print_exc()
                                else:
                                    time.sleep(1)

                        # ── Telegram alert ──
                        alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
                        _valid_regimes = [r.value for r in MarketRegime]
                        mode_label = (
                            MarketRegime(active_mode_value).telegram_label
                            if active_mode_value in _valid_regimes
                            else '[CRASH CATCHER 🚨]'
                        )

                        if strategy_type == 'PULLBACK':
                            alert_reason = (
                                f"- Strategy: A (Pullback)\n"
                                f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                f"- 15m Z-Score: {current_zscore:+.2f} (threshold: {ZSCORE_LONG_THRESHOLD})\n"
                                f"- Bullish OB: ${float(ob_low):.2f} - ${float(ob_high):.2f}\n"
                                f"- OB Volume: {vol_ratio} avg"
                            )
                        elif strategy_type == 'BREAKOUT':
                            alert_reason = (
                                f"- Strategy: B (Momentum Breakout)\n"
                                f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                f"- Breakout Volume: {vol_ratio} avg\n"
                                f"- Consolidation High Cleared!"
                            )
                        elif strategy_type == 'TREND_ALIGN':
                            alert_reason = (
                                f"- Strategy: C (Trend Alignment) 🧪\n"
                                f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                f"- Price: ${current_price:.2f} > SMA-50: ${current_sma:.2f}\n"
                                f"- ⚠️ TESTNET ONLY — No OB/Volume confirmation"
                            )
                        elif strategy_type == 'TREND_MOMENTUM':
                            alert_reason = (
                                f"- Strategy: E (Trend Momentum)\n"
                                f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                f"- Price > EMA-20 (riding fast MA)\n"
                                f"- RSI: {current_rsi:.1f} (momentum zone 50-75)\n"
                                f"- Micro-breakout: Close > prior 3-candle high"
                            )
                        elif strategy_type == 'RANGE_MEAN_REVERSION':
                            alert_reason = (
                                f"- Strategy: Mean Reversion (Range)\n"
                                f"- RSI: {current_rsi:.1f} (oversold <35)\n"
                                f"- Bullish OB touch: ${bullish_ob['low']:.2f}–${bullish_ob['high']:.2f}"
                            )
                        elif strategy_type in ('CRASH_CATCHER_LONG', 'CRASH_CATCHER_SHORT'):
                            _cc = cc_signal if cc_signal else {}
                            alert_reason = (
                                f"- Strategy: 🚨 CRASH CATCHER (Extreme Mean Reversion)\n"
                                f"- Trigger: Z-Score {current_zscore:+.3f} (≤{CRASH_CATCHER_ZSCORE_LONG}) + RSI {current_rsi:.1f} (<{CRASH_CATCHER_RSI_LONG})\n"
                                f"- Volume Anomaly: {_cc.get('vol_ratio', 0):.1f}x MA — Whale absorption detected\n"
                                f"- Pin Bar: Lower wick ratio {_cc.get('wick_ratio', 0):.2f} — Bottom defended\n"
                                f"- SL: ${new_stop_loss:.4f} (wick low −0.1% buffer) — WICK BREACH = EXIT\n"
                                f"- TSL: Activates at {CRASH_CATCHER_TSL_ATR_MULT}x ATR (rubber-band lock-in)\n"
                                f"- ⚡ STORM Freeze BYPASSED — Counter-trend override active"
                            )
                        else:
                            alert_reason = "- Strategy: Unknown"

                        alert_msg = (
                            f"{ALERT_PREFIX} 🚨 *QUANT ALERT: {'SCALE-UP LONG' if dca_level > 0 else 'OPEN LONG'}* 🚨\n"
                            f"\n"
                            f"🤖 Active Mode: {mode_label}\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Price:* ${float(current_price):.2f}\n"
                            f"*Time:* {alert_time}\n"
                            f"\n"
                            f"💡 *MTF Confluence:*\n"
                            f"{alert_reason}\n"
                            f"\n"
                            f"🛡 *Risk Management:*\n"
                            f"- Entry Price: ${float(portfolio['average_entry_price']):.2f}\n"
                            f"- Stop-Loss: ${new_stop_loss:.2f} (Dynamic)\n"
                            f"- Trailing Stop: {tsl_atr_activation_mult}x ATR act / {tsl_atr_trail_mult}x ATR trail\n"
                            f"\n"
                            f"💼 *Virtual Portfolio:*\n"
                            f"- Slot Budget: ${SLOT_BUDGET:,.0f}\n"
                            f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
                            f"- Asset Balance: {portfolio['asset_balance']:.6f}\n"
                            f"- Total Value: ${total_value:.2f}\n"
                            f"- DCA Iteration: {dca_level + 1}/3"
                        )
                        if not futures_client or order:
                            send_telegram_alert(alert_msg)

                # ── Execute SHORT (open or DCA) ──
                elif decision == 'SHORT':
                    vol_ratio = 0.0
                    if strategy_type == 'PULLBACK' and bearish_ob and 'vol_ratio' in bearish_ob:
                        vol_ratio = bearish_ob['vol_ratio']
                    elif strategy_type == 'BREAKOUT' and bearish_breakout and 'vol_ratio' in bearish_breakout:
                        vol_ratio = bearish_breakout['vol_ratio']

                    abs_z = abs(current_zscore) if current_zscore else 0.0

                    allocation_pct = CONVICTION_TIER3_ALLOC
                    tier_str = "Tier 3"
                    if abs_z >= CONVICTION_TIER1_Z and vol_ratio >= CONVICTION_TIER1_VOL:
                        allocation_pct = CONVICTION_TIER1_ALLOC
                        tier_str = "Tier 1"
                    elif abs_z >= CONVICTION_TIER2_Z and vol_ratio >= CONVICTION_TIER2_VOL:
                        allocation_pct = CONVICTION_TIER2_ALLOC
                        tier_str = "Tier 2"

                    if futures_client:
                        actual_balance = get_futures_balance(futures_client)
                    else:
                        actual_balance = portfolio.get('usdt_balance', 0.0)

                    spend = actual_balance * allocation_pct

                    if actual_balance < spend or actual_balance <= 0:
                        print(
                            f"⚠️ Skipping execution: Insufficient USDT balance "
                            f"({actual_balance:.2f} USDT available)",
                            flush=True,
                        )
                        return False

                    if portfolio['usdt_balance'] >= spend:
                        effective_usdt = spend * (1 - TRADING_FEE)
                        asset_sold = effective_usdt / float(current_price)

                        old_asset = float(portfolio.get('asset_balance', 0) or 0)
                        old_avg = float(portfolio.get('average_entry_price', 0) or 0)
                        old_val = old_asset * old_avg
                        new_val = asset_sold * float(current_price)

                        portfolio['asset_balance'] = round(old_asset + asset_sold, 6)
                        portfolio['average_entry_price'] = (old_val + new_val) / portfolio['asset_balance']
                        portfolio['dca_level'] = dca_level + 1
                        portfolio['last_exec_price'] = float(current_price)
                        portfolio['total_cost'] = float(portfolio.get('total_cost', 0)) + effective_usdt
                        portfolio['usdt_balance'] -= spend
                        portfolio['position_direction'] = 'SHORT'

                        if dca_level == 0:
                            if not new_stop_loss or float(new_stop_loss) <= 0.0:
                                new_stop_loss = float(current_price) * 1.015
                        elif dca_level > 0:
                            new_stop_loss = min(float(new_stop_loss), portfolio['average_entry_price'])

                        portfolio['stop_loss_price'] = float(new_stop_loss)
                        portfolio['stop_loss'] = float(new_stop_loss)
                        if dca_level == 0:
                            portfolio['lowest_price_since_entry'] = float(current_price)
                            portfolio['highest_price_since_entry'] = None

                        order = None
                        total_value = portfolio['usdt_balance'] + (float(portfolio['asset_balance']) * float(current_price))

                        # Generate reason_msg / strategy_name
                        if strategy_type == 'PULLBACK':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}, OB:{vol_ratio:.1f}x] - Pullback"
                            ob_low = bearish_ob['low'] if bearish_ob else 0
                            ob_high = bearish_ob['high'] if bearish_ob else 0
                            reason_msg = (
                                f"{strategy_name} | Macro: {macro_trend} | "
                                f"OB: ${float(ob_low):.2f}-${float(ob_high):.2f}"
                            )
                        elif strategy_type == 'BREAKOUT':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}, OB:{vol_ratio:.1f}x] - Breakout"
                            reason_msg = f"{strategy_name} | Macro: {macro_trend} | Consolidation Low Broken"
                        elif strategy_type == 'TREND_ALIGN':
                            strategy_name = f"{tier_str} - Trend Align"
                            reason_msg = (
                                f"{strategy_name} | Macro: {macro_trend} | "
                                f"Price ${current_price:.2f} < SMA-50 ${current_sma:.2f} | 🧪 TESTNET ONLY"
                            )
                        elif strategy_type == 'TREND_MOMENTUM':
                            strategy_name = f"{tier_str} [Z:{abs_z:.1f}] - Trend Momentum"
                            reason_msg = (
                                f"{strategy_name} | Macro: {macro_trend} | "
                                f"Price < EMA-20 + RSI {current_rsi:.1f} + Micro-breakdown"
                            )
                        elif strategy_type in ('CRASH_CATCHER_LONG', 'CRASH_CATCHER_SHORT'):
                            _cc = cc_signal if cc_signal else {}
                            strategy_name = (
                                f"CRASH_CATCHER [Z:{abs_z:.1f}, "
                                f"Vol:{_cc.get('vol_ratio', 0):.1f}x, "
                                f"Wick:{_cc.get('wick_ratio', 0):.2f}] - Extreme MR"
                            )
                            reason_msg = (
                                f"{strategy_name} | SL: wick_high=${new_stop_loss:.4f} | "
                                f"TSL: {CRASH_CATCHER_TSL_ATR_MULT}x ATR | Counter-trend bypass active"
                            )
                        else:
                            strategy_name = f"{tier_str} - Unknown"
                            reason_msg = "Strategy: Unknown"

                        portfolio['strategy'] = strategy_name

                        lowest_price = portfolio.get('lowest_price_since_entry')
                        lp_since_entry = float(lowest_price) if lowest_price is not None else float(current_price)

                        portfolio_record = PortfolioState(
                            timestamp=datetime.now(),
                            symbol=symbol,
                            decision='SHORT',
                            current_price=float(current_price),
                            usdt_balance=float(portfolio['usdt_balance']),
                            asset_balance=float(portfolio['asset_balance']),
                            position_direction='SHORT',
                            average_entry_price=float(portfolio['average_entry_price']),
                            dca_level=int(portfolio['dca_level']),
                            last_exec_price=float(portfolio['last_exec_price']),
                            total_cost=float(portfolio['total_cost']),
                            highest_price_since_entry=None,
                            lowest_price_since_entry=lp_since_entry,
                            stop_loss_price=float(new_stop_loss),
                            stop_loss=float(new_stop_loss),
                            strategy=strategy_name,
                            trailing_active=False,
                            pnl_pct=None,
                            pnl_usd=None,
                            total_portfolio_value=float(round(total_value, 2)),
                            entry_reason=reason_msg,
                            active_mode=active_mode_value,
                        )
                        for attempt in range(3):
                            try:
                                session.add(portfolio_record)
                                session.commit()
                                break
                            except Exception as e:
                                session.rollback()
                                if attempt == 2:
                                    print(
                                        f"Warning: Failed to save portfolio state to DB after 3 attempts: {e}",
                                        flush=True,
                                    )
                                    traceback.print_exc()
                                else:
                                    time.sleep(1)

                        # ── Telegram alert ──
                        alert_time = datetime.now().strftime('%Y-%m-%d %I:%M %p')
                        mode_label = (
                            MarketRegime(active_mode_value).telegram_label
                            if active_mode_value
                            else '[TREND 🚀]'
                        )

                        if strategy_type == 'PULLBACK':
                            alert_reason = (
                                f"- Strategy: A (Pullback)\n"
                                f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                f"- 15m Z-Score: {current_zscore:+.2f} (threshold: +{ZSCORE_SHORT_THRESHOLD})\n"
                                f"- Bearish OB: ${float(ob_low):.2f} - ${float(ob_high):.2f}\n"
                                f"- OB Volume: {vol_ratio} avg"
                            )
                        elif strategy_type == 'BREAKOUT':
                            alert_reason = (
                                f"- Strategy: B (Momentum Breakout)\n"
                                f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                f"- Breakout Volume: {vol_ratio} avg\n"
                                f"- Consolidation Low Broken!"
                            )
                        elif strategy_type == 'TREND_ALIGN':
                            alert_reason = (
                                f"- Strategy: C (Trend Alignment) 🧪\n"
                                f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                f"- Price: ${current_price:.2f} < SMA-50: ${current_sma:.2f}\n"
                                f"- ⚠️ TESTNET ONLY — No OB/Volume confirmation"
                            )
                        elif strategy_type == 'TREND_MOMENTUM':
                            alert_reason = (
                                f"- Strategy: E (Trend Momentum)\n"
                                f"- Macro Trend: {macro_emoji} {macro_trend}\n"
                                f"- Price < EMA-20 (riding below fast MA)\n"
                                f"- RSI: {current_rsi:.1f} (momentum zone 25-50)\n"
                                f"- Micro-breakdown: Close < prior 3-candle low"
                            )
                        elif strategy_type == 'RANGE_MEAN_REVERSION':
                            alert_reason = (
                                f"- Strategy: Mean Reversion (Range)\n"
                                f"- RSI: {current_rsi:.1f} (overbought >65)\n"
                                f"- Bearish OB touch: ${bearish_ob['low']:.2f}–${bearish_ob['high']:.2f}"
                            )
                        elif strategy_type in ('CRASH_CATCHER_LONG', 'CRASH_CATCHER_SHORT'):
                            _cc = cc_signal if cc_signal else {}
                            alert_reason = (
                                f"- Strategy: 🚨 CRASH CATCHER (Extreme Mean Reversion)\n"
                                f"- Trigger: Z-Score {current_zscore:+.3f} (≥+{CRASH_CATCHER_ZSCORE_SHORT}) + RSI {current_rsi:.1f} (>{CRASH_CATCHER_RSI_SHORT})\n"
                                f"- Volume Anomaly: {_cc.get('vol_ratio', 0):.1f}x MA — Forced short squeeze detected\n"
                                f"- Pin Bar: Upper wick ratio {_cc.get('wick_ratio', 0):.2f} — Top actively rejected\n"
                                f"- SL: ${new_stop_loss:.4f} (wick high +0.1% buffer) — WICK BREACH = EXIT\n"
                                f"- TSL: Activates at {CRASH_CATCHER_TSL_ATR_MULT}x ATR (rubber-band lock-in)\n"
                                f"- ⚡ Macro Hard Filter BYPASSED — Counter-trend override active"
                            )
                        else:
                            alert_reason = "- Strategy: Unknown"

                        alert_msg = (
                            f"{ALERT_PREFIX} 🚨 *QUANT ALERT: {'SCALE-UP SHORT' if dca_level > 0 else 'OPEN SHORT'}* 🚨\n"
                            f"\n"
                            f"🤖 Active Mode: {mode_label}\n"
                            f"*Symbol:* {symbol}\n"
                            f"*Price:* ${float(current_price):.2f}\n"
                            f"*Time:* {alert_time}\n"
                            f"\n"
                            f"💡 *MTF Confluence:*\n"
                            f"{alert_reason}\n"
                            f"\n"
                            f"🛡 *Risk Management:*\n"
                            f"- Entry Price: ${float(portfolio['average_entry_price']):.2f}\n"
                            f"- Stop-Loss: ${new_stop_loss:.2f} (Dynamic)\n"
                            f"- Trailing Stop: {tsl_atr_activation_mult}x ATR act / {tsl_atr_trail_mult}x ATR trail\n"
                            f"\n"
                            f"💼 *Virtual Portfolio:*\n"
                            f"- Slot Budget: ${SLOT_BUDGET:,.0f}\n"
                            f"- USDT Balance: ${portfolio['usdt_balance']:.2f}\n"
                            f"- Asset Balance: {portfolio['asset_balance']:.6f}\n"
                            f"- Total Value: ${total_value:.2f}\n"
                            f"- DCA Iteration: {dca_level + 1}/3"
                        )
                        if not futures_client or order:
                            send_telegram_alert(alert_msg)

                # ── Close LONG via SHORT signal (if holding LONG) ──
                elif decision == 'SHORT' and in_position and pos_direction == 'LONG':
                    ep = float(portfolio['average_entry_price']) if portfolio['average_entry_price'] else 0
                    if ep > 0:
                        profit_pct = (float(current_price) - ep) / ep
                        if profit_pct < MIN_PROFIT_PCT:
                            print(
                                f"⏸️ [{symbol}] Signal CLOSE LONG suppressed: profit "
                                f"{profit_pct * 100:.2f}% < min gate {MIN_PROFIT_PCT * 100:.1f}%",
                                flush=True,
                            )
                        else:
                            print(
                                f"✅ [{symbol}] Signal CLOSE LONG executing: profit "
                                f"{profit_pct * 100:.2f}% >= min gate {MIN_PROFIT_PCT * 100:.1f}%",
                                flush=True,
                            )
                            portfolio = _close_position_handler(
                                portfolio, current_price, symbol, session, 'SIGNAL',
                                futures_client, bullish_ob, bearish_ob, current_rsi,
                                current_zscore, macro_info,
                            )
                    else:
                        portfolio = _close_position_handler(
                            portfolio, current_price, symbol, session, 'SIGNAL',
                            futures_client, bullish_ob, bearish_ob, current_rsi,
                            current_zscore, macro_info,
                        )
            else:
                print(f"[{symbol}] Duplicate {decision} signal — Telegram alert suppressed.", flush=True)

        # ──────────────────────────────────────────────────────────
        #  SYNC POSITION STATE TO DB (for Laravel dashboard)
        # ──────────────────────────────────────────────────────────
        if not risk_exit_triggered and decision == 'WAIT':
            portfolio = load_portfolio(session, symbol)

            portfolio, db_decision, in_position = sync_binance_position(
                symbol=symbol,
                portfolio=portfolio,
                session=session,
                futures_client=futures_client,
                current_price=current_price,
                current_atr=current_atr,
                tsl_atr_trail_mult=tsl_atr_trail_mult,
                partial_tp_pct=partial_tp_pct,
                sl_atr_mult=sl_atr_mult,
                db_decision=db_decision,
                active_mode_value=active_mode_value,
            )

        # ── Back-patch TradingSignal if sync changed db_decision ──
        if signal.decision != db_decision:
            print(
                f"  🔄 [{symbol}] Patching TradingSignal: "
                f"'{signal.decision}' → '{db_decision}'",
                flush=True,
            )
            signal.decision = db_decision
            try:
                session.commit()
            except Exception as e:
                session.rollback()
                print(f"Warning: Failed to patch TradingSignal decision: {e}", flush=True)

        # ── 7. Print summary ──
        print(f"\n=== Execution Summary [{symbol}] (15m MTF) ===", flush=True)
        print(f"Symbol: {symbol} | Timeframe: {timeframe}", flush=True)
        print(
            f"Price: ${current_price:,.2f} | RSI: {current_rsi:.1f}"
            if current_rsi
            else f"Price: ${current_price:,.2f}",
            flush=True,
        )
        print(f"15m Z-Score: {current_zscore:+.3f}" if current_zscore else "15m Z-Score: N/A", flush=True)
        print(f"Macro Trend: {macro_emoji} {macro_trend} | 1h Z: {macro_zscore:+.2f}", flush=True)

        print("\n--- Order Blocks (Volume-Filtered) ---", flush=True)
        if bullish_ob:
            print(
                f"Bullish OB: ${bullish_ob['low']:.2f} - ${bullish_ob['high']:.2f} "
                f"(Vol: {bullish_ob.get('vol_ratio', 0):.1f}x avg)",
                flush=True,
            )
        else:
            print("Bullish OB: Not found (or volume too low)", flush=True)

        if bearish_ob:
            print(
                f"Bearish OB: ${bearish_ob['low']:.2f} - ${bearish_ob['high']:.2f} "
                f"(Vol: {bearish_ob.get('vol_ratio', 0):.1f}x avg)",
                flush=True,
            )
        else:
            print("Bearish OB: Not found (or volume too low)", flush=True)

        # Risk management status summary
        portfolio = load_portfolio(session, symbol)
        in_position = portfolio['asset_balance'] is not None and portfolio['asset_balance'] > 0
        pos_direction = portfolio.get('position_direction')
        if in_position and portfolio.get('average_entry_price'):
            ep = float(portfolio['average_entry_price'])
            cp = float(current_price)
            if pos_direction == 'LONG':
                hp = float(portfolio['highest_price_since_entry']) if portfolio.get('highest_price_since_entry') else cp
                current_sl = float(portfolio.get('stop_loss_price', 0) or 0)
                unrealized = ((cp - ep) / ep) * 100
                is_trailing = portfolio.get('trailing_active', False)
                trailing_status = "🟢 ACTIVE" if is_trailing else "⚪ INACTIVE"
                tp_status = "✅ HIT (50% Closed)" if portfolio.get('partial_tp_hit') else f"⚪ WAITING (+{partial_tp_pct * 100:.1f}%)"
                print(f"\n--- Risk Management (LONG) ---", flush=True)
                print(f"Entry: ${ep:.2f} | Unrealized: {unrealized:+.2f}% | Peak: ${hp:.2f}", flush=True)
                tsl_act_str = f"+${tsl_atr_activation_mult * current_atr:.4f} ({tsl_atr_activation_mult}x ATR)" if current_atr else f"+{0.8:.1f}%"
                tsl_trl_str = f"${tsl_atr_trail_mult * current_atr:.4f} ({tsl_atr_trail_mult}x ATR)" if current_atr else "0.4%"
                print(
                    f"Stop-Loss: ${current_sl:.2f} | TSL: {trailing_status} | Partial TP: {tp_status} "
                    f"(activates at {tsl_act_str}, trails {tsl_trl_str})",
                    flush=True,
                )
            elif pos_direction == 'SHORT':
                lp = float(portfolio['lowest_price_since_entry']) if portfolio.get('lowest_price_since_entry') else cp
                current_sl = float(portfolio.get('stop_loss_price', 0) or 0)
                unrealized = ((ep - cp) / ep) * 100
                is_trailing = portfolio.get('trailing_active', False)
                trailing_status = "🟢 ACTIVE" if is_trailing else "⚪ INACTIVE"
                tp_status = "✅ HIT (50% Closed)" if portfolio.get('partial_tp_hit') else f"⚪ WAITING (+{partial_tp_pct * 100:.1f}%)"
                print(f"\n--- Risk Management (SHORT) ---", flush=True)
                print(f"Entry: ${ep:.2f} | Unrealized: {unrealized:+.2f}% | Trough: ${lp:.2f}", flush=True)
                tsl_act_str = f"+${tsl_atr_activation_mult * current_atr:.4f} ({tsl_atr_activation_mult}x ATR)" if current_atr else "+0.8%"
                tsl_trl_str = f"${tsl_atr_trail_mult * current_atr:.4f} ({tsl_atr_trail_mult}x ATR)" if current_atr else "0.4%"
                print(
                    f"Stop-Loss: ${current_sl:.2f} | TSL: {trailing_status} | Partial TP: {tp_status} "
                    f"(activates at {tsl_act_str}, trails {tsl_trl_str})",
                    flush=True,
                )

        if risk_exit_triggered:
            print(f"\n⚠️ [{symbol}] Risk exit was triggered this cycle.", flush=True)
        else:
            print(f"\n[{symbol}] Decision '{db_decision}' saved to database successfully.", flush=True)

        # ════════════════════════════════════════════════════════════════════
        # 🚀 EXECUTION ENGINE: BINANCE API PLACEMENT (Production Guards)
        # ════════════════════════════════════════════════════════════════════
        _exec_logger = logging.getLogger("FuturesExecutor")

        if futures_client and db_decision in ('LONG', 'SHORT'):
            direction = db_decision

            # ── GUARD 0: Global Max Positions & Trade Upgrading (Position Rotation) ──
            current_open_count = count_all_open_positions(futures_client)
            can_proceed_with_entry = True

            live_pos = get_position_info(futures_client, symbol)
            if live_pos and live_pos['size'] > 0:
                _exec_logger.info(
                    f"🔒 [SKIP ENTRY] {symbol} already has an active Binance "
                    f"{live_pos['direction']} position (size={live_pos['size']}). Skipping."
                )
                can_proceed_with_entry = False

            if can_proceed_with_entry and current_open_count >= MAX_GLOBAL_POSITIONS:
                cand_vol = 1.0
                if whale_strike:
                    cand_vol = whale_strike['vol_ratio']
                elif bullish_ob and 'vol_ratio' in bullish_ob:
                    cand_vol = bullish_ob['vol_ratio']
                elif bearish_ob and 'vol_ratio' in bearish_ob:
                    cand_vol = bearish_ob['vol_ratio']
                elif bullish_breakout and 'vol_ratio' in bullish_breakout:
                    cand_vol = bullish_breakout['vol_ratio']
                elif bearish_breakout and 'vol_ratio' in bearish_breakout:
                    cand_vol = bearish_breakout['vol_ratio']

                cand_score = calculate_strength_score(current_zscore, cand_vol)

                # NEW RULE: Strictly forbid upgrading an active position with Strategy A or B signals.
                # ONLY a Whale Strike (Strategy C) may trigger a POSITION_UPGRADE.
                if not whale_strike:
                    _exec_logger.warning(
                        f"🛑 [SKIP UPGRADE] Max global positions ({MAX_GLOBAL_POSITIONS}) reached. "
                        f"Signal {symbol} is a standard Strategy A/B signal. Position upgrades are STRICTLY FORBIDDEN "
                        f"unless triggered by a Whale Strike (Strategy C)."
                    )
                    can_proceed_with_entry = False
                else:
                    weak_sym, weak_score, weak_port, weak_stagnant = find_weakest_active_position(session, futures_client)
                    weak_score_val = float(weak_score) if weak_score is not None else 0.0

                    if weak_sym and weak_sym != symbol and (weak_stagnant or (cand_score >= weak_score_val + POSITION_UPGRADE_SCORE_GAP)):
                        upgrade_msg = (
                            f"🔄 [POSITION UPGRADE] Max slots ({MAX_GLOBAL_POSITIONS}) reached! "
                            f"Closing weak position '{weak_sym}' (Score={weak_score_val:.2f}, Stagnant={weak_stagnant}) "
                            f"to enter Whale Strike signal '{symbol}' {direction} "
                            f"(Score={cand_score:.2f}, Z={current_zscore:+.2f})."
                        )
                        print(upgrade_msg, flush=True)
                        log_to_db(session, symbol, "UPGRADE", upgrade_msg)
                        send_telegram_alert(upgrade_msg)

                        weak_price = float(weak_port.get('average_entry_price') or current_price) if weak_port else current_price
                        _close_position_handler(
                            weak_port, weak_price, weak_sym, session, 'POSITION_UPGRADE',
                            futures_client, bullish_ob, bearish_ob, current_rsi,
                            current_zscore, macro_info,
                        )

                        # ── ROTATION SYNC: Verify closure on Binance before proceeding ──
                        closed_confirmed = False
                        if futures_client:
                            for _attempt in range(ROTATION_CONFIRM_RETRIES):
                                time.sleep(ROTATION_CONFIRM_SLEEP_S)
                                check_pos = get_position_info(futures_client, weak_sym)
                                if not check_pos or check_pos.get('size', 0.0) == 0.0:
                                    closed_confirmed = True
                                    break
                        else:
                            closed_confirmed = True

                        if closed_confirmed:
                            current_open_count = count_all_open_positions(futures_client)
                            if current_open_count < MAX_GLOBAL_POSITIONS:
                                can_proceed_with_entry = True
                            else:
                                _exec_logger.error(
                                    f"❌ [ROTATION SYNC] After closing {weak_sym}, "
                                    f"open count ({current_open_count}) >= limit ({MAX_GLOBAL_POSITIONS}). "
                                    "Blocking new entry."
                                )
                                can_proceed_with_entry = False
                        else:
                            _exec_logger.error(
                                f"❌ [ROTATION SYNC FAILED] Could not confirm closure of "
                                f"{weak_sym} on Binance after retries. "
                                f"Aborting upgrade entry for {symbol}."
                            )
                            can_proceed_with_entry = False
                    else:
                        _exec_logger.warning(
                            f"🛑 [SKIP UPGRADE] Max global positions ({MAX_GLOBAL_POSITIONS}) reached. "
                            f"Whale Strike candidate {symbol} (Score={cand_score:.2f}, Z={current_zscore:+.2f}) "
                            f"is not strong enough to replace weakest position '{weak_sym or 'N/A'}' "
                            f"(Score={weak_score_val:.2f}). Required gap: +{POSITION_UPGRADE_SCORE_GAP}."
                        )
                        can_proceed_with_entry = False

            if can_proceed_with_entry:
                # ── GUARD 2: Live Free Margin Check ──
                available_balance = get_futures_balance(futures_client)

                # ── Position Sizing: Conviction Tier (capped for live safety) ──
                _alloc_pct = BINANCE_TIER2_ALLOC   # Tier 3/default
                _abs_z = abs(current_zscore) if current_zscore else 0.0
                _vol_ratio = 0.0
                if bullish_ob and 'vol_ratio' in bullish_ob:
                    _vol_ratio = bullish_ob['vol_ratio']
                elif bearish_ob and 'vol_ratio' in bearish_ob:
                    _vol_ratio = bearish_ob['vol_ratio']
                if _abs_z >= CONVICTION_TIER1_Z and _vol_ratio >= CONVICTION_TIER1_VOL:
                    _alloc_pct = BINANCE_TIER1_ALLOC   # Tier 1 — capped at 10%
                elif _abs_z >= CONVICTION_TIER2_Z and _vol_ratio >= CONVICTION_TIER2_VOL:
                    _alloc_pct = BINANCE_TIER2_ALLOC   # Tier 2 — capped at 5%

                # ── GUARD 3: Position Size Hard Cap ──
                calculated_size = available_balance * _alloc_pct
                wallet_cap      = available_balance * MAX_WALLET_PCT
                allocated_usdt  = min(calculated_size, wallet_cap, MAX_POSITION_USDT)

                _exec_logger.info(
                    f"[{symbol}] Sizing: calculated=${calculated_size:.2f}, "
                    f"wallet_cap=${wallet_cap:.2f}, hard_cap=${MAX_POSITION_USDT}, "
                    f"final_allocated=${allocated_usdt:.2f}"
                )

                # ── GUARD 4: Safety Bypass ──
                if available_balance < 10.0:
                    _exec_logger.warning(
                        f"⚠️ [SKIP EXECUTION] Insufficient Free Margin: "
                        f"${available_balance:.2f} available (Required: ${allocated_usdt:.2f}) "
                        f"for {symbol} {direction}."
                    )
                elif allocated_usdt > available_balance:
                    _exec_logger.warning(
                        f"⚠️ [SKIP EXECUTION] Insufficient Free Margin: "
                        f"${available_balance:.2f} available (Required: ${allocated_usdt:.2f}) "
                        f"for {symbol} {direction}."
                    )
                elif allocated_usdt < 5.0:
                    _exec_logger.warning(
                        f"⚠️ [SKIP EXECUTION] Allocated amount too small: "
                        f"${allocated_usdt:.2f} for {symbol} {direction}."
                    )
                elif (
                    new_stop_loss <= 0.0
                    or pd.isna(new_stop_loss)
                    or (direction == 'LONG' and new_stop_loss >= current_price)
                    or (direction == 'SHORT' and new_stop_loss <= current_price)
                ):
                    _exec_logger.error(
                        f"🛑 [SKIP EXECUTION] {symbol} {direction}: Invalid or missing Stop-Loss price "
                        f"(${new_stop_loss} vs Price ${current_price}). "
                        "Binance order placement blocked."
                    )
                else:
                    try:
                        order = open_position(futures_client, symbol, direction, allocated_usdt)
                        if order:
                            _exec_logger.info(
                                f"✅ EXECUTED ON BINANCE: {direction} | "
                                f"Symbol: {symbol} | "
                                f"OrderID: {order.get('orderId')} | "
                                f"Allocated: ${allocated_usdt:.2f}"
                            )
                            if new_stop_loss and float(new_stop_loss) > 0:
                                set_stop_loss_order(futures_client, symbol, direction, float(new_stop_loss))
                            return True
                        else:
                            _exec_logger.warning(
                                f"⚠️ [{symbol}] open_position returned None. "
                                "Order blocked or restricted."
                            )
                            return False

                    except BinanceAPIException as api_err:
                        _check_api_exception_for_blacklist(symbol, api_err)
                        _exec_logger.warning(
                            f"⚠️ BINANCE API EXCEPTION: {symbol} {direction} | "
                            f"[{api_err.code}] {api_err.message}"
                        )
                    except Exception as e:
                        _exec_logger.error(
                            f"❌ BINANCE REJECTED ORDER: {symbol} {direction} | "
                            f"Allocated: ${allocated_usdt:.2f} | "
                            f"Error: {str(e)}"
                        )
                        traceback.print_exc()

    except Exception as e:
        session.rollback()
        print(f"Error during analysis of {symbol}: {e}", flush=True)
        traceback.print_exc()
    finally:
        session.close()
        print(f"--- Execution Analyzer Completed [{symbol}] ---", flush=True)

    return False


# ══════════════════════════════════════════════════════════════════════
#  __main__ ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if ENGINE_ROLE.upper() == "MACRO":
        run_macro_analyzer()
    else:
        run_analyzer()
