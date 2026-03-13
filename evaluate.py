"""
Deney Degerlendirici
Son deneyin sonucunu okur, best_score (win_rate) ile karsilastirir,
KEEP/DISCARD karari verir ve history/ klasorune yazar.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path


def load_last_result():
    """Son deney sonucunu yukle."""
    if not os.path.exists("last_result.json"):
        print("ERROR: last_result.json bulunamadi. Once strategy.py calistir.")
        sys.exit(1)

    with open("last_result.json") as f:
        return json.load(f)


def load_best_score():
    """Simdiye kadar ki en iyi skoru yukle."""
    history_dir = Path("history")
    if not history_dir.exists():
        return 0.0, 0

    experiments = list(history_dir.glob("exp_*.json"))
    if not experiments:
        return 0.0, 0

    best_score = 0.0
    for exp_file in experiments:
        with open(exp_file) as f:
            exp = json.load(f)
            if exp.get("decision") == "KEEP" and exp.get("score", 0) > best_score:
                best_score = exp["score"]

    return best_score, len(experiments)


def evaluate():
    """Degerlendirme yap ve sonucu yazdir."""
    result = load_last_result()
    best_score, exp_count = load_best_score()
    next_exp_num = exp_count + 1

    score = result.get("score", 0.0)          # win_rate
    win_rate = result.get("win_rate", result.get("backtest_win_rate", 0.0))
    wins = result.get("wins", result.get("backtest_wins", 0))
    losses = result.get("losses", result.get("backtest_losses", 0))
    trades = result.get("trades_executed", result.get("backtest_trades", 0))
    total_pnl = result.get("total_pnl_usd", result.get("backtest_pnl_usd", 0.0))
    avg_strength = result.get("avg_signal_strength", 0.0)

    print(f"\n{'='*60}")
    print(f"DENEY #{next_exp_num} DEGERLENDIRME")
    print(f"{'='*60}")
    print(f"  Win Rate (score): {score:.4f}  (best: {best_score:.4f})")
    print(f"  Trades:           {trades} ({wins}W / {losses}L)")
    print(f"  Total PnL:        ${total_pnl:+.2f}")
    print(f"  Avg Signal Str:   {avg_strength:.4f}")
    print(f"{'='*60}")

    # Karar ver
    if trades < 3:
        decision = "DISCARD"
        print(f"KARAR: DISCARD (yetersiz trade: {trades} < 3)")
    elif score > best_score:
        decision = "KEEP"
        print(f"KARAR: KEEP (score {score:.4f} > best {best_score:.4f})")
        print("-> git commit")
    else:
        decision = "DISCARD"
        print(f"KARAR: DISCARD (score {score:.4f} <= best {best_score:.4f})")
        print("-> git checkout strategy.py")

    # History'e yaz
    history_dir = Path("history")
    history_dir.mkdir(exist_ok=True)

    exp_data = {
        "experiment": next_exp_num,
        "timestamp": datetime.now().isoformat(),
        "config": result.get("config", {}),
        "score": score,
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "trades": trades,
        "total_pnl_usd": total_pnl,
        "avg_signal_strength": avg_strength,
        "decision": decision,
        "best_score_before": best_score,
    }

    exp_file = history_dir / f"exp_{next_exp_num:03d}.json"
    with open(exp_file, "w") as f:
        json.dump(exp_data, f, indent=2)

    print(f"\nHistory: {exp_file}")
    print(f"{'='*60}\n")
    print(f"RESULT_JSON: {json.dumps(exp_data)}")

    return decision, exp_data


if __name__ == "__main__":
    decision, data = evaluate()
    sys.exit(0 if decision == "KEEP" else 1)
