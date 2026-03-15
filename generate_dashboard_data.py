"""Generate optimizer dashboard data from polymarket_history.json.
Outputs optimizer_dashboard.json with:
- top10: best configs by profit factor
- equity_curve: cumulative PnL per window for best config
- market_conditions: performance in uptrend/downtrend/sideways
"""
import json
import itertools
from datetime import datetime, timezone
from collections import defaultdict

HISTORY_FILE = "polymarket_history.json"
OUTPUT_FILE = "optimizer_dashboard.json"


def load_windows():
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [w for w in data if w["outcome"] in ("UP", "DOWN")]


def backtest(windows, sl, tp, direction_mode, flip):
    entry = 0.50
    trade_size = 5.0
    tokens = trade_size / entry
    use_sl = sl is not None
    use_tp = tp is not None
    trades = []
    cum_pnl = 0

    for i, w in enumerate(windows):
        outcome = w["outcome"]

        if direction_mode == "always_up":
            d = "UP"
        elif direction_mode == "always_down":
            d = "DOWN"
        elif direction_mode == "momentum":
            d = windows[i - 1]["outcome"] if i > 0 else "UP"
        else:
            d = "UP"

        if flip:
            d = "DOWN" if d == "UP" else "UP"

        correct = (d == outcome)

        if correct:
            if use_tp and tp < 1.0:
                exit_p = tp
                reason = "take_profit"
            else:
                exit_p = 1.0
                reason = "expiry"
        else:
            if use_sl and sl > 0.0:
                exit_p = sl
                reason = "stop_loss"
            else:
                exit_p = 0.0
                reason = "expiry"

        pnl = tokens * exit_p - trade_size
        cum_pnl += pnl
        trades.append({
            "i": i,
            "dir": d,
            "outcome": outcome,
            "correct": correct,
            "exit": round(exit_p, 2),
            "reason": reason,
            "pnl": round(pnl, 4),
            "cum": round(cum_pnl, 4),
            "ts": w["block_ts"],
        })

    return trades


def calc_stats(trades, bankroll=85.0):
    if not trades:
        return None
    wins = sum(1 for t in trades if t["pnl"] > 0)
    n = len(trades)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gp / gl if gl > 0 else 999.0
    total = sum(t["pnl"] for t in trades)

    # Max drawdown in dollars
    running = 0
    peak = 0
    max_dd = 0
    dd_start = 0
    dd_end = 0
    cur_dd_start = 0
    for i, t in enumerate(trades):
        running += t["pnl"]
        if running > peak:
            peak = running
            cur_dd_start = i
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
            dd_start = cur_dd_start
            dd_end = i

    # Max drawdown as % of bankroll
    dd_pct = round(max_dd / bankroll * 100, 2) if bankroll > 0 else 0

    # Max consecutive losses
    max_consec_loss = 0
    cur_consec = 0
    for t in trades:
        if t["pnl"] < 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # Recovery: how many wins needed to recover from max DD
    # avg win = gp / wins, recovery = max_dd / avg_win
    avg_win = gp / wins if wins > 0 else 0
    recovery_wins = round(max_dd / avg_win, 1) if avg_win > 0 else 999

    # Ruin risk: probability of hitting 0 bankroll
    # Simplified: can survive N consecutive losses where N = bankroll / loss_per_trade
    avg_loss = gl / (n - wins) if (n - wins) > 0 else 0
    max_losses_before_ruin = int(bankroll / avg_loss) if avg_loss > 0 else 999

    return {
        "trades": n,
        "wins": wins,
        "wr": round(wins / n, 4),
        "pnl": round(total, 2),
        "pf": round(pf, 4),
        "gp": round(gp, 2),
        "gl": round(gl, 2),
        "dd": round(max_dd, 2),
        "dd_pct": dd_pct,
        "max_consec_loss": max_consec_loss,
        "recovery_wins": recovery_wins,
        "max_losses_before_ruin": max_losses_before_ruin,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
    }


def classify_4h_blocks(windows):
    """Classify windows into uptrend/downtrend/sideways based on 4h blocks."""
    blocks = defaultdict(list)
    for w in windows:
        dt = datetime.fromtimestamp(w["block_ts"], tz=timezone.utc)
        key = (dt.year, dt.month, dt.day, dt.hour // 4)
        blocks[key].append(w)

    uptrend = []
    downtrend = []
    sideways = []
    for key in sorted(blocks.keys()):
        wins = blocks[key]
        n = len(wins)
        up_n = sum(1 for w in wins if w["outcome"] == "UP")
        up_pct = up_n / n * 100 if n > 0 else 50
        if up_pct > 56:
            uptrend.extend(wins)
        elif up_pct < 44:
            downtrend.extend(wins)
        else:
            sideways.extend(wins)

    return uptrend, downtrend, sideways


def main():
    windows = load_windows()
    windows.sort(key=lambda w: w["block_ts"])
    print(f"Loaded {len(windows)} windows")

    # Grid search
    sl_values = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, None]
    tp_values = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, None]
    dir_values = ["always_up", "always_down", "momentum"]
    flip_values = [True, False]

    results = []

    for sl, tp, direction, flip in itertools.product(sl_values, tp_values, dir_values, flip_values):
        if sl is not None and tp is not None and sl >= tp:
            continue
        if direction == "always_up" and flip:
            continue
        if direction == "always_down" and flip:
            continue

        trades = backtest(windows, sl, tp, direction, flip)
        stats = calc_stats(trades)
        if stats and stats["trades"] >= 10:
            results.append({
                "sl": sl,
                "tp": tp,
                "dir": direction,
                "flip": flip,
                "stats": stats,
                "equity": [t["cum"] for t in trades],
                "timestamps": [t["ts"] for t in trades],
            })

    results.sort(key=lambda r: r["stats"]["pf"], reverse=True)
    print(f"Tested {len(results)} configs")

    # Top 10
    top10 = []
    for r in results[:10]:
        top10.append({
            "sl": r["sl"],
            "tp": r["tp"],
            "dir": r["dir"],
            "flip": r["flip"],
            "stats": r["stats"],
        })

    # Best config equity curve (downsample for dashboard)
    best = results[0]
    eq_len = len(best["equity"])
    step = max(1, eq_len // 200)
    equity_curve = {
        "values": best["equity"][::step],
        "timestamps": best["timestamps"][::step],
        "config": {
            "sl": best["sl"],
            "tp": best["tp"],
            "dir": best["dir"],
            "flip": best["flip"],
        },
    }

    # Market conditions analysis
    uptrend, downtrend, side = classify_4h_blocks(windows)
    print(f"Uptrend: {len(uptrend)}, Downtrend: {len(downtrend)}, Sideways: {len(side)}")

    conditions = {}
    strategies = [
        ("always_up", "always_up", False),
        ("always_down", "always_down", False),
        ("momentum", "momentum", False),
        ("contrarian", "momentum", True),
    ]

    for label, direction, flip in strategies:
        cond = {}
        for cond_name, cond_windows in [("uptrend", uptrend), ("downtrend", downtrend),
                                         ("sideways", side), ("all", windows)]:
            if not cond_windows:
                cond[cond_name] = {"trades": 0, "wr": 0, "pnl": 0, "pf": 0}
                continue
            # Test with SL=0.35 (conservative realistic)
            trades = backtest(cond_windows, 0.35, None, direction, flip)
            stats = calc_stats(trades)
            cond[cond_name] = stats
        conditions[label] = cond

    # SL sensitivity
    sl_sensitivity = []
    for sl in [0.00, 0.10, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.48]:
        entry = 0.50
        risk = entry - sl
        reward = 1.0 - entry
        rr = reward / risk if risk > 0 else 999
        bep = risk / (risk + reward) * 100

        row = {"sl": sl, "risk": round(risk, 2), "rr": round(rr, 1), "bep": round(bep, 1)}
        for strat_name in ["UP", "DN", "MOM"]:
            trades = backtest(windows, sl, None,
                              "always_up" if strat_name == "UP" else
                              "always_down" if strat_name == "DN" else "momentum", False)
            stats = calc_stats(trades)
            row[strat_name + "_pnl"] = stats["pnl"]
            row[strat_name + "_pf"] = stats["pf"]
        sl_sensitivity.append(row)

    # Output
    output = {
        "generated": datetime.now().isoformat(),
        "total_windows": len(windows),
        "up_windows": sum(1 for w in windows if w["outcome"] == "UP"),
        "down_windows": sum(1 for w in windows if w["outcome"] == "DOWN"),
        "top10": top10,
        "equity_curve": equity_curve,
        "conditions": conditions,
        "sl_sensitivity": sl_sensitivity,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"Written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
