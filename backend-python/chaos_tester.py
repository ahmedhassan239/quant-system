import pandas as pd
import joblib
import numpy as np


def run_base_backtest(df_oos, model, feature_cols,
                      confidence_threshold=0.65, trailing_stop_pct=0.015,
                      stop_loss_pct=0.01, trading_fee=0.004,
                      initial_capital=10000.0):
    """
    Run the base backtest on OOS data with extreme slippage/fees.
    Returns: final capital, trade count, win/loss counts, and a list of
    per-trade percentage returns (PnL%).
    """
    X = df_oos[feature_cols]
    buy_confidence = model.predict_proba(X)[:, 1]
    df_oos = df_oos.copy()
    df_oos['Buy_Confidence'] = buy_confidence

    capital = initial_capital
    in_position = False
    entry_price = 0.0
    position_size = 0.0
    trades = 0
    winning_trades = 0
    losing_trades = 0
    highest_price = 0.0
    trade_pnl_pcts = []  # Store each trade's % return

    for row in df_oos.itertuples(index=False):
        current_price = row.close
        confidence = row.Buy_Confidence

        # BUY Logic
        if confidence >= confidence_threshold and not in_position:
            capital_after_fee = capital * (1 - trading_fee)
            position_size = capital_after_fee / current_price
            capital = 0.0

            entry_price = current_price
            in_position = True
            highest_price = current_price

        # SELL Logic: Trailing Stop OR Hard Stop Loss only
        elif in_position:
            highest_price = max(highest_price, current_price)

            hit_ts = current_price <= highest_price * (1 - trailing_stop_pct)
            hit_sl = current_price <= entry_price * (1 - stop_loss_pct)

            if hit_ts or hit_sl:
                gross_revenue = position_size * current_price
                net_revenue = gross_revenue * (1 - trading_fee)

                # Per-trade PnL % (accounts for both entry and exit fees)
                cost_basis = (position_size * entry_price) / (1 - trading_fee)
                trade_return_pct = ((net_revenue - cost_basis) / cost_basis) * 100
                trade_pnl_pcts.append(trade_return_pct)

                capital = net_revenue
                position_size = 0.0

                if current_price > entry_price:
                    winning_trades += 1
                else:
                    losing_trades += 1

                trades += 1
                in_position = False
                entry_price = 0.0
                highest_price = 0.0

    # Close any remaining open position
    if in_position:
        final_price = df_oos.iloc[-1]['close']
        gross_revenue = position_size * final_price
        net_revenue = gross_revenue * (1 - trading_fee)

        cost_basis = (position_size * entry_price) / (1 - trading_fee)
        trade_return_pct = ((net_revenue - cost_basis) / cost_basis) * 100
        trade_pnl_pcts.append(trade_return_pct)

        capital = net_revenue

        if final_price > entry_price:
            winning_trades += 1
        else:
            losing_trades += 1
        trades += 1

    roi = ((capital - initial_capital) / initial_capital) * 100
    win_rate = (winning_trades / trades * 100) if trades > 0 else 0.0

    return {
        'initial_capital': initial_capital,
        'final_capital': capital,
        'roi': roi,
        'trades': trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': win_rate,
        'trade_pnl_pcts': trade_pnl_pcts,
    }


def run_monte_carlo(trade_pnl_pcts, initial_capital=10000.0,
                    n_simulations=1000, ruin_threshold=5000.0):
    """
    Monte Carlo simulation: resample trade returns with replacement
    to generate alternate equity curves.
    """
    rng = np.random.default_rng(seed=42)
    n_trades = len(trade_pnl_pcts)

    if n_trades == 0:
        return {
            'median_final': initial_capital,
            'worst_case': initial_capital,
            'best_case': initial_capital,
            'mean_final': initial_capital,
            'p5_final': initial_capital,
            'p95_final': initial_capital,
            'risk_of_ruin_pct': 0.0,
            'n_simulations': n_simulations,
            'n_trades': n_trades,
        }

    pnl_array = np.array(trade_pnl_pcts) / 100.0  # Convert % to decimal
    final_capitals = np.zeros(n_simulations)

    for i in range(n_simulations):
        # Randomly sample trade returns with replacement
        sampled_returns = rng.choice(pnl_array, size=n_trades, replace=True)
        # Compound the returns to get the final capital
        capital = initial_capital
        for r in sampled_returns:
            capital *= (1 + r)
        final_capitals[i] = capital

    ruin_count = np.sum(final_capitals < ruin_threshold)

    return {
        'median_final': np.median(final_capitals),
        'worst_case': np.min(final_capitals),
        'best_case': np.max(final_capitals),
        'mean_final': np.mean(final_capitals),
        'p5_final': np.percentile(final_capitals, 5),
        'p95_final': np.percentile(final_capitals, 95),
        'risk_of_ruin_pct': (ruin_count / n_simulations) * 100,
        'n_simulations': n_simulations,
        'n_trades': n_trades,
    }


def run_chaos_test():
    print("=" * 60, flush=True)
    print("     CHAOS ENGINEERING & MONTE CARLO STRESS TEST", flush=True)
    print("=" * 60, flush=True)

    # 1. Load the trained model
    model_path = 'models/quant_rf_model.pkl'
    try:
        model = joblib.load(model_path)
        print(f"Loaded model from {model_path}", flush=True)
    except FileNotFoundError:
        print(f"Model file {model_path} not found. Run model_trainer.py first.", flush=True)
        return

    # 2. Load the dataset
    data_path = 'data/processed/PAXGUSDT_features.csv'
    try:
        df = pd.read_csv(data_path)
        print(f"Loaded dataset from {data_path} with {len(df):,} rows.", flush=True)
    except FileNotFoundError:
        print(f"Dataset {data_path} not found. Ensure feature engineering is complete.", flush=True)
        return

    # 3. Parse timestamp and filter for Out-of-Sample period (Jan 2025+)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df_oos = df[df['timestamp'] >= '2025-01-01'].reset_index(drop=True)

    if len(df_oos) == 0:
        print("No data found for OOS period (2025+). Aborting.", flush=True)
        return

    print(f"OOS period: {df_oos['timestamp'].min().date(flush=True)} → "
          f"{df_oos['timestamp'].max().date()} ({len(df_oos):,} candles)")

    # 4. Define features
    feature_cols = [
        'open', 'high', 'low', 'close', 'volume',
        'RSI_14', 'MACD', 'MACD_signal', 'BB_high', 'BB_low',
        'ATR_14', 'SMA_50', 'SMA_200'
    ]

    # 5. Run base backtest with EXTREME slippage (0.4% per side = 0.8% round-trip)
    print("\nRunning base OOS backtest with EXTREME slippage (0.4% per side)...", flush=True)
    base = run_base_backtest(
        df_oos=df_oos,
        model=model,
        feature_cols=feature_cols,
        trading_fee=0.004,  # 0.4% per side (extreme)
    )

    # 6. Run Monte Carlo simulation
    print(f"Running Monte Carlo simulation (1,000 iterations, "
          f"{base['trades']} trades per iteration)...")
    mc = run_monte_carlo(
        trade_pnl_pcts=base['trade_pnl_pcts'],
        initial_capital=base['initial_capital'],
        n_simulations=1000,
        ruin_threshold=5000.0,
    )

    # 7. Print the comprehensive report
    print("\n", flush=True)
    print("=" * 60, flush=True)
    print("     CHAOS ENGINEERING & MONTE CARLO REPORT", flush=True)
    print("=" * 60, flush=True)

    print("\n  ┌─────────────────────────────────────────────────────┐", flush=True)
    print("  │  SECTION 1: BASE RUN WITH EXTREME SLIPPAGE (0.8%)  │", flush=True)
    print("  └─────────────────────────────────────────────────────┘", flush=True)
    print(f"  Slippage Model:    0.4% Entry + 0.4% Exit (0.8% round-trip)", flush=True)
    print(f"  OOS Period:        {df_oos['timestamp'].min().date(flush=True)} → "
          f"{df_oos['timestamp'].max().date()}")
    print(f"  Initial Capital:   ${base['initial_capital']:,.2f}", flush=True)
    print(f"  Final Capital:     ${base['final_capital']:,.2f}", flush=True)
    print(f"  Total ROI:         {base['roi']:,.2f}%", flush=True)
    print(f"  Total Trades:      {base['trades']}", flush=True)
    print(f"  Winning Trades:    {base['winning_trades']}", flush=True)
    print(f"  Losing Trades:     {base['losing_trades']}", flush=True)
    print(f"  Win Rate:          {base['win_rate']:,.2f}%", flush=True)

    if base['trade_pnl_pcts']:
        avg_win = np.mean([p for p in base['trade_pnl_pcts'] if p > 0]) if any(
            p > 0 for p in base['trade_pnl_pcts']) else 0.0
        avg_loss = np.mean([p for p in base['trade_pnl_pcts'] if p <= 0]) if any(
            p <= 0 for p in base['trade_pnl_pcts']) else 0.0
        print(f"  Avg Winning Trade: {avg_win:+.2f}%", flush=True)
        print(f"  Avg Losing Trade:  {avg_loss:+.2f}%", flush=True)

    print(f"\n  ┌─────────────────────────────────────────────────────┐", flush=True)
    print(f"  │  SECTION 2: MONTE CARLO SIMULATION (1,000 RUNS)    │", flush=True)
    print(f"  └─────────────────────────────────────────────────────┘", flush=True)
    print(f"  Simulations Run:   {mc['n_simulations']:,}", flush=True)
    print(f"  Trades per Sim:    {mc['n_trades']}", flush=True)
    print(f"  Median Capital:    ${mc['median_final']:,.2f}", flush=True)
    print(f"  Mean Capital:      ${mc['mean_final']:,.2f}", flush=True)
    print(f"  Best Case:         ${mc['best_case']:,.2f}", flush=True)
    print(f"  Worst Case:        ${mc['worst_case']:,.2f}", flush=True)
    print(f"  5th Percentile:    ${mc['p5_final']:,.2f}", flush=True)
    print(f"  95th Percentile:   ${mc['p95_final']:,.2f}", flush=True)

    print(f"\n  ┌─────────────────────────────────────────────────────┐", flush=True)
    print(f"  │  SECTION 3: RISK ASSESSMENT                        │", flush=True)
    print(f"  └─────────────────────────────────────────────────────┘", flush=True)
    print(f"  Ruin Threshold:    $5,000 (50% max drawdown)", flush=True)
    print(f"  Risk of Ruin:      {mc['risk_of_ruin_pct']:.1f}%", flush=True)

    if mc['risk_of_ruin_pct'] == 0.0:
        print(f"  Verdict:           ✅ ZERO risk of ruin across 1,000 simulations.", flush=True)
    elif mc['risk_of_ruin_pct'] < 5.0:
        print(f"  Verdict:           ✅ LOW risk of ruin (<5%). Strategy is robust.", flush=True)
    elif mc['risk_of_ruin_pct'] < 20.0:
        print(f"  Verdict:           ⚠️  MODERATE risk of ruin. Review position sizing.", flush=True)
    else:
        print(f"  Verdict:           🚨 HIGH risk of ruin (>{mc['risk_of_ruin_pct']:.0f}%, flush=True). "
              f"Strategy needs revision.")

    median_roi = ((mc['median_final'] - base['initial_capital'])
                  / base['initial_capital']) * 100
    if base['roi'] > 0 and median_roi > 0:
        print(f"\n  🏁 FINAL: Strategy survives chaos testing.", flush=True)
        print(f"     Base ROI with 0.8% fees: {base['roi']:+.2f}%", flush=True)
        print(f"     Monte Carlo Median ROI:  {median_roi:+.2f}%", flush=True)
    elif base['roi'] > 0 and median_roi <= 0:
        print(f"\n  ⚠️  FINAL: Base run profitable but Monte Carlo median is negative.", flush=True)
        print(f"     Results may be sequence-dependent — proceed with caution.", flush=True)
    else:
        print(f"\n  🚨 FINAL: Strategy is unprofitable even in base run under chaos fees.", flush=True)

    print(f"\n{'=' * 60}\n", flush=True)


if __name__ == "__main__":
    run_chaos_test()
