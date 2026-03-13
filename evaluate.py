"""
Deney Değerlendirici
Son deneyin sonucunu okur, best_score ile karşılaştırır,
KEEP/DISCARD kararı verir ve history/ klasörüne yazar.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path


def load_last_result():
    """Son deney sonucunu yükle."""
    if not os.path.exists("last_result.json"):
        print("ERROR: last_result.json bulunamadı. Önce strategy.py çalıştır.")
        sys.exit(1)
    
    with open("last_result.json") as f:
        return json.load(f)


def load_best_score():
    """Şimdiye kadar ki en iyi skoru yükle."""
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
    """Değerlendirme yap ve sonucu yazdır."""
    # Sonuçları yükle
    result = load_last_result()
    best_score, exp_count = load_best_score()
    next_exp_num = exp_count + 1
    
    score = result.get("score", 0.0)
    fill_rate = result.get("fill_rate", 0.0)
    avg_edge = result.get("avg_edge", 0.0)
    trades = result.get("trades_executed", 0)
    
    print(f"\n{'='*50}")
    print(f"DENEY #{next_exp_num} DEĞERLENDİRME")
    print(f"{'='*50}")
    print(f"Score:      {score:.4f}")
    print(f"Best Score: {best_score:.4f}")
    print(f"Fill Rate:  {fill_rate:.4f}")
    print(f"Avg Edge:   {avg_edge:.4f}")
    print(f"Trades:     {trades}")
    print(f"{'='*50}")
    
    # Karar ver
    if score > best_score * 1.5 and score > 0:
        decision = "CONFIRM"
        print(f"KARAR: CONFIRM ✓✓ (score {score:.4f} >> best {best_score:.4f})")
        print("→ Bir daha test et, sonra commit at")
    elif score > best_score:
        decision = "KEEP"
        print(f"KARAR: KEEP ✓ (score {score:.4f} > best {best_score:.4f})")
        print("→ git add . && git commit")
    else:
        decision = "DISCARD"
        print(f"KARAR: DISCARD ✗ (score {score:.4f} <= best {best_score:.4f})")
        print("→ git checkout strategy.py")
    
    # History'e yaz
    history_dir = Path("history")
    history_dir.mkdir(exist_ok=True)
    
    exp_data = {
        "experiment": next_exp_num,
        "timestamp": datetime.now().isoformat(),
        "config": result.get("config", {}),
        "score": score,
        "fill_rate": fill_rate,
        "avg_edge": avg_edge,
        "win_rate": result.get("win_rate", 0.0),
        "trades": trades,
        "opportunities": result.get("opportunities_found", 0),
        "decision": decision,
        "best_score_before": best_score,
    }
    
    exp_file = history_dir / f"exp_{next_exp_num:03d}.json"
    with open(exp_file, "w") as f:
        json.dump(exp_data, f, indent=2)
    
    print(f"\nHistory kaydedildi: {exp_file}")
    print(f"{'='*50}\n")
    
    # Claude Code için çıktı
    print(f"RESULT_JSON: {json.dumps(exp_data)}")
    
    return decision, exp_data


if __name__ == "__main__":
    decision, data = evaluate()
    sys.exit(0 if decision in ["KEEP", "CONFIRM"] else 1)
