import pandas as pd
import joblib
import numpy as np


def run_phase_backtest(df_phase, model, feature_cols, phase_name,
                       confidence_threshold=0.65, trailing_stop_pct=0.015,
                       stop_loss_pct=0.01, trading_fee=0.001,
                       initial_capital=10000.0):
    """
    Run a single-phase backtest on a slice of data.
    Returns a dict with the phase results.
    """
    X = df_phase[feature_cols]
    buy_confidence = model.predict_proba(X)[:, 1]
    df_phase = df_phase.copy()
    df_phase['Buy_Confidence'] = buy_confidence

    capital = initial_capital
    in_position = False
    entry_price = 0.0
    position_size = 0.0
    trades = 0
    winning_trades = 0
    losing_trades = 0
    highest_price = 0.0

    for row in df_phase.itertuples(index=False):
        current_price = row.close
        confidence = row.Buy_Confidence

        # BUY Logic: Only enter if confidence >= threshold
        if confidence >= confidence_threshold and not in_position:
            capital_after_fee = capital * (1 - trading_fee)
            position_size = capital_after_fee / current_price
            capital = 0.0

            entry_price = current_price
            in_position = True
            highest_price = current_price

        # SELL Logic: Only Trailing Stop and Hard Stop Loss
        elif in_position:
            highest_price = max(highest_price, current_price)

            hit_ts = current_price <= highest_price * (1 - trailing_stop_pct)
            hit_sl = current_price <= entry_price * (1 - stop_loss_pct)

            if hit_ts or hit_sl:
                gross_revenue = position_size * current_price
                capital = gross_revenue * (1 - trading_fee)
                position_size = 0.0

                if current_price > entry_price:
                    winning_trades += 1
                else:
                    losing_trades += 1

                trades += 1
                in_position = False
                entry_price = 0.0
                highest_price = 0.0

    # Close out any remaining open position on the last candle
    if in_position:
        final_price = df_phase.iloc[-1]['close']
        gross_revenue = position_size * final_price
        capital = gross_revenue * (1 - trading_fee)

        if final_price > entry_price:
            winning_trades += 1
        else:
            losing_trades += 1
        trades += 1

    roi = ((capital - initial_capital) / initial_capital) * 100
    win_rate = (winning_trades / trades * 100) if trades > 0 else 0.0

    return {
        'phase_name': phase_name,
        'initial_capital': initial_capital,
        'final_capital': capital,
        'roi': roi,
        'trades': trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': win_rate,
        'rows': len(df_phase),
    }


def run_stress_test():
    print("=" * 60)
    print("      MULTI-PHASE STRESS TEST & OUT-OF-SAMPLE VALIDATION")
    print("=" * 60)

    # 1. Load the trained model
    model_path = 'models/quant_rf_model.pkl'
    try:
        model = joblib.load(model_path)
        print(f"Loaded model from {model_path}")
    except FileNotFoundError:
        print(f"Model file {model_path} not found. Run model_trainer.py first.")
        return

    # 2. Load the dataset
    data_path = 'data/processed/PAXGUSDT_features.csv'
    try:
        df = pd.read_csv(data_path)
        print(f"Loaded dataset from {data_path} with {len(df):,} rows.")
    except FileNotFoundError:
        print(f"Dataset {data_path} not found. Ensure feature engineering is complete.")
        return

    # 3. Parse the timestamp column
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    print(f"Date range: {df['timestamp'].min()} → {df['timestamp'].max()}")

    # 4. Define Features (X) matrix exactly as trained
    feature_cols = [
        'open', 'high', 'low', 'close', 'volume',
        'RSI_14', 'MACD', 'MACD_signal', 'BB_high', 'BB_low',
        'ATR_14', 'SMA_50', 'SMA_200'
    ]

    # 5. Define testing periods
    phases = [
        {
            'name': 'Phase 1: Bull Market (2021)',
            'start': '2021-01-01',
            'end': '2021-12-31',
        },
        {
            'name': 'Phase 2: Bear Market Stress Test (2022)',
            'start': '2022-01-01',
            'end': '2022-12-31',
        },
        {
            'name': 'Phase 3: Out-of-Sample / Forward Test (2025-2026)',
            'start': '2025-01-01',
            'end': '2026-12-31',
        },
    ]

    # 6. Run each phase independently
    results = []
    for phase in phases:
        mask = (df['timestamp'] >= phase['start']) & (df['timestamp'] <= phase['end'])
        df_phase = df.loc[mask].reset_index(drop=True)

        if len(df_phase) == 0:
            print(f"\n⚠  {phase['name']}: No data found for "
                  f"{phase['start']} → {phase['end']}. Skipping.\n")
            continue

        print(f"\nRunning {phase['name']}  "
              f"({len(df_phase):,} candles, "
              f"{df_phase['timestamp'].min().date()} → "
              f"{df_phase['timestamp'].max().date()})...")

        result = run_phase_backtest(
            df_phase=df_phase,
            model=model,
            feature_cols=feature_cols,
            phase_name=phase['name'],
        )
        results.append(result)

    # 7. Print comprehensive report
    print("\n")
    print("=" * 60)
    print("          === PHASE STRESS TEST REPORT ===")
    print("=" * 60)

    for r in results:
        print(f"\n  {r['phase_name']}")
        print(f"  {'─' * 50}")
        print(f"  Candles Tested:  {r['rows']:,}")
        print(f"  Initial Capital: ${r['initial_capital']:,.2f}")
        print(f"  Final Capital:   ${r['final_capital']:,.2f}")
        print(f"  Total ROI:       {r['roi']:,.2f}%")
        print(f"  Total Trades:    {r['trades']}")
        print(f"  Winning Trades:  {r['winning_trades']}")
        print(f"  Losing Trades:   {r['losing_trades']}")
        print(f"  Win Rate:        {r['win_rate']:,.2f}%")

    # 8. Summary comparison
    print(f"\n{'=' * 60}")
    print("  CROSS-PHASE COMPARISON")
    print(f"{'=' * 60}")
    print(f"  {'Phase':<50} {'ROI':>8}  {'Trades':>7}  {'Win%':>6}")
    print(f"  {'─' * 50} {'─' * 8}  {'─' * 7}  {'─' * 6}")
    for r in results:
        print(f"  {r['phase_name']:<50} {r['roi']:>7.2f}%  {r['trades']:>7}  {r['win_rate']:>5.1f}%")

    avg_roi = np.mean([r['roi'] for r in results]) if results else 0.0
    total_trades = sum(r['trades'] for r in results)
    total_wins = sum(r['winning_trades'] for r in results)
    overall_win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0.0

    print(f"  {'─' * 50} {'─' * 8}  {'─' * 7}  {'─' * 6}")
    print(f"  {'AVERAGE / TOTAL':<50} {avg_roi:>7.2f}%  {total_trades:>7}  {overall_win_rate:>5.1f}%")
    print(f"{'=' * 60}\n")

    # 9. Overfitting verdict
    if len(results) >= 2:
        oos_results = [r for r in results if 'Out-of-Sample' in r['phase_name']]
        if oos_results:
            oos_roi = oos_results[0]['roi']
            if oos_roi > 0:
                print("  ✅ VERDICT: Out-of-Sample ROI is POSITIVE — low overfitting risk.")
            else:
                print("  ⚠️  VERDICT: Out-of-Sample ROI is NEGATIVE — potential overfitting detected.")
    print()


if __name__ == "__main__":
    run_stress_test()
